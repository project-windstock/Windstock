"""
Terminal colour for the launcher and the slash console.

Escape sequences only -- no block-drawing glyphs, because the Windows console is
cp1252 and those raise UnicodeEncodeError. Colour is just ``ESC[..m``, which
Windows 10+ terminals render once VT processing is switched on below.

``NO_COLOR=1`` disables colour; ``FORCE_COLOR=1`` forces it (handy when piping).
"""
import os
import re
import sys

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")

RESET = "\033[0m"
BOLD, DIM = "\033[1m", "\033[2m"
RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, WHITE = (
    "\033[31m", "\033[32m", "\033[33m", "\033[34m",
    "\033[35m", "\033[36m", "\033[37m")
BRED, BGREEN, BYELLOW, BBLUE, BMAGENTA, BCYAN, BWHITE = (
    "\033[91m", "\033[92m", "\033[93m", "\033[94m",
    "\033[95m", "\033[96m", "\033[97m")


def _enable_windows_ansi():
    """Switch on VT processing so Windows consoles interpret our colours."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.GetStdHandle(-11)          # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(k32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def _supports_colour():
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    try:
        if not sys.__stdout__ or not sys.__stdout__.isatty():
            return False
    except Exception:
        return False
    return _enable_windows_ansi()


COLOR = _supports_colour()
if not COLOR:
    RESET = BOLD = DIM = RED = GREEN = YELLOW = BLUE = MAGENTA = CYAN = WHITE = ""
    BRED = BGREEN = BYELLOW = BBLUE = BMAGENTA = BCYAN = BWHITE = ""


def paint(text, *styles):
    """Wrap text in ANSI styles (a no-op when colour is turned off)."""
    if not COLOR or not styles:
        return str(text)
    return "".join(styles) + str(text) + RESET


def fg256(n):
    """A 256-colour foreground code -- used for the banner gradient."""
    return "\033[38;5;%dm" % n if COLOR else ""


def clear_screen():
    """Wipe ALL printed text -- the visible screen AND the scrollback -- then home
    the cursor.

    ``ESC[2J`` on its own only clears what is currently on screen; everything
    printed earlier is still there when you scroll up, so the terminal looked
    like it had merely scrolled. ``ESC[3J`` erases the saved lines as well
    (terminals that don't implement it ignore it). Written straight to the real
    stdout so the escape codes never land in server-log.txt. Returns True when a
    terminal actually accepted the sequence; False with no terminal attached (the
    windowed exe) or when output is redirected to a file/pipe.
    """
    out = sys.__stdout__
    try:
        if out is None or not out.isatty():
            return False
        out.write("\033[2J\033[3J\033[H")
        out.flush()
        return True
    except Exception:
        return False


def box(title, lines=(), colour=None, pad=2):
    """Print an ASCII-framed block -- the console's 'square' for a chunk of text.

    Borders are + - | only (no block-drawing glyphs), so this is safe on the
    cp1252 Windows console. Widths are computed from the plain text, so the
    colour escapes never affect the frame.
    """
    colour = colour if colour is not None else BCYAN
    lines = [str(x) for x in lines]
    title = str(title or "")
    inner = max([len(title)] + [len(x) for x in lines] + [0]) + pad
    bar = "+" + "-" * inner + "+"
    print(paint(bar, colour))
    if title:
        print(paint("|", colour) + " " + paint(title.ljust(inner - 1), BOLD, BWHITE)
              + paint("|", colour))
        print(paint(bar, colour))
    for x in lines:
        print(paint("|", colour) + " " + x.ljust(inner - 1) + paint("|", colour))
    print(paint(bar, colour))
