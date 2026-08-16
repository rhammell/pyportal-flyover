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
import pwmio

try:
    from paralleldisplaybus import ParallelBus  # CircuitPython 9+
except ImportError:
    from displayio import ParallelBus

DATA_PATH = "/flyover.dat"
STEP = 1  # pixels advanced per frame
TARGET_FPS = 50  # pan speed = STEP * TARGET_FPS px/s
FADE_IN_S = 4.0  # backlight fade-in duration at the start of each flight
FADE_OUT_S = 4.0  # backlight fade-out duration at the end of each flight
HOLD_BLACK_S = 1.0  # pause on black between flights
BRIGHTNESS = 1.0  # steady-state backlight level (0.0-1.0); the fades
#                   ramp between black and this level

# Physical mounting orientation. False = landscape: new terrain enters on
# the right edge. True = portrait, stood on its side with the landscape-left
# edge at the top: the panel output is rotated 180 degrees in its memory,
# so new terrain enters at the top of the rotated screen and scrolls down.
PORTRAIT = True

# The landscape MADCTL (0xA8) mirrors the panel's native line order
# relative to screen x, so panning forward means decrementing the scroll
# register; the portrait MADCTL (0x68) flips the line order back, so it
# increments. If the image pans with a marching band of garbage, negate.
SCROLL_DIR = 1 if PORTRAIT else -1

W = 320
H = 240
COL_BYTES = H * 2

# --- Take over the display bus -------------------------------------------

displayio.release_displays()

bus = ParallelBus(
    data0=board.LCD_DATA0,
    command=board.TFT_RS,  # PB05; board.TFT_DC wrongly aliases the WR pin
    chip_select=board.TFT_CS,
    write=board.TFT_WR,
    read=board.TFT_RD,
    reset=board.TFT_RESET,
)
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
    # MADCTL: BGR color order; 0x68 flips both address-order bits relative
    # to 0xA8, i.e. a 180-degree rotation in panel memory for portrait.
    (0x36, b"\x68" if PORTRAIT else b"\xa8", 0),
    (0x37, b"\x00\x00", 0),  # Scroll start = 0
    (0x3A, b"\x55", 0),  # 16 bits per pixel
    (0xB1, b"\x00\x18", 0),  # Frame rate control
    # Widen the vertical porches so the blanking window after each TE
    # pulse is long enough (~2.5 ms) to bump the scroll register and
    # write a full column before the panel starts scanning again.
    (0xB5, b"\x10\x30\x0a\x14", 0),  # VFP=16, VBP=48 lines
    (0x35, b"\x00", 0),  # Tearing-effect line on (V-blank pulses only)
    (0xB6, b"\x08\xa2\x27", 0),  # Display function control
    (0xF2, b"\x00", 0),  # 3Gamma off
    (0x26, b"\x01", 0),  # Gamma curve
    (0xE0, b"\x0f\x31\x2b\x0c\x0e\x08\x4e\xf1\x37\x07\x10\x03\x0e\x09\x00", 0),
    (0xE1, b"\x00\x0e\x14\x03\x11\x07\x31\xc1\x48\x08\x0f\x0c\x31\x36\x0f", 0),
    (0x33, b"\x00\x00\x01\x40\x00\x00", 0),  # Scroll area = full 320 lines
    (0x11, b"", 120),  # Exit sleep
    (0x29, b"", 120),  # Display on
)

for cmd, cmd_data, delay_ms in INIT:
    bus.send(cmd, cmd_data)
    if delay_ms:
        time.sleep(delay_ms / 1000)

# --- Column drawing -------------------------------------------------------

data = open(DATA_PATH, "rb")
data.seek(0, 2)
total_cols = data.tell() // COL_BYTES
colbuf = bytearray(COL_BYTES)

ROW_WINDOW = struct.pack(">HH", 0, H - 1)  # full-height row address window
scroll = 0  # current VSCRSAD register value


def set_scroll(value):
    bus.send(0x37, struct.pack(">H", value))


def load_column(world_col):
    """Read one strip column from disk into the column buffer."""
    data.seek(world_col * COL_BYTES)
    data.readinto(colbuf)


def blit_column(screen_x):
    """Write the column buffer into panel memory so that it appears at
    screen_x under the current scroll value."""
    xw = (screen_x + SCROLL_DIR * scroll) % W
    bus.send(0x2A, struct.pack(">HH", xw, xw))  # column address
    bus.send(0x2B, ROW_WINDOW)  # row address
    bus.send(0x2C, colbuf)  # memory write


# The panel's tearing-effect pin pulses high during each vertical
# blanking interval (~66 Hz). Scroll bumps and column writes are done
# inside that window, while the panel is not scanning.
te = digitalio.DigitalInOut(board.TFT_TE)
te.switch_to_input()


def wait_for_blanking():
    """Block until the next rising edge of the tearing-effect signal."""
    while te.value:
        pass
    while not te.value:
        pass


# The backlight starts dark; each flight fades it up at the start and
# back down at the end, so the reset between flights happens unseen.
backlight = pwmio.PWMOut(board.TFT_BACKLIGHT, frequency=25000, duty_cycle=0)

# --- Animation loop -------------------------------------------------------

max_pos = total_cols - W
frame_period = 1 / TARGET_FPS
# Time one flight takes, used to schedule the fade-out against its end.
flight_s = -(-max_pos // STEP) * frame_period
frames = 0
last_report = time.monotonic()


def update_fade(flight_start):
    """Set the backlight from the flight's elapsed time: ramp up over
    FADE_IN_S, hold at BRIGHTNESS, ramp down over the final FADE_OUT_S.
    Squaring the ramp compensates for the eye's nonlinear brightness
    response so the fades look even."""
    t = time.monotonic() - flight_start
    k = min(t / FADE_IN_S, (flight_s - t) / FADE_OUT_S, 1.0)
    if k < 0:
        k = 0
    backlight.duty_cycle = int(65535 * BRIGHTNESS * k * k)


while True:
    # Draw the starting screenful while the backlight is dark, then fly
    # the route once, scrolling in one direction only.
    for x in range(W):
        load_column(x)
        blit_column(x)

    pos = 0  # world column shown at the left edge of the screen
    flight_start = time.monotonic()
    next_frame = flight_start

    while pos < max_pos:
        delta = min(STEP, max_pos - pos)
        pos += delta

        first = True
        for x in range(W - delta, W):  # columns entering on the right
            # Read from flash before syncing, so the blanking window is
            # spent only on fast bus writes.
            load_column(pos + x)
            if first:
                # The scroll register only latches at a frame boundary,
                # but memory writes land immediately -- so until the next
                # boundary the entering column's memory line is still
                # mapped to the exiting edge, where the panel sweep can
                # catch it and flash it there. Doing the scroll bump and
                # write inside vertical blanking prevents that.
                wait_for_blanking()
                scroll = (scroll + SCROLL_DIR * delta) % W
                set_scroll(scroll)
                first = False
            blit_column(x)

        frames += 1
        now = time.monotonic()
        if now - last_report >= 5:
            print(f"{frames / (now - last_report):.1f} fps")
            frames = 0
            last_report = now

        # Spend the inter-frame wait updating the backlight ramp in
        # small slices so the fades stay smooth between frames.
        next_frame += frame_period
        while True:
            update_fade(flight_start)
            remaining = next_frame - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(remaining, 0.02))
        if time.monotonic() - next_frame > frame_period:
            next_frame = time.monotonic()  # behind schedule; resync

    # Flight complete: settle on black, pause, then restart from the top.
    backlight.duty_cycle = 0
    time.sleep(HOLD_BLACK_S)
