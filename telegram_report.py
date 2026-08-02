"""
매일 아침 글로벌 시장 요약 발송 스크립트
발송 우선순위: 텔레그램 → Discord → 슬랙 → Gmail
Windows 작업 스케줄러에 등록하여 자동 실행
"""

import os
import sys
import ssl
import json
import html
import smtplib
import requests
import urllib3
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
import anthropic

# GitHub Actions 등 클라우드 러너는 기본 시간대가 UTC이므로 한국시간을 명시.
KST = ZoneInfo("Asia/Seoul")

# 회사 네트워크 자체서명 인증서 우회
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
ssl._create_default_https_context = ssl._create_unverified_context

load_dotenv()

BOT_TOKEN       = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID         = os.getenv("TELEGRAM_CHAT_ID", "")
# 공개 채널(무료 구독자 대상). 채널 자체는 비공개 정보가 아니라 코드 기본값으로 둠 —
# 필요시 TELEGRAM_CHANNEL_ID secret으로 덮어쓸 수 있음.
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "@a86791245")
API_KEY         = os.getenv("ANTHROPIC_API_KEY", "")
SLACK_WEBHOOK   = os.getenv("SLACK_WEBHOOK_URL", "")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "")
GMAIL_USER      = os.getenv("GMAIL_USER", "")
GMAIL_PASS      = os.getenv("GMAIL_APP_PASSWORD", "")
GMAIL_TO        = os.getenv("GMAIL_TO", GMAIL_USER)

HEADERS = {"User-Agent": "Mozilla/5.0"}

# ── 시세 수집 대상 ───────────────────────────────────────────
WATCHLIST = {
    "🇺🇸 미국": {
        "S&P 500":   "^GSPC",
        "나스닥":     "^IXIC",
        "다우존스":   "^DJI",
        "VIX":       "^VIX",
        "달러인덱스": "DX-Y.NYB",
    },
    "🇰🇷 한국": {
        "KOSPI":   "^KS11",
        "KOSDAQ":  "^KQ11",
        "원/달러": "USDKRW=X",
    },
    "🏦 자산": {
        "금":       "GC=F",
        "WTI 원유": "CL=F",
        "비트코인": "BTC-USD",
    },
}

NEWS_SOURCES = ["SPY", "QQQ", "^KS11"]

BLOCKED_ERRORS = (
    requests.exceptions.SSLError,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
)

# ── 데이터 안전장치 ──────────────────────────────────────────
# 하루 등락폭이 이 값을 넘으면 야후 데이터 소스 지연/오류로 의심하고,
# 숫자를 그대로 내보내지 않고 "확인 필요"로 표시한다.
# (근거: ^KS11/^KQ11에서 전일과 완전히 동일한 값이 이틀 연속 나온 뒤
#  나중에 -10% 안팎으로 몰아서 튀는 현상이 실제로 발견됨. 미국 지수는
#  동일 기간 정상 범위였음 — 한국 지수 데이터 소스 자체의 지연/재사용 이슈로 추정)
SANITY_MAX_PCT = {
    "^GSPC": 7, "^IXIC": 8, "^DJI": 7,
    "^KS11": 8, "^KQ11": 10,
    "^VIX": 50, "DX-Y.NYB": 3,
    "USDKRW=X": 5, "EURUSD=X": 4, "USDJPY=X": 4,
    "GC=F": 8, "CL=F": 12, "^TNX": 15, "BTC-USD": 20,
}
DEFAULT_MAX_PCT = 10

# 한국 지수는 야후보다 네이버금융 실시간 API가 더 신뢰도가 높음
# (야후 ^KS11/^KQ11에서 전일과 완전히 동일한 값이 반복되다 나중에
#  몰아서 튀는 현상이 실제 발견됨 — 네이버는 거래소 실시간 시세를 직접 반영)
NAVER_INDEX_MAP = {"^KS11": "KOSPI", "^KQ11": "KOSDAQ"}


