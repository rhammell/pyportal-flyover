"""Render tutorial images explaining how chunks are fetched and cut out.

This reproduces exactly what generate_flyover.py does -- compute a
chunk's axis-aligned bounding box, download it, and cut the rotated
corridor rectangle out of it -- and saves images for the project
writeup:

  output/<run_id>_tutorial_download_footprint.png
      The full downloaded bbox image for a single chunk, overlaid with
      the outline of the chunk's rotated flight-path rectangle (the
      footprint of the pixel content that ends up in the chunk). The
      outline corners are the four bbox-defining corners, so it also
      shows why the download is sized the way it is.

  output/<run_id>_tutorial_chunk_content.png
      The final CHUNK x HEIGHT (e.g. 1024x240) chunk image produced
      from that download with the same affine transform the generator
      uses.

  output/<run_id>_tutorial_download_footprint_multi.png
      Same idea for three sequential chunks: the three per-chunk
      downloads (the same tight boxes the generator requests)
      composited at their true positions on a transparent canvas, with
      each chunk's footprint outlined. Areas outside the per-chunk
      boxes stay empty.

  output/<run_id>_tutorial_chunk_content_multi.png
      The three chunk images stitched side by side, as they appear in
      the final flyover strip.

  output/<run_id>_tutorial_route_overview.png
      A single wide download covering the whole route, with the
      straight flight path and its endpoints drawn on top.

  output/<run_id>_tutorial_zoom_compare.png
      The same corridor segment rendered at several zoom levels and
      stacked with labels, showing how ZOOM acts as flight altitude.

  output/<run_id>_tutorial_scroll.gif
      A short animation of the PyPortal's 320x240 viewport panning
      across the stitched strip at the firmware's pan speed.

A unique <run_id> is generated for each run so repeated runs never
overwrite earlier outputs.

Usage:
    python tutorial/make_tutorial_images.py [chunk_index]

chunk_index selects the single chunk, and is also the first of the
three sequential chunks in the multi images.
"""

import math
import sys
import uuid
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

# The generator is a sibling directory, not a package; put it on the
# import path so its constants and helpers can be reused directly.
sys.path.insert(0, str(Path(__file__).parent.parent / "generator"))

from generate_flyover import (
    CHUNK,
    HEIGHT,
    R,
    START,
    END,
    ZOOM,
    fetch,
    to_mercator,
)

OUTPUT_DIR = Path(__file__).parent / "output"

# Outline color for all chunk footprints and route markings.
FOOTPRINT_COLOR = (255, 60, 60)

# Longest side of the route overview image, in pixels.
OVERVIEW_MAX_PX = 1200

# Zoom levels shown in the zoom comparison image.
ZOOM_COMPARE = (13, 14, 15, 16)

# Scroll preview GIF: PyPortal screen size, pan speed (matches the
# firmware's STEP * TARGET_FPS = 50 px/s), length, and frame rate.
SCREEN_W, SCREEN_H = 320, 240
SCROLL_SPEED = 50
SCROLL_SECONDS = 5
SCROLL_FPS = 25


