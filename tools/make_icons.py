"""產生 PWA 圖示（方向盤，純幾何、不依賴任何字型）。

    uv run --with pillow python tools/make_icons.py

刻意不畫文字：CJK 字型在不同機器上路徑不同，用幾何圖形才能保證重跑結果一致。
內容收在中央 76% 的安全區內，因為 Android 的 maskable icon 會依裝置裁成圓形／
方形／水滴形，貼邊的東西會被切掉。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "static"
BLUE = (47, 109, 246, 255)
WHITE = (255, 255, 255, 255)
SS = 4  # 超取樣倍率：PIL 沒有抗鋸齒，先畫大再縮


def wheel(size: int, radius_ratio: float, bleed: bool) -> Image.Image:
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    if bleed:
        d.rectangle([0, 0, s, s], fill=BLUE)
    else:
        d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * radius_ratio), fill=BLUE)

    c = s / 2
    r = s * 0.30          # 外圈半徑，落在 maskable 安全區內
    ring = s * 0.052      # 圈線粗細
    d.ellipse([c - r, c - r, c + r, c + r], outline=WHITE, width=int(ring))

    hub = s * 0.083
    d.ellipse([c - hub, c - hub, c + hub, c + hub], fill=WHITE)

    # 三根輻條：左上、右上、正下 —— 也剛好呼應三選一
    spoke = s * 0.036
    inner, outer = hub * 0.6, r - ring / 2
    for dx, dy in ((-0.7071, -0.7071), (0.7071, -0.7071), (0.0, 1.0)):
        d.line(
            [c + dx * inner, c + dy * inner, c + dx * outer, c + dy * outer],
            fill=WHITE, width=int(spoke),
        )

    return img.resize((size, size), Image.LANCZOS)


for name, size, bleed in (
    ("icon-192.png", 192, False),
    ("icon-512.png", 512, False),
    ("apple-touch-icon.png", 180, True),  # iOS 自己會裁圓角，且不接受透明背景
):
    wheel(size, 0.22, bleed).save(OUT / name)
    print(f"{name}  {size}x{size}")
