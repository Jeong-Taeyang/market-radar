"""
마켓레이더 - 글로벌 시장 브리핑 웹사이트 데이터 생성 & Vercel 자동 배포
매일 07:30 Windows 작업 스케줄러에 의해 실행됨

사이트 URL : https://market-radar-phi.vercel.app/
GitHub    : https://github.com/Jeong-Taeyang/market-radar

파이프라인:
  1. KCIF 국제금융속보 스크래핑
  2. Yahoo Finance API 시세 수집 + historical JSON 누적
  3. Claude API 4 페르소나 분석 생성 (강세론자/약세론자/퀀트/버핏)
  4. public/data/ JSON 파일 저장
  5. git add/commit/push → Vercel 자동 배포
"""

import os
import sys
import json
import ssl
import re
import subprocess
import urllib3
from datetime import datetime
from pathlib import Path

import requests
import yfinance as yf
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
import anthropic

# ── 초기화 ────────────────────────────────────────────────────────
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
ssl._create_default_https_context = ssl._create_unverified_context

load_dotenv()

# GitHub Actions 등 클라우드 러너는 기본 시간대가 UTC라, 반드시 한국시간(KST)을
# 명시해야 주말 스킵/날짜 스탬프가 로컬 PC(KST)에서 돌릴 때와 동일하게 동작함.
KST = ZoneInfo("Asia/Seoul")

def now_kst() -> datetime:
    return datetime.now(KST)

PROJECT_ROOT = Path(__file__).parent
DATA_DIR     = PROJECT_ROOT / "public" / "data"
HIST_DIR     = DATA_DIR / "historical"

API_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Referer": "https://www.kcif.or.kr/",
}

# ── 시세 종목 ─────────────────────────────────────────────────────
TICKERS = {
    "sp500":   {"sym": "^GSPC",    "label": "S&P 500",    "prefix": "",  "decimals": 1},
    "kospi":   {"sym": "^KS11",    "label": "KOSPI",       "prefix": "",  "decimals": 2},
    "usd_krw": {"sym": "USDKRW=X", "label": "원/달러",    "prefix": "₩", "decimals": 1},
    "wti":     {"sym": "CL=F",     "label": "WTI",         "prefix": "$", "decimals": 2},
    "gold":    {"sym": "GC=F",     "label": "금",           "prefix": "$", "decimals": 1},
    "vix":     {"sym": "^VIX",     "label": "VIX",         "prefix": "",  "decimals": 2},
    "dxy":     {"sym": "DX-Y.NYB", "label": "달러인덱스",  "prefix": "",  "decimals": 2},
    "us10y":   {"sym": "^TNX",     "label": "미국 10년물",  "prefix": "",  "decimals": 3},
    "eurusd":  {"sym": "EURUSD=X", "label": "EUR/USD",     "prefix": "",  "decimals": 4},
    "usdjpy":  {"sym": "USDJPY=X", "label": "USD/JPY",     "prefix": "",  "decimals": 2},
}

# ── 데이터 안전장치 ───────────────────────────────────────────────
# 하루 등락폭이 이 값을 넘으면 데이터 소스 지연/오류로 의심하고
# 사이트에 그 숫자를 그대로 게시하지 않는다.
# (근거: ^KS11/^KQ11에서 전일과 완전히 동일한 값이 이틀 연속 나온 뒤
#  나중에 -10% 안팎으로 몰아서 튀는 현상이 실제로 발견됨. 미국 지수는
#  동일 기간 정상 범위였음 — 한국 지수 데이터 소스 자체의 지연/재사용 이슈로 추정)
SANITY_MAX_PCT = {
    "sp500": 7, "kospi": 8, "usd_krw": 5, "wti": 12, "gold": 8,
    "vix": 50, "dxy": 3, "us10y": 15, "eurusd": 4, "usdjpy": 4,
}
DEFAULT_MAX_PCT = 10

