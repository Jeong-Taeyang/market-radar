"""
과거 리포트 JSON에 synthesis(종합결론)/percentiles(히스토리 백분위)를
소급 계산해서 채워넣는 1회성 스크립트.

사용법: python backfill_premium.py 2026-07-08
(날짜 생략 시 public/data/ 안에서 4-페르소나 분석이 모두 정상 생성된
가장 최근 파일을 자동으로 찾아 사용)
"""

import sys
import json
from pathlib import Path

from daily_build import (
    generate_synthesis, build_percentiles, generate_analyses,
    PROJECT_ROOT, DATA_DIR,
)

PERSONA_KEYS = ["bull", "bear", "quant", "buffett"]


def _to_raw(value_str: str) -> float:
    return float(value_str.replace(",", ""))


def find_valid_date() -> str | None:
    files = sorted(DATA_DIR.glob("*.json"), reverse=True)
    for f in files:
        if f.name == "reports.json":
            continue
        with open(f, encoding="utf-8") as fh:
            data = json.load(fh)
        texts = [data.get(k, "") for k in PERSONA_KEYS]
        if all(texts) and not any("⚠️" in t for t in texts):
            return data["date"]
    return None


def backfill(date: str):
    path = DATA_DIR / f"{date}.json"
    if not path.exists():
        print(f"❌ {path} 없음")
        sys.exit(1)

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    quotes = {}
    for key, md in data["market_data"].items():
        if md.get("suspect"):
            continue
        quotes[key] = {"value": md["value"], "change": md["change"], "_raw": _to_raw(md["value"])}

    analyses = {k: data.get(k, "") for k in PERSONA_KEYS}
    if not all(analyses.values()) or any("⚠️" in v for v in analyses.values()):
        # 원래 저장된 페르소나 분석이 실패(API 오류 등)했던 경우 —
        # 저장된 market_data + title로 4개 페르소나부터 다시 생성.
        print(f"[{date}] 기존 페르소나 분석이 유효하지 않아 재생성합니다...")
        analyses = generate_analyses(quotes, data.get("title", ""), "")
        data.update(analyses)

    print(f"[{date}] 종합 결론 생성 중...")
    synthesis = generate_synthesis(analyses, quotes)

    print(f"[{date}] 히스토리 백분위 계산 중...")
    percentiles = build_percentiles(quotes)

    data["synthesis"] = synthesis
    data["percentiles"] = percentiles

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✅ {path.name} 업데이트 완료")
    print(f"   synthesis: {synthesis[:80]}...")
    print(f"   percentiles: {percentiles}")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    target_date = arg.strip() or find_valid_date()
    if not target_date:
        print("❌ 유효한(4개 페르소나 정상 생성) 리포트를 찾지 못함")
        sys.exit(1)
    backfill(target_date)
