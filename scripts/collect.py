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
                            "tag": "전망" if re.search(r"forecast|projected|outlook|QoQ|전망|预测|预计", title, re.I) else ""})
            if lang != "ko" and new:
                for n, ko in zip(new, translate([n["title"] for n in new], lang)):
                    n["ko"] = ko
            for n in new:
                items[n["link"]] = n
                added += 1
        except Exception as e:
            errors.append(f"{src}/{lang}: {e}")
    # 최근 14일, 최대 120건 유지
    cutoff = (NOW - timedelta(days=14)).strftime("%Y-%m-%d")
    kept = sorted([n for n in items.values() if n["date"] >= cutoff], key=lambda n: n["date"], reverse=True)[:120]
    news["items"] = kept
    if not kept:
        raise RuntimeError("; ".join(errors) or "뉴스 없음")
    ok("뉴스", f"신규 {added}건" + (f" (일부 실패: {len(errors)})" if errors else ""))


# ─────────────────────────────────────────────
def main():
    os.makedirs(DATA, exist_ok=True)
    spot = load("spot.json", {"dramexchange": {}, "cfm": {}})
    rev = load("revenue.json", {})
    news = load("news.json", {"items": []})

    for name, fn in [("DRAMeXchange", lambda: collect_dramexchange(spot)),
                     ("CFM", lambda: collect_cfm(spot)),
                     ("대만 월매출", lambda: collect_revenue(rev)),
                     ("주가", collect_stocks),
                     ("뉴스", lambda: collect_news(news))]:
        try:
            fn()
        except Exception as e:
            fail(name, e)

    save("spot.json", spot)
    save("revenue.json", rev)
    save("news.json", news)
    save("status.json", status)
    print(json.dumps(status, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
