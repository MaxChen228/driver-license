"""把危險感知的抽取結果組成題庫 —— data/source/hazard_*.json → data/hazard_*_questions.json。

刻意產出與筆試題庫「完全相同」的 schema，只多一個 video 欄位。這樣 SRS、佇列、
題庫瀏覽、統計、離線快取整條路都不必為新題型改寫 —— 對前端而言它就是第三、
第四份題庫，差別只在 render 時放的是 <video> 而不是 <img>。

兩個欄位沒有來源，說明取捨：

  architecture / category
      危險感知的 PDF 沒有分類，而 hpt.thb.gov.tw 上的分類（未保持安全距離、
      轉彎不當…）並沒有隨題目一起公告。與其猜一個對不上官方的分類，不如誠實
      只給一個分類 —— 篩選器會退化成單選，但不會給出錯的資訊。

  version
      這批沒有官方公告版號（筆試題庫有「115.5.29」這種）。改用題目內容的
      sha256 前 8 碼：內容變它才變，正好符合 /api/config 拿它當 ?v= 快取鍵的用途
      —— 題目沒改就永遠命中，改了就換網址。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "source"
VIDEOS = ROOT / "data" / "videos"
MANIFEST = VIDEOS / "manifest.json"

SPECS = {
    "hazard_moto": {
        "raw": "hazard_moto.json",
        "prefix": "HM",
        "title": "機車危險感知影片題",
        "architecture": "危險感知",
        "category": "機車危險感知影片",
    },
    "hazard_car": {
        "raw": "hazard_car.json",
        "prefix": "HC",
        "title": "汽車危險感知影片題",
        "architecture": "危險感知",
        "category": "汽車危險感知影片",
    },
}


def number_of(no: str) -> int:
    """題號轉序號：機車是 '001'，汽車是 '危感-01'。"""
    m = re.search(r"(\d+)\s*$", no)
    if not m:
        raise ValueError(f"取不出題號序數：{no}")
    return int(m.group(1))


def build(key: str, spec: dict, videos: dict[str, str]) -> dict:
    raw = json.loads((SRC / spec["raw"]).read_text(encoding="utf-8"))
    rows = raw["questions"]

    questions = []
    for r in rows:
        n = number_of(r["no"])
        questions.append(
            {
                "id": f"{spec['prefix']}{n:04d}",
                "number": n,
                "answer": r["answer"],
                "stem": r["stem"],
                "stem_is_image": False,
                "options": [
                    {"n": i, "text": t, "is_image": False}
                    for i, t in enumerate(r["options"], 1)
                ],
                "images": [],
                # video 是轉檔後的檔名（內容雜湊，共用情境的題目會指到同一個），
                # video_no 保留公路局的影片編號 —— 對照官方教材時要得回這個號碼。
                "video": videos.get(r["video"], ""),
                "video_no": r["video"],
                "architecture": spec["architecture"],
                "category": spec["category"],
                "page": None,
            }
        )
    questions.sort(key=lambda q: q["number"])

    digest = hashlib.sha256(
        json.dumps(questions, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:8]

    return {
        "source": {
            "title": spec["title"],
            "version": digest,
            "file": raw["source"],
            "question_count": len(questions),
            "extracted_with": "tools/extract_hazard.py → tools/build_hazard_decks.py",
        },
        "questions": questions,
    }


def audit(key: str, deck: dict) -> bool:
    """全綠才算數。影片檔的存在與否單獨報 —— 缺影片不擋題庫產出，
    但那是「這份題庫還不能上線」的意思。"""
    bad = []
    seen = set()
    for q in deck["questions"]:
        if q["id"] in seen:
            bad.append((q["id"], "題號重複"))
        seen.add(q["id"])
        if not q["stem"]:
            bad.append((q["id"], "無題幹"))
        if len(q["options"]) != 3 or not all(o["text"] for o in q["options"]):
            bad.append((q["id"], "選項不足 3 個"))
        if q["answer"] not in (1, 2, 3):
            bad.append((q["id"], f"答案異常 {q['answer']}"))
        if not q["video"]:
            bad.append((q["id"], f"影片 {q['video_no']} 不在 manifest 裡"))

    n = len(deck["questions"])
    missing = [q["video_no"] for q in deck["questions"]
               if not q["video"] or not (VIDEOS / f"{q['video']}.mp4").exists()]
    noposter = [q["video_no"] for q in deck["questions"]
                if not q["video"] or not (VIDEOS / f"{q['video']}.jpg").exists()]
    uniq = {q["video"] for q in deck["questions"] if q["video"]}

    print(f"{key}: {n} 題 · v{deck['source']['version']} · 問題 {len(bad)}")
    print(f"   影片 {n - len(missing)}/{n}（{len(uniq)} 支不重複）· poster {n - len(noposter)}/{n}")
    for b in bad[:10]:
        print("   ", b)
    if missing[:5]:
        print("    缺影片：", ", ".join(missing[:5]), "…" if len(missing) > 5 else "")
    return not bad


def main() -> int:
    if not MANIFEST.exists():
        print("沒有 data/videos/manifest.json —— 先跑 tools/transcode_hazard_videos.py")
        return 1
    videos = json.loads(MANIFEST.read_text(encoding="utf-8"))["videos"]

    ok = True
    for key, spec in SPECS.items():
        deck = build(key, spec, videos)
        ok &= audit(key, deck)
        out = ROOT / "data" / f"{key}_questions.json"
        out.write_text(
            json.dumps(deck, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"   → {out.relative_to(ROOT)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
