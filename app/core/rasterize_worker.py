"""Child process for SVG rasterize.

FONTCONFIG_FILE must be set in the environment *before* this process starts
so Pango/cairo initialize against the right fonts. The parent worker cannot
toggle fontconfig after the first cairosvg import — that is how Barlow leaked
onto BICCU/KGC.
"""

import json
import sys

import cairosvg


def main():
    data = json.load(sys.stdin)
    png = cairosvg.svg2png(
        bytestring=data["svg"].encode("utf-8"),
        output_width=int(data["w"]),
        output_height=int(data["h"]),
    )
    sys.stdout.buffer.write(png)


if __name__ == "__main__":
    main()