def fetch_quote(sym: str) -> dict | None:
    is_naver = sym in NAVER_INDEX_MAP
    q = _fetch_naver(NAVER_INDEX_MAP[sym]) if is_naver else _fetch_yahoo(sym)
    if q is None:
        return None

    if is_naver:
        # 네이버(KOSPI/KOSDAQ)는 거래소 실시간 시세를 직접 받아오므로, 야후에서
        # 발견됐던 "전일과 동일한 값 반복" 같은 소스 자체의 결함이 구조적으로
        # 발생하지 않는다. 등락폭이 커도 실제 시세일 수 있으므로(예: 2026-08-01
        # 전후 급변동 시 야후·네이버 양쪽 모두 동일 수치로 교차 확인됨) 등락폭
        # 기준으로 걸러내지 않고, 값 자체가 비정상(0 이하 등)인 경우만 검증한다.
        suspect = q["price"] <= 0
    else:
        limit = SANITY_MAX_PCT.get(sym, DEFAULT_MAX_PCT)
        suspect = abs(q["pct"]) > limit
        if suspect:
            print(f"  ⚠️ 데이터 이상 감지: {sym} 등락률 {q['pct']:+.2f}% (기준 ±{limit}% 초과) — 발송에서 확인필요 처리")

    return {"price": q["price"], "change": q["change"], "pct": q["pct"], "suspect": suspect}


def _fetch_yahoo(sym: str) -> dict | None:
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
        resp = requests.get(url, params={"interval": "1d", "range": "5d"},
                            headers=HEADERS, verify=False, timeout=15)
        result = resp.json()["chart"]["result"][0]
        closes = [c for c in result["indicators"]["quote"][0]["close"] if c is not None]
        if len(closes) < 2:
            return None
        prev, last = closes[-2], closes[-1]
        chg = last - prev
        pct = (chg / prev) * 100 if prev else 0
        return {"price": last, "change": chg, "pct": pct}
    except Exception:
        return None


def _fetch_naver(item_code: str) -> dict | None:
    """네이버금융 실시간 지수 API (KOSPI/KOSDAQ 전용)."""
    try:
        url = f"https://polling.finance.naver.com/api/realtime/domestic/index/{item_code}"
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.naver.com/"},
            verify=False, timeout=15,
        )
        d = resp.json()["datas"][0]
        price = float(d["closePriceRaw"])
        pct   = abs(float(d["fluctuationsRatioRaw"]))
        chg   = abs(float(d["compareToPreviousClosePriceRaw"]))
        direction = d.get("compareToPreviousPrice", {}).get("text", "")
        if "하락" in direction or "하한" in direction:
            pct, chg = -pct, -chg
        return {"price": price, "change": chg, "pct": pct}
    except Exception as e:
        print(f"  네이버금융 조회 실패({item_code}): {e}")
        return None


def fetch_news(limit: int = 8) -> list[str]:
    return []


def arrow(pct: float) -> str:
    return "▲" if pct > 0 else ("▼" if pct < 0 else "━")


def format_market_block(quotes: dict, mode: str = "telegram") -> str:
    lines = []
    for market, tickers in WATCHLIST.items():
        lines.append(f"\n{market}")
        for name, sym in tickers.items():
            q = quotes.get(sym)
            if not q:
                continue
            if q.get("suspect"):
                lines.append(f"  ⚠️ {name}: 데이터 확인중 (자동 검증 보류)")
                continue
            sign = "+" if q["pct"] >= 0 else ""
            price = f"{q['price']:,.2f}"
            pct   = f"{sign}{q['pct']:.2f}%"
            if mode == "telegram":
                lines.append(f"  {arrow(q['pct'])} {name}: <code>{price}</code> ({pct})")
            else:
                lines.append(f"  {arrow(q['pct'])} {name}: `{price}` ({pct})")
    return "\n".join(lines)