# 한국 지수는 야후(^KS11)보다 네이버금융 실시간 API가 더 신뢰도가 높음
# (야후에서 전일과 완전히 동일한 값이 반복되다 나중에 몰아서 튀는
# 현상이 실제 발견됨 — 네이버는 거래소 실시간 시세를 직접 반영)
NAVER_INDEX_MAP = {"kospi": "KOSPI"}

# ── AI 페르소나 ───────────────────────────────────────────────────
PERSONAS = {
    "bull": {
        "name": "🐂 강세론자",
        "system": (
            "당신은 낙관적인 강세론자 투자 분석가입니다. "
            "시장의 긍정적 신호, 상승 모멘텀, 투자 기회를 부각하는 관점에서 분석합니다. "
            "리스크보다 기회를 강조하고 장기 성장 스토리를 지지하는 논거를 제시합니다."
        ),
    },
    "bear": {
        "name": "🦉 신중론자",
        "system": (
            "당신은 신중론자 투자 분석가입니다. "
            "시장의 과열 신호, 하방 리스크, 잠재적 위험 요인을 중심으로 분석합니다. "
            "낙관론에 경고를 보내고 방어적 포지션의 근거를 제시합니다. "
            "비관이 아니라 신중함이 목적이므로, 단정적인 공포 조장보다는 "
            "'무엇을 확인하기 전까지는 주의가 필요하다'는 톤을 유지합니다."
        ),
    },
    "quant": {
        "name": "📐 퀀트",
        "system": (
            "당신은 데이터 기반 퀀트 애널리스트입니다. "
            "숫자, 통계, 지표 간 상관관계를 중심으로 분석합니다. "
            "VIX 레벨, 모멘텀, 기술적 레벨, 변동성 패턴 등 계량적 관점에서 설명합니다. "
            "감정이 아닌 데이터로만 말합니다."
        ),
    },
    "buffett": {
        "name": "🎩 워런 버핏",
        "system": (
            "당신은 워런 버핏의 가치투자 철학을 따르는 장기 투자자입니다. "
            "'다른 사람이 탐욕스러울 때 두려워하고, 두려워할 때 탐욕스러워라'는 관점으로 분석합니다. "
            "단기 변동보다 기업 펀더멘털, 내재가치, 장기 성장성에 집중합니다. "
            "복잡한 금융 용어보다 쉽고 통찰 있는 언어를 사용합니다."
        ),
    },
}

# 무료: 강세론자 1개만 전체 공개. 유료: 나머지 3개 + 종합결론 + 히스토리 백분위.
FREE_PERSONA_KEY = "bull"
PREMIUM_PERSONA_KEYS = ["bear", "quant", "buffett"]

# ════════════════════════════════════════════════════════════════
# 1. KCIF 국제금융속보 스크래핑
# ════════════════════════════════════════════════════════════════

KCIF_LIST = "https://www.kcif.or.kr/front/board/listBoardMsg.do?boardId=73"
KCIF_BASE = "https://www.kcif.or.kr"


def scrape_kcif() -> dict:
    today = now_kst()
    date_patterns = [
        f"[{today.month}.{today.day:02d}]",
        f"[{today.month}.{today.day}]",
        f"{today.month}.{today.day:02d}",
        f"{today.month}.{today.day}",
    ]

    try:
        resp = requests.get(KCIF_LIST, headers=HEADERS, verify=False, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "lxml")

        # reportView 링크 탐색
        links = soup.find_all("a", href=re.compile(r"reportView\?rpt_no=\d+"))
        target = None
        for link in links:
            text = link.get_text(separator=" ", strip=True)
            for pat in date_patterns:
                if pat in text:
                    target = link
                    break
            if target:
                break

        # 오늘 날짜 없으면 최신 링크 사용
        if not target and links:
            print("  오늘 날짜 속보 없음 → 최신 링크 사용")
            target = links[0]

        if not target:
            return _kcif_fallback()

        # 링크 텍스트에서 제목 추출 (가장 신뢰할 수 있는 소스)
        raw_text = target.get_text(separator=" ", strip=True)
        # 날짜 접미사 제거, 카테고리 태그 제거
        link_title = re.sub(r'\s*\d{1,2}\.\d{2}\s*$', '', raw_text)
        link_title = re.sub(r'^\s*\|[^|]+\|\s*', '', link_title).strip()

        href = target.get("href", "")
        url = href if href.startswith("http") else KCIF_BASE + href
        return _fetch_detail(url, prefill_title=link_title)

    except Exception as e:
        print(f"  KCIF 목록 스크래핑 실패: {e}")
        return _kcif_fallback()


