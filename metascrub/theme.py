"""
Windows 95 styling for the desktop window.

Why this look rather than a "modern" one: Tk's own `classic` theme is that
lineage, so this works WITH the toolkit instead of against it. Chasing a flat
contemporary look in Tk means either fighting the widget set or adding a theme
dependency, which would undo the reason Tkinter was chosen in the first place.
Everything here uses the standard library.

The one rule this file must not break: the payload of this window is a per-file
verdict, and RESIDUAL_FOUND has to remain the most legible thing on screen.
Retro chrome is decoration; "STILL LEAKING" is the product. Every colour below
was checked against the 3:1 contrast floor for bold UI text on the 0xC0C0C0
face, and `tests/test_theme.py` asserts it rather than trusting this comment.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Dict, Tuple

# The Windows 95 system palette, by its actual system-colour names.
FACE = "#C0C0C0"          # COLOR_3DFACE, the grey everything sits on
HILIGHT = "#FFFFFF"       # COLOR_3DHILIGHT, top-left bevel
LIGHT = "#DFDFDF"         # COLOR_3DLIGHT, inner top-left bevel
SHADOW = "#808080"        # COLOR_3DSHADOW, inner bottom-right bevel
DKSHADOW = "#000000"      # COLOR_3DDKSHADOW, outer bottom-right bevel
WINDOW = "#FFFFFF"        # COLOR_WINDOW, the sunken list area
WINDOWTEXT = "#000000"
SELECTED = "#000080"      # COLOR_HIGHLIGHT, the famous navy
SELECTEDTEXT = "#FFFFFF"
DISABLED = "#808080"

# Status colours. These are NOT the 16-colour VGA palette: pure #FF0000 on
# #C0C0C0 is only 3.0:1 and vibrates badly, and the whole point of the window
# is that a leaking file is unmissable. These are darkened until they clear the
# contrast floor while still reading as their VGA ancestors.
OK = "#006000"            # sanitized / verified clean
NEUTRAL = "#000080"       # clean, skipped: informational, not success
WARN = "#804000"          # deferred, partial
DANGER = "#A00000"        # failed, still leaking

# Win95 shipped MS Sans Serif; Microsoft Sans Serif is its metric-compatible
# successor and is present on every modern Windows. Tk falls back on its own if
# neither exists, which is why a family list is given rather than one name.
UI_FONT: Tuple[str, int] = ("Microsoft Sans Serif", 8)
UI_FONT_BOLD: Tuple[str, int, str] = ("Microsoft Sans Serif", 8, "bold")
TITLE_FONT: Tuple[str, int, str] = ("Microsoft Sans Serif", 14, "bold")


def _relative_luminance(hex_colour: str) -> float:
    """WCAG relative luminance for an #RRGGBB string."""
    r, g, b = (int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5))

    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = channel(r), channel(g), channel(b)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: str, b: str) -> float:
    """WCAG contrast ratio between two #RRGGBB colours. Symmetric, >= 1."""
    la, lb = _relative_luminance(a), _relative_luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


def status_colours() -> Dict[str, str]:
    """The verdict palette, keyed by the scrubber's status constants."""
    from .scrubber import (
        STATUS_CLEAN, STATUS_DEFERRED, STATUS_ERROR, STATUS_SANITIZED,
        STATUS_UNSUPPORTED,
    )
    return {
        STATUS_SANITIZED: OK,
        STATUS_CLEAN: NEUTRAL,
        STATUS_UNSUPPORTED: DISABLED,
        STATUS_DEFERRED: WARN,
        STATUS_ERROR: DANGER,
    }


def apply(root: tk.Misc) -> ttk.Style:
    """
    Apply the theme to a window and return the Style it configured.

    Built on ttk's `classic` theme, which already draws the beveled borders
    this look needs. `vista` and `xpnative` draw their own chrome from the OS
    and ignore most colour options, so they cannot be restyled this way.
    """
    style = ttk.Style(root)
    try:
        style.theme_use("classic")
    except tk.TclError:  # pragma: no cover - classic ships with Tk everywhere
        style.theme_use("default")

    try:
        root.configure(background=FACE)
    except tk.TclError:
        pass

    style.configure(".", background=FACE, foreground=WINDOWTEXT,
                    font=UI_FONT, borderwidth=1)

    style.configure("TFrame", background=FACE)
    style.configure("TLabel", background=FACE, foreground=WINDOWTEXT, font=UI_FONT)

    # Raised bevel that goes in on press, which is the whole feel of the era.
    style.configure("TButton", background=FACE, foreground=WINDOWTEXT,
                    font=UI_FONT, relief="raised", borderwidth=2,
                    padding=(10, 3), focuscolor=DKSHADOW)
    style.map("TButton",
              relief=[("pressed", "sunken"), ("!pressed", "raised")],
              background=[("active", FACE), ("disabled", FACE)],
              foreground=[("disabled", DISABLED)])

    # The indicator is deliberately NOT restyled. Filling it to match the Win95
    # palette set the selected colour to the same #FFFFFF as the unselected
    # one, so a ticked box rendered identically to an empty one and the state
    # became invisible. That is not cosmetic: "Keep backup" is the control that
    # decides whether the user's original files survive, and a toggle whose
    # position cannot be read is worse than an ugly one.
    #
    # ttk's classic indicator already looks the part, so the palette stops at
    # the surrounding chrome. tests/test_theme.py asserts that selected and
    # unselected remain distinguishable.
    style.configure("TCheckbutton", background=FACE, foreground=WINDOWTEXT,
                    font=UI_FONT, focuscolor=DKSHADOW)
    style.map("TCheckbutton",
              background=[("active", FACE)],
              foreground=[("disabled", DISABLED)])

    # The file list: a sunken white well, the way a Win95 list control looked.
    style.configure("Treeview", background=WINDOW, fieldbackground=WINDOW,
                    foreground=WINDOWTEXT, font=UI_FONT,
                    relief="sunken", borderwidth=2, rowheight=18)
    style.map("Treeview",
              background=[("selected", SELECTED)],
              foreground=[("selected", SELECTEDTEXT)])
    style.configure("Treeview.Heading", background=FACE, foreground=WINDOWTEXT,
                    font=UI_FONT, relief="raised", borderwidth=2, padding=(4, 2))
    style.map("Treeview.Heading", relief=[("pressed", "sunken")])

    style.configure("TScrollbar", background=FACE, troughcolor=LIGHT,
                    relief="raised", borderwidth=2, arrowcolor=WINDOWTEXT)
    style.configure("TProgressbar", background=SELECTED, troughcolor=LIGHT,
                    relief="sunken", borderwidth=2)

    # The application title, styled like a Win95 title bar rather than a label.
    style.configure("Title.TLabel", background=SELECTED, foreground=SELECTEDTEXT,
                    font=TITLE_FONT, padding=(6, 2))
    style.configure("Status.TLabel", background=FACE, foreground=WINDOWTEXT,
                    font=UI_FONT, relief="sunken", borderwidth=1, padding=(4, 2))
    style.configure("Hint.TLabel", background=FACE, foreground=DKSHADOW, font=UI_FONT)
    style.configure("EngineOK.TLabel", background=FACE, foreground=OK, font=UI_FONT_BOLD)
    style.configure("EngineWarn.TLabel", background=FACE, foreground=WARN, font=UI_FONT_BOLD)
    style.configure("EngineBad.TLabel", background=FACE, foreground=DANGER, font=UI_FONT_BOLD)

    return style
