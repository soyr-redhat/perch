"""Build OS icons matching the bird in static/icon.svg."""

from pathlib import Path
from PIL import Image, ImageDraw


def make_icons(destination):
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGBA", (1024, 1024))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, 1023, 1023), radius=256, fill="#426d57")
    def points(*coordinates):
        return tuple(round(value * 16) for value in coordinates)

    for segment in ((30, 40, 30, 47), (37, 40, 37, 46), (14, 49, 52, 45), (47, 45.5, 50, 40)):
        draw.line(points(*segment), fill="#b1cebd", width=40)
        for x, y in (segment[:2], segment[2:]):
            draw.ellipse(points(x - 1.25, y - 1.25, x + 1.25, y + 1.25), fill="#b1cebd")
    draw.polygon(points(25, 33, 14, 28, 20, 39, 30, 40), fill="#edf3ee")
    draw.ellipse(points(20, 21, 46, 43), fill="#edf3ee")
    draw.ellipse(points(30, 13, 48, 31), fill="#edf3ee")
    draw.polygon(points(47, 21, 54, 25, 47, 27), fill="#d8ba7b")
    draw.ellipse(points(23, 28, 37, 38), fill="#b1cebd")
    draw.ellipse(points(39.2, 19.2, 42.8, 22.8), fill="#263b31")
    image.save(root / "perch.png")
    image.save(root / "perch.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    image.save(root / "perch.icns")
    return root