def main():
    chunk_index = int(sys.argv[1]) if len(sys.argv) > 1 else 0

    # Unique per-run id so repeated runs never overwrite earlier outputs.
    run_id = uuid.uuid4().hex[:8]

    # Same route geometry as generate_flyover.main().
    sx, sy = to_mercator(*START)
    ex, ey = to_mercator(*END)
    mpp = 2 * math.pi * R / 256 / 2**ZOOM
    dist = math.hypot(ex - sx, ey - sy)
    length = int(dist / mpp)
    dx, dy = (ex - sx) / dist, (ey - sy) / dist
    ndx, ndy = dy, -dx
    half = (HEIGHT - 1) / 2

    if chunk_index * CHUNK >= length:
        sys.exit(f"chunk_index out of range (route has "
                 f"{math.ceil(length / CHUNK)} chunks)")

    # The helpers take meters-per-pixel as a parameter (defaulting to the
    # configured ZOOM's value) so the zoom comparison can reuse them.
    def corner_points(u0, cw, mpp=mpp):
        """Mercator coords of a chunk's four bbox-defining corners."""
        xs, ys = [], []
        for u, v in ((u0, -half - 1), (u0, half + 1),
                     (u0 + cw, -half - 1), (u0 + cw, half + 1)):
            xs.append(sx + (u * dx + v * ndx) * mpp)
            ys.append(sy + (u * dy + v * ndy) * mpp)
        return xs, ys

    def bbox_from_points(xs, ys, mpp=mpp):
        """Same padded, pixel-aligned bbox computation as the generator."""
        pad = 4 * mpp
        xmin, ymax = min(xs) - pad, max(ys) + pad
        w_f = math.ceil((max(xs) + pad - xmin) / mpp)
        h_f = math.ceil((ymax - (min(ys) - pad)) / mpp)
        return xmin, ymax - h_f * mpp, xmin + w_f * mpp, ymax, w_f, h_f

    def coeffs_for(u0, xmin, ymax, mpp=mpp):
        """Affine coefficients mapping chunk-output (x, y) to source
        pixel coordinates in a download whose top-left is (xmin, ymax)."""
        return (
            dx, ndx, (sx - xmin) / mpp + (u0 + 0.5) * dx - half * ndx - 0.5,
            -dy, -ndy, (ymax - sy) / mpp - (u0 + 0.5) * dy + half * ndy - 0.5,
        )

    def footprint_for(coeffs, cw):
        """Chunk output corners pushed through the affine mapping, i.e.
        where the chunk's content lands in the source download."""
        a, b, c, d, e, f = coeffs
        corners = [(0, 0), (cw, 0), (cw, HEIGHT), (0, HEIGHT)]
        return [(a * x + b * y + c, d * x + e * y + f) for x, y in corners]

    session = requests.Session()
    OUTPUT_DIR.mkdir(exist_ok=True)

    # --- Single chunk, exactly as before ---------------------------------
    u0 = chunk_index * CHUNK
    cw = min(CHUNK, length - u0)
    xs, ys = corner_points(u0, cw)
    xmin, ymin, xmax, ymax, w_f, h_f = bbox_from_points(xs, ys)

    print(f"Chunk {chunk_index}: {cw}x{HEIGHT} px, download {w_f}x{h_f} px")
    src = fetch(session, (xmin, ymin, xmax, ymax), w_f, h_f)

    coeffs = coeffs_for(u0, xmin, ymax)
    footprint = footprint_for(coeffs, cw)

    overlay = src.copy()
    draw = ImageDraw.Draw(overlay)
    draw.line(footprint + [footprint[0]], fill=FOOTPRINT_COLOR, width=4,
              joint="curve")
    overlay_output = OUTPUT_DIR / f"{run_id}_tutorial_download_footprint.png"
    overlay.save(overlay_output)

    piece = src.transform((cw, HEIGHT), Image.Transform.AFFINE, coeffs,
                          resample=Image.Resampling.BICUBIC)
    content_output = OUTPUT_DIR / f"{run_id}_tutorial_chunk_content.png"
    piece.save(content_output)

    print(f"Saved {overlay_output}")
    print(f"Saved {content_output}")

    # --- Three sequential chunks ------------------------------------------
    spans = [(i * CHUNK, min(CHUNK, length - i * CHUNK))
             for i in range(chunk_index, chunk_index + 3)
             if i * CHUNK < length]

    # One download per chunk, each with its own tight bbox -- the same
    # three requests the generator would make.
    downloads = []
    for u0m, cwm in spans:
        xs, ys = corner_points(u0m, cwm)
        xmin, ymin, xmax, ymax, w_f, h_f = bbox_from_points(xs, ys)
        print(f"Chunk {u0m // CHUNK}: download {w_f}x{h_f} px")
        img = fetch(session, (xmin, ymin, xmax, ymax), w_f, h_f)
        downloads.append((u0m, cwm, xmin, ymax, img))

    # Composite the three downloads onto a transparent canvas at their
    # true positions; anything outside the per-chunk boxes stays empty.
    union_xmin = min(d[2] for d in downloads)
    union_ymax = max(d[3] for d in downloads)
    canvas_w = max(round((d[2] - union_xmin) / mpp) + d[4].width
                   for d in downloads)
    canvas_h = max(round((union_ymax - d[3]) / mpp) + d[4].height
                   for d in downloads)
    overlay_multi = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    for _, _, xmin, ymax, img in downloads:
        overlay_multi.paste(
            img, (round((xmin - union_xmin) / mpp),
                  round((union_ymax - ymax) / mpp)))

    # Draw the footprint outlines on top, then cut and stitch the chunk
    # content from each chunk's own download.
    draw = ImageDraw.Draw(overlay_multi)
    stitched = Image.new(downloads[0][4].mode,
                         (sum(cwm for _, cwm in spans), HEIGHT))
    x_off = 0
    for u0m, cwm, xmin, ymax, img in downloads:
        fp = footprint_for(coeffs_for(u0m, union_xmin, union_ymax), cwm)
        draw.line(fp + [fp[0]], fill=FOOTPRINT_COLOR, width=4,
                  joint="curve")
        piece = img.transform((cwm, HEIGHT), Image.Transform.AFFINE,
                              coeffs_for(u0m, xmin, ymax),
                              resample=Image.Resampling.BICUBIC)
        stitched.paste(piece, (x_off, 0))
        x_off += cwm

    multi_overlay_output = (
        OUTPUT_DIR / f"{run_id}_tutorial_download_footprint_multi.png")
    overlay_multi.save(multi_overlay_output)
    multi_content_output = (
        OUTPUT_DIR / f"{run_id}_tutorial_chunk_content_multi.png")
    stitched.save(multi_content_output)

    print(f"Saved {multi_overlay_output}")
    print(f"Saved {multi_content_output}")

    # --- Route overview -----------------------------------------------------
    # One wide download covering the whole route, with the flight path
    # and endpoints drawn on top.
    margin = 0.06 * dist
    oxmin, oxmax = min(sx, ex) - margin, max(sx, ex) + margin
    oymin, oymax = min(sy, ey) - margin, max(sy, ey) + margin
    mpp_o = max(oxmax - oxmin, oymax - oymin) / OVERVIEW_MAX_PX
    w_o = round((oxmax - oxmin) / mpp_o)
    h_o = round((oymax - oymin) / mpp_o)

    print(f"Route overview: download {w_o}x{h_o} px")
    overview = fetch(session, (oxmin, oymin, oxmax, oymax), w_o, h_o)

    def to_overview_px(x, y):
        return ((x - oxmin) / mpp_o, (oymax - y) / mpp_o)

    draw = ImageDraw.Draw(overview)
    p_start, p_end = to_overview_px(sx, sy), to_overview_px(ex, ey)
    draw.line([p_start, p_end], fill=FOOTPRINT_COLOR, width=5)
    for px, py in (p_start, p_end):
        draw.ellipse([px - 9, py - 9, px + 9, py + 9],
                     fill=FOOTPRINT_COLOR, outline=(255, 255, 255), width=2)

    overview_output = OUTPUT_DIR / f"{run_id}_tutorial_route_overview.png"
    overview.save(overview_output)
    print(f"Saved {overview_output}")

    # --- Zoom level comparison ----------------------------------------------
    # The same stretch of ground rendered at each comparison zoom: a
    # CHUNK x HEIGHT corridor segment centered on the single chunk's
    # midpoint. Higher zooms cover less ground in the same pixels.
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 36)
    except OSError:
        font = ImageFont.load_default()

    u_center_m = (u0 + cw / 2) * mpp  # along-track position, in meters

    gap = 10
    compare = Image.new(
        "RGB", (CHUNK, len(ZOOM_COMPARE) * (HEIGHT + gap) - gap),
        (255, 255, 255))
    draw = ImageDraw.Draw(compare)
    for k, z in enumerate(ZOOM_COMPARE):
        mpp_z = 2 * math.pi * R / 256 / 2**z
        length_z = int(dist / mpp_z)
        u0_z = max(0.0, min(u_center_m / mpp_z - CHUNK / 2,
                            length_z - CHUNK))
        xs, ys = corner_points(u0_z, CHUNK, mpp_z)
        xmin, ymin, xmax, ymax, w_f, h_f = bbox_from_points(xs, ys, mpp_z)
        print(f"Zoom {z}: download {w_f}x{h_f} px")
        img = fetch(session, (xmin, ymin, xmax, ymax), w_f, h_f)
        panel = img.transform((CHUNK, HEIGHT), Image.Transform.AFFINE,
                              coeffs_for(u0_z, xmin, ymax, mpp_z),
                              resample=Image.Resampling.BICUBIC)

        y_off = k * (HEIGHT + gap)
        compare.paste(panel, (0, y_off))
        label = f"Zoom {z}"
        tx0, ty0, tx1, ty1 = draw.textbbox((0, 0), label, font=font)
        draw.rectangle([12, y_off + 12,
                        12 + (tx1 - tx0) + 20, y_off + 12 + (ty1 - ty0) + 16],
                       fill=(0, 0, 0))
        draw.text((22, y_off + 20 - ty0), label, fill=(255, 255, 255),
                  font=font)

    compare_output = OUTPUT_DIR / f"{run_id}_tutorial_zoom_compare.png"
    compare.save(compare_output)
    print(f"Saved {compare_output}")

    # --- Scroll preview GIF ---------------------------------------------
    # Pan the PyPortal-sized viewport across the stitched strip at the
    # firmware's real pan speed.
    frames = []
    for i in range(SCROLL_SECONDS * SCROLL_FPS):
        x = round(i * SCROLL_SPEED / SCROLL_FPS)
        if x + SCREEN_W > stitched.width:
            break
        frames.append(stitched.crop((x, 0, x + SCREEN_W, SCREEN_H)))

    scroll_output = OUTPUT_DIR / f"{run_id}_tutorial_scroll.gif"
    frames[0].save(scroll_output, save_all=True, append_images=frames[1:],
                   duration=1000 // SCROLL_FPS, loop=0)
    print(f"Saved {scroll_output}")


if __name__ == "__main__":
    main()
