"""
GUI tests.

These drive the real widget tree and the real worker thread rather than
importing the module and calling it covered. The window is withdrawn, so
nothing appears on screen, but every assertion below is made against the state
the user would actually see in it.

The load-bearing case is the same one the CLI regression covers: with metadata
unreadable, the window must refuse to scrub rather than paint reassuring rows.
A GUI reporting a green CLEAN for a file nothing examined is worse than no GUI,
because the reassurance is what the user walks away with.
"""

from __future__ import annotations

import os

import pytest

tk = pytest.importorskip("tkinter", reason="tkinter not available on this machine")

from conftest import build, contains_anywhere
from metascrub import exif_io
from metascrub import gui as gui_module
from metascrub.gui import ScrubberWindow, _verdict_text
from metascrub.scrubber import STATUS_CLEAN, STATUS_ERROR, STATUS_SANITIZED


@pytest.fixture
def window(monkeypatch, tk_root):
    """
    A withdrawn real window, destroyed afterwards, with modal dialogs captured
    instead of shown.

    The capture is not merely to stop the suite hanging (a real modal blocks a
    headless run forever). It makes the dialogs assertable: warning the user
    when a file is still leaking is behaviour worth testing, and a stub that
    silently swallowed it would let that warning be deleted without any test
    noticing.
    """
    from metascrub import gui as gui_module

    dialogs = []

    def _capture(kind):
        def _show(title, message, *args, **kwargs):
            dialogs.append((kind, message))
            return True
        return _show

    monkeypatch.setattr(gui_module.messagebox, "showwarning", _capture("warning"))
    monkeypatch.setattr(gui_module.messagebox, "showerror", _capture("error"))
    monkeypatch.setattr(gui_module.messagebox, "askokcancel", _capture("confirm"))

    window = tk.Toplevel(tk_root)
    window.withdraw()
    win = ScrubberWindow(window)
    win.dialogs = dialogs
    yield win
    window.destroy()


def _run_to_completion(win, scrub: bool, timeout: float = 120.0) -> None:
    """
    Start a run and pump the Tk event loop until the worker is finished.

    The queue is drained by the same _drain_queue callback the live window
    uses, so this exercises the real main-thread hand-off rather than a test
    specific shortcut.
    """
    import time

    win._start(scrub=scrub)
    deadline = time.time() + timeout
    while time.time() < deadline:
        win.root.update()
        if win._worker is not None and not win._worker.is_alive() \
                and win._queue.empty() and len(win.results) == len(win.paths):
            break
        time.sleep(0.01)
    win.root.update()
    assert len(win.results) == len(win.paths), "run did not finish in time"


# QUEUEING

def test_queue_skips_backups_and_duplicates(window, tmp_path):
    path, _ = build(".pdf", tmp_path)
    with open(path + ".backup", "wb") as fh:
        fh.write(b"pretend backup")

    window._queue_paths([str(tmp_path)])
    queued = [os.path.basename(p) for p in window.paths]

    assert any(name.endswith(".pdf") for name in queued)
    assert not any(name.endswith(".backup") for name in queued), (
        "a .backup is the only remaining copy of the original and must never "
        "be queued for scrubbing"
    )

    before = len(window.paths)
    window._queue_paths([path])
    assert len(window.paths) == before, "the same file was queued twice"


def test_clear_empties_queue_and_table(window, tmp_path):
    build(".pdf", tmp_path)
    window._queue_paths([str(tmp_path)])
    assert window.paths

    window._clear()
    assert window.paths == []
    assert window.results == {}
    assert window.tree.get_children() == ()


# INSPECT IS READ ONLY

def test_inspect_changes_nothing(window, tmp_path):
    path, value = build(".pdf", tmp_path)
    window._queue_paths([path])

    _run_to_completion(window, scrub=False)

    assert contains_anywhere(path, value), "inspect must not modify the file"
    result = window.results[path]
    assert result["inspect_only"] is True
    assert result["carriers"], "inspect should have found the fixture's metadata"


# SCRUBBING

def test_scrub_removes_metadata_and_shows_it(window, tmp_path):
    path, value = build(".pdf", tmp_path)
    window._queue_paths([path])

    _run_to_completion(window, scrub=True)

    assert not contains_anywhere(path, value), "metadata survived a GUI scrub"
    result = window.results[path]
    assert result["status"] == STATUS_SANITIZED

    row = window.tree.item(path)["values"]
    assert row[1] == "SANITIZED"
    assert row[3] == "verified clean"


# THE FAIL-CLOSED CASE

def test_leaking_file_warns_the_user(window, tmp_path, monkeypatch):
    """
    A file that still leaks after a scrub must interrupt the user, not just
    colour a row. This is the one outcome they must not be able to miss.
    """
    from metascrub import gui as gui_module

    path, _ = build(".pdf", tmp_path)
    window._queue_paths([path])

    real_apply = window._apply_result

    def _pretend_it_leaked(result):
        result["verification"] = {"verdict": "residual_found",
                                  "residual_values": ["leaked-value"], "clean": False}
        return real_apply(result)

    monkeypatch.setattr(window, "_apply_result", _pretend_it_leaked)
    _run_to_completion(window, scrub=True)

    assert any(kind == "warning" for kind, _ in window.dialogs), (
        "a still-leaking file produced no dialog; the user would not be told"
    )
    assert "STILL LEAKING" in window.status_label.cget("text")


