"""Build OS icon containers from Perch's geometric mark."""

from pathlib import Path
from PIL import Image, ImageDraw


def make_icons(destination):
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGBA", (1024, 1024))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, 1023, 1023), radius=256, fill="#4361be")
    draw.rectangle((304, 304, 432, 720), fill="white")
    draw.rounded_rectangle((304, 304, 768, 688), radius=192, fill="white")
    draw.rectangle((304, 304, 528, 688), fill="white")
    draw.rounded_rectangle((432, 432, 624, 560), radius=64, fill="#4361be")
    draw.rectangle((432, 560, 768, 720), fill="#4361be")
    draw.rounded_rectangle((272, 760, 768, 808), radius=24, fill="#a8c4ff")
    image.save(root / "perch.png")
    image.save(root / "perch.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    image.save(root / "perch.icns")
    return root