def build_snapshot_text(quotes: dict) -> str:
    # 주의: 여기서 만든 텍스트가 그대로 AI(Claude) 프롬프트에 들어가 코멘트로
    # 재생산되므로, 검증 실패(suspect) 데이터는 절대 포함시키지 않는다.
    # (AI가 잘못된 수치를 사실처럼 서술하는 2차 오류를 막기 위함)
    lines = []
    for market, tickers in WATCHLIST.items():
        lines.append(f"\n[{market}]")
        for name, sym in tickers.items():
            q = quotes.get(sym)
            if not q:
                continue
            if q.get("suspect"):
                lines.append(f"  {name}: 데이터 검증 실패로 이번 분석에서 제외됨")
                continue
            sign = "+" if q["pct"] >= 0 else ""
            lines.append(f"  {name}: {q['price']:,.2f} ({sign}{q['pct']:.2f}%)")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# 4-페르소나 (무료: 강세론자 1개 / 프리미엄: 나머지 3개 + 종합결론 + 히스토리 컨텍스트)
# ════════════════════════════════════════════════════════════════

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

# 텔레그램 watchlist 심볼 -> daily_build.py가 쌓아온 historical/*.json 파일 키
HIST_KEY_MAP = {
    "^GSPC": "sp500", "^KS11": "kospi", "USDKRW=X": "usd_krw",
    "GC=F": "gold", "CL=F": "wti", "^VIX": "vix", "DX-Y.NYB": "dxy",
}
HIST_DIR = Path(__file__).parent / "public" / "data" / "historical"


