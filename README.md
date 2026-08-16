# pyportal-flyover

The PyPortal Flyover Viewer is a visualization for the Adafruit PyPortal that shows an animated flyover between any two locations (US only). A continuous scroll of overhead satellite imagery, captured along the flight path, pans across the PyPortal screen, creating an ever-changing view of the terrain and landscape that exists between the two locations.

This repository contains the source code for generating and displaying the flyover imagery, along with CAD files for a 3D-printable PyPortal stand. 

For a complete description and step-by-step build tutorial, visit the [PyPortal Flyover Viewer](https://www.hackster.io/rhammell/pyportal-flyover-viewer-1bc359) project on Hackster.io.

## Flyover Visualization

Displaying the animated flyover relies on two separate processes - generating a continuous strip of satellite imagery, and scrolling it across the PyPortal's screen. 

### Image Generation

The image strip is built by `generator/generate_flyover.py`, which runs on your computer. Three variables at the top of the script define the flyover: `START` and `END` set the route endpoints as (lat, lon) coordinates, and `ZOOM` sets the Web Mercator zoom level, which controls the imagery detail -- effectively the flight altitude.

The script projects both endpoints into the Web Mercator projection (EPSG:3857) and traces a straight line between them, defining a 240 px tall imagery corridor along the route. Imagery along this corridor is downloaded from the [USGS National Map](https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer) export API, which serves public-domain aerial photography (US coverage only) in the same Web Mercator projection.

Because a route can be hundreds of thousands of pixels long, the corridor is processed in 1024-column chunks. For each chunk, the script computes an axis-aligned bounding box around that segment of the corridor, makes one API call to download it at native resolution, then applies an affine transform to rotate and crop the chunk so the direction of travel runs along the image's long axis.

Each finished chunk is converted to RGB565 pixel data and appended to `output/flyover.dat`, so the chunks stitch together end to end into one continuous image strip in the exact byte layout the PyPortal reads. A preview image, `output/flyover.bmp`, is also rendered from the finished data file so the full route can be inspected on your computer before deploying.

### Image Display

The flyover is displayed by `firmware/code.py`, which runs on the PyPortal and reads `flyover.dat` from the CIRCUITPY drive. Two variables at the top of the script control the pan speed: `STEP` sets the pixels advanced per frame and `TARGET_FPS` sets the frame rate, for an overall speed of STEP x TARGET_FPS px/s.

The data file is far larger than the PyPortal's available RAM, so it is never loaded whole. Because the pixels are stored column-major, each screen column is one contiguous 480-byte record in the file, and the script reads columns piece by piece into a single reusable buffer -- only one column is ever held in memory, keeping memory use constant no matter how long the route is.

For smooth animation, the script bypasses displayio and drives the ILI9341 display controller directly over its 8-bit parallel bus, using the panel's hardware scrolling. After the first screenful is drawn, each frame only bumps the controller's scroll register and writes the few newly exposed columns into the panel's own frame memory, which wraps around like a ring buffer. These updates are synced to the panel's vertical blanking signal, producing tear-free panning at a steady frame rate instead of a slow full-screen repaint.

When the route completes, the backlight fades out, the flyover resets to the start, and a new flight fades back in. Touching the screen cycles the backlight through preset brightness levels.

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