def test_clean_run_does_not_nag(window, tmp_path):
    """The converse: a fully successful run must not raise a dialog at all."""
    path, _ = build(".pdf", tmp_path)
    window._queue_paths([path])

    _run_to_completion(window, scrub=True)

    assert window.dialogs == [], f"unexpected dialog on a clean run: {window.dialogs}"


def test_window_disables_scrubbing_when_metadata_is_unreadable(window, monkeypatch):
    """
    The GUI mirror of the CLI fail-open regression. With exiftool unusable the
    Scrub button must be disabled and the header must say why.
    """
    def _no_helper(_self):
        raise RuntimeError("exiftool binary not usable: not found on PATH")

    monkeypatch.setattr(
        type(exif_io.session()), "helper", property(_no_helper), raising=True
    )
    window._refresh_engine_status()

    assert window.readable is False
    assert str(window.scrub_button.cget("state")) == "disabled"
    assert "exiftool" in window.engine_label.cget("text").lower()
    assert "winget" in window.status_label.cget("text").lower(), (
        "the window must say how to fix it, not just that it is broken"
    )


def test_inspect_reports_unreadable_rather_than_empty(window, tmp_path, monkeypatch):
    """`cannot read` and `carries nothing` must not render the same."""
    path, _ = build(".pdf", tmp_path)
    window._queue_paths([path])

    def _no_helper(_self):
        raise RuntimeError("exiftool binary not usable: not found on PATH")

    monkeypatch.setattr(
        type(exif_io.session()), "helper", property(_no_helper), raising=True
    )
    _run_to_completion(window, scrub=False)

    result = window.results[path]
    assert result["status"] == STATUS_ERROR
    assert "cannot read metadata" in result["error"]
    assert not result.get("carriers")


# VERDICT RENDERING

@pytest.mark.parametrize("result,expected", [
    ({}, "not verified"),
    ({"verification": {"verdict": "verified_clean"}}, "verified clean"),
    ({"verification": {"verdict": "carriers_remain"}}, "carriers remain"),
    ({"verification": {"verdict": "unverified"}}, "not verified"),
    ({"verification": {"verdict": "residual_found",
                       "residual_values": ["a", "b"]}}, "STILL LEAKING (2)"),
    ({"verification": {"verdict": "verified_clean"},
      "completeness": "partial", "status": STATUS_SANITIZED},
     "clean (partial format)"),
])
def test_verdict_text_never_renders_blank_for_an_unverified_file(result, expected):
    """
    A blank verification cell reads as "fine". An unverified file is not fine,
    so every state must render as words.
    """
    text = _verdict_text(result)
    assert text == expected
    assert text.strip(), "an empty cell would read as success"


# DRAG AND DROP

def test_drop_payload_with_spaces_is_split_into_real_paths(window):
    """
    A dropped path containing spaces arrives brace-wrapped as a Tcl list.
    Splitting on whitespace would turn one real path into several nonexistent
    ones, and the drop would silently add nothing.
    """
    parsed = window._split_drop("{C:/two words/a.pdf} C:/b.pdf")
    assert parsed == ["C:/two words/a.pdf", "C:/b.pdf"]

    single = window._split_drop("{C:/a folder with spaces/report.pdf}")
    assert single == ["C:/a folder with spaces/report.pdf"]

    plain = window._split_drop("C:/nospaces.pdf")
    assert plain == ["C:/nospaces.pdf"]


def test_dropping_real_files_queues_them(window, tmp_path):
    """The whole point of the dependency: a drop must actually enqueue."""
    path, _ = build(".pdf", tmp_path)

    class _Event:
        data = "{" + path + "}"

    window._on_drop(_Event())
    assert path in window.paths


def test_drop_of_a_folder_expands_it(window, tmp_path):
    build(".pdf", tmp_path)
    build(".docx", tmp_path)

    class _Event:
        data = "{" + str(tmp_path) + "}"

    window._on_drop(_Event())
    assert len(window.paths) == 2


@pytest.mark.skipif(not gui_module._HAVE_DND, reason="tkinterdnd2 not installed")
def test_drag_and_drop_is_live_on_a_dnd_root(window):
    """With the dependency installed and a TkinterDnD root, DnD must be on."""
    assert window.dnd_enabled is True
    assert "Drop files here" in window.drop_hint.cget("text")


def test_window_survives_when_drop_registration_fails(tk_root, monkeypatch):
    """
    Importable tkinterdnd2 is not enough: its Tcl commands exist only on a
    TkinterDnD root. On a plain tk.Tk() root - which is what a test harness or
    an application embedding this window would hand it - registration raises
    `invalid command name "tkdnd::drop_target"`. Before this was handled,
    installing the dependency broke every such window at construction.

    The failure is injected at the registration call rather than by building a
    plain root, because a second live Tk interpreter in one process fails
    outright and would break every other GUI test in the run.
    """
    import tkinter.ttk as ttk_mod

    def _refuse(self, *args, **kwargs):
        raise tk.TclError('invalid command name "tkdnd::drop_target"')

    monkeypatch.setattr(ttk_mod.Treeview, "drop_target_register", _refuse,
                        raising=False)

    win = ScrubberWindow(tk.Toplevel(tk_root))
    assert win.dnd_enabled is False
    assert "Add Files" in win.drop_hint.cget("text")
    assert "Drop files here" not in win.drop_hint.cget("text")
