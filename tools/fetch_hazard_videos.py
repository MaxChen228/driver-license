"""下載危險感知影片 —— 從公路局 Synology Drive 的公開分享連結取回原始檔。

每題一支影片，連結在 data/hazard_*.json 的 share_url（由 extract_hazard.py 從
PDF 的超連結取出）。整條路徑不需要瀏覽器：

  1. GET 分享頁 —— 伺服器在 Set-Cookie 放一枚 `drive-sharing-<sharing_link>`，
     那就是後續 API 要的 sharing_token（324 字元的不透明字串）。
  2. SYNO.SynologyDrive.Shard getjs —— 回一份 JS，裡面的 window.getDriveFile()
     是這個檔案的 metadata：file_id、name、size。
  3. SYNO.SynologyDrive.Files method=download version=2，帶 files=["id:<file_id>"]
     與 sharing_token —— 回原始檔本身。少了 sharing_token 會拿到
     {"error":{"code":1002}}「create file selectors failed」，那是唯一的關卡。

完整性檢查用 size 不用 hash：metadata 那個 hash 不是內容的 md5（Synology 自己的
同步用雜湊，實測與 md5 不符），但 size 對得上，而且對得上就是整支檔案沒被截斷。
檔名另外驗一次必須等於該題的影片編號 —— 這是「連結真的對應到這題」的獨立佐證。

走 curl 而不是 urllib：Python 的 ssl 認不得這張政府憑證（Missing Subject Key
Identifier），curl 走 macOS 信任鏈則沒問題。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "source" / "videos"
API = "https://space2.thb.gov.tw/webapi/entry.cgi"

WORKERS = 4      # 對方是政府機關的 NAS，不是 CDN；併發開小一點
RETRIES = 3
TIMEOUT = 300    # 單檔最大 8 MB，但對方上行不保證


def curl(args: list[str], binary: bool = False) -> bytes:
    r = subprocess.run(
        ["curl", "-sS", "--fail", "--max-time", str(TIMEOUT), *args],
        capture_output=True,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace").strip() or f"curl exit {r.returncode}")
    return r.stdout if binary else r.stdout


def share_ids(url: str) -> tuple[str, str]:
    """/d/s/<permanent_link>/<sharing_link>"""
    m = re.search(r"/d/s/([^/]+)/([^/?#]+)", url)
    if not m:
        raise ValueError(f"看不懂的分享連結：{url}")
    return m.group(1), m.group(2)


def metadata(url: str, jar: Path) -> tuple[str, str, int, str]:
    """(file_id, name, size, sharing_token)"""
    perm, link = share_ids(url)
    curl(["-c", str(jar), "-b", str(jar), "-L", "-o", "/dev/null", url])
    token = ""
    for line in jar.read_text(encoding="utf-8", errors="replace").splitlines():
        if f"drive-sharing-{link}" in line:
            token = line.split("\t")[-1].strip()
    if not token:
        raise RuntimeError("分享頁沒有給 sharing token")

    js = curl(
        ["-b", str(jar), "-G", API,
         "--data-urlencode", "api=SYNO.SynologyDrive.Shard",
         "--data-urlencode", "version=1",
         "--data-urlencode", "method=getjs",
         "--data-urlencode", f'permanent_link="{perm}"',
         "--data-urlencode", 'sharing_type="public_sharing"',
         "--data-urlencode", f'sharing_link="{link}"']
    ).decode("utf-8", errors="replace")

    i = js.find("window.getDriveFile")
    if i < 0:
        raise RuntimeError("getjs 沒有 getDriveFile —— 連結可能已失效")
    start = js.index("return ", i) + len("return ")
    info = json.loads(js[start : js.index("};", start) + 1])
    return info["file_id"], info["name"], info["size"], token


def fetch(q: dict) -> tuple[str, str]:
    """回 (影片編號, 狀態)。已經下載好、大小對得上的直接跳過。"""
    vid = q["video"]
    dest = OUT / f"{vid}.mp4"
    jar = OUT / f".{vid}.cookies"
    try:
        file_id, name, size, token = metadata(q["share_url"], jar)

        # 連結與題目的對應：檔名必須就是這題的影片編號。
        # 汽車那批的檔名帶一個空格（「2026 01.mp4」對應影片編號 202601），
        # 只忽略空白、不做其他寬鬆化 —— 真的對不上時仍要擋下來。
        if re.sub(r"\s+", "", Path(name).stem) != vid:
            return vid, f"檔名不符：遠端是 {name}"

        if dest.exists() and dest.stat().st_size == size:
            return vid, "skip"

        for attempt in range(1, RETRIES + 1):
            try:
                tmp = dest.with_suffix(".part")
                curl(
                    ["-b", str(jar), "-G", API, "-o", str(tmp),
                     "--data-urlencode", "api=SYNO.SynologyDrive.Files",
                     "--data-urlencode", "method=download",
                     "--data-urlencode", "version=2",
                     "--data-urlencode", f'files=["id:{file_id}"]',
                     "--data-urlencode", f"sharing_token={token}"]
                )
                got = tmp.stat().st_size
                if got != size:
                    tmp.unlink(missing_ok=True)
                    raise RuntimeError(f"大小不符：{got} != {size}")
                tmp.rename(dest)     # 對得上才就位，中斷不會留下半支能播的檔
                return vid, "ok"
            except Exception as exc:
                if attempt == RETRIES:
                    return vid, f"失敗：{exc}"
        return vid, "失敗：未知"
    finally:
        jar.unlink(missing_ok=True)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    questions = []
    for tag in ("moto", "car"):
        data = json.loads((OUT.parent / f"hazard_{tag}.json").read_text(encoding="utf-8"))
        questions.extend(data["questions"])
    print(f"{len(questions)} 支影片 → {OUT.relative_to(ROOT)}")

    results: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for n, (vid, status) in enumerate(pool.map(fetch, questions), 1):
            results[vid] = status
            if status not in ("ok", "skip") or n % 20 == 0 or n == len(questions):
                print(f"  [{n}/{len(questions)}] {vid} {status}", flush=True)

    bad = {v: s for v, s in results.items() if s not in ("ok", "skip")}
    got = sum(1 for s in results.values() if s == "ok")
    total_bytes = sum(f.stat().st_size for f in OUT.glob("*.mp4"))
    print(f"\n新下載 {got} · 已存在 {sum(1 for s in results.values() if s == 'skip')} · 失敗 {len(bad)}")
    print(f"目前 {len(list(OUT.glob('*.mp4')))} 支、{total_bytes / 1e9:.2f} GB")
    for v, s in bad.items():
        print("   ", v, s)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
