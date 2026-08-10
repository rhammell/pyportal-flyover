# pyportal-flyover

A desktop "ornament" for the Adafruit PyPortal: a slow, smooth satellite-imagery
flyover between two coordinates, panning continuously across the 320x240 screen
using the ILI9341's hardware scrolling.

## How it works

- `generator/generate_flyover.py` (runs on your computer) fetches aerial
  imagery from the USGS National Map along a straight route between `START`
  and `END`, rotates it so the direction of travel runs along the image's
  long axis, and packs it into `flyover.dat` -- raw big-endian RGB565 pixels
  stored column-major, so each screen column is one contiguous 480-byte read.
- `firmware/code.py` (runs on the PyPortal) drives the display controller
  directly over the 8-bit parallel bus. After drawing the first screenful, each
  frame only bumps the panel's hardware scroll register and writes the newly
  exposed column (~500 bytes), giving tear-free panning at a steady frame rate.

## Repo layout

```text
firmware/     code.py, copied to the CIRCUITPY drive
generator/    flyover generator + requirements (outputs to generator/output/)
cad/          stand design (source/ = editable CAD, print/ = printable exports)
```

## Usage

One-time setup:

```bash
python3 -m venv .venv
.venv/bin/pip install -r generator/requirements.txt
```

Generate a route (edit `START`, `END`, and `ZOOM` at the top of the script):

```bash
.venv/bin/python generator/generate_flyover.py
```

Preview `generator/output/flyover.bmp`, then deploy:

```bash
cp generator/output/flyover.dat firmware/code.py /Volumes/CIRCUITPY/
```

The PyPortal auto-reloads and starts the flyover. Pan speed is set by
`STEP` and `TARGET_FPS` at the top of `code.py` (speed = STEP x TARGET_FPS
px/s).

## Imagery

Imagery is public-domain aerial photography from the
[USGS National Map](https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer)
(US coverage only).
