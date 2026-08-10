"""PyPortal flyover: tear-free panning via ILI9341 hardware scroll.

Instead of letting displayio repaint all 76,800 pixels per frame (slow,
visible wipe/tearing), this drives the display controller directly:

  - takes over the 8-bit parallel bus from displayio
  - sends the same ILI9341 init sequence the stock firmware uses
  - draws the first screenful once, then each frame only bumps the
    panel's vertical-scroll register and writes the few newly exposed
    columns (~1 KB) into the panel's own frame memory

The ILI9341's scroll axis is its long (320 px) axis, which in the
PyPortal's landscape mounting is exactly the horizontal pan direction,
and the scroll wraps around -- the panel's frame memory acts as the
ring buffer.

Requires flyover.dat: raw big-endian RGB565 pixels, column-major
(each column is one contiguous 480-byte record).
"""

import struct
import time

import board
import digitalio
import displayio

# ParallelBus moved out of displayio in CircuitPython 9.
try:
    from paralleldisplaybus import ParallelBus  # CircuitPython 9+
except ImportError:
    from displayio import ParallelBus

# Tunables.
DATA_PATH = "/flyover.dat"
STEP = 1  # pixels advanced per frame
TARGET_FPS = 15  # pan speed = STEP * TARGET_FPS px/s

# MADCTL 0xA8 mirrors the panel's native line order relative to screen x,
# so panning forward means decrementing the scroll register. If the image
# pans with a marching band of garbage, set this to False.
REVERSE = True

# Panel geometry (landscape) and bytes per RGB565 column.
W = 320
H = 240
COL_BYTES = H * 2

# --- Take over the display bus -------------------------------------------

# Detach displayio from the hardware so we can drive the bus ourselves.
displayio.release_displays()

# Rebuild the 8-bit parallel bus with the pins displayio normally uses.
bus = ParallelBus(
    data0=board.LCD_DATA0,
    command=board.TFT_RS,  # PB05; board.TFT_DC wrongly aliases the WR pin
    chip_select=board.TFT_CS,
    write=board.TFT_WR,
    read=board.TFT_RD,
    reset=board.TFT_RESET,
)

# Hardware-reset the controller and give it time to come back up.
bus.reset()
time.sleep(0.1)

# Stock PyPortal init sequence (from the CircuitPython board definition).
INIT = (
    (0xEF, b"\x03\x80\x02", 0),
    (0xCF, b"\x00\xc1\x30", 0),
    (0xED, b"\x64\x03\x12\x81", 0),
    (0xE8, b"\x85\x00\x78", 0),
    (0xCB, b"\x39\x2c\x00\x34\x02", 0),
    (0xF7, b"\x20", 0),
    (0xEA, b"\x00\x00", 0),
    (0xC0, b"\x23", 0),  # Power control VRH
    (0xC1, b"\x10", 0),  # Power control SAP/BT
    (0xC5, b"\x3e\x28", 0),  # VCM control
    (0xC7, b"\x86", 0),  # VCM control 2
    (0x36, b"\xa8", 0),  # MADCTL: landscape, BGR
    (0x37, b"\x00\x00", 0),  # Scroll start = 0
    (0x3A, b"\x55", 0),  # 16 bits per pixel
    (0xB1, b"\x00\x18", 0),  # Frame rate control
    (0xB6, b"\x08\xa2\x27", 0),  # Display function control
    (0xF2, b"\x00", 0),  # 3Gamma off
    (0x26, b"\x01", 0),  # Gamma curve
    (0xE0, b"\x0f\x31\x2b\x0c\x0e\x08\x4e\xf1\x37\x07\x10\x03\x0e\x09\x00", 0),
    (0xE1, b"\x00\x0e\x14\x03\x11\x07\x31\xc1\x48\x08\x0f\x0c\x31\x36\x0f", 0),
    (0x33, b"\x00\x00\x01\x40\x00\x00", 0),  # Scroll area = full 320 lines
    (0x11, b"", 120),  # Exit sleep
    (0x29, b"", 120),  # Display on
)

# Send each init command, honoring the required post-command delays.
for cmd, cmd_data, delay_ms in INIT:
    bus.send(cmd, cmd_data)
    if delay_ms:
        time.sleep(delay_ms / 1000)

# --- Column drawing -------------------------------------------------------

# Open the image data and size it in columns; reuse one column buffer.
data = open(DATA_PATH, "rb")
data.seek(0, 2)
total_cols = data.tell() // COL_BYTES
colbuf = bytearray(COL_BYTES)

scroll = 0  # current VSCRSAD register value


def set_scroll(value):
    """Set the panel's vertical scroll start address (VSCRSAD)."""
    bus.send(0x37, struct.pack(">H", value))


def write_column(screen_x, world_col):
    """Load one flyover column from disk into panel memory so that it
    appears at screen_x under the current scroll value."""
    # Read the column's pixels straight from flash into the buffer.
    data.seek(world_col * COL_BYTES)
    data.readinto(colbuf)
    # Convert screen position to a frame-memory line, undoing the scroll.
    if REVERSE:
        xw = (screen_x - scroll) % W
    else:
        xw = (screen_x + scroll) % W
    # Set a 1-line-wide write window and blast the pixels into it.
    bus.send(0x2A, struct.pack(">HH", xw, xw))  # column address
    bus.send(0x2B, struct.pack(">HH", 0, H - 1))  # row address
    bus.send(0x2C, colbuf)  # memory write


# Draw the first screenful before turning the backlight on, so the
# panel's power-up noise is never visible.
for x in range(W):
    write_column(x, x)

# Now that the screen holds a valid image, switch the backlight on.
backlight = digitalio.DigitalInOut(board.TFT_BACKLIGHT)
backlight.switch_to_output(value=True)

# --- Animation loop -------------------------------------------------------

# Pan state: bounce between the ends of the image at a fixed frame rate.
pos = 0  # world column shown at the left edge of the screen
direction = 1
max_pos = total_cols - W
frame_period = 1 / TARGET_FPS
frames = 0
last_report = time.monotonic()
next_frame = last_report

while True:
    # Advance the pan position, reversing direction at either end.
    prev = pos
    pos += STEP * direction
    if pos >= max_pos:
        pos = max_pos
        direction = -1
    elif pos <= 0:
        pos = 0
        direction = 1
    delta = pos - prev  # actual signed movement this frame

    if delta:
        # Bump the hardware scroll register by the movement amount.
        step = -delta if REVERSE else delta
        scroll = (scroll + step) % W
        set_scroll(scroll)
        # Fill in only the columns that just scrolled into view.
        if delta > 0:
            new_xs = range(W - delta, W)  # columns entering on the right
        else:
            new_xs = range(0, -delta)  # columns entering on the left
        for x in new_xs:
            write_column(x, pos + x)

    # Print the measured frame rate every 5 seconds.
    frames += 1
    now = time.monotonic()
    if now - last_report >= 5:
        print(f"{frames / (now - last_report):.1f} fps")
        frames = 0
        last_report = now

    # Sleep until the next frame slot; if we're behind, reset the schedule.
    next_frame += frame_period
    delay = next_frame - time.monotonic()
    if delay > 0:
        time.sleep(delay)
    else:
        next_frame = time.monotonic()
