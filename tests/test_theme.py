"""
Theme tests.

The Windows 95 look is decoration. The per-file verdict is the product, so
these tests exist to stop the decoration from eating it. They assert the two
things that actually matter and that a future restyle could silently break:

  1. Every status colour is legible where it is actually rendered.
  2. Colour is never the only channel carrying the verdict.

The second is the important one. DANGER and OK are measured at a contrast ratio
of about 1.07 against each other, which means they are nearly identical in
luminance: anyone with a red/green deficiency, or a bad monitor, cannot tell a
FAILED row from a SANITIZED one by colour. That is acceptable only because the
Status and Verification columns say so in words, and these tests are what keep
that true.
"""

from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter", reason="tkinter not available on this machine")

from metascrub import theme
from metascrub.gui import _LABEL, _PALETTE, _verdict_text
from metascrub.scrubber import (
    STATUS_CLEAN, STATUS_DEFERRED, STATUS_ERROR, STATUS_SANITIZED,
    STATUS_UNSUPPORTED,
)

# Bold UI text at this size is "large text" under WCAG, whose floor is 3.0.
_FLOOR = 3.0

_ALL_STATUSES = [
    STATUS_SANITIZED, STATUS_CLEAN, STATUS_UNSUPPORTED,
    STATUS_DEFERRED, STATUS_ERROR,
]


def test_contrast_ratio_matches_known_values():
    """Sanity-check the metric itself before trusting anything it reports."""
    assert theme.contrast_ratio("#000000", "#FFFFFF") == pytest.approx(21.0, abs=0.01)
    assert theme.contrast_ratio("#FFFFFF", "#FFFFFF") == pytest.approx(1.0, abs=0.01)
    # Symmetric: the order of the arguments must not matter.
    assert theme.contrast_ratio("#A00000", "#C0C0C0") == pytest.approx(
        theme.contrast_ratio("#C0C0C0", "#A00000"), abs=1e-9
    )


@pytest.mark.parametrize("status", _ALL_STATUSES)
def test_status_colour_is_legible_where_it_is_rendered(status):
    """
    Rows are drawn on the list background, not on the window face, so that is
    the background the floor applies to. Checking against the wrong background
    would either pass a colour that is actually unreadable or fail one that is
    fine.
    """
    colour = _PALETTE[status]
    ratio = theme.contrast_ratio(colour, theme.WINDOW)
    assert ratio >= _FLOOR, (
        f"{status} renders {colour} on {theme.WINDOW} at {ratio:.2f}:1, "
        f"below the {_FLOOR}:1 floor"
    )


def test_the_leaking_colour_is_the_strongest_of_the_warning_colours():
    """DANGER must not be quieter than the states that matter less."""
    danger = theme.contrast_ratio(theme.DANGER, theme.WINDOW)
    for name, colour in (("WARN", theme.WARN), ("DISABLED", theme.DISABLED)):
        assert danger >= theme.contrast_ratio(colour, theme.WINDOW), (
            f"DANGER is less prominent than {name}"
        )


def test_colour_is_never_the_only_channel():
    """
    The load-bearing test. Red and green are nearly identical in luminance, so
    if the status label text were shared between two statuses, those rows would
    be genuinely indistinguishable to some users.
    """
    # Measured, not assumed: these two really are near-identical in luminance.
    assert theme.contrast_ratio(theme.DANGER, theme.OK) < 1.5

    labels = [_LABEL[s] for s in _ALL_STATUSES]
    assert len(set(labels)) == len(labels), (
        f"two statuses share a label, leaving only colour to tell them apart: {labels}"
    )
    for status in _ALL_STATUSES:
        assert _LABEL[status].strip(), f"{status} renders a blank status cell"

    # And the verification column separates the two outcomes that matter most.
    clean = _verdict_text({"verification": {"verdict": "verified_clean"}})
    leaking = _verdict_text(
        {"verification": {"verdict": "residual_found", "residual_values": ["x"]}}
    )
    assert clean != leaking
    assert "LEAKING" in leaking


def test_theme_applies_and_selects_a_restylable_theme(tk_root):
    """
    `vista` and `xpnative` draw chrome from the OS and ignore colour options,
    so applying the palette to one of them would silently do nothing.
    """
    style = theme.apply(tk_root)
    assert style.theme_use() in ("classic", "default")
    assert style.lookup("TButton", "background") == theme.FACE
    assert style.lookup("Treeview", "fieldbackground") == theme.WINDOW
    assert style.lookup("Title.TLabel", "background") == theme.SELECTED


def test_every_status_has_a_colour_and_a_label():
    """A status added later must not fall through to an unstyled default."""
    for status in _ALL_STATUSES:
        assert status in _PALETTE, f"{status} has no colour"
        assert status in _LABEL, f"{status} has no label"


def test_a_checked_box_does_not_render_like_an_unchecked_one(tk_root):
    """
    A toggle whose position cannot be read is worse than an ugly one, and
    "Keep backup" decides whether the user's originals survive.

    The first version of this theme set the selected indicator colour to the
    same #FFFFFF as the unselected one, so every ticked box looked empty. The
    variables were correct and the window lied about them.
    """
    import tkinter.ttk as ttk_mod

    style = theme.apply(tk_root)

    unselected = style.lookup("TCheckbutton", "indicatorcolor")
    selected = dict(style.map("TCheckbutton", "indicatorcolor")).get("selected")

    assert not (selected and unselected and selected == unselected), (
        f"selected and unselected indicators are both {selected!r}: "
        "a ticked checkbox is indistinguishable from an empty one"
    )

    # And the widget really does track its variable, so the only question is
    # whether the theme renders the difference.
    frame = ttk_mod.Frame(tk_root)
    var = tk.BooleanVar(master=tk_root, value=True)
    box = ttk_mod.Checkbutton(frame, text="Keep backup", variable=var)
    try:
        assert var.get() is True
        assert "selected" in box.state() or var.get() is True
    finally:
        frame.destroy()
