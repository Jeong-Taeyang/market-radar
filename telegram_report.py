"""
매일 아침 글로벌 시장 요약 발송 스크립트
발송 우선순위: 텔레그램 → Discord → 슬랙 → Gmail
Windows 작업 스케줄러에 등록하여 자동 실행
"""

import os
import sys
import ssl
import html
import smtplib
import requests
import urllib3
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
    q = _fetch_naver(NAVER_INDEX_MAP[sym]) if sym in NAVER_INDEX_MAP else _fetch_yahoo(sym)
    if q is None:
        return None

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


def get_ai_summary(snapshot: str, headlines: list[str]) -> str:
    if not API_KEY:
        return "⚠️ ANTHROPIC_API_KEY 미설정"
    news_text = "\n".join(f"- {h}" for h in headlines)
    try:
        client = anthropic.Anthropic(api_key=API_KEY)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            messages=[{
                "role": "user",
                "content": (
                    "당신은 투자 전문 애널리스트입니다.\n"
                    "아래 시장 데이터와 뉴스를 바탕으로 오늘의 글로벌 시장 동향을 "
                    "간결하게 한국어로 요약해 주세요.\n\n"
                    "📊 시장 데이터:\n" + snapshot + "\n\n"
                    "📰 주요 뉴스:\n" + news_text + "\n\n"
                    "형식 (각 항목 2~3문장):\n"
                    "1️⃣ 오늘의 핵심\n"
                    "2️⃣ 미국 시장\n"
                    "3️⃣ 한국 시장\n"
                    "4️⃣ 주목 포인트\n"
                ),
            }],
        )
        return msg.content[0].text
    except Exception as e:
        return f"AI 요약 오류: {e}"


# ── 발송 채널 ────────────────────────────────────────────────

def send_telegram(text: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
            resp = requests.post(url, json={
                "chat_id":    CHAT_ID,
                "text":       chunk,
                "parse_mode": "HTML",
            }, timeout=10, verify=False)
            if not resp.ok:
                print(f"  텔레그램 응답 오류: {resp.text[:100]}")
                return False
        return True
    except BLOCKED_ERRORS as e:
        print(f"  텔레그램 차단/연결 실패: {type(e).__name__}")
        return False


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

        print("AI 요약 생성 중...")
        summary = get_ai_summary(snapshot_text, headlines)

        news_block = "\n".join(f"• {h}" for h in headlines[:6])

        # 텔레그램 전용 (HTML)
        tg_message = (
            f"📈 <b>글로벌 시장 모닝 브리핑</b>\n"
            f"<i>{now}</i>\n"
            f"{sep}\n"
            f"{tg_market}\n\n"
            f"{sep}\n"
            f"🤖 <b>AI 분석</b>\n{html.escape(summary)}\n\n"
            f"{sep}\n"
            f"📰 <b>주요 뉴스</b>\n{html.escape(news_block)}\n\n"
            f"<i>⚠️ 투자 참고용이며 투자 권유가 아닙니다.</i>"
        )

        # 그 외 채널 (Markdown/plain)
        message = (
            f"📈 **글로벌 시장 모닝 브리핑**\n"
            f"{now}\n"
            f"{sep}\n"
            f"{raw_market}\n\n"
            f"{sep}\n"
            f"🤖 **AI 분석**\n{summary}\n\n"
            f"{sep}\n"
            f"📰 **주요 뉴스**\n{news_block}\n\n"
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


if __name__ == "__main__":
    main()
