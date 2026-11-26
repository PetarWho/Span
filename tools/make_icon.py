"""Draws the Span icon: assets/icon.png, icon.ico (Windows), icon.icns (macOS) and the
Android launcher icons in mobile/android.

The icon shows what the app does: a file being dragged across the gap between
two panes - cobalt for this computer, jade for the other - on the graphite of
the window's bridge. Shapes are kept bold so it still reads at 16x16.

    python tools/make_icon.py
"""
import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath

OUT = Path(__file__).resolve().parent.parent / "assets"
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)

GRAPHITE_TOP, GRAPHITE_BOTTOM = QColor("#2F353E"), QColor("#21252C")
COBALT, JADE = QColor("#6E95F0"), QColor("#3FC4A3")


def draw(size, mac=False, tile=True, content_scale=1.0):
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size / 256.0
    p.scale(s, s)  # draw on a 256 grid
    if mac:
        # Apple's icon grid: the tile is 824/1024 of the canvas, centred, so
        # Span sits at the same visual size as other apps in the Dock
        k = (824 / 1024) / (240 / 256)
        p.translate(128, 128)
        p.scale(k, k)
        p.translate(-128, -128)

    p.setPen(Qt.PenStyle.NoPen)
    if tile:
        margin = 8 if size >= 32 else 2  # small sizes use the full canvas
        rect = QRectF(margin, margin, 256 - 2 * margin, 256 - 2 * margin)
        grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        grad.setColorAt(0, GRAPHITE_TOP)
        grad.setColorAt(1, GRAPHITE_BOTTOM)
        p.setBrush(grad)
        p.drawRoundedRect(rect, 56, 56)
    if content_scale != 1.0:
        p.translate(128, 128)
        p.scale(content_scale, content_scale)
        p.translate(-128, -128)

    small = size < 32
    # the two panes: this computer (cobalt) and the other one (jade)
    p.setBrush(COBALT)
    p.drawRoundedRect(QRectF(34, 50, 80, 156), 20, 20)
    p.setBrush(JADE)
    p.drawRoundedRect(QRectF(142, 50, 80, 156), 20, 20)

    # a file mid-drag across the gap, tilted as if held by the cursor
    p.save()
    p.translate(128, 132)
    if not small:
        p.rotate(-9)
    w, h, fold = (96, 116, 30) if small else (84, 104, 26)
    body = QPainterPath()
    body.moveTo(-w / 2 + 10, -h / 2)
    body.lineTo(w / 2 - fold, -h / 2)
    body.lineTo(w / 2, -h / 2 + fold)
    body.lineTo(w / 2, h / 2 - 10)
    body.quadTo(w / 2, h / 2, w / 2 - 10, h / 2)
    body.lineTo(-w / 2 + 10, h / 2)
    body.quadTo(-w / 2, h / 2, -w / 2, h / 2 - 10)
    body.lineTo(-w / 2, -h / 2 + 10)
    body.quadTo(-w / 2, -h / 2, -w / 2 + 10, -h / 2)
    if not small:  # soft shadow so it lifts off the panes
        p.save()
        p.translate(5, 8)
        p.fillPath(body, QColor(0, 0, 0, 70))
        p.restore()
    p.fillPath(body, QColor("#F7F9FB"))
    corner = QPainterPath()
    corner.moveTo(w / 2 - fold, -h / 2)
    corner.lineTo(w / 2 - fold, -h / 2 + fold - 6)
    corner.quadTo(w / 2 - fold, -h / 2 + fold, w / 2 - fold + 6, -h / 2 + fold)
    corner.lineTo(w / 2, -h / 2 + fold)
    corner.closeSubpath()
    p.fillPath(corner, QColor("#C9D2DC"))
    if not small:  # two text lines hint "document" at larger sizes
        p.setBrush(QColor("#C9D2DC"))
        p.drawRoundedRect(QRectF(-w / 2 + 16, -4, w - 32, 9), 4.5, 4.5)
        p.drawRoundedRect(QRectF(-w / 2 + 16, 18, w - 46, 9), 4.5, 4.5)
    p.restore()
    p.end()
    return img


