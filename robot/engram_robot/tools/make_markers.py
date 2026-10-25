"""R04: print ArUco markers at a known physical size.

    python tools/make_markers.py 7 8 --mm 40 -o objects.png      # object tags (default_tag_size_m 0.04)
    python tools/make_markers.py 20 21 --mm 60 -o zones.png       # zone tags (tag_sizes_m 0.06)

Print at 100% scale (no "fit to page"), then MEASURE the black square with a ruler and put the
measured value in the config. Leave the white border around each marker when cutting.
"""
import argparse

import cv2
import numpy as np

ap = argparse.ArgumentParser()
ap.add_argument("ids", type=int, nargs="+")
ap.add_argument("--mm", type=float, default=40)
ap.add_argument("--dpi", type=int, default=300)
ap.add_argument("-o", "--out", default="markers.png")
a = ap.parse_args()

d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
px = int(round(a.mm / 25.4 * a.dpi))
pad = px // 3
tiles = []
for i in a.ids:
    m = cv2.aruco.generateImageMarker(d, i, px, borderBits=1)
    m = cv2.copyMakeBorder(m, pad, pad + 40, pad, pad, cv2.BORDER_CONSTANT, value=255)
    cv2.putText(m, f"id {i}  {a.mm:g} mm", (pad, m.shape[0] - 15), cv2.FONT_HERSHEY_SIMPLEX, 0.8, 0, 2)
    tiles.append(m)
sheet = np.hstack(tiles)
cv2.imwrite(a.out, sheet)
print(f"wrote {a.out}: {len(tiles)} markers, {a.mm} mm at {a.dpi} dpi")