def _fetch_detail(url: str, prefill_title: str = "") -> dict:
    try:
        resp = requests.get(url, headers=HEADERS, verify=False, timeout=20)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.content, "lxml")

        # 제목: 링크 텍스트에서 미리 추출한 값 우선 사용
        title = prefill_title
        if not title:
            for sel in ["h1.tit", "h2.tit", ".view_tit", ".subject", "h1", "h2"]:
                el = soup.select_one(sel)
                if el:
                    t = el.get_text(strip=True)
                    if len(t) > 10 and "주요뉴스" not in t:
                        title = t
                        break

        # 본문 추출
        body = ""
        candidates = [
            soup.select_one(".view_cont"),
            soup.select_one(".board_cont"),
            soup.select_one(".content_area"),
            soup.select_one("#content"),
            soup.select_one(".bbs_view"),
        ]
        for c in candidates:
            if c:
                t = c.get_text(separator="\n", strip=True)
                if len(t) > 200:
                    body = t
                    break

        # 전체 텍스트에서 주요뉴스 섹션 추출 (fallback)
        if not body:
            all_lines = [l.strip() for l in soup.get_text("\n").split("\n") if l.strip()]
            try:
                s = next(i for i, l in enumerate(all_lines) if "주요뉴스" in l or "주요 뉴스" in l)
                e = next(
                    (i for i, l in enumerate(all_lines[s+1:], s+1)
                     if any(k in l for k in ["국제금융시장", "자료:", "※", "문의처"])),
                    s + 25,
                )
                body = "\n".join(all_lines[s:e])
            except StopIteration:
                body = "\n".join(all_lines[:30])

        if not title and body:
            title = body.split("\n")[0][:120]

        print(f"  KCIF 속보: {title[:60]}...")
        return {"title": title, "body": body, "url": url, "success": True}

    except Exception as e:
        print(f"  KCIF 상세 스크래핑 실패: {e}")
        return _kcif_fallback()


def _kcif_fallback() -> dict:
    return {
        "title": now_kst().strftime("%Y년 %m월 %d일 글로벌 시장 브리핑"),
        "body": "",
        "url": KCIF_LIST,
        "success": False,
    }


# ════════════════════════════════════════════════════════════════
# 2. 시세 수집 + historical JSON 업데이트
# ════════════════════════════════════════════════════════════════

def _naver_index_quote(item_code: str) -> dict | None:
    """네이버금융 실시간 지수 API (KOSPI/KOSDAQ 전용).
    한국 지수는 야후보다 이 소스가 더 신뢰도 높음."""
    try:
        url = f"https://polling.finance.naver.com/api/realtime/domestic/index/{item_code}"
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.naver.com/"},
            verify=False, timeout=15,
        )
        d = resp.json()["datas"][0]
        value = float(d["closePriceRaw"])
        pct   = abs(float(d["fluctuationsRatioRaw"]))
        direction = d.get("compareToPreviousPrice", {}).get("text", "")
        if "하락" in direction or "하한" in direction:
            pct = -pct
        return {"value": value, "pct": pct}
    except Exception as e:
        print(f"  네이버금융 조회 실패({item_code}): {e}")
        return None


