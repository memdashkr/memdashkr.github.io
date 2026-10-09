"""
메모리 현황판 — 매일 공개 정보 수집 스크립트
GitHub Actions에서 실행. 각 출처는 독립적으로 수집되며, 하나가 실패해도 나머지는 계속 진행.
결과: data/spot.json, data/revenue.json, data/stocks.json, data/news.json, data/status.json
"""
import json
import re
import os
from datetime import datetime, timedelta, timezone
from io import StringIO

import requests
import pandas as pd
from bs4 import BeautifulSoup

KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)
TODAY = NOW.strftime("%Y-%m-%d")
DATA = os.path.join(os.path.dirname(__file__), "..", "data")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9"}

status = {"updated_at": NOW.strftime("%Y-%m-%d %H:%M KST"), "sources": {}}


def load(name, default):
    p = os.path.join(DATA, name)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return default


def save(name, obj):
    with open(os.path.join(DATA, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def ok(src, msg=""):
    status["sources"][src] = {"ok": True, "msg": msg}


def fail(src, e):
    status["sources"][src] = {"ok": False, "msg": str(e)[:200]}
    print(f"[FAIL] {src}: {e}")


def num(x):
    try:
        return float(str(x).replace(",", "").replace("%", "").replace("$", "").strip())
    except ValueError:
        return None


def upsert(series, point):
    """같은 날짜가 있으면 교체, 없으면 추가 (날짜순 유지)"""
    series[:] = [p for p in series if p["date"] != point["date"]] + [point]
    series.sort(key=lambda p: p["date"])


# ─────────────────────────────────────────────
# 1. DRAMeXchange 무료 현물가
# ─────────────────────────────────────────────
DX_ITEMS = {
    # 페이지 표기 → (그룹, 표시명)
    "DDR5 16Gb (2Gx8) 4800/5600": ("DRAM", "DDR5 16Gb"),
    "DDR5 16Gb (2Gx8) eTT": ("DRAM", "DDR5 16Gb eTT"),
    "DDR4 16Gb (2Gx8) 3200": ("DRAM", "DDR4 16Gb"),
    "DDR4 8Gb (1Gx8) 3200": ("DRAM", "DDR4 8Gb"),
    "DDR4 8Gb (1Gx8) eTT": ("DRAM", "DDR4 8Gb eTT"),
    "512Gb TLC": ("NAND", "512Gb TLC 웨이퍼"),
    "256Gb TLC": ("NAND", "256Gb TLC 웨이퍼"),
    "128Gb TLC": ("NAND", "128Gb TLC 웨이퍼"),
    "MLC 64Gb 8GBx8": ("NAND", "MLC 64Gb"),
    "DDR5 RDIMM 32GB 4800/5600": ("모듈", "DDR5 RDIMM 32GB"),
    "DDR5 UDIMM 16GB 4800/5600": ("모듈", "DDR5 UDIMM 16GB"),
    "GDDR6 8Gb": ("그래픽", "GDDR6 8Gb"),
}
DATE_RE = re.compile(r"Last Update\s*:?\s*([A-Za-z]{3})\.?\s*(\d{1,2})\s+(\d{4})", re.I)


def norm(s):
    return re.sub(r"\s+", " ", str(s)).strip()


def collect_dramexchange(spot):
    r = requests.get("https://www.dramexchange.com/", headers=UA, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    found = 0
    done = set()
    dates = []
    for table in soup.find_all("table"):
        # 표를 감싼 가장 가까운 블록에서 "Last Update" 날짜 찾기 (태그가 나뉘어 있어도 인식)
        date = None
        node = table
        for _ in range(5):
            node = node.parent
            if node is None:
                break
            m = DATE_RE.search(norm(node.get_text(" ")))
            if m:
                try:
                    date = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%b %d %Y").strftime("%Y-%m-%d")
                except ValueError:
                    pass
                break
        if not date:
            continue  # 날짜를 모르는 표는 건너뜀 (잘못된 날짜로 저장 방지)
        for tr in table.find_all("tr"):
            cells = [norm(td.get_text(" ")) for td in tr.find_all(["td", "th"])]
            if len(cells) < 5:
                continue
            key = next((k for k in DX_ITEMS if norm(k).lower() == cells[0].lower()), None)
            if not key or key in done:
                continue
            # 열 구성: 품목 | 고가 | 저가 | (세션 고가 | 세션 저가) | 평균 | 변동(%)
            pct_idx = max((i for i, c in enumerate(cells) if "%" in c), default=None)
            if pct_idx is None or pct_idx < 2:
                continue
            high, low = num(cells[1]), num(cells[2])
            avg, chg = num(cells[pct_idx - 1]), num(cells[pct_idx])
            if avg is None:
                continue
            done.add(key)
            dates.append(date)
            group, label = DX_ITEMS[key]
            s = spot["dramexchange"].setdefault(label, {"group": group, "series": []})
            prev = [p for p in s["series"] if p["date"] < date]
            if prev and prev[-1].get("avg") == avg and prev[-1].get("chg") == chg:
                found += 1
                continue  # 주간 품목처럼 아직 갱신 안 된 값은 새 날짜로 중복 저장하지 않음
            upsert(s["series"], {"date": date, "high": high, "low": low, "avg": avg, "chg": chg})
            found += 1
    if found == 0:
        raise RuntimeError("표에서 품목을 찾지 못함 (페이지 구조 변경 가능성)")
    ok("DRAMeXchange", f"{found}개 품목 (기준일 {', '.join(sorted(set(dates)))})")


# ─────────────────────────────────────────────
# 2. CFM 中国闪存市场 (중국 채널가)
# ─────────────────────────────────────────────
CFM_ITEMS = {
    "1Tb QLC": ("NAND", "1Tb QLC 웨이퍼"),
    "1Tb TLC": ("NAND", "1Tb TLC 웨이퍼"),
    "512Gb TLC": ("NAND", "512Gb TLC 웨이퍼"),
    "256Gb TLC": ("NAND", "256Gb TLC 웨이퍼"),
    "DDR5 16Gb": ("DRAM", "DDR5 16Gb"),
    "DDR4 16Gb": ("DRAM", "DDR4 16Gb"),
    "DDR4 8Gb": ("DRAM", "DDR4 8Gb"),
}


def collect_cfm(spot):
    r = requests.get("https://www.chinaflashmarket.com/", headers=UA, timeout=30)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    soup = BeautifulSoup(r.text, "lxml")
    found = 0
    seen = set()
    for tr in soup.find_all("tr"):
        cells = [norm(td.get_text(" ")) for td in tr.find_all(["td", "th"])]
        if len(cells) < 2:
            continue
        name = cells[0]
        key = next((k for k in CFM_ITEMS if name.lower().startswith(k.lower())), None)
        if not key or key in seen:
            continue
        # eTT 등 변형 품목 제외
        if "ett" in name.lower() and "ett" not in key.lower():
            continue
        price = next((num(c) for c in cells[1:] if num(c) is not None), None)
        if price is None:
            continue
        group, label = CFM_ITEMS[key]
        s = spot["cfm"].setdefault(label, {"group": group, "series": []})
        upsert(s["series"], {"date": TODAY, "avg": price})
        seen.add(key)
        found += 1
    if found == 0:
        raise RuntimeError("표에서 품목을 찾지 못함 (페이지 구조 변경 가능성)")
    ok("CFM", f"{found}개 품목")


# ─────────────────────────────────────────────
# 3. 대만 메모리 업체 월매출 (TWSE 상장 / TPEx 상장)
# ─────────────────────────────────────────────
TW_COMPANIES = {
    "2408": ("Nanya", "南亞科", "범용 DRAM"),
    "2344": ("Winbond", "華邦電", "스페셜티 DRAM·NOR"),
    "8299": ("Phison", "群聯", "NAND 컨트롤러·모듈"),
    "3260": ("ADATA", "威剛", "메모리 모듈"),
}
TW_SOURCES = [
    "https://openapi.twse.com.tw/v1/opendata/t187ap05_L",   # 상장
    "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O",  # 장외(上櫃)
]


def roc_ym(s):
    s = str(s).strip()
    y, m = int(s[:-2]) + 1911, int(s[-2:])
    return f"{y}-{m:02d}"


def pick(row, *keys):
    for k in keys:
        for rk in row:
            if k in rk:
                return row[rk]
    return None


def collect_revenue(rev):
    found = 0
    for url in TW_SOURCES:
        r = requests.get(url, headers=UA, timeout=60)
        r.raise_for_status()
        for row in r.json():
            code = str(pick(row, "公司代號", "SecuritiesCompanyCode") or "").strip()
            if code not in TW_COMPANIES:
                continue
            ym = roc_ym(pick(row, "資料年月", "YearMonth"))
            val = num(pick(row, "當月營收", "MonthlyRevenue"))
            yoy = num(pick(row, "去年同月增減", "YoY"))
            mom = num(pick(row, "上月比較增減", "MoM"))
            en, zh, desc = TW_COMPANIES[code]
            c = rev.setdefault(code, {"name": en, "zh": zh, "desc": desc, "series": []})
            c["series"] = [p for p in c["series"] if p["ym"] != ym] + [
                {"ym": ym, "rev": val, "yoy": yoy, "mom": mom}]
            c["series"].sort(key=lambda p: p["ym"])
            found += 1
    if found == 0:
        raise RuntimeError("대상 기업 데이터 없음")
    ok("대만 월매출", f"{found}개사")


# ─────────────────────────────────────────────
# 4. 주가 (Yahoo Finance)
# ─────────────────────────────────────────────
STOCKS = {
    "005930.KS": ("삼성전자", "KRW"),
    "000660.KS": ("SK하이닉스", "KRW"),
    "MU": ("마이크론", "USD"),
    "SNDK": ("샌디스크", "USD"),
    "285A.T": ("키옥시아", "JPY"),
}


def collect_stocks():
    import yfinance as yf
    out = {}
    for t, (name, cur) in STOCKS.items():
        try:
            h = yf.Ticker(t).history(period="1y", interval="1d", auto_adjust=False)
            if h.empty:
                continue
            closes = [{"date": d.strftime("%Y-%m-%d"), "close": round(float(c), 2)}
                      for d, c in h["Close"].dropna().items()]
            out[t] = {"name": name, "cur": cur, "series": closes}
        except Exception as e:
            print(f"[WARN] {t}: {e}")
    if not out:
        raise RuntimeError("주가 데이터 없음")
    save("stocks.json", out)
    ok("주가", f"{len(out)}개 종목")


# ─────────────────────────────────────────────
# 5. 뉴스 헤드라인 (RSS) — 요약 없이 헤드라인 + 원문 링크
# ─────────────────────────────────────────────
KEYWORDS = re.compile(
    r"DRAM|NAND|HBM|memory|SSD|flash|Micron|Hynix|Kioxia|SanDisk|CXMT|YMTC|"
    r"메모리|낸드|D램|디램|HBM|하이닉스|삼성전자|"
    r"存储|内存|闪存|长鑫|长江存储|颗粒", re.I)
NEWS_FEEDS = [
    ("TrendForce", "en", "https://www.trendforce.com/feed/Semiconductors.html"),
    ("디일렉", "ko", "https://www.thelec.kr/rss/allArticle.xml"),
    ("Google News", "en", "https://news.google.com/rss/search?q=DRAM+OR+NAND+OR+HBM+price+when:2d&hl=en-US&gl=US&ceid=US:en"),
    ("Google News", "ko", "https://news.google.com/rss/search?q=D%EB%9E%A8+OR+%EB%82%B8%EB%93%9C+OR+HBM+when:2d&hl=ko&gl=KR&ceid=KR:ko"),
    ("Google News", "zh", "https://news.google.com/rss/search?q=%E5%AD%98%E5%82%A8+%E4%BB%B7%E6%A0%BC+OR+%E9%95%BF%E9%91%AB+OR+%E9%95%BF%E6%B1%9F%E5%AD%98%E5%82%A8+when:2d&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"),
]


def translate(texts, src):
    """무료 번역(키 불필요). 실패하면 원문 유지."""
    try:
        from deep_translator import GoogleTranslator
        tr = GoogleTranslator(source="zh-CN" if src == "zh" else src, target="ko")
        return [tr.translate(t) or t for t in texts]
    except Exception as e:
        print(f"[WARN] 번역 실패: {e}")
        return [None] * len(texts)


PRICE_RE = re.compile(r"price|pricing|contract|spot|ASP|가격|고정거래|현물|价格|涨价|降价|报价", re.I)
FCST_RE = re.compile(r"forecast|projected|expected|outlook|QoQ|YoY|전망|예상|预测|预计|预期", re.I)
CORE_RE = re.compile(r"DRAM|NAND|HBM|D램|낸드|CXMT|YMTC|长鑫|长江存储|内存|闪存", re.I)
GOOD_OUTLETS = re.compile(r"TrendForce|DigiTimes|디일렉|THE ELEC|Reuters|Bloomberg|Nikkei|매일경제|한국경제|전자신문|연합뉴스|조선비즈|集微|EE Times|TechInsights|Tom's Hardware|SemiAnalysis|财联社|第一财经", re.I)
BAD_OUTLETS = re.compile(r"TradingKey|TradingView|Motley|Fool|Benzinga|Seeking Alpha|Yahoo|Investing\.com|Zacks|MarketBeat|InvestorPlace|BigGo|Blog|블로그|티스토리|Tistory|知乎|搜狐号", re.I)
STOCKISH = re.compile(r"stock|shares|rally|buy|sell|target price|주가|목표가|매수|股价|涨停", re.I)


def score_news(n):
    t, o = n["title"], n.get("outlet", "")
    sc = 0
    if PRICE_RE.search(t): sc += 4
    if FCST_RE.search(t): sc += 2
    sc += min(2, len(set(m.lower() for m in CORE_RE.findall(t))))
    if GOOD_OUTLETS.search(o): sc += 2
    if o == "TrendForce": sc += 2
    if BAD_OUTLETS.search(o): sc -= 4
    if STOCKISH.search(t): sc -= 3
    return sc


def tokens(t):
    return set(re.findall(r"[A-Za-z0-9]+|[가-힣]{2,}|[\u4e00-\u9fff]", t.lower()))


def dedupe(items):
    """비슷한 제목(같은 사건)은 점수 높은 것 하나만 남김"""
    def sim(a, b):
        best = 0
        for ta in (a["title"], a.get("ko") or ""):
            for tb in (b["title"], b.get("ko") or ""):
                x, y = tokens(ta), tokens(tb)
                if x and y:
                    best = max(best, len(x & y) / len(x | y))
        return best
    kept = []
    for n in sorted(items, key=lambda n: (n["score"], n["date"]), reverse=True):
        if any(sim(n, k) > 0.45 for k in kept):
            continue
        kept.append(n)
    return kept


def collect_news(news):
    import feedparser
    items = {n["link"]: n for n in news.get("items", [])}
    added, errors = 0, []
    for src, lang, url in NEWS_FEEDS:
        try:
            r = requests.get(url, headers=UA, timeout=30)
            r.raise_for_status()
            feed = feedparser.parse(r.content)
            new = []
            for e in feed.entries[:40]:
                title = norm(e.get("title", ""))
                link = e.get("link", "")
                if not title or not link or link in items:
                    continue
                if not KEYWORDS.search(title):
                    continue
                pub = e.get("published_parsed") or e.get("updated_parsed")
                date = datetime(*pub[:6], tzinfo=timezone.utc).astimezone(KST).strftime("%Y-%m-%d") if pub else TODAY
                outlet = src
                if src == "Google News" and " - " in title:
                    title, outlet = title.rsplit(" - ", 1)
                new.append({"title": title, "link": link, "outlet": outlet, "lang": lang, "date": date,
                            "tag": "전망" if (re.search(r"price|pricing|ASP|contract|가격|고정거래|价格|涨价|报价", title, re.I)
                                             and re.search(r"forecast|projected|expected|QoQ|YoY|전망|예상|预测|预计|预期", title, re.I)) else ""})
            if lang != "ko" and new:
                for n, ko in zip(new, translate([n["title"] for n in new], lang)):
                    n["ko"] = ko
            for n in new:
                items[n["link"]] = n
                added += 1
        except Exception as e:
            errors.append(f"{src}/{lang}: {e}")
    # 점수 계산 → 최근 7일 → 중복 제거 → 언어별 상위 15건만 보관 (화면은 최근 3일 상위 10건)
    cutoff = (NOW - timedelta(days=7)).strftime("%Y-%m-%d")
    pool = [n for n in items.values() if n["date"] >= cutoff]
    for n in pool:
        n["score"] = score_news(n)
    pool = dedupe(pool)
    kept = []
    for lang in ("ko", "en", "zh"):
        kept += sorted([n for n in pool if n["lang"] == lang], key=lambda n: (n["score"], n["date"]), reverse=True)[:15]
    kept.sort(key=lambda n: (n["date"], n["score"]), reverse=True)
    news["items"] = kept
    if not kept:
        raise RuntimeError("; ".join(errors) or "뉴스 없음")
    ok("뉴스", f"신규 {added}건" + (f" (일부 실패: {len(errors)})" if errors else ""))


# ─────────────────────────────────────────────
# 6. 기관 전망 (TrendForce 보도자료의 분기 가격 전망 % 추출)
# ─────────────────────────────────────────────
PRODUCTS = [("HBM", r"HBM"), ("NAND", r"NAND|enterprise SSD|eSSD"),
            ("서버 DRAM", r"server DRAM"), ("모바일 DRAM", r"mobile DRAM|LPDDR"),
            ("범용 DRAM", r"conventional DRAM|DRAM")]
RANGE_RE = re.compile(r"(rise|rising|increase|grow|climb|up|gain|fall|falling|decline|drop|decrease|down)"
                      r"[^.%]{0,60}?(\d+(?:\.\d+)?)\s*(?:%\s*)?(?:–|-|—|to|~)\s*(\d+(?:\.\d+)?)%\s*(QoQ|YoY)?", re.I)
SINGLE_RE = re.compile(r"(rise|rising|increase|grow|climb|up|gain|fall|falling|decline|drop|decrease|down)"
                       r"[^.%]{0,60}?(\d+(?:\.\d+)?)%\s*(QoQ|YoY)", re.I)
QTR_RE = re.compile(r"\b([1-4])Q(\d{2})\b")


def parse_forecasts(text):
    out = []
    ctx = ""  # 앞 문장에서 언급된 분기를 이어서 사용
    for sent in re.split(r"(?<=[.!?])\s+", text):
        q0 = QTR_RE.search(sent)
        if q0:
            ctx = f"20{q0.group(2)}년 {q0.group(1)}분기"
        if "%" not in sent:
            continue
        prod = next((p for p, pat in PRODUCTS if re.search(pat, sent, re.I)), None)
        if not prod:
            continue
        m = RANGE_RE.search(sent)
        if m:
            lo, hi, basis = float(m.group(2)), float(m.group(3)), (m.group(4) or "")
        else:
            m = SINGLE_RE.search(sent)
            if not m:
                continue
            lo = hi = float(m.group(2)); basis = m.group(3)
        if re.match(r"fall|decline|drop|decrease|down", m.group(1), re.I):
            lo, hi = -hi, -lo
        basis = "YoY" if basis.upper() == "YOY" else "QoQ"
        y = re.search(r"\b(20\d{2})\b", sent)
        period = ctx if basis == "QoQ" else (f"{y.group(1)}년" if y else "")
        out.append({"product": prod, "low": lo, "high": hi, "basis": basis, "period": period})
    return out


def collect_forecast(fc):
    r = requests.get("https://www.trendforce.com/presscenter/news", headers=UA, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    links = {}
    for a in soup.find_all("a", href=re.compile(r"/presscenter/news/\d{8}-\d+\.html")):
        href = a["href"] if a["href"].startswith("http") else "https://www.trendforce.com" + a["href"]
        title = norm(a.get_text(" "))
        if title and len(title) > len(links.get(href, "")):
            links[href] = title
    known = {i["link"] for i in fc["items"]}
    added = 0
    for href, title in list(links.items())[:10]:
        if href in known or not re.search(r"DRAM|NAND|HBM|memory|SSD", title, re.I):
            continue
        try:
            page = requests.get(href, headers=UA, timeout=30)
            page.raise_for_status()
            body = norm(BeautifulSoup(page.text, "lxml").get_text(" "))[:20000]
        except Exception as e:
            print(f"[WARN] {href}: {e}")
            continue
        d = re.search(r"/(\d{4})(\d{2})(\d{2})-", href)
        date = f"{d.group(1)}-{d.group(2)}-{d.group(3)}"
        seen = set()
        for f in parse_forecasts(body):
            key = (f["product"], f["period"], f["basis"])
            if key in seen:
                continue
            seen.add(key)
            fc["items"].append({**f, "date": date, "title": title, "link": href})
            added += 1
    fc["items"].sort(key=lambda i: i["date"], reverse=True)
    fc["items"] = fc["items"][:60]
    ok("기관 전망", f"신규 {added}건")


# ─────────────────────────────────────────────
# 7. 가격 신호등 (공개 지표 방향 종합)
# ─────────────────────────────────────────────
def build_signal(spot, rev, fc):
    stocks = load("stocks.json", {})
    rows = []

    def add(name, val, unit, up, down, note):
        if val is None:
            rows.append({"name": name, "value": None, "dir": 0, "note": note + " · 데이터 누적 중"})
            return
        d = 1 if val >= up else -1 if val <= down else 0
        rows.append({"name": name, "value": round(val, 2), "unit": unit, "dir": d, "note": note})

    # 1) DRAM 현물가 4주 추세 (DDR5 16Gb, DDR4 8Gb 평균)
    ch = []
    for k in ("DDR5 16Gb", "DDR4 8Gb"):
        ser = spot["dramexchange"].get(k, {}).get("series", [])
        cut = (NOW - timedelta(days=28)).strftime("%Y-%m-%d")
        base = next((p for p in ser if p["date"] >= cut), None)
        if base and ser and ser[-1]["date"] > base["date"] and base["date"] <= (NOW - timedelta(days=14)).strftime("%Y-%m-%d"):
            ch.append((ser[-1]["avg"] / base["avg"] - 1) * 100)
    add("DRAM 현물가 4주 변화", sum(ch) / len(ch) if ch else None, "%", 2, -2, "현물가는 고정가보다 먼저 움직입니다")

    # 2) NAND 웨이퍼 현물가 최근 변화
    w = spot["dramexchange"].get("512Gb TLC 웨이퍼", {}).get("series", [])
    add("NAND 웨이퍼 최근 변화", w[-1]["chg"] if w else None, "%", 1, -1, "512Gb TLC 주간 변동")

    # 3) 대만 업체 월매출 전월 대비 (평균)
    moms = [c["series"][-1]["mom"] for c in rev.values() if c.get("series") and c["series"][-1].get("mom") is not None]
    add("대만 업체 월매출 전월 대비", sum(moms) / len(moms) if moms else None, "%", 3, -3, "Nanya·Winbond·Phison·ADATA 평균")

    # 4) 모듈 업체 매출 (재고 축적 신호)
    ad = rev.get("3260", {}).get("series", [])
    add("모듈 업체(ADATA) 전월 대비", ad[-1]["mom"] if ad else None, "%", 10, -10, "급증하면 가격 상승을 예상한 재고 축적 신호")

    # 5) 메모리 주가 1개월 (5사 평균)
    rets = [(v["series"][-1]["close"] / v["series"][-22]["close"] - 1) * 100 for v in stocks.values() if len(v.get("series", [])) > 22]
    add("메모리 주가 1개월", sum(rets) / len(rets) if rets else None, "%", 5, -5, "시장 기대가 먼저 반영됩니다")

    # 6) 최신 기관 전망 (범용 DRAM 우선)
    latest = next((i for i in fc["items"] if i["product"] == "범용 DRAM"), None) or (fc["items"][0] if fc["items"] else None)
    add("최신 기관 전망", (latest["low"] + latest["high"]) / 2 if latest else None, "%", 2, -2,
        f"TrendForce {latest['product']} {latest['period']} {latest['basis']}" if latest else "TrendForce")

    score = sum(r["dir"] for r in rows)
    counted = sum(1 for r in rows if r["value"] is not None)
    verdict = "상승" if score >= 2 else "하락" if score <= -2 else "보합"
    save("signal.json", {"date": TODAY, "verdict": verdict, "score": score, "counted": counted, "rows": rows})
    ok("신호등", f"{verdict} ({score:+d}, 지표 {counted}개)")


# ─────────────────────────────────────────────
def main():
    os.makedirs(DATA, exist_ok=True)
    spot = load("spot.json", {"dramexchange": {}, "cfm": {}})
    rev = load("revenue.json", {})
    news = load("news.json", {"items": []})
    fc = load("forecast.json", {"items": []})

    for name, fn in [("DRAMeXchange", lambda: collect_dramexchange(spot)),
                     ("CFM", lambda: collect_cfm(spot)),
                     ("대만 월매출", lambda: collect_revenue(rev)),
                     ("주가", collect_stocks),
                     ("뉴스", lambda: collect_news(news)),
                     ("기관 전망", lambda: collect_forecast(fc))]:
        try:
            fn()
        except Exception as e:
            fail(name, e)

    save("spot.json", spot)
    save("revenue.json", rev)
    save("news.json", news)
    save("forecast.json", fc)
    try:
        build_signal(spot, rev, fc)
    except Exception as e:
        fail("신호등", e)
    save("status.json", status)
    print(json.dumps(status, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