def generate_persona_analyses(snapshot: str, headlines: list[str]) -> dict:
    if not API_KEY:
        return {k: "⚠️ ANTHROPIC_API_KEY 미설정" for k in PERSONAS}
    news_text = "\n".join(f"- {h}" for h in headlines)
    client = anthropic.Anthropic(api_key=API_KEY)
    results = {}
    for key, persona in PERSONAS.items():
        try:
            msg = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1200,
                system=persona["system"],
                messages=[{
                    "role": "user",
                    "content": (
                        f"📊 오늘의 시장 데이터:\n{snapshot}\n\n"
                        f"📰 주요 뉴스:\n{news_text}\n\n"
                        "아래 형식으로 한국어 분석을 작성해주세요. 각 항목은 2~3문장으로 간결하게.\n\n"
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


def generate_single_persona(key: str, snapshot: str, headlines: list[str]) -> str:
    """페르소나 1개만 생성 — 무료 채널(강세론자)용. 4개 다 돌리는 비용을 아낀다."""
    if not API_KEY:
        return "⚠️ ANTHROPIC_API_KEY 미설정"
    persona = PERSONAS[key]
    news_text = "\n".join(f"- {h}" for h in headlines)
    try:
        client = anthropic.Anthropic(api_key=API_KEY)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1200,
            system=persona["system"],
            messages=[{
                "role": "user",
                "content": (
                    f"📊 오늘의 시장 데이터:\n{snapshot}\n\n"
                    f"📰 주요 뉴스:\n{news_text}\n\n"
                    "아래 형식으로 한국어 분석을 작성해주세요. 각 항목은 2~3문장으로 간결하게.\n\n"
                    "1️⃣ 오늘의 핵심 판단\n"
                    "2️⃣ 시장 분석\n"
                    "3️⃣ 주목할 포인트\n\n"
                    "반드시 3️⃣ 항목까지 전부 작성하고 완결된 문장으로 마무리하세요 — "
                    "문장이나 항목이 중간에 끊기지 않도록 하세요."
                ),
            }],
        )
        return msg.content[0].text
    except Exception as e:
        return f"⚠️ 분석 생성 실패: {e}"


def generate_synthesis(persona_analyses: dict, snapshot: str) -> str:
    """4개 페르소나 의견을 종합해 실행 가능한 결론 도출 — 프리미엄의 핵심 차별점."""
    if not API_KEY:
        return "⚠️ ANTHROPIC_API_KEY 미설정"
    combined = "\n\n".join(
        f"[{PERSONAS[k]['name']}]\n{v}" for k, v in persona_analyses.items()
    )
    try:
        client = anthropic.Anthropic(api_key=API_KEY)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=700,
            messages=[{
                "role": "user",
                "content": (
                    f"📊 오늘의 시장 데이터:\n{snapshot}\n\n"
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


def percentile_context(hist_key: str, current_value: float, lookback: int = 252) -> str | None:
    """최근 1년(약 252거래일) 히스토리 대비 현재 값의 백분위 — 프리미엄 전용."""
    path = HIST_DIR / f"{hist_key}.json"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        values = [d["v"] for d in data["data"][-lookback:]]
        if len(values) < 30:
            return None
        rank = sum(1 for v in values if v <= current_value) / len(values) * 100
        if rank >= 80:
            desc = "상위권"
        elif rank <= 20:
            desc = "하위권"
        else:
            desc = "평균 범위"
        return f"최근 1년 대비 {rank:.0f}백분위 ({desc})"
    except Exception:
        return None


def build_premium_message(quotes: dict, headlines: list[str]) -> tuple[str, str]:
    """프리미엄 채널 전용 메시지 조립: 4-페르소나 + 종합 결론 + 히스토리 컨텍스트."""
    snapshot = build_snapshot_text(quotes)
    personas = generate_persona_analyses(snapshot, headlines)  # 종합결론 재료로 4개 다 필요
    synthesis = generate_synthesis(personas, snapshot)
    now_str = datetime.now(KST).strftime("%Y년 %m월 %d일 %H:%M")
    sep = "─" * 28

    # 표시는 프리미엄 전용 3개만 — 강세론자는 무료 채널에서 이미 공개됨
    premium_personas = {k: v for k, v in personas.items() if k in PREMIUM_PERSONA_KEYS}
    persona_blocks_html = "\n\n".join(
        f"<b>{PERSONAS[k]['name']}</b>\n{html.escape(v)}" for k, v in premium_personas.items()
    )
    persona_blocks_md = "\n\n".join(
        f"**{PERSONAS[k]['name']}**\n{v}" for k, v in premium_personas.items()
    )

    ctx_lines_html, ctx_lines_md = [], []
    for market, tickers in WATCHLIST.items():
        for name, sym in tickers.items():
            q = quotes.get(sym)
            hist_key = HIST_KEY_MAP.get(sym)
            if not q or q.get("suspect") or not hist_key:
                continue
            ctx = percentile_context(hist_key, q["price"])
            if ctx:
                ctx_lines_html.append(f"  {name}: {ctx}")
                ctx_lines_md.append(f"  {name}: {ctx}")
    ctx_block_html = "\n".join(ctx_lines_html) or "  (데이터 부족으로 이번엔 생략)"
    ctx_block_md   = "\n".join(ctx_lines_md) or "  (데이터 부족으로 이번엔 생략)"

    tg_message = (
        f"🌟 <b>프리미엄 브리핑</b>\n<i>{now_str}</i>\n{sep}\n\n"
        f"{persona_blocks_html}\n\n{sep}\n"
        f"🎯 <b>종합 결론</b>\n{html.escape(synthesis)}\n\n{sep}\n"
        f"📊 <b>히스토리 컨텍스트</b>\n{ctx_block_html}\n\n{sep}\n"
        f"<i>⚠️ 투자 참고용이며 투자 권유가 아닙니다.</i>"
    )
    plain_message = (
        f"🌟 **프리미엄 브리핑**\n{now_str}\n{sep}\n\n"
        f"{persona_blocks_md}\n\n{sep}\n"
        f"🎯 **종합 결론**\n{synthesis}\n\n{sep}\n"
        f"📊 **히스토리 컨텍스트**\n{ctx_block_md}\n\n{sep}\n"
        f"⚠️ 투자 참고용이며 투자 권유가 아닙니다."
    )
    return tg_message, plain_message


# ── 발송 채널 ────────────────────────────────────────────────

def _send_telegram_to(chat_id: str, text: str) -> bool:
    if not BOT_TOKEN or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
            resp = requests.post(url, json={
                "chat_id":    chat_id,
                "text":       chunk,
                "parse_mode": "HTML",
            }, timeout=10, verify=False)
            if not resp.ok:
                print(f"  텔레그램 응답 오류({chat_id}): {resp.text[:200]}")
                return False
        return True
    except BLOCKED_ERRORS as e:
        print(f"  텔레그램 차단/연결 실패({chat_id}): {type(e).__name__}")
        return False


def send_telegram(text: str) -> bool:
    # 개인 채팅(관리자 모니터링용)과 공개 채널(무료 구독자) 양쪽에 발송.
    # 하나라도 성공하면 전체 발송 성공으로 취급(다음 채널 폴백으로 넘어가지 않음).
    ok_personal = _send_telegram_to(CHAT_ID, text)
    print(f"  개인 채팅 발송: {'✅ 성공' if ok_personal else '❌ 실패'}")

    ok_channel = _send_telegram_to(TELEGRAM_CHANNEL_ID, text)
    print(f"  공개 채널({TELEGRAM_CHANNEL_ID}) 발송: {'✅ 성공' if ok_channel else '❌ 실패'}")

    return ok_personal or ok_channel


def send_discord(text: str) -> bool:
    if not DISCORD_WEBHOOK:
        return False
    # Discord 마크다운: *bold* 미지원 → **bold**, ` ` 코드는 동일
    discord_text = text.replace("*", "**")
    try:
        for chunk in [discord_text[i:i+1900] for i in range(0, len(discord_text), 1900)]:
            resp = requests.post(DISCORD_WEBHOOK, json={"content": chunk},
                                 timeout=10, verify=False)
            if not resp.ok:
                print(f"  Discord 응답 오류: {resp.text[:100]}")
                return False
        return True
    except BLOCKED_ERRORS as e:
        print(f"  Discord 차단/연결 실패: {type(e).__name__}")
        return False


def send_slack(text: str) -> bool:
    if not SLACK_WEBHOOK:
        return False
    try:
        for chunk in [text[i:i+3000] for i in range(0, len(text), 3000)]:
            resp = requests.post(SLACK_WEBHOOK, json={"text": chunk}, timeout=10, verify=False)
            if not resp.ok:
                print(f"  슬랙 응답 오류: {resp.text[:100]}")
                return False
        return True
    except BLOCKED_ERRORS as e:
        print(f"  슬랙 차단/연결 실패: {type(e).__name__}")
        return False


def send_gmail(subject: str, body: str) -> bool:
    if not GMAIL_USER or not GMAIL_PASS:
        print("  GMAIL_USER 또는 GMAIL_APP_PASSWORD 미설정")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = GMAIL_USER
        msg["To"]      = GMAIL_TO
        # 일반 텍스트 + HTML 두 파트 첨부
        plain = body.replace("`", "").replace("*", "").replace("_", "")
        html  = "<pre style='font-family:monospace'>" + plain.replace("\n", "<br>") + "</pre>"
        msg.attach(MIMEText(plain, "plain", "utf-8"))
        msg.attach(MIMEText(html,  "html",  "utf-8"))
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(GMAIL_USER, GMAIL_PASS)
            smtp.sendmail(GMAIL_USER, GMAIL_TO, msg.as_string())
        return True
    except Exception as e:
        print(f"  Gmail 발송 실패: {e}")
        return False


# ── 메인 ────────────────────────────────────────────────────

def main():
    today_kst = datetime.now(KST)
    is_weekend = today_kst.weekday() >= 5
    day_name = ["월", "화", "수", "목", "금", "토", "일"][today_kst.weekday()]

    now = today_kst.strftime("%Y년 %m월 %d일 %H:%M")
    print(f"[{now}] 시장 데이터 수집 중...")

    quotes: dict[str, dict] = {}
    for tickers in WATCHLIST.values():
        for sym in tickers.values():
            q = fetch_quote(sym)
            if q:
                quotes[sym] = q

    tg_market  = format_market_block(quotes, mode="telegram")
    raw_market = format_market_block(quotes, mode="plain")
    sep        = "─" * 28

    if is_weekend:
        # 주말: 휴장 안내 + 직전 거래일(금요일) 종가 기준 간단 요약만.
        # AI 분석·뉴스 수집은 생략 (휴장일이라 갱신될 정보가 없어 API 비용만 낭비됨).
        print(f"  {day_name}요일 — 휴장 안내 + 간단 요약만 발송합니다.")
        notice = f"🌙 오늘은 {day_name}요일, 한국·미국 시장 모두 휴장입니다."

        tg_message = (
            f"📈 <b>글로벌 시장 모닝 브리핑</b>\n"
            f"<i>{now}</i>\n"
            f"{sep}\n"
            f"{notice}\n"
            f"(아래는 직전 거래일 종가 기준입니다)\n\n"
            f"{tg_market}\n\n"
            f"{sep}\n"
            f"<i>⚠️ 투자 참고용이며 투자 권유가 아닙니다.</i>"
        )
        message = (
            f"📈 **글로벌 시장 모닝 브리핑**\n"
            f"{now}\n"
            f"{sep}\n"
            f"{notice}\n"
            f"(아래는 직전 거래일 종가 기준입니다)\n\n"
            f"{raw_market}\n\n"
            f"{sep}\n"
            f"⚠️ 투자 참고용이며 투자 권유가 아닙니다."
        )
        subject = f"🌙 휴장 안내 — {now}"
    else:
        headlines     = fetch_news()
        snapshot_text = build_snapshot_text(quotes)

        print(f"{PERSONAS[FREE_PERSONA_KEY]['name']} 분석 생성 중 (무료 기본 제공)...")
        bull_analysis = generate_single_persona(FREE_PERSONA_KEY, snapshot_text, headlines)

        news_block = "\n".join(f"• {h}" for h in headlines[:6])
        persona_label = PERSONAS[FREE_PERSONA_KEY]["name"]

        # 텔레그램 전용 (HTML)
        tg_message = (
            f"📈 <b>글로벌 시장 모닝 브리핑</b>\n"
            f"<i>{now}</i>\n"
            f"{sep}\n"
            f"{tg_market}\n\n"
            f"{sep}\n"
            f"{persona_label} <b>분석</b>\n{html.escape(bull_analysis)}\n\n"
            f"{sep}\n"
            f"📰 <b>주요 뉴스</b>\n{html.escape(news_block)}\n\n"
            f"<i>💎 신중론자·퀀트·워런 버핏 관점과 종합 결론은 프리미엄에서 확인하세요.</i>\n"
            f"<i>⚠️ 투자 참고용이며 투자 권유가 아닙니다.</i>"
        )

        # 그 외 채널 (Markdown/plain)
        message = (
            f"📈 **글로벌 시장 모닝 브리핑**\n"
            f"{now}\n"
            f"{sep}\n"
            f"{raw_market}\n\n"
            f"{sep}\n"
            f"{persona_label} **분석**\n{bull_analysis}\n\n"
            f"{sep}\n"
            f"📰 **주요 뉴스**\n{news_block}\n\n"
            f"💎 신중론자·퀀트·워런 버핏 관점과 종합 결론은 프리미엄에서 확인하세요.\n"
            f"⚠️ 투자 참고용이며 투자 권유가 아닙니다."
        )
        subject = f"📈 글로벌 시장 모닝 브리핑 — {now}"

    channels = [
        ("텔레그램", lambda: send_telegram(tg_message)),
        ("Discord",  lambda: send_discord(message)),
        ("슬랙",     lambda: send_slack(message)),
        ("Gmail",    lambda: send_gmail(subject, message)),
    ]

    for name, fn in channels:
        print(f"{name} 발송 중...")
        if fn():
            print(f"✅ {name} 발송 완료!")
            sys.exit(0)

    print("❌ 모든 채널 발송 실패")
    sys.exit(1)


def preview_premium():
    """프리미엄 콘텐츠 품질 확인용 — 개인 채팅(CHAT_ID)에만 발송, 채널엔 안 보냄."""
    print("[프리미엄 미리보기] 시장 데이터 수집 중...")
    quotes: dict[str, dict] = {}
    for tickers in WATCHLIST.values():
        for sym in tickers.values():
            q = fetch_quote(sym)
            if q:
                quotes[sym] = q

    headlines = fetch_news()
    print("[프리미엄 미리보기] 4-페르소나 + 종합 결론 생성 중 (AI 호출 5회, 시간이 조금 걸립니다)...")
    tg_message, _ = build_premium_message(quotes, headlines)

    ok = _send_telegram_to(CHAT_ID, tg_message)
    print("✅ 개인 채팅으로 미리보기 발송 완료" if ok else "❌ 미리보기 발송 실패")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    if "--premium-preview" in sys.argv:
        preview_premium()
    else:
        main()
