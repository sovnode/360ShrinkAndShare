"""Draws icon.ico (a 360 panorama band with a turn arrow) and the Start shortcut. Run once after install:
    python make_icon.py
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths  # noqa: E402


def draw(size=256):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 256.0
    d.rounded_rectangle([8 * s, 8 * s, 248 * s, 248 * s], radius=44 * s, fill=(31, 111, 235, 255))
    d.ellipse([34 * s, 70 * s, 222 * s, 186 * s], outline=(255, 255, 255, 255), width=int(12 * s))
    d.polygon([(186 * s, 64 * s), (226 * s, 84 * s), (192 * s, 108 * s)], fill=(255, 255, 255, 255))
    try:
        font = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", int(70 * s))
    except OSError:
        font = ImageFont.load_default()
    d.text((128 * s, 128 * s), "360", fill=(255, 255, 255, 255), font=font, anchor="mm")
    return img


def make_shortcut():
    lnk = os.path.join(HERE, "%s.lnk" % paths.APP_NAME)
    import win32com.client  # pywin32
    sh = win32com.client.Dispatch("WScript.Shell")
    sc = sh.CreateShortcut(lnk)
    sc.TargetPath = paths.PYTHONW
    sc.Arguments = '"%s"' % os.path.join(HERE, "app.pyw")
    sc.WorkingDirectory = HERE
    sc.IconLocation = paths.ICON
    sc.Description = "Shrink 360 videos into 360 slideshows or panoramas"
    sc.Save()
    return lnk


if __name__ == "__main__":
    draw().save(paths.ICON, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("icon:", paths.ICON)
    print("shortcut:", make_shortcut())
