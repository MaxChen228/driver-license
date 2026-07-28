"""把原始危險感知影片轉成對外播放版 —— data/source/videos → data/videos。

原始檔的實測樣貌（不是我原本以為的樣子，記下來免得下次又猜錯）：
  長度 6–13 秒（不是 30 秒）、多數 1280x720@59.94，少數 1920x1080@59.94 與 30fps，
  視訊 2–9.3 Mbps，音軌是純數位靜音（volumedetect 全部 -91.0 dB 定值）。
  136 個檔案裡只有 119 支不同的影片 —— 有 17 題與另一題共用同一段情境。

轉檔決策，逐項理由：
  1080p → 720p   多數素材本來就是 720p，統一到 720p 是這批裡最大的一筆節省。
  維持原 fps      60 → 30 幾乎不省（CRF 之下少了張數、每張就變貴，實測 6.31 → 6.01 MB），
                 而危險感知考的是「什麼時候看得出危險」，動態流暢度不該為了 5% 讓步。
  CRF 26         畫質與體積的折衷。再往上（28）約再省 27%，但這是要看清遠處車輛
                 與行人動作的素材，壓過頭會糊掉判斷依據。
  -an            音軌是靜音的，留著只是多一條沒有內容的串流。
  +faststart     moov 搬到檔頭，瀏覽器不必等整支下載完才能開始播。

去重與檔名：輸出以「原始檔 md5 ＋ 轉檔參數」的雜湊命名，不是以影片編號命名。
  - 共用同一段影片的題目指到同一個檔案：省 12% 的儲存與流量，而且使用者做完
    第 33 題再做第 34 題時，瀏覽器直接命中快取而不是再抓一次 6 MB。
  - 參數進雜湊是必要的：只用原始檔 md5 的話，日後調 CRF 檔名不變，
    而這些檔案是要掛 immutable 快取的 —— 邊緣與瀏覽器會永遠餵舊的那一版。

順便產一張 poster（第一幀）：沒有它，未播放的 <video> 在 iOS 上是一塊空白。
第一幀是情境開始前的畫面，不會洩漏答案。

產出 data/videos/manifest.json 給 tools/build_hazard_decks.py 用：
影片編號 → 檔名。題庫不自己算雜湊，避免兩邊規則各自演化。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "source" / "videos"
OUT = ROOT / "data" / "videos"
MANIFEST = OUT / "manifest.json"

WORKERS = 3       # felix 同時在跑 Xcode build 與常駐服務，別把核心吃光
CRF = "26"
MAX_WIDTH = 1280
# 進檔名雜湊。改了任何一項就要改這個字串，否則舊檔名會配上新內容。
PROFILE = f"h264-crf{CRF}-w{MAX_WIDTH}-noaudio-v1"


def run(args: list[str]) -> None:
    r = subprocess.run(args, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace").strip()[-400:])


def digest(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def key_for(src_md5: str) -> str:
    return hashlib.sha256(f"{src_md5}|{PROFILE}".encode()).hexdigest()[:12]


def convert(job: tuple[Path, str]) -> tuple[str, str, int, int]:
    """回 (檔名, 狀態, 原始 bytes, 產出 bytes)"""
    src, key = job
    dst = OUT / f"{key}.mp4"
    poster = OUT / f"{key}.jpg"
    before = src.stat().st_size

    if dst.exists() and poster.exists():
        return key, "skip", before, dst.stat().st_size

    try:
        tmp = dst.with_suffix(".part.mp4")
        run([
            "ffmpeg", "-y", "-v", "error", "-i", str(src),
            # 只縮不放：本來就 ≤720p 的維持原樣，-2 讓高度自動配合且保持偶數
            "-vf", f"scale='min({MAX_WIDTH},iw)':-2",
            "-c:v", "libx264", "-preset", "slow", "-crf", CRF,
            "-pix_fmt", "yuv420p",          # 少了它某些來源會產出 Safari 播不了的 422
            "-an",
            "-movflags", "+faststart",
            str(tmp),
        ])
        tmp.rename(dst)

        ptmp = poster.with_suffix(".part.jpg")
        run(["ffmpeg", "-y", "-v", "error", "-i", str(dst), "-frames:v", "1",
             "-q:v", "4", str(ptmp)])
        ptmp.rename(poster)
        return key, "ok", before, dst.stat().st_size
    except Exception as exc:
        return key, f"失敗：{exc}", before, 0


def main() -> int:
    if not SRC.exists() or not any(SRC.glob("*.mp4")):
        print("沒有原始檔 —— 先跑 tools/fetch_hazard_videos.py")
        return 1
    OUT.mkdir(parents=True, exist_ok=True)

    # 影片編號 → 輸出檔名；同時挑出每個唯一內容的代表檔去轉
    sources = sorted(SRC.glob("*.mp4"))
    mapping: dict[str, str] = {}
    jobs: dict[str, Path] = {}
    for src in sources:
        key = key_for(digest(src))
        mapping[src.stem] = key
        jobs.setdefault(key, src)

    print(f"{len(sources)} 個檔案 · {len(jobs)} 支不同的影片 → {OUT.relative_to(ROOT)}")
    print(f"（{PROFILE}）")

    # 先寫 manifest 再開始轉檔：對應關係只由「原始檔 md5 ＋ 參數」決定，
    # 與轉檔成功與否無關。放在最後寫的話，中途失敗就連題庫都建不起來。
    MANIFEST.write_text(
        json.dumps({"profile": PROFILE, "videos": mapping}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )

    done = raw = enc = 0
    bad = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for key, status, before, after in pool.map(
            convert, [(src, key) for key, src in jobs.items()]
        ):
            done += 1
            if status.startswith("失敗"):
                bad.append((key, status))
            else:
                raw += before
                enc += after
            if done % 20 == 0 or done == len(jobs):
                print(f"  [{done}/{len(jobs)}]", flush=True)

    posters = sum(f.stat().st_size for f in OUT.glob("*.jpg"))
    every = sum(f.stat().st_size for f in SRC.glob("*.mp4"))
    print(f"\n唯一影片 原始 {raw / 1e6:.0f} MB → 播放版 {enc / 1e6:.0f} MB（{enc / raw * 100:.0f}%）")
    print(f"poster {posters / 1e6:.0f} MB · 對外合計 {(enc + posters) / 1e6:.0f} MB")
    print(f"（含重複的原始檔全量是 {every / 1e6:.0f} MB）")
    print(f"   → {MANIFEST.relative_to(ROOT)}")
    for k, s in bad:
        print("   ", k, s)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
