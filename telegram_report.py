"""
매일 아침 글로벌 시장 요약 발송 스크립트
발송 우선순위: 텔레그램 → 슬랙 → Gmail
Windows 작업 스케줄러에 등록하여 자동 실행
"""

import os
import sys
import ssl
import smtplib
import requests
import urllib3
import yfinance as yf
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
import anthropic

# 회사 네트워크 자체서명 인증서 우회
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
ssl._create_default_https_context = ssl._create_unverified_context

load_dotenv()

BOT_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID       = os.getenv("TELEGRAM_CHAT_ID", "")
API_KEY       = os.getenv("ANTHROPIC_API_KEY", "")
SLACK_WEBHOOK = os.getenv("SLACK_WEBHOOK_URL", "")
GMAIL_USER    = os.getenv("GMAIL_USER", "")
GMAIL_PASS    = os.getenv("GMAIL_APP_PASSWORD", "")
GMAIL_TO      = os.getenv("GMAIL_TO", GMAIL_USER)

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


def fetch_quote(ticker: str) -> dict | None:
    try:
        hist = yf.Ticker(ticker).history(period="5d", interval="1d")
        if len(hist) < 2:
            return None
        prev = float(hist["Close"].iloc[-2])
        last = float(hist["Close"].iloc[-1])
        chg  = last - prev
        pct  = (chg / prev) * 100 if prev else 0
        return {"price": last, "change": chg, "pct": pct}
    except Exception:
        return None


def fetch_news(limit: int = 8) -> list[str]:
    headlines, seen = [], set()
    for sym in NEWS_SOURCES:
        try:
            for n in (yf.Ticker(sym).news or [])[:5]:
                title = n.get("title", "")
                if title and title not in seen:
                    seen.add(title)
                    headlines.append(title)
                    if len(headlines) >= limit:
                        return headlines
        except Exception:
            continue
    return headlines


def arrow(pct: float) -> str:
    return "▲" if pct > 0 else ("▼" if pct < 0 else "━")


def format_market_block(quotes: dict) -> str:
    lines = []
    for market, tickers in WATCHLIST.items():
        lines.append(f"\n{market}")
        for name, sym in tickers.items():
            q = quotes.get(sym)
            if not q:
                continue
            sign = "+" if q["pct"] >= 0 else ""
            lines.append(
                f"  {arrow(q['pct'])} {name}: `{q['price']:,.2f}` ({sign}{q['pct']:.2f}%)"
            )
    return "\n".join(lines)


def build_snapshot_text(quotes: dict) -> str:
    lines = []
    for market, tickers in WATCHLIST.items():
        lines.append(f"\n[{market}]")
        for name, sym in tickers.items():
            q = quotes.get(sym)
            if q:
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
                "parse_mode": "Markdown",
            }, timeout=10, verify=False)
            if not resp.ok:
                print(f"  텔레그램 응답 오류: {resp.text[:100]}")
                return False
        return True
    except BLOCKED_ERRORS as e:
        print(f"  텔레그램 차단/연결 실패: {type(e).__name__}")
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
    now = datetime.now().strftime("%Y년 %m월 %d일 %H:%M")
    print(f"[{now}] 시장 데이터 수집 중...")

    quotes: dict[str, dict] = {}
    for tickers in WATCHLIST.values():
        for sym in tickers.values():
            q = fetch_quote(sym)
            if q:
                quotes[sym] = q

    headlines     = fetch_news()
    snapshot_text = build_snapshot_text(quotes)

    print("AI 요약 생성 중...")
    summary = get_ai_summary(snapshot_text, headlines)

    market_block = format_market_block(quotes)
    news_block   = "\n".join(f"• {h}" for h in headlines[:6])

    message = (
        f"📈 *글로벌 시장 모닝 브리핑*\n"
        f"_{now}_\n"
        f"{'─' * 28}\n"
        f"{market_block}\n\n"
        f"{'─' * 28}\n"
        f"🤖 *AI 분석*\n{summary}\n\n"
        f"{'─' * 28}\n"
        f"📰 *주요 뉴스*\n{news_block}\n\n"
        f"_⚠️ 투자 참고용이며 투자 권유가 아닙니다._"
    )
    subject = f"📈 글로벌 시장 모닝 브리핑 — {now}"

    channels = [
        ("텔레그램", lambda: send_telegram(message)),
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