def _yahoo_quote(sym: str) -> dict | None:
    """Yahoo Finance Chart API를 requests(verify=False)로 직접 호출."""
    import time as _time
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
    params = {"interval": "1d", "range": "5d"}
    for attempt in range(2):
        try:
            resp = requests.get(url, params=params, headers=HEADERS,
                                verify=False, timeout=15)
            data = resp.json()
            closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
            closes = [c for c in closes if c is not None]
            if len(closes) < 2:
                return None
            prev = closes[-2]
            last = closes[-1]
            pct  = (last - prev) / prev * 100 if prev else 0
            return {"value": last, "pct": pct}
        except Exception:
            if attempt == 0:
                _time.sleep(1)
    return None


def _yahoo_history(sym: str, period: str = "5y") -> list[dict]:
    """Yahoo Finance Chart API로 히스토리컬 데이터 수집."""
    import time as _time
    range_map = {"5y": "5y", "10y": "10y", "max": "max"}
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
    params = {"interval": "1d", "range": range_map.get(period, "5y")}
    for attempt in range(2):
        try:
            resp = requests.get(url, params=params, headers=HEADERS,
                                verify=False, timeout=30)
            data = resp.json()
            result = data["chart"]["result"][0]
            timestamps = result["timestamp"]
            closes     = result["indicators"]["quote"][0]["close"]
            rows = []
            for ts, c in zip(timestamps, closes):
                if c is None:
                    continue
                from datetime import date as _date
                d = _date.fromtimestamp(ts).isoformat()
                rows.append({"d": d, "v": c})
            return rows
        except Exception:
            if attempt == 0:
                _time.sleep(1)
    return []


def fetch_market_data() -> dict:
    print("  시세 수집 중 (KOSPI: 네이버금융 / 그 외: Yahoo Finance 직접 호출)...")
    import time as _time
    quotes = {}
    for key, cfg in TICKERS.items():
        if key in NAVER_INDEX_MAP:
            q = _naver_index_quote(NAVER_INDEX_MAP[key])
        else:
            q = _yahoo_quote(cfg["sym"])
        if q:
            d    = cfg["decimals"]
            sign = "+" if q["pct"] >= 0 else ""
            if key in NAVER_INDEX_MAP:
                # 네이버(KOSPI)는 거래소 실시간 시세 직접 반영이라 등락폭이 커도
                # 실제 시세일 수 있음 — 값 자체 유효성만 체크. "전일과 완전히
                # 동일한 값" 검증은 아래 update_historical()에서 별도로 수행.
                suspect = q["value"] <= 0
            else:
                limit   = SANITY_MAX_PCT.get(key, DEFAULT_MAX_PCT)
                suspect = abs(q["pct"]) > limit
                if suspect:
                    print(f"  ⚠️ 데이터 이상 감지: {key} 등락률 {sign}{q['pct']:.2f}% (기준 ±{limit}% 초과)")
            quotes[key] = {
                "value":   f"{q['value']:,.{d}f}",
                "change":  f"{sign}{q['pct']:.2f}%",
                "_raw":    q["value"],
                "_pct":    q["pct"],
                "_suspect": suspect,
            }
        _time.sleep(0.3)   # rate limit 방지
    return quotes


def _init_historical(key: str, cfg: dict) -> dict:
    print(f"    {key} 5년치 초기화 중...")
    data = {
        "key": key, "label": cfg["label"],
        "prefix": cfg["prefix"], "decimals": cfg["decimals"],
        "updated": "", "data": [],
    }
    rows = _yahoo_history(cfg["sym"], "5y")
    if rows:
        data["data"] = [{"d": r["d"], "v": round(r["v"], cfg["decimals"])} for r in rows]
        print(f"    {key}: {len(data['data'])}일치 수집 완료")
    else:
        print(f"    {key} 초기화 실패")
    return data


