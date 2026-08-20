"""Generate a satellite imagery flyover between two coordinates.

Given START and END (lat, lon), this renders the imagery corridor along
the route, rotated so the direction of travel runs along the image's
long axis -- like a cockpit view, with "screen up" being the left side
of the flight path. The route is a straight line in Web Mercator space
(a constant-bearing course), any bearing, any length.

Imagery comes from the USGS National Map (public domain, US only),
fetched in chunks along the corridor and resampled with a single
affine transform per chunk. Outputs:

  output/flyover.dat -- raw big-endian RGB565 pixels, column-major
                        (each screen column is one contiguous 480-byte record)
  output/flyover.bmp -- a preview BMP decoded from flyover.dat, for
                        troubleshooting on your laptop: it always shows
                        the route end to end (downscaled if very long),
                        so assembly problems anywhere in the data file
                        are visible

Usage:
    python generator/generate_flyover.py

Then copy generator/output/flyover.dat and firmware/code.py to the
CIRCUITPY drive.
"""

import array
import io
import math
import sys
import time
from pathlib import Path

import requests
from PIL import Image

# Route endpoints as (lat, lon). Both points must be within the United
# States -- the USGS imagery service has no coverage elsewhere, and
# routes outside the US come back blank.
START = (38.030, -78.477)  # Charlottesville, VA
END = (38.9531, -77.4565)  # Dulles International Airport, VA

# Web Mercator zoom level. 13-14 = regional/airliner view, 15-16 = low altitude.
ZOOM = 14

# Corridor dimensions in pixels.
HEIGHT = 240  # corridor height = screen height
CHUNK = 1024  # output columns fetched per request (keeps requests < 4096 px)

# Widest preview BMP to write. Longer routes are downscaled (never
# truncated) by an integer factor: the preview's job is to show the
# whole flyover end to end for troubleshooting, while staying a
# practical size to open.
PREVIEW_MAX_COLS = 20000

# Output files, written next to this script.
OUTPUT_DIR = Path(__file__).parent / "output"
DATA_OUTPUT = OUTPUT_DIR / "flyover.dat"
PREVIEW_OUTPUT = OUTPUT_DIR / "flyover.bmp"

# USGS National Map imagery export endpoint (public domain, US only).
EXPORT_URL = (
    "https://basemap.nationalmap.gov/arcgis/rest/services/"
    "USGSImageryOnly/MapServer/export"
)

# Web Mercator sphere radius in meters.
R = 6378137.0


def to_mercator(lat, lon):
    """Project lat/lon (degrees) to EPSG:3857 meters."""
    x = math.radians(lon) * R
    y = R * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
    return x, y


def fetch(session, bbox, width, height):
    """Fetch one axis-aligned bbox (EPSG:3857 meters) as an RGB image."""
    # Ask the export endpoint for exactly this box at exactly this pixel
    # size, so no client-side resampling is needed.
    params = {
        "bbox": ",".join(str(v) for v in bbox),
        "bboxSR": "3857",
        "imageSR": "3857",
        "size": f"{width},{height}",
        "format": "jpg",
        "f": "image",
    }
    resp = session.get(EXPORT_URL, params=params, timeout=120)
    resp.raise_for_status()

    # The server reports errors as JSON with a 200 status, so check the
    # content type rather than the status code.
    if "image" not in resp.headers.get("Content-Type", ""):
        raise RuntimeError(f"Server error: {resp.text[:300]}")
    return Image.open(io.BytesIO(resp.content)).convert("RGB")


