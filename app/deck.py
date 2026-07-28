"""題庫載入。

題目內容留在 JSON、只有作答狀態進 SQLite —— 這樣監理所改版時可以直接重跑
tools/ 的 pipeline 覆蓋題庫，不需要 DB migration。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# 危險感知是另一種題型（每題綁一支情境影片），但題目本身的 schema 與筆試完全相同，
# 只多一個 video 欄位 —— 所以對 SRS、佇列、瀏覽、統計而言它就只是另外兩份題庫。
DECKS = ("car", "moto", "hazard_car", "hazard_moto")
DECK_LABELS = {
    "car": "汽車",
    "moto": "機車",
    "hazard_car": "汽車危險感知",
    "hazard_moto": "機車危險感知",
}


@dataclass(frozen=True)
class Deck:
    key: str
    label: str
    title: str
    version: str
    questions: list[dict]
    by_id: dict[str, dict]
    categories: list[dict]


def _load_one(key: str) -> Deck:
    raw = json.loads((DATA_DIR / f"{key}_questions.json").read_text(encoding="utf-8"))
    questions = raw["questions"]
    for q in questions:
        q["deck"] = key

    # 架構 → 分類的有序目錄，供前端篩選；順序照題號，即題庫本身的教學順序
    cats: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for q in questions:
        pair = (q["architecture"], q["category"])
        if pair in seen:
            continue
        seen.add(pair)
        cats.append(
            {
                "architecture": q["architecture"],
                "category": q["category"],
                "count": sum(
                    1
                    for x in questions
                    if x["architecture"] == pair[0] and x["category"] == pair[1]
                ),
            }
        )

    return Deck(
        key=key,
        label=DECK_LABELS[key],
        title=raw["source"]["title"],
        version=raw["source"]["version"],
        questions=questions,
        by_id={q["id"]: q for q in questions},
        categories=cats,
    )


def load_decks() -> dict[str, Deck]:
    return {k: _load_one(k) for k in DECKS}