def update_historical(date: str, quotes: dict):
    HIST_DIR.mkdir(parents=True, exist_ok=True)
    for key, cfg in TICKERS.items():
        if key not in quotes:
            continue
        path = HIST_DIR / f"{key}.json"
        if path.exists():
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = _init_historical(key, cfg)

        raw_val  = round(quotes[key]["_raw"], cfg["decimals"])
        existing = {e["d"]: i for i, e in enumerate(data["data"])}

        # 전일과 완전히 동일한 값이면(활발히 거래되는 지수·환율에서 통계적으로
        # 거의 불가능) 데이터 소스가 갱신되지 않은 것으로 의심 — 의심 처리.
        if data["data"] and date not in existing:
            last_val = data["data"][-1]["v"]
            if raw_val == last_val:
                quotes[key]["_suspect"] = True
                print(f"  ⚠️ 데이터 이상 감지: {key} 값이 전일과 완전히 동일함({raw_val}) — 확인필요 처리")

        if quotes[key].get("_suspect"):
            # 의심스러운 값은 히스토리(차트)에 반영하지 않고 건너뛴다.
            # → 나쁜 값이 장기 데이터에 영구히 남는 것을 방지.
            print(f"  ⏭️  {key}: 검증 실패로 히스토리 업데이트 건너뜀 (기존 값 유지)")
            continue

        # 오늘 데이터 추가/갱신
        if date in existing:
            data["data"][existing[date]]["v"] = raw_val
        else:
            data["data"].append({"d": date, "v": raw_val})
            data["data"].sort(key=lambda x: x["d"])

        data["data"]  = data["data"][-2000:]   # 최대 2000일 유지
        data["updated"] = date

        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    print(f"  historical JSON 업데이트 완료 ({len(quotes)}개 종목)")


# ════════════════════════════════════════════════════════════════
# 3. Claude API 4 페르소나 분석
# ════════════════════════════════════════════════════════════════

def _market_text(quotes: dict) -> str:
    # 주의: 이 텍스트가 그대로 AI(Claude) 프롬프트에 들어가므로, 검증 실패
    # (_suspect) 데이터는 절대 포함시키지 않는다 — AI가 잘못된 수치를
    # 사실처럼 서술하는 2차 오류를 막기 위함.
    lines = []
    for key, cfg in TICKERS.items():
        q = quotes.get(key)
        if not q:
            continue
        if q.get("_suspect"):
            lines.append(f"{cfg['label']}: 데이터 검증 실패로 이번 분석에서 제외됨")
            continue
        lines.append(f"{cfg['label']}: {q['value']} ({q['change']})")
    return "\n".join(lines)


def generate_analyses(quotes: dict, kcif_title: str, kcif_body: str) -> dict:
    if not API_KEY:
        return {k: f"⚠️ ANTHROPIC_API_KEY 미설정" for k in PERSONAS}

    client   = anthropic.Anthropic(api_key=API_KEY)
    snapshot = _market_text(quotes)
    news_ctx = f"{kcif_title}\n\n{kcif_body[:1500]}" if kcif_body else kcif_title
    results  = {}

    for key, persona in PERSONAS.items():
        print(f"  {persona['name']} 분석 생성 중...")
        try:
            msg = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1200,
                system=persona["system"],
                messages=[{
                    "role": "user",
                    "content": (
                        f"📊 오늘의 시장 데이터:\n{snapshot}\n\n"
                        f"📰 오늘의 KCIF 국제금융속보:\n{news_ctx}\n\n"
                        "아래 형식으로 한국어 분석을 작성해주세요. "
                        "각 항목은 2~3문장으로 간결하게.\n\n"
                        "1️⃣ 오늘의 핵심 판단\n"
                        "2️⃣ 시장 분석\n"
                        "3️⃣ 주목할 포인트\n\n"
                        "반드시 3️⃣ 항목까지 전부 작성하고 완결된 문장으로 마무리하세요 — "
                        "문장이나 항목이 중간에 끊기지 않도록 하세요."
                    ),
                }],
            )
            results[key] = msg.content[0].text
        except Exception as e:
            results[key] = f"⚠️ 분석 생성 실패: {e}"

    return results


