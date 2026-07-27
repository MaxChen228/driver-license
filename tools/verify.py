"""題庫稽核 —— 用來證明 data/*.json 沒有從 PDF 掉字、也沒有重複計算。

跑法：uv run python tools/verify.py

這支是整條抽取 pipeline 的守門員。抽取本身很容易「看起來對」：題數對、編號連續、
每題都有三個選項，但實際上少了半句話或多算了一個字。所以這裡做的是**字元級對帳**：

  EXTRA  = 抽出來比 PDF 多的字元 → 必須是 0（多了就是重複計算，例如表格 cell
           邊界上的字被兩格各認領一次，症狀是「再行駛。。」這種雙句號）
  MISSING= PDF 有但沒進題目文字的字元 → 必須能逐項解釋完（題號數字、答案欄、
           表頭列、分類標題、頁碼），不能有來路不明的中文字

另外對汽車做一個外部驗證：PDF 自己印了「架構一…合計 424 題」「架構二…230 題」
「架構三…436 題」，我們算出來的分類題數必須完全吻合 —— 這是不依賴我們自己
程式碼的獨立佐證。
"""

from __future__ import annotations

import collections
import json
import re
import sys
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SOURCE = DATA / "source"

DOCS = {
    "car": dict(pdf="汽車筆試題庫_公告115.06.9.pdf", count=1090, top=52, bot=790),
    "moto": dict(pdf="機車駕照筆試題庫(全部804題)-1150218.pdf", count=806, top=52, bot=790),
}
PRINTED_TOTALS_CAR = [230, 424, 436]  # PDF 自印的三架構合計

failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


def audit(tag: str, spec: dict) -> None:
    print(f"\n=== {tag} ===")
    doc = json.loads((DATA / f"{tag}_questions.json").read_text(encoding="utf-8"))
    qs = doc["questions"]

    nums = [q["number"] for q in qs]
    check(len(qs) == spec["count"], "題數", f"{len(qs)} / 預期 {spec['count']}")
    check(nums == list(range(1, len(qs) + 1)), "編號連續無缺無重號")
    check(all(q["answer"] in (1, 2, 3) for q in qs), "答案全在 {1,2,3}")
    check(all(q["stem"] or q["images"] for q in qs), "每題都有題幹或圖")
    check(
        all(len(q["options"]) == 3 for q in qs)
        and all(o["text"] or o["is_image"] for q in qs for o in q["options"]),
        "每題三個選項且非空（選項是圖的有 is_image 標記）",
    )

    missing_files = [
        im["file"] for q in qs for im in q["images"] if not (DATA / im["file"]).exists()
    ]
    check(not missing_files, "圖檔都在", f"缺 {len(missing_files)} 張")

    # ---- 字元級對帳 ----
    pdf_path = SOURCE / spec["pdf"]
    if not pdf_path.exists():
        check(False, "來源 PDF 存在", str(pdf_path))
        return

    got = collections.Counter(re.sub(r"\s", "", "".join(q["stem"] + "".join(
        o["text"] for o in q["options"]) for q in qs)))
    src = collections.Counter()
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for w in page.extract_words():
                if spec["top"] < (w["top"] + w["bottom"]) / 2 < spec["bot"]:
                    src.update(re.sub(r"\s", "", w["text"]))

    extra = got - src
    check(sum(extra.values()) == 0, "無重複字元（EXTRA==0）",
          f"多出 {sum(extra.values())} 字：{extra.most_common(5)}")

    # MISSING 逐項解釋。沒進「題目文字」不代表掉了 —— 章節標題進了 architecture /
    # category 欄位，只是不在 stem/options 裡，所以要把這些字一併算成已解釋。
    explained = set("()（）第頁題號答案目內容圖示分類架構一二三▎、—━合計～「」，：【】")
    explained |= set(doc["source"]["title"]) | set("題庫索引")
    for q in qs:
        explained |= set(q["architecture"] or "") | set(q["category"] or "")

    missing = src - got
    unexplained = {c: n for c, n in missing.items() if not (c.isdigit() or c in explained)}
    check(not unexplained, "MISSING 可完全解釋（題號/答案/表頭/章節標題/頁碼）",
          f"無法解釋：{sorted(unexplained.items(), key=lambda x: -x[1])[:8]}")

    if tag == "car":
        per_arch: collections.Counter[str] = collections.Counter()
        for q in qs:
            per_arch[q["architecture"]] += 1
        check(sorted(per_arch.values()) == PRINTED_TOTALS_CAR,
              "分類題數吻合 PDF 自印合計 424/230/436",
              str(sorted(per_arch.values())))


def main() -> int:
    for tag, spec in DOCS.items():
        audit(tag, spec)
    print()
    if failures:
        print(f"FAILED: {len(failures)} 項 —— {failures}")
        return 1
    print("題庫稽核全綠。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
