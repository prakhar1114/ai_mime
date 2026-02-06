"""Write AppIcon.icns directly from docs/logo source PNGs.

ICNS format (PNG-based icon types, macOS 10.7+):
  Header:  b'icns' + uint32 BE total-file-size
  Per icon: type-code (4 chars) + uint32 BE png-blob-size + png-blob

Type codes used here:
  ic04  16×16   (legacy; PNG not guaranteed — we write it as PNG anyway,
                 which modern macOS handles fine inside a .app bundle)
  ic05  32×32
  ic07  128×128
  ic08  256×256
  ic09  512×512
  ic10  1024×1024  (renders as 512@2x on Retina)
"""

from __future__ import annotations

import io
import struct
import sys
from pathlib import Path

from PIL import Image

# (type_code, target_pixel_size)  — ordered small→large as convention.
_ICONS: list[tuple[str, int]] = [
    ("ic04", 16),
    ("ic05", 32),
    ("ic07", 128),
    ("ic08", 256),
    ("ic09", 512),
    ("ic10", 1024),
]

# Best source for each target: pick the smallest source that is >= target.
# Available: 32, 60, 128, 256, 1000
_SOURCE_MAP: dict[int, str] = {
    16: "icon32.png",
    32: "icon32.png",
    128: "icon128.png",
    256: "icon256.png",
    512: "icon1000.png",
    1024: "icon1000.png",
}


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def main(repo_root: Path) -> None:
    logo_dir = repo_root / "docs" / "logo"
    out_path = repo_root / "AppIcon.icns"

    # Pre-encode each icon size as PNG bytes.
    icons: list[tuple[str, bytes]] = []
    for type_code, px in _ICONS:
        src_name = _SOURCE_MAP[px]
        src = logo_dir / src_name
        if not src.exists():
            raise FileNotFoundError(f"Icon source not found: {src}")
        img = Image.open(src).convert("RGBA").resize((px, px), Image.LANCZOS)
        icons.append((type_code, _png_bytes(img)))

    # Calculate total file size: 8-byte header + (8-byte chunk header + data) per icon.
    total = 8 + sum(8 + len(data) for _, data in icons)

    with open(out_path, "wb") as f:
        f.write(b"icns")
        f.write(struct.pack(">I", total))
        for type_code, data in icons:
            f.write(type_code.encode("ascii"))
            f.write(struct.pack(">I", len(data) + 8))  # ICNS size field includes the 8-byte chunk header
            f.write(data)

    print(f"Created {out_path}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <repo_root>", file=sys.stderr)
        sys.exit(1)
    main(Path(sys.argv[1]))