def generate_synthesis(analyses: dict, quotes: dict) -> str:
    """4개 페르소나 의견을 종합해 실행 가능한 결론 도출 — 프리미엄 전용."""
    if not API_KEY:
        return "⚠️ ANTHROPIC_API_KEY 미설정"
    combined = "\n\n".join(
        f"[{PERSONAS[k]['name']}]\n{v}" for k, v in analyses.items()
    )
    try:
        client = anthropic.Anthropic(api_key=API_KEY)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=700,
            messages=[{
                "role": "user",
                "content": (
                    f"📊 오늘의 시장 데이터:\n{_market_text(quotes)}\n\n"
                    f"아래는 오늘 시장에 대한 4가지 관점의 분석입니다:\n\n{combined}\n\n"
                    "이 4가지 관점을 종합해서, 오늘 투자자가 참고할 수 있는 "
                    "'종합 결론'을 3~4문장으로 작성해주세요. "
                    "관점이 서로 상충한다면 그 긴장관계를 짚어주고, "
                    "관망/리스크관리/기회탐색 중 방향성을 명확히 제시하세요. "
                    "특정 종목 매수·매도를 지시하지 말고 참고용 관점으로 서술하세요. "
                    "반드시 완결된 문장으로 마무리하세요 — 문장이 중간에 끊기지 않도록 "
                    "여유 있게 작성하고, 마지막 문장을 결론으로 깔끔하게 맺으세요."
                ),
            }],
        )
        return msg.content[0].text
    except Exception as e:
        return f"⚠️ 종합 결론 생성 실패: {e}"


def percentile_context(key: str, current_value: float, lookback: int = 252) -> dict | None:
    """최근 1년(약 252거래일) 히스토리 대비 현재 값의 백분위 — 프리미엄 전용."""
    path = HIST_DIR / f"{key}.json"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        values = [d["v"] for d in data["data"][-lookback:]]
        if len(values) < 30:
            return None
        rank = sum(1 for v in values if v <= current_value) / len(values) * 100
        desc = "상위권" if rank >= 80 else ("하위권" if rank <= 20 else "평균 범위")
        return {"rank": round(rank), "desc": desc}
    except Exception:
        return None


def build_percentiles(quotes: dict) -> dict:
    out = {}
    for key, q in quotes.items():
        if q.get("_suspect"):
            continue
        ctx = percentile_context(key, q["_raw"])
        if ctx:
            out[key] = ctx
    return out


# ════════════════════════════════════════════════════════════════
# 4. JSON 파일 빌드
# ════════════════════════════════════════════════════════════════

def _clean_quotes(quotes: dict) -> dict:
    # 검증 실패(_suspect) 데이터는 사이트에 숫자를 그대로 노출하지 않고
    # "확인중" 상태로 내려보낸다 — 프론트엔드에서 badge로 표시 가능.
    out = {}
    for k, v in quotes.items():
        if v.get("_suspect"):
            out[k] = {"value": v["value"], "change": v["change"], "suspect": True}
        else:
            out[k] = {"value": v["value"], "change": v["change"]}
    return out


def save_daily_json(date: str, title: str, source_url: str,
                    quotes: dict, analyses: dict, synthesis: str, percentiles: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "date":        date,
        "title":       title,
        "source_url":  source_url,
        "market_data": _clean_quotes(quotes),
        "synthesis":   synthesis,     # 프리미엄 전용 — 프론트에서 블러 처리
        "percentiles": percentiles,   # 프리미엄 전용 — 프론트에서 블러 처리
        **analyses,
    }
    path = DATA_DIR / f"{date}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"  {path.name} 저장 완료")


def update_reports_json(date: str, title: str, quotes: dict, analyses: dict):
    path = DATA_DIR / "reports.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {"updated_at": "", "total": 0, "reports": []}

    entry = {
        "date":         date,
        "title":        title,
        "has_bull":     bool(analyses.get("bull")),
        "has_bear":     bool(analyses.get("bear")),
        "has_quant":    bool(analyses.get("quant")),
        "has_buffett":  bool(analyses.get("buffett")),
        "market_data":  _clean_quotes(quotes),
    }

    # 오늘 날짜 항목 교체 or 맨 앞에 추가
    existing_idx = next((i for i, r in enumerate(data["reports"]) if r["date"] == date), None)
    if existing_idx is not None:
        data["reports"][existing_idx] = entry
    else:
        data["reports"].insert(0, entry)

    data["reports"]  = data["reports"][:100]
    data["updated_at"] = now_kst().strftime("%Y-%m-%d %H:%M KST")
    data["total"]    = len(data["reports"])

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  reports.json 업데이트 완료 (총 {data['total']}개)")


