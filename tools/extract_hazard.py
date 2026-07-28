"""危險感知影片選擇題抽取 —— 從公路局公告的 PDF 產生題庫 JSON。

跟筆試題庫（extract_car.py / extract_moto.py）是不同的題型：
每題綁一支情境影片，看完影片才作答，所以題目本身多一個 video 欄位。

兩份 PDF 的版面不一樣，各走各的路徑：

  moto —— 每列的「題號 答案 …題幹… 影片編號」是同一行起頭，續行接在後面，
          用行首 regex 切塊就夠。extract_table() 反而會把換行的儲存格截掉。
  car  —— 題號／答案／影片編號那一欄是垂直置中的，文字流會把它插進題幹中間
          （extract_text 會讀成「…你應該？ (1)先 / 危感-01 (1) 減速，…202601」）。
          這份必須走 find_tables() 依欄位取，不能用行序。

影片連結：PDF 的「影片編號」是超連結，指向公路局 Synology Drive 的單檔分享頁。
依文件順序取唯一 URI，與題目一一對應（機車 126 題對 126 條，已逐筆核對檔名）。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "source"

FULLWIDTH = str.maketrans("１２３", "123")
OPT = re.compile(r"[（(]([123１２３])[）)]")
MOTO_START = re.compile(r"^\d{3}\s+[123１２３]\s")
MOTO_ROW = re.compile(r"^(?P<no>\d{3})\s+(?P<ans>[123１２３])\s*(?P<rest>.*?(?P<vid>\d{4}).*)$")
CAR_NO = re.compile(r"^危感-\d{2}$")


def _split_options(body: str) -> tuple[str, list[str]]:
    """把「題幹(1)…(2)…(3)…」拆成題幹與三個選項。"""
    parts = OPT.split(body)
    opts: dict[int, str] = {}
    for i in range(1, len(parts) - 1, 2):
        opts[int(parts[i].translate(FULLWIDTH))] = parts[i + 1].strip(" 。、").strip()
    return parts[0].strip(), [opts.get(i, "") for i in (1, 2, 3)]


def _share_links(pdf) -> list[str]:
    """依文件順序取出唯一的影片分享連結。"""
    seen: set[str] = set()
    ordered: list[str] = []
    for page in pdf.pages:
        annots = sorted(page.annots or [], key=lambda a: ((a["top"] + a["bottom"]) / 2, a["x0"]))
        for a in annots:
            uri = ((a.get("data") or {}).get("A") or {}).get("URI")
            if isinstance(uri, bytes):
                uri = uri.decode()
            if uri and "/d/s/" in uri and uri not in seen:
                seen.add(uri)
                ordered.append(uri)
    return ordered


def extract_moto(path: Path) -> list[dict]:
    with pdfplumber.open(path) as pdf:
        links = _share_links(pdf)
        text = "\n".join(p.extract_text() or "" for p in pdf.pages)

    blocks: list[list[str]] = []
    for line in text.split("\n"):
        if MOTO_START.match(line):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)

    rows = []
    for block in blocks:
        joined = re.sub(r"\s*\d+\s*$", "", "".join(x.strip() for x in block))  # 去頁碼
        m = MOTO_ROW.match(joined)
        if not m:
            continue
        stem, options = _split_options(m.group("rest").replace(m.group("vid"), "", 1))
        rows.append(
            {
                "no": m.group("no"),
                "answer": int(m.group("ans").translate(FULLWIDTH)),
                "video": m.group("vid"),
                "stem": stem,
                "options": options,
            }
        )
    return _attach(rows, links)


def extract_car(path: Path) -> list[dict]:
    with pdfplumber.open(path) as pdf:
        page = pdf.pages[0]
        links = _share_links(pdf)
        table = page.find_tables()[0].extract()

    rows = []
    for raw in table:
        cells = [(c or "").replace("\n", "") for c in raw]
        if len(cells) < 5 or not CAR_NO.match(cells[0].strip()):
            continue
        stem, options = _split_options(cells[3])
        ans = re.search(r"[123]", cells[2])
        rows.append(
            {
                "no": cells[0].strip(),
                "answer": int(ans.group()) if ans else None,
                "video": cells[4].strip(),
                "stem": stem,
                "options": options,
            }
        )
    return _attach(rows, links)


def _attach(rows: list[dict], links: list[str]) -> list[dict]:
    if len(links) != len(rows):
        raise SystemExit(f"連結數 {len(links)} 與題數 {len(rows)} 不符 —— 對應關係不可信，停手")
    for r, url in zip(rows, links):
        r["share_url"] = url
    return rows


def audit(tag: str, rows: list[dict]) -> bool:
    """全綠才算數 —— 比照 tools/verify.py 的精神。"""
    bad = []
    for r in rows:
        if not r["stem"]:
            bad.append((r["no"], "無題幹"))
        if not all(r["options"]):
            bad.append((r["no"], "選項不足 3 個"))
        if r["answer"] not in (1, 2, 3):
            bad.append((r["no"], f"答案異常 {r['answer']}"))
        if not r["share_url"]:
            bad.append((r["no"], "無影片連結"))
    nos = [r["no"] for r in rows]
    vids = [r["video"] for r in rows]
    if len(set(nos)) != len(nos):
        bad.append(("-", "題號重複"))
    if len(set(vids)) != len(vids):
        bad.append(("-", "影片編號重複"))
    dist = {a: sum(1 for r in rows if r["answer"] == a) for a in (1, 2, 3)}
    print(f"{tag}: {len(rows)} 題 · 答案分布 {dist} · 問題 {len(bad)}")
    for b in bad[:10]:
        print("   ", b)
    return not bad


def main() -> int:
    specs = [
        ("moto", SRC / "(中文)機車危險感知影片選擇題.pdf", extract_moto),
        ("car", SRC / "1150605-危險感知影片題(調整影片順序).pdf", extract_car),
    ]
    ok = True
    for tag, path, fn in specs:
        rows = fn(path)
        ok &= audit(tag, rows)
        # 中間產物，跟它的來源 PDF 放一起。前端吃的是 tools/build_hazard_decks.py
        # 產生的 data/hazard_{tag}_questions.json。
        out = SRC / f"hazard_{tag}.json"
        out.write_text(
            json.dumps(
                {"deck": tag, "kind": "hazard", "source": path.name,
                 "count": len(rows), "questions": rows},
                ensure_ascii=False, indent=1,
            ),
            encoding="utf-8",
        )
        print(f"   → {out.relative_to(ROOT)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