def build_preview(length):
    """Render the troubleshooting preview BMP from the finished .dat.

    The preview exists to sanity-check the data file end to end: it is
    decoded from the exact bytes the PyPortal will read (including the
    RGB565 color quantization), and it always covers the full route,
    so a misordered chunk, seam, or blank region anywhere in the file
    shows up visually. Routes longer than PREVIEW_MAX_COLS are
    downscaled along the route axis by an integer factor rather than
    truncated -- full detail matters less than complete coverage. The
    file is processed in blocks, so memory stays flat even for huge
    routes."""

    # Downscale the preview if the route is longer than PREVIEW_MAX_COLS
    factor = max(1, math.ceil(length / PREVIEW_MAX_COLS))
    pw = math.ceil(length / factor)
    note = f" ({factor}x downscaled)" if factor > 1 else ""
    print(f"Building preview from flyover.dat{note} ...")

    # Expand RGB565 -> RGB888 via a lookup table over all 16-bit values,
    # replicating each channel's high bits into its low bits.
    lut = [
        bytes((
            ((v >> 8) & 0xF8) | (v >> 13),
            ((v >> 3) & 0xFC) | ((v >> 9) & 0x03),
            ((v << 3) & 0xF8) | ((v >> 2) & 0x07),
        ))
        for v in range(65536)
    ]

    # Columns per block: a multiple of the downscale factor (~4096
    # columns, ~2 MB) so each full block maps to a whole number of
    # preview columns.
    block_cols = factor * max(1, 4096 // factor)

    preview = Image.new("RGB", (pw, HEIGHT))
    with open(DATA_OUTPUT, "rb") as f:
        x = 0  # next preview column to fill
        for c0 in range(0, length, block_cols):
            ncols = min(block_cols, length - c0)
            pixels = array.array("H")
            pixels.frombytes(f.read(ncols * HEIGHT * 2))
            if sys.byteorder == "little":
                pixels.byteswap()
            raw = b"".join(map(lut.__getitem__, pixels))

            # The .dat is column-major, so these bytes describe the
            # transposed block; transpose to restore ncols x HEIGHT.
            block = Image.frombytes("RGB", (HEIGHT, ncols), raw)
            block = block.transpose(Image.Transpose.TRANSPOSE)

            # Last block absorbs any rounding so the preview fills fully.
            bw = pw - x if c0 + ncols >= length else ncols // factor
            if factor > 1:
                block = block.resize((bw, HEIGHT), Image.Resampling.BOX)
            preview.paste(block, (x, 0))
            x += bw

    preview.save(PREVIEW_OUTPUT)


def main():
    # Project both endpoints into Web Mercator meters, and work out the
    # projected size of one pixel at the chosen zoom (256 px tiles).
    sx, sy = to_mercator(*START)
    ex, ey = to_mercator(*END)
    mpp = 2 * math.pi * R / 256 / 2**ZOOM  # projected meters per pixel

    # Route geometry in Mercator space: total length in output pixels,
    # the unit vector pointing along the direction of travel, and the
    # normal pointing toward the bottom of the screen (right of track).
    dist = math.hypot(ex - sx, ey - sy)
    length = int(dist / mpp)  # flyover length in pixels
    dx, dy = (ex - sx) / dist, (ey - sy) / dist
    ndx, ndy = dy, -dx

    # Route stats for the summary printout.
    bearing = math.degrees(math.atan2(ex - sx, ey - sy)) % 360
    mid_lat = math.radians((START[0] + END[0]) / 2)
    ground_km = dist * math.cos(mid_lat) / 1000
    n_chunks = math.ceil(length / CHUNK)

    print(f"Route: {START} -> {END}")
    print(f"Bearing {bearing:.0f} deg, ~{ground_km:.1f} km, zoom {ZOOM} "
          f"(~{mpp * math.cos(mid_lat):.1f} m/px)")
    print(f"Flyover: {length}x{HEIGHT} px = {length * HEIGHT * 2 / 1e6:.1f} MB, "
          f"{length / 15 / 60:.1f} min one-way at 15 px/s, "
          f"{n_chunks} requests")

    # Chunks are streamed straight to the .dat file as they arrive, so
    # memory use stays constant regardless of route length. `half` is
    # the cross-track offset of the middle row.
    session = requests.Session()
    half = (HEIGHT - 1) / 2
    OUTPUT_DIR.mkdir(exist_ok=True)
    out = open(DATA_OUTPUT, "wb")

    # Fetch the corridor one chunk at a time, appending each to the file.
    for i, u0 in enumerate(range(0, length, CHUNK)):
        # Width of this chunk (the last one may be short).
        cw = min(CHUNK, length - u0)

        # Axis-aligned bounding box (in meters) around this chunk's
        # rotated corridor rectangle, padded by a few pixels.
        xs, ys = [], []
        for u, v in ((u0, -half - 1), (u0, half + 1),
                     (u0 + cw, -half - 1), (u0 + cw, half + 1)):
            xs.append(sx + (u * dx + v * ndx) * mpp)
            ys.append(sy + (u * dy + v * ndy) * mpp)
        pad = 4 * mpp
        xmin, ymax = min(xs) - pad, max(ys) + pad
        w_f = math.ceil((max(xs) + pad - xmin) / mpp)
        h_f = math.ceil((ymax - (min(ys) - pad)) / mpp)
        xmax = xmin + w_f * mpp  # snap bbox to the pixel grid
        ymin = ymax - h_f * mpp

        # Download the bbox at native resolution.
        src = fetch(session, (xmin, ymin, xmax, ymax), w_f, h_f)

        # Rotate/crop the chunk out of the bbox with one affine
        # transform. The coefficients map output (x, y) to source pixel
        # coords: output x runs along the path from u0, output y across
        # the corridor, with the path through the middle row.
        coeffs = (
            dx, ndx, (sx - xmin) / mpp + (u0 + 0.5) * dx - half * ndx - 0.5,
            -dy, -ndy, (ymax - sy) / mpp - (u0 + 0.5) * dy + half * ndy - 0.5,
        )
        piece = src.transform((cw, HEIGHT), Image.Transform.AFFINE, coeffs,
                              resample=Image.Resampling.BICUBIC)

        # Reorder this chunk's pixels column-major (transposing swaps
        # the axes, so the transposed row-major bytes are the chunk's
        # columns in order) and pack RGB888 -> RGB565, big-endian (the
        # ILI9341 takes the high byte first over the 8-bit parallel
        # bus). Appending chunk after chunk yields the full file in
        # column order.
        raw = piece.transpose(Image.Transpose.TRANSPOSE).tobytes()
        pixels = array.array(
            "H",
            (
                ((raw[j] & 0xF8) << 8) | ((raw[j + 1] & 0xFC) << 3) | (raw[j + 2] >> 3)
                for j in range(0, len(raw), 3)
            ),
        )
        if sys.byteorder == "little":
            pixels.byteswap()
        out.write(pixels.tobytes())

        # Progress report, and a short pause to be polite to the server.
        print(f"  chunk {i + 1}/{n_chunks}", flush=True)
        if u0 + CHUNK < length:
            time.sleep(0.25)

    out.close()

    build_preview(length)

    # Print the results
    print(f"Saved {DATA_OUTPUT} ({length * HEIGHT * 2} bytes) "
          f"and {PREVIEW_OUTPUT} (preview)")
    print("Copy generator/output/flyover.dat and firmware/code.py "
          "to the CIRCUITPY drive.")


if __name__ == "__main__":
    main()