# ════════════════════════════════════════════════════════════════
# 5. Git 배포
# ════════════════════════════════════════════════════════════════

def git_deploy(date: str) -> bool:
    def run(cmd):
        r = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8")
        if r.returncode != 0:
            print(f"  git 오류: {r.stderr.strip()[:200]}")
        return r.returncode == 0

    print("  git push 중...")
    ok = (
        run(["git", "add", "public/data/"])
        and run(["git", "commit", "-m", f"data: {date} 시장 브리핑 업데이트"])
        and run(["git", "push"])
    )
    if ok:
        print("  ✅ Vercel 자동 배포 트리거 완료")
    else:
        print("  ⚠️ git push 실패 — 로컬 JSON은 저장됨")
    return ok


# ════════════════════════════════════════════════════════════════
# 메인
# ════════════════════════════════════════════════════════════════

def main():
    now  = now_kst()
    # 날짜를 인자로 넘기면(예: 놓친 토요일 자동화를 뒤늦게 수동으로 채울 때)
    # 해당 날짜로 강제 지정하고 요일 스킵 검사를 건너뛴다. 인자가 없으면
    # 지금까지처럼 실제 현재 시각 기준으로 동작한다.
    override_date = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    date = override_date or now.strftime("%Y-%m-%d")
    print(f"\n{'='*50}")
    print(f"마켓레이더 빌드 시작: {date}" + (" (수동 지정)" if override_date else f" {now.strftime('%H:%M')}"))
    print(f"{'='*50}\n")

    # 일요일만 건너뜀. 토요일 07:30 KST 시점엔 금요일 한국장(전일 마감)뿐 아니라
    # 금요일 미국장(현지시간 16:00 ET 마감 = 한국시간 토요일 새벽 5시경)까지
    # 이미 확정돼있으므로, 토요일 빌드가 "금요일 거래일"을 담당하는 정상 빌드다.
    if not override_date and now.weekday() == 6:
        print("  일요일 — 신규로 마감된 거래일이 없어 빌드를 건너뜁니다.")
        sys.exit(0)

    # 1. KCIF 스크래핑
    print("[1/5] KCIF 국제금융속보 수집...")
    news = scrape_kcif()

    # 2. 시세 수집
    print("\n[2/5] 시세 데이터 수집...")
    quotes = fetch_market_data()
    if not quotes:
        print("  ❌ 시세 수집 실패 — 중단")
        sys.exit(1)
    print(f"  수집 완료: {len(quotes)}개 종목")

    # 3. historical JSON 업데이트
    print("\n[3/5] Historical JSON 업데이트...")
    update_historical(date, quotes)

    # 4. AI 분석 생성
    print("\n[4/5] AI 페르소나 분석 생성...")
    analyses = generate_analyses(quotes, news["title"], news["body"])

    print("  종합 결론 생성 중 (프리미엄)...")
    synthesis = generate_synthesis(analyses, quotes)

    print("  히스토리 백분위 계산 중 (프리미엄)...")
    percentiles = build_percentiles(quotes)

    # 5. JSON 저장
    print("\n[5/5] JSON 파일 저장...")
    save_daily_json(date, news["title"], news["url"], quotes, analyses, synthesis, percentiles)
    update_reports_json(date, news["title"], quotes, analyses)

    # 6. Git 배포
    print("\n[배포] Vercel 배포...")
    git_deploy(date)

    print(f"\n✅ 빌드 완료: {datetime.now().strftime('%H:%M:%S')}")


if __name__ == "__main__":
    main()
