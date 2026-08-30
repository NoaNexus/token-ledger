from __future__ import annotations

from pathlib import Path

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCE = PROJECT_ROOT / "assets" / "token-ledger-icon.png"
TARGET = PROJECT_ROOT / "assets" / "token-ledger.ico"
CLEAN_SOURCE = PROJECT_ROOT / "assets" / "token-ledger-icon.clean.png"
SIZES = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)]


def main() -> None:
    with Image.open(SOURCE) as source:
        rgba = source.convert("RGBA")
        if rgba.width != rgba.height:
            edge = max(rgba.size)
            square = Image.new("RGBA", (edge, edge), (0, 0, 0, 0))
            square.alpha_composite(rgba, ((edge - rgba.width) // 2, (edge - rgba.height) // 2))
            rgba = square
        # Re-encode without generator-specific PNG chunks or embedded metadata.
        rgba.save(CLEAN_SOURCE, format="PNG", optimize=True)
        CLEAN_SOURCE.replace(SOURCE)
        rgba.save(TARGET, format="ICO", sizes=SIZES, bitmap_format="png")
    print(f"Created {TARGET} ({TARGET.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
