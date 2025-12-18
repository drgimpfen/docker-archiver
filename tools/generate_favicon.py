"""
Small utility to generate a site logo and favicon from a source image.
Usage:
  python tools/generate_favicon.py /path/to/source-image.png

It writes:
  app/static/assets/docker-archiver-logo.png  (max width 240, kept aspect)
  app/static/assets/favicon.png               (32x32 square)

If the output folder doesn't exist it will be created.
Requires: Pillow (PIL)
"""
import sys
import os
from PIL import Image

OUT_DIR = os.path.join('app', 'static', 'assets')
LOGO_OUT = os.path.join(OUT_DIR, 'docker-archiver-logo.png')
FAV_OUT = os.path.join(OUT_DIR, 'favicon.png')

def ensure_out_dir():
    os.makedirs(OUT_DIR, exist_ok=True)


def make_logo(img, max_width=240):
    w, h = img.size
    if w <= max_width:
        return img.copy()
    ratio = max_width / float(w)
    new_size = (int(w * ratio), int(h * ratio))
    return img.resize(new_size, Image.LANCZOS)


def make_favicon(img, size=(32,32)):
    # Create a square canvas and paste a centered, resized thumbnail
    # Keep aspect ratio
    thumb = img.copy()
    thumb.thumbnail(size, Image.LANCZOS)
    canvas = Image.new('RGBA', size, (255,255,255,0))
    x = (size[0] - thumb.size[0]) // 2
    y = (size[1] - thumb.size[1]) // 2
    canvas.paste(thumb, (x,y), thumb if thumb.mode=='RGBA' else None)
    return canvas


def main():
    if len(sys.argv) < 2:
        print('Usage: python tools/generate_favicon.py /path/to/source-image.png')
        sys.exit(2)

    src = sys.argv[1]
    if not os.path.exists(src):
        print('Source image not found:', src)
        sys.exit(1)

    ensure_out_dir()

    with Image.open(src) as im:
        # ensure RGBA for transparency handling
        if im.mode not in ('RGBA', 'RGB'):
            im = im.convert('RGBA')

        logo = make_logo(im)
        logo.save(LOGO_OUT)
        print('Wrote logo ->', LOGO_OUT)

        fav = make_favicon(im)
        # save favicon as PNG
        fav.save(FAV_OUT)
        print('Wrote favicon ->', FAV_OUT)

    print('Done. Add and commit the files, and rebuild your container if needed.')

if __name__ == '__main__':
    main()