def png_bytes(img):
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(ba)


def write_ico(path, images):
    """Multi-size .ico with PNG-compressed entries (supported since Vista)."""
    entries = [(img.width(), png_bytes(img)) for img in images]
    header = struct.pack("<HHH", 0, 1, len(entries))
    offset = 6 + 16 * len(entries)
    table, data = b"", b""
    for size, png in entries:
        dim = 0 if size >= 256 else size
        table += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset + len(data))
        data += png
    path.write_bytes(header + table + data)


# .icns entries: (type code, pixel size). PNG data is valid for all of these
# on macOS 10.7+; the 2x types are the Retina versions of the smaller sizes.
ICNS_TYPES = (("icp4", 16), ("icp5", 32), ("ic11", 32), ("icp6", 64), ("ic12", 64), ("ic07", 128),
              ("ic13", 256), ("ic08", 256), ("ic14", 512), ("ic09", 512), ("ic10", 1024))


def write_icns(path):
    """macOS .icns: 'icns' + total length, then (type, length, PNG) chunks."""
    cache, chunks = {}, b""
    for code, size in ICNS_TYPES:
        if size not in cache:
            cache[size] = png_bytes(draw(size, mac=True))
        chunks += code.encode("ascii") + struct.pack(">I", 8 + len(cache[size])) + cache[size]
    path.write_bytes(b"icns" + struct.pack(">I", 8 + len(chunks)) + chunks)


ANDROID_RES = OUT.parent / "mobile" / "android" / "app" / "src" / "main" / "res"
ANDROID_DENSITIES = {"mdpi": 1, "hdpi": 1.5, "xhdpi": 2, "xxhdpi": 3, "xxxhdpi": 4}


def write_android():
    """Adaptive launcher icon (Android 8+) plus classic PNGs for older phones.

    The adaptive foreground is 108dp with only the central ~66dp guaranteed
    visible once the phone masks it into a circle or squircle, so the artwork
    is shrunk into that safe zone over a plain graphite background.
    """
    if not ANDROID_RES.exists():
        return
    for name, k in ANDROID_DENSITIES.items():
        d = ANDROID_RES / f"mipmap-{name}"
        d.mkdir(exist_ok=True)
        draw(round(48 * k)).save(str(d / "ic_launcher.png"))
        draw(round(108 * k), tile=False, content_scale=0.56).save(str(d / "ic_launcher_foreground.png"))
    anydpi = ANDROID_RES / "mipmap-anydpi-v26"
    anydpi.mkdir(exist_ok=True)
    (anydpi / "ic_launcher.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">\n'
        '    <background android:drawable="@color/ic_launcher_background"/>\n'
        '    <foreground android:drawable="@mipmap/ic_launcher_foreground"/>\n'
        '</adaptive-icon>\n', encoding="utf-8")
    (ANDROID_RES / "values" / "ic_launcher_background.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'
        '    <color name="ic_launcher_background">#282D35</color>\n</resources>\n', encoding="utf-8")


def main():
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)  # noqa: F841 - needed for painting
    OUT.mkdir(exist_ok=True)
    images = [draw(s) for s in SIZES]
    images[-1].save(str(OUT / "icon.png"))
    draw(512).save(str(OUT / "icon-512.png"))
    write_ico(OUT / "icon.ico", images)
    write_icns(OUT / "icon.icns")
    write_android()
    # preview strip of the small sizes, for checking legibility
    strip = QImage(sum(SIZES[:6]) + 10 * 7, 60, QImage.Format.Format_ARGB32)
    strip.fill(QColor("#F2F4F6"))
    p = QPainter(strip)
    x = 10
    for img in images[:6]:
        p.drawImage(x, 30 - img.height() // 2, img)
        x += img.width() + 10
    p.end()
    strip.save(str(Path(__file__).with_name("icon-preview-small.png")))
    print("wrote", ", ".join(f.name for f in sorted(OUT.iterdir())))


if __name__ == "__main__":
    main()
