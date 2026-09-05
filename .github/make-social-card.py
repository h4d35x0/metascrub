"""
Social preview card for github.com/h4d35x0/metascrub.

1280x640, the size GitHub renders in link cards.

Follows the h4d35x0 visual identity spec rather than inventing one:
Chakra Petch Bold for display (uppercase, tight negative tracking), JetBrains
Mono for data, near-black ground, and colour used semantically. Per the spec,
green means verified, red means failed. So the card does not decorate with
those colours, it uses them on the actual result: the standard tool's line is
red because it leaves the author in the file, ours is green because it does not.
"""
from PIL import Image, ImageDraw, ImageFont

W, H = 1280, 640
import os
# Chakra Petch and JetBrains Mono, both SIL OFL 1.1. Point FONT_DIR at a
# directory holding ChakraPetch-Bold.ttf, ChakraPetch-SemiBold.ttf and
# JetBrainsMono.ttf.
FD = os.environ.get("FONT_DIR", "fonts")

BG      = "#04070a"
PANEL   = "#0c141b"
LINE    = "#1b2c38"
TEXT    = "#eaf4f8"
DIM     = "#8aa3b0"
FAINT   = "#4d626e"
SIGNAL  = "#00f5a0"
ICE     = "#43c9ff"
ALERT   = "#ff4d5e"

def f(name, size):
    return ImageFont.truetype(f"{FD}/{name}", size)

disp   = lambda s: f("ChakraPetch-Bold.ttf", s)
displt = lambda s: f("ChakraPetch-SemiBold.ttf", s)
mono   = lambda s: f("JetBrainsMono.ttf", s)

img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)

# Faint structural grid: instrument-panel texture, never noticeable as decoration.
for x in range(0, W, 40):
    d.line([(x, 0), (x, H)], fill="#070d13", width=1)
for y in range(0, H, 40):
    d.line([(0, y), (W, y)], fill="#070d13", width=1)

# Accent bar, the wiping bar from the video identity, frozen.
d.rectangle([0, 0, 6, H], fill=SIGNAL)

M = 64  # left margin

def tracked(draw, xy, text, font, fill, tracking=0):
    """Draw text with letter tracking. Negative tightens, per the spec."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking
    return x

# --- kicker -----------------------------------------------------------------
tracked(d, (M, 54), "METASCRUB", mono(23), SIGNAL, tracking=5)
d.text((M + 218, 57), "//  metadata removal, proven", font=mono(20), fill=FAINT)

# --- headline ---------------------------------------------------------------
# Uppercase, Chakra Petch Bold, tight negative tracking.
y = 112
for line in ["YOUR METADATA SCRUBBER", "IS LYING TO YOU"]:
    tracked(d, (M, y), line, disp(66), TEXT, tracking=-2.5)
    y += 78

# --- the proof panel --------------------------------------------------------
py0 = 300
d.rectangle([M, py0, W - M, py0 + 176], fill=PANEL, outline=LINE, width=1)

d.text((M + 26, py0 + 22), "one PDF carrying an author, both tools run:",
       font=mono(19), fill=DIM)

row = py0 + 62
d.text((M + 26, row), "exiftool -all=", font=mono(24), fill=TEXT)
d.text((M + 300, row), "file GREW  1519 -> 1846 B", font=mono(22), fill=DIM)
d.text((M + 700, row), "author still there x2", font=mono(22), fill=ALERT)
d.text((W - M - 40, row), "LEAK", font=mono(22), fill=ALERT, anchor="ra")

row += 48
d.text((M + 26, row), "metascrub", font=mono(24), fill=TEXT)
d.text((M + 300, row), "file SHRANK 1519 -> 1025 B", font=mono(22), fill=DIM)
d.text((M + 700, row), "0 hits in the bytes", font=mono(22), fill=SIGNAL)
d.text((W - M - 40, row), "CLEAN", font=mono(22), fill=SIGNAL, anchor="ra")

# --- footer -----------------------------------------------------------------
fy = 530
tracked(d, (M, fy), "71 FORMATS", displt(30), ICE, tracking=1)
d.text((M + 205, fy + 6), "7 engines", font=mono(22), fill=DIM)
d.text((M + 355, fy + 6), "951 tests", font=mono(22), fill=DIM)
d.text((M + 505, fy + 6), "Apache-2.0", font=mono(22), fill=DIM)

d.text((W - M, fy + 6), "github.com/h4d35x0/metascrub",
       font=mono(22), fill=FAINT, anchor="ra")

d.line([(M, fy + 52), (W - M, fy + 52)], fill=LINE, width=1)

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "social-preview.png")
import os
os.makedirs(os.path.dirname(out), exist_ok=True)
img.save(out, "PNG", optimize=True)
print("wrote", out, os.path.getsize(out), "bytes,", img.size)
