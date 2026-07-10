"""
Human- and machine-readable progress reporting.

FAOSTATdb.md ("Progress UI: useful, but should degrade gracefully") asks for a
package-manager-style display that *degrades*: rich progress bars when available,
plain lines otherwise, machine-readable JSON for CI, and switches to force ASCII
icons or suppress dynamic output on fussy terminals.

The :class:`Reporter` centralizes all of that behind two verbs:

* ``log(msg)``            — a free-form human line (always to **stderr**).
* ``event(code, stage, status, ...)`` — a structured per-dataset transition,
  rendered as an icon line (human) or a JSON object on **stdout** (``--json``).

Splitting streams (human on stderr, JSON on stdout) means ``faostatdb build
--json > events.jsonl`` yields clean machine-readable output while progress noise
still reaches the terminal.
"""

from __future__ import annotations

import json
import sys
import threading
from contextlib import contextmanager
from typing import Iterator

try:  # rich is an optional extra; importing this module must never require it.
    from rich.console import Console  # type: ignore
    from rich.progress import (  # type: ignore
        BarColumn,
        DownloadColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
    )

    _RICH = True
except ImportError:  # plain fallback
    _RICH = False


# Icon sets: fancy Unicode by default, plain ASCII with --ascii (or when the
# active code page can't encode the glyphs).
_ICONS_UNICODE = {"ok": "✓", "fail": "✗", "active": "→", "info": "…"}
_ICONS_ASCII = {"ok": "[OK]", "fail": "[X]", "active": "[>]", "info": "[..]"}

# Map a per-dataset status string to an icon bucket.
_STATUS_ICON = {
    "success": "ok",
    "imported": "ok",
    "validated": "ok",
    "downloaded": "ok",
    "skipped": "info",
    "failed": "fail",
    "invalid": "fail",
    "downloading": "active",
    "importing": "active",
    "validating": "active",
}


class Reporter:
    """Emits progress in the mode the user asked for.

    Parameters
    ----------
    json_mode:
        Emit one JSON object per :meth:`event` to stdout; suppress fancy UI.
    ascii_mode:
        Use ASCII icons instead of Unicode glyphs.
    no_progress:
        Suppress live/animated progress bars (individual event lines still print).
    """

    def __init__(
        self,
        *,
        json_mode: bool = False,
        ascii_mode: bool = False,
        no_progress: bool = False,
    ) -> None:
        self.json_mode = json_mode
        self.no_progress = no_progress
        # In JSON mode we never render rich UI (stdout is reserved for JSON).
        self._use_rich = _RICH and not json_mode
        self._console = Console(stderr=True) if self._use_rich else None
        # Auto-fall back to ASCII if the terminal encoding can't render the glyphs.
        self.icons = _ICONS_ASCII if (ascii_mode or not _stdout_unicode()) else _ICONS_UNICODE

    @property
    def shows_live_progress(self) -> bool:
        """True if :meth:`download_phase` will render live per-dataset bars.

        When it does, a textual ``download: downloading`` event line is pure
        redundancy — the bar already conveys start/progress. Callers gate that
        line on this so plain, ``--no-progress`` and ``--json`` modes (where the
        bar is absent) still get the transition, but rich terminals don't.
        """
        return self._use_rich and not self.no_progress

    # -- human free-form line ------------------------------------------------
    def log(self, message: str) -> None:
        """Emit a single free-form progress line to stderr."""
        if self.json_mode:
            # Keep stdout pure JSON; still surface the message as an event line.
            self._emit_json({"stage": "log", "message": message})
            return
        if self._console is not None:
            self._console.print(message)
        else:
            print(message, file=sys.stderr, flush=True)

    # -- structured per-dataset transition -----------------------------------
    def event(
        self,
        dataset: str,
        stage: str,
        status: str,
        *,
        rows: int | None = None,
        message: str | None = None,
    ) -> None:
        """Report that ``dataset`` reached ``status`` at ``stage``.

        Human mode prints ``<icon> <CODE>: <message>``; JSON mode prints a compact
        object suitable for logs and CI.
        """
        if self.json_mode:
            obj = {"dataset": dataset, "stage": stage, "status": status}
            if rows is not None:
                obj["rows"] = rows
            if message is not None:
                obj["message"] = message
            self._emit_json(obj)
            return

        icon = self.icons[_STATUS_ICON.get(status, "info")]
        text = message or f"{stage}: {status}"
        self.log(f"{icon} {dataset}: {text}")

    def _emit_json(self, obj: dict) -> None:
        print(json.dumps(obj), flush=True)

    # -- download phase progress --------------------------------------------
    @contextmanager
    def download_phase(self, total: int) -> Iterator["DownloadTracker"]:
        """Context manager wrapping the parallel-download phase.

        Yields a :class:`DownloadTracker`. With rich (and progress enabled) it
        renders live per-dataset bars; otherwise it is a quiet no-op tracker and
        callers rely on :meth:`event` lines for feedback.
        """
        if self._use_rich and not self.no_progress and total > 0:
            progress = Progress(
                SpinnerColumn(),
                TextColumn("[bold]{task.description}"),
                BarColumn(),
                DownloadColumn(),
                console=self._console,
                transient=True,
            )
            with progress:
                yield DownloadTracker(progress)
        else:
            yield DownloadTracker(None)


class DownloadTracker:
    """Per-dataset download bars, backed by a rich ``Progress`` or a no-op.

    Safe to drive from the download worker pool: ``start``/``advance`` are called
    from worker threads while ``finish`` is called from the main ``as_completed``
    loop, so the shared ``_tasks`` map is guarded by a lock (rich's own ``Progress``
    is separately thread-safe, and the no-op path does nothing). The lock — not the
    GIL — is what keeps the map consistent, so this also holds on free-threaded
    Python builds.
    """

    def __init__(self, progress) -> None:
        self._progress = progress
        self._tasks: dict[str, int] = {}
        self._lock = threading.Lock()

    def start(self, code: str, total_bytes: int | None) -> None:
        if self._progress is None:
            return
        task_id = self._progress.add_task(
            code, total=total_bytes if total_bytes else None
        )
        with self._lock:
            self._tasks[code] = task_id

    def advance(self, code: str, done: int, total: int | None) -> None:
        if self._progress is None:
            return
        with self._lock:
            task_id = self._tasks.get(code)
        if task_id is None:
            return
        # completed is absolute bytes-so-far; update total in case it arrived late.
        self._progress.update(task_id, completed=done, total=total if total else None)

    def finish(self, code: str) -> None:
        if self._progress is None:
            return
        with self._lock:
            task_id = self._tasks.pop(code, None)
        if task_id is None:
            return
        self._progress.remove_task(task_id)


def _stdout_unicode() -> bool:
    """True if stderr can encode our Unicode icons (Windows code pages may not)."""
    enc = (getattr(sys.stderr, "encoding", None) or "").lower()
    if not enc:
        return False
    try:
        "✓✗→…".encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


# -- module-level convenience (back-compat) ---------------------------------
_default = Reporter()


def log(message: str) -> None:
    """Module-level shortcut using a default human reporter."""
    _default.log(message)


@contextmanager
def step(label: str) -> Iterator[None]:
    """Context manager that brackets a unit of work with start/done lines."""
    log(f"… {label}")
    try:
        yield
    except Exception:
        log(f"✗ {label}")
        raise
    else:
        log(f"✓ {label}")
