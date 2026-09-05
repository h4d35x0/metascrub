"""
Tkinter desktop front end.

Design constraints, each for a reason:

1. **The GUI is a view over the same MetadataScrubber the CLI drives.** It does
   not reimplement dispatch, verification or status handling. A second copy of
   that logic drifting from the first is the exact failure already visible in
   the the parent project repo, where gui/sanitizer.py is a stale fork of cli/sanitizer.py.

2. **Tkinter, from the standard library.** No new dependency for a tool whose
   value is being small and auditable. Drag-and-drop upgrades itself if
   tkinterdnd2 happens to be installed and degrades to the Add buttons if not.

3. **Scrubbing runs on a worker thread and never touches a widget.** Tk is not
   thread safe. Results cross back over a Queue and are drained by a periodic
   poll on the main thread, so a large directory cannot freeze the window and a
   background thread cannot corrupt the widget tree.

4. **The window refuses to scrub when metadata cannot be read.** A GUI that
   renders a green CLEAN row for a file nothing examined is worse than no GUI,
   because the reassurance is what the user takes away. Engine status is checked
   at startup, shown in the header, and re-checked before every run.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import traceback
from typing import Dict, List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import exif_io, theme
from .capabilities import CAPABILITIES, DEFERRED, Completeness, spec_for
from .engines import engine_status
from .scrubber import (
    STATUS_CLEAN, STATUS_DEFERRED, STATUS_ERROR, STATUS_SANITIZED,
    STATUS_UNSUPPORTED, MetadataScrubber,
)

# Optional. Real OS drag-and-drop if the package is present, Add buttons if not.
try:  # pragma: no cover - presence depends on the machine, not on logic
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _HAVE_DND = True
except Exception:  # noqa: BLE001 - any import failure means "no drag and drop"
    DND_FILES = None
    TkinterDnD = None
    _HAVE_DND = False


logger = logging.getLogger("metascrub.gui")
logger.addHandler(logging.NullHandler())

APP_TITLE = "metascrub"

# Row colours come from the theme, which owns the palette and its contrast
# floor. Deliberately not a green/red binary: DEFERRED and SKIPPED are their
# own states, and flattening them into "ok" or "bad" would tell the user
# something the tool does not actually know.
#
# Colour is REINFORCEMENT here, never the only channel. Measured: DANGER and OK
# differ by a contrast ratio of 1.07, so they are nearly identical in
# luminance and anyone with a red/green deficiency cannot tell them apart. The
# Status column ("SANITIZED" vs "FAILED") and the Verification column
# ("verified clean" vs "STILL LEAKING") carry the distinction as text, and
# tests/test_theme.py asserts that they do.
_PALETTE = theme.status_colours()
_LABEL = {
    STATUS_SANITIZED: "SANITIZED",
    STATUS_CLEAN: "CLEAN",
    STATUS_UNSUPPORTED: "SKIPPED",
    STATUS_DEFERRED: "DEFERRED",
    STATUS_ERROR: "FAILED",
}


def _verdict_text(result: Dict) -> str:
    """
    One short cell describing what verification actually established.

    An absent verification is rendered as "not verified" rather than as blank.
    Blank reads as "fine"; it is not.
    """
    verification = result.get("verification") or {}
    verdict = verification.get("verdict")
    if not verdict:
        return "not verified"
    if verdict == "verified_clean":
        if result.get("completeness") == Completeness.PARTIAL.value \
                and result.get("status") == STATUS_SANITIZED:
            return "clean (partial format)"
        return "verified clean"
    if verdict == "residual_found":
        count = len(verification.get("residual_values", []))
        return f"STILL LEAKING ({count})"
    if verdict == "carriers_remain":
        return "carriers remain"
    return "not verified"


class ScrubberWindow:
    """The whole application. One window, one worker at a time."""

    def __init__(self, root: tk.Misc) -> None:
        self.root = root
        self.paths: List[str] = []
        self.results: Dict[str, Dict] = {}
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None

        root.title(APP_TITLE)
        root.geometry("940x560")
        root.minsize(720, 420)

        # Applied before any widget is built, so every widget picks it up. A
        # theming failure must not cost the window, so it degrades to whatever
        # Tk's default is rather than propagating.
        try:
            self.style = theme.apply(root)
        except tk.TclError as exc:  # pragma: no cover - theme ships with Tk
            logger.debug("could not apply theme: %s", exc)
            self.style = ttk.Style(root)

        self._build_header()
        self._build_table()
        self._build_controls()
        self._build_status_bar()

        self._refresh_engine_status()

        # The drain loop reschedules itself forever. Hold its id and stop it
        # when the window goes away, otherwise a destroyed window keeps a live
        # Tk timer pointing at dead widgets. That surfaced as
        # "RuntimeError: main thread is not in main loop" during interpreter
        # teardown once enough windows had been created and destroyed.
        self._drain_id: Optional[str] = None
        self._closed = False
        self.root.bind("<Destroy>", self._on_destroy, add="+")
        self._schedule_drain()

    # LAYOUT

    def _build_header(self) -> None:
        bar = ttk.Frame(self.root, padding=(12, 10, 12, 6))
        bar.pack(fill="x")

        ttk.Label(bar, text=APP_TITLE, style="Title.TLabel").pack(side="left")

        self.engine_label = ttk.Label(bar, text="checking engines...", style="Hint.TLabel")
        self.engine_label.pack(side="right")
        ttk.Button(bar, text="Recheck", width=9,
                   command=self._refresh_engine_status).pack(side="right", padx=(0, 8))

    def _build_table(self) -> None:
        wrap = ttk.Frame(self.root, padding=(12, 0))
        wrap.pack(fill="both", expand=True)

        # Text is set after the drop target is registered, because whether drag
        # and drop actually works is not knowable until then.
        self.drop_hint = ttk.Label(wrap, text="", style="Hint.TLabel")
        self.drop_hint.pack(anchor="w", pady=(0, 4))

        columns = ("file", "status", "engine", "verdict", "detail")
        self.tree = ttk.Treeview(wrap, columns=columns, show="headings", selectmode="extended")
        for column, heading, width, anchor in (
            ("file", "File", 250, "w"),
            ("status", "Status", 95, "w"),
            ("engine", "Engine", 80, "w"),
            ("verdict", "Verification", 150, "w"),
            ("detail", "Detail", 330, "w"),
        ):
            self.tree.heading(column, text=heading)
            self.tree.column(column, width=width, anchor=anchor, stretch=(column == "detail"))

        scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        for status, colour in _PALETTE.items():
            self.tree.tag_configure(status, foreground=colour)
        self.tree.tag_configure("pending", foreground=theme.DISABLED)

        self.tree.bind("<Double-1>", self._show_detail)
        self.tree.bind("<Delete>", lambda _e: self._remove_selected())

        self.dnd_enabled = self._enable_drag_and_drop()
        self.drop_hint.configure(text=(
            "Drop files here, or use Add Files / Add Folder"
            if self.dnd_enabled else
            "Use Add Files / Add Folder"
            + ("" if _HAVE_DND else "  (pip install tkinterdnd2 for drag and drop)")
        ))

    def _enable_drag_and_drop(self) -> bool:
        """
        Register the file list as a drop target, and report whether it worked.

        Having tkinterdnd2 importable is NOT sufficient: its Tcl commands only
        exist when the root was created as TkinterDnD.Tk(). A ScrubberWindow
        built on a plain tk.Tk() root - which is what a test harness or any
        application embedding this window would hand it - raises
        `invalid command name "tkdnd::drop_target"` instead. Measured
        2026-09-04, immediately after installing tkinterdnd2: every window built
        on a plain root failed to construct at all.

        Losing drag and drop is a missing convenience. Losing the window is not,
        so this degrades instead of propagating.
        """
        if not _HAVE_DND:
            return False
        try:
            self.tree.drop_target_register(DND_FILES)
            self.tree.dnd_bind("<<Drop>>", self._on_drop)
            return True
        except tk.TclError as exc:
            logger.debug("drag and drop unavailable: %s", exc)
            return False

    def _build_controls(self) -> None:
        panel = ttk.Frame(self.root, padding=(12, 8))
        panel.pack(fill="x")

        self.backup_var = tk.BooleanVar(value=True)
        self.reset_times_var = tk.BooleanVar(value=False)
        self.recursive_var = tk.BooleanVar(value=True)

        ttk.Checkbutton(panel, text="Keep backup", variable=self.backup_var).pack(side="left")
        ttk.Checkbutton(panel, text="Reset timestamps",
                        variable=self.reset_times_var).pack(side="left", padx=(12, 0))
        ttk.Checkbutton(panel, text="Recurse folders",
                        variable=self.recursive_var).pack(side="left", padx=(12, 0))

        self.scrub_button = ttk.Button(panel, text="Scrub", command=self._start_scrub)
        self.scrub_button.pack(side="right")
        self.inspect_button = ttk.Button(panel, text="Inspect", command=self._start_inspect)
        self.inspect_button.pack(side="right", padx=(0, 8))
        self.report_button = ttk.Button(panel, text="Save report...",
                                        command=self._save_report, state="disabled")
        self.report_button.pack(side="right", padx=(0, 8))

        ttk.Button(panel, text="Clear", command=self._clear).pack(side="right", padx=(0, 8))
        ttk.Button(panel, text="Add Folder",
                   command=self._add_folder).pack(side="right", padx=(0, 8))
        ttk.Button(panel, text="Add Files",
                   command=self._add_files).pack(side="right", padx=(0, 8))

    def _build_status_bar(self) -> None:
        bar = ttk.Frame(self.root, padding=(12, 0, 12, 10))
        bar.pack(fill="x")
        self.progress = ttk.Progressbar(bar, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True)
        self.status_label = ttk.Label(bar, text="No files queued.", style="Status.TLabel")
        self.status_label.pack(side="right", padx=(10, 0))

    # ENGINE STATUS

    def _refresh_engine_status(self) -> None:
        """
        Check what can actually run, and say so before anything is queued.

        The exiftool probe is separate from the per-engine check on purpose:
        every engine reads its baseline metadata through exiftool even when it
        writes with pikepdf, zipfile or ffmpeg, so exiftool being absent
        disables the whole tool rather than just the image formats.
        """
        statuses = engine_status()
        broken = {name: reason for name, reason in statuses.items() if reason != "ready"}
        self.readable, read_reason = exif_io.session().probe()

        if not self.readable:
            self.engine_label.configure(
                text="exiftool missing - scrubbing disabled", style="EngineBad.TLabel")
            self.scrub_button.configure(state="disabled")
            self._set_status(
                "Cannot scrub: " + read_reason
                + "   Install with:  winget install OliverBetz.ExifTool"
            )
            return

        self.scrub_button.configure(state="normal")
        if broken:
            self.engine_label.configure(
                text=f"{len(statuses) - len(broken)}/{len(statuses)} engines ready: "
                     + ", ".join(sorted(broken)) + " unavailable",
                style="EngineWarn.TLabel")
        else:
            self.engine_label.configure(
                text=f"all {len(statuses)} engines ready", style="EngineOK.TLabel")

    # FILE QUEUE

    def _add_files(self) -> None:
        extensions = " ".join(f"*{ext}" for ext in sorted(CAPABILITIES))
        chosen = filedialog.askopenfilenames(
            title="Add files",
            filetypes=[("Supported files", extensions), ("All files", "*.*")],
        )
        self._queue_paths(list(chosen))

    def _add_folder(self) -> None:
        folder = filedialog.askdirectory(title="Add folder")
        if folder:
            self._queue_paths([folder])

    def _on_drop(self, event) -> None:
        """
        Handle a dropped selection.

        The payload is a Tcl list, not a plain string: a path containing spaces
        arrives brace-wrapped as `{C:/two words/a.pdf} C:/b.pdf`. Splitting on
        whitespace would turn one real path into several nonexistent ones and
        the drop would silently add nothing, so the Tcl interpreter does the
        parsing.
        """
        self._queue_paths(self._split_drop(event.data))

    def _split_drop(self, data) -> List[str]:
        """Split a drop payload into paths. Separated out so it can be tested."""
        if isinstance(data, (list, tuple)):
            return [str(item) for item in data]
        try:
            return [str(item) for item in self.root.tk.splitlist(data)]
        except tk.TclError:
            # A payload Tcl cannot parse is better handled as one raw path than
            # discarded: _queue_paths drops anything that is not a real file.
            return [str(data)]

    def _queue_paths(self, incoming: List[str]) -> None:
        """
        Expand folders and add files not already queued.

        Backups are filtered out here for the same reason sanitize_directory
        filters them: a .backup is the only remaining copy of the original, and
        scrubbing it defeats the point of having made it.
        """
        added = 0
        for raw in incoming:
            path = os.path.abspath(raw.strip("{}"))
            if os.path.isdir(path):
                for found in self._walk(path):
                    added += self._add_one(found)
            elif os.path.isfile(path):
                added += self._add_one(path)

        if added:
            self._set_status(f"{len(self.paths)} file(s) queued.")
        elif incoming:
            self._set_status("Nothing added: already queued, or no readable files found.")

    def _walk(self, folder: str) -> List[str]:
        found: List[str] = []
        if self.recursive_var.get():
            for root, _dirs, files in os.walk(folder):
                found.extend(os.path.join(root, name) for name in files)
        else:
            found.extend(
                os.path.join(folder, name) for name in os.listdir(folder)
                if os.path.isfile(os.path.join(folder, name))
            )
        return sorted(found)

    def _add_one(self, path: str) -> int:
        if path.endswith(".backup") or path in self.paths:
            return 0
        self.paths.append(path)
        spec = spec_for(path)
        if spec:
            engine, detail = spec.engine.value, spec.note or ""
        else:
            engine = "-"
            detail = DEFERRED.get(os.path.splitext(path.lower())[1], "format not handled")
        self.tree.insert(
            "", "end", iid=path,
            values=(os.path.basename(path), "queued", engine, "", detail),
            tags=("pending",),
        )
        return 1

    def _remove_selected(self) -> None:
        for iid in self.tree.selection():
            self.tree.delete(iid)
            if iid in self.paths:
                self.paths.remove(iid)
            self.results.pop(iid, None)
        self._set_status(f"{len(self.paths)} file(s) queued.")

    def _clear(self) -> None:
        self.paths.clear()
        self.results.clear()
        self.tree.delete(*self.tree.get_children())
        self.progress.configure(value=0)
        self.report_button.configure(state="disabled")
        self._set_status("No files queued.")

    # RUNNING

    def _start_inspect(self) -> None:
        self._start(scrub=False)

    def _start_scrub(self) -> None:
        if not self.paths:
            self._set_status("Nothing queued.")
            return
        # Re-check rather than trusting the startup probe: exiftool may have
        # been installed, or removed, since the window opened.
        self._refresh_engine_status()
        if not self.readable:
            messagebox.showerror(
                APP_TITLE,
                "Metadata cannot be read on this machine, so nothing can be "
                "verified and nothing will be scrubbed.\n\n"
                "Install exiftool, then press Recheck:\n\n"
                "    winget install OliverBetz.ExifTool",
            )
            return
        count = len(self.paths)
        if self.backup_var.get():
            question = f"Scrub {count} file(s) in place? Backups will be kept alongside them."
        else:
            question = (f"Scrub {count} file(s) in place with NO BACKUP?\n\n"
                        "The originals cannot be recovered.")
        if messagebox.askokcancel(APP_TITLE, question):
            self._start(scrub=True)

    def _start(self, scrub: bool) -> None:
        if self._worker and self._worker.is_alive():
            self._set_status("Already running.")
            return
        if not self.paths:
            self._set_status("Nothing queued.")
            return

        self._set_busy(True)
        self.progress.configure(value=0, maximum=len(self.paths))
        targets = list(self.paths)

        # Read every Tk variable HERE, on the main thread, and hand the worker
        # plain Python values. Tk is not thread safe, and a widget read from a
        # worker deadlocks against the main loop rather than failing: the run
        # simply never finishes and the window never repaints. Measured
        # 2026-09-04 - calling backup_var.get() inside the worker hung every
        # run indefinitely.
        options = {
            "backup": self.backup_var.get(),
            "reset_times": self.reset_times_var.get(),
        }
        self._worker = threading.Thread(
            target=self._run, args=(targets, scrub, options), daemon=True
        )
        self._worker.start()

    def _run(self, targets: List[str], scrub: bool, options: Dict) -> None:
        """
        Worker body. Runs off the main thread and touches no widget: every
        value it needs arrives in `options`, and every outcome goes back through
        the queue.
        """
        try:
            with MetadataScrubber(
                backup=options["backup"],
                reset_times=options["reset_times"],
            ) as scrubber:
                for path in targets:
                    try:
                        if scrub:
                            result = scrubber.sanitize_file(path, remove_all=True)
                        else:
                            result = self._inspect_one(path)
                    except Exception as exc:  # noqa: BLE001 - a worker must not die silently
                        result = {
                            "file": path,
                            "status": STATUS_ERROR,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    self._queue.put(("result", result))
        except Exception:  # noqa: BLE001
            self._queue.put(("fatal", traceback.format_exc()))
        finally:
            self._queue.put(("done", scrub))

    def _inspect_one(self, path: str) -> Dict:
        """Read-only: report what the file carries, change nothing."""
        read = exif_io.session().read(path)
        spec = spec_for(path)
        base = {
            "file": path,
            "engine": spec.engine.value if spec else "-",
            "completeness": spec.completeness.value if spec else None,
        }
        if read.failed:
            # An unreadable file is an error, never an empty-and-therefore-clean
            # one. This is the same conflation the scrubber used to make.
            base.update({"status": STATUS_ERROR,
                         "error": f"cannot read metadata: {read.error}"})
            return base

        carriers = sorted(
            key for key in read.metadata
            if (key.split(":", 1)[0] if ":" in key else key)
            not in ("File", "System", "Composite", "ExifTool") and key != "SourceFile"
        )
        if not carriers and read.unparsed:
            # The same conflation one level up: exiftool read the file, could
            # not parse it, and surfaced nothing. Rendering that as CLEAN is a
            # claim about the file made from a fact about the reader.
            base.update({
                "status": STATUS_ERROR,
                "error": "exiftool could not parse this file, so it reported no "
                         "metadata carriers: " + "; ".join(read.parse_failures),
            })
            return base

        base.update({
            "status": STATUS_CLEAN if not carriers else STATUS_SANITIZED,
            "detail": (f"{len(carriers)} metadata tag(s): " + ", ".join(carriers[:6])
                       if carriers else "no metadata carriers found"),
            "inspect_only": True,
            "carriers": carriers,
            "metadata": {k: str(v) for k, v in read.metadata.items()},
        })
        return base

    # MAIN-THREAD QUEUE DRAIN

    def _schedule_drain(self) -> None:
        if self._closed:
            return
        try:
            self._drain_id = self.root.after(100, self._drain_queue)
        except tk.TclError:
            # The widget was destroyed between one tick and the next.
            self._closed = True

    def _on_destroy(self, event) -> None:
        """Stop the drain loop when this window's own widget is destroyed."""
        if event.widget is not self.root:
            return  # a child widget, not the window itself
        self._closed = True
        if self._drain_id is not None:
            try:
                self.root.after_cancel(self._drain_id)
            except tk.TclError:
                pass
            self._drain_id = None

        # Release the Tk variables while the interpreter is still alive.
        # tkinter.Variable.__del__ calls back into Tk, so a BooleanVar that
        # survives until garbage collection is finalised after the loop is gone
        # and raises "RuntimeError: main thread is not in main loop" from a
        # deallocator, where it cannot be caught or acted on. Dropping the
        # references here makes the collection deterministic and quiet.
        #
        # Same root cause as the drain timer above: anything holding a Tk handle
        # past the window's life is a landmine at teardown.
        self._options_at_close = {
            "backup": self._read_var(self.backup_var, True),
            "reset_times": self._read_var(self.reset_times_var, False),
            "recursive": self._read_var(self.recursive_var, True),
        }
        self.backup_var = None
        self.reset_times_var = None
        self.recursive_var = None

    @staticmethod
    def _read_var(var, default):
        """Read a Tk variable that may already be torn down."""
        try:
            return var.get() if var is not None else default
        except (tk.TclError, AttributeError):
            return default

    def _drain_queue(self) -> None:
        if self._closed:
            return
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "result":
                    self._apply_result(payload)
                elif kind == "done":
                    self._finish(payload)
                elif kind == "fatal":
                    self._set_busy(False)
                    messagebox.showerror(APP_TITLE, f"Run failed:\n\n{payload}")
        except queue.Empty:
            pass
        self._schedule_drain()

    def _apply_result(self, result: Dict) -> None:
        path = result.get("file", "")
        self.results[path] = result
        status = result.get("status", STATUS_ERROR)

        if result.get("inspect_only"):
            # Inspect never claims a file was sanitized; it reports what is there.
            label = "HAS METADATA" if result.get("carriers") else "NO METADATA"
            verdict = ""
        else:
            label = _LABEL.get(status, status.upper())
            verdict = _verdict_text(result)

        detail = result.get("error") or result.get("detail") or ""
        if not detail and result.get("completeness") == Completeness.PARTIAL.value \
                and status == STATUS_SANITIZED:
            detail = "PARTIAL: " + (result.get("engine_note") or "residue may remain")

        if self.tree.exists(path):
            self.tree.item(
                path,
                values=(os.path.basename(path), label,
                        result.get("engine", "-"), verdict, detail),
                tags=(status,),
            )
        self.progress.step(1)

    def _finish(self, scrubbed: bool) -> None:
        self._set_busy(False)
        self.report_button.configure(state="normal" if self.results else "disabled")

        counts: Dict[str, int] = {}
        for result in self.results.values():
            counts[result.get("status", STATUS_ERROR)] = \
                counts.get(result.get("status", STATUS_ERROR), 0) + 1

        if not scrubbed:
            carrying = sum(1 for r in self.results.values() if r.get("carriers"))
            self._set_status(f"Inspected {len(self.results)} file(s); "
                             f"{carrying} carry metadata. Nothing was changed.")
            return

        leaking = [
            r for r in self.results.values()
            if (r.get("verification") or {}).get("verdict") == "residual_found"
        ]
        summary = (f"{counts.get(STATUS_SANITIZED, 0)} sanitized, "
                   f"{counts.get(STATUS_CLEAN, 0)} already clean, "
                   f"{counts.get(STATUS_ERROR, 0)} failed")
        if leaking:
            summary += f"  -  {len(leaking)} STILL LEAKING"
        self._set_status(summary)

        if leaking or counts.get(STATUS_ERROR):
            messagebox.showwarning(
                APP_TITLE,
                f"{summary}.\n\nDouble-click a row for the detail. "
                "A failed or leaking file was NOT cleaned.",
            )

    # DETAIL AND REPORT

    def _show_detail(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        result = self.results.get(selection[0])
        if not result:
            return

        window = tk.Toplevel(self.root)
        window.title(os.path.basename(selection[0]))
        window.geometry("680x440")
        text = tk.Text(window, wrap="none", font=("Consolas", 9))
        scroll = ttk.Scrollbar(window, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        import json
        text.insert("1.0", json.dumps(result, indent=2, default=str))
        text.configure(state="disabled")

    def _save_report(self) -> None:
        if not self.results:
            return
        target = filedialog.asksaveasfilename(
            title="Save report", defaultextension=".json",
            filetypes=[("JSON", "*.json")], initialfile="scrub-report.json",
        )
        if not target:
            return
        scrubber = MetadataScrubber()
        scrubber.generate_sanitization_report(list(self.results.values()), target)
        self._set_status(f"Report written to {target}")

    # SMALL HELPERS

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.inspect_button.configure(state=state)
        self.scrub_button.configure(
            state="disabled" if (busy or not self.readable) else "normal")
        if busy:
            self._set_status("Working...")

    def _set_status(self, message: str) -> None:
        self.status_label.configure(text=message)


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point. Any paths passed on the command line are pre-queued."""
    root = TkinterDnD.Tk() if _HAVE_DND else tk.Tk()
    window = ScrubberWindow(root)
    if argv:
        window._queue_paths(list(argv))

    root.protocol("WM_DELETE_WINDOW", lambda: (exif_io.close_session(), root.destroy()))
    root.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys
    raise SystemExit(main(sys.argv[1:]))
