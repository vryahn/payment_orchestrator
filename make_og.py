from PIL import Image, ImageDraw, ImageFont

W, H = 1200, 630
ABYSSAL, PAPER, TERRA = "#0c2a36", "#f7f3ee", "#c2410c"
ON_DARK = (247, 243, 238, 190)

FRAUNCES = "/Users/vryahn/Library/Fonts/Fraunces[SOFT,WONK,opsz,wght].ttf"
INTER_M = "/Users/vryahn/Library/Fonts/Inter-Medium.otf"
INTER_R = "/Users/vryahn/Library/Fonts/Inter-Regular.otf"


def fraunces(size, weight=600):
    f = ImageFont.truetype(FRAUNCES, size)
    try:
        f.set_variation_by_axes([min(float(size), 144.0), float(weight), 0.0, 0.0])
    except Exception:
        pass
    return f


img = Image.new("RGB", (W, H), ABYSSAL)
d = ImageDraw.Draw(img, "RGBA")

# terracotta rule, top-left, marks the brand
d.rectangle([80, 80, 80 + 96, 80 + 6], fill=TERRA)

kicker = ImageFont.truetype(INTER_M, 26)
d.text((80, 116), "PAYMENT ROUTING ORCHESTRATOR", font=kicker, fill=ON_DARK)

title = fraunces(84)
d.text((80, 190), "Evidence in the hot path,", font=title, fill=PAPER)
d.text((80, 296), "AI at the edges.", font=title, fill="#e8763f")

lede = ImageFont.truetype(INTER_R, 30)
d.text((80, 430),
       "Deterministic PSP routing on empirical approval evidence.",
       font=lede, fill=ON_DARK)
d.text((80, 472),
       "The model is not in the router.",
       font=lede, fill=ON_DARK)

foot = ImageFont.truetype(INTER_M, 26)
d.text((80, 540), "orchestrator.vryahn.com", font=foot, fill=PAPER)

wordmark = fraunces(40, 700)
right = d.textlength("BR.", font=wordmark)
d.text((W - 80 - right, 530), "BR.", font=wordmark, fill=PAPER)

img.save("public/og.png", "PNG", optimize=True)
print("written")
