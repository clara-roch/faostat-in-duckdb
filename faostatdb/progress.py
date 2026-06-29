"""Human-readable progress reporting.

Uses ``rich`` when available for live progress; otherwise falls back to plain
line-by-line stderr output. Importing this module never requires ``rich``.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Iterator

try:
    from rich.console import Console  # type: ignore

    _console: "Console | None" = Console(stderr=True)
except ImportError:  # plain fallback
    _console = None


def log(message: str) -> None:
    """Emit a single progress line."""
    if _console is not None:
        _console.print(message)
    else:
        print(message, file=sys.stderr, flush=True)


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
