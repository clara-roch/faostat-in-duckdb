"""Download-concurrency benchmarking (FAOSTATdb.md v0.3 > "Benchmarking of concurrency").

The one concurrency knob in FAOSTATdb is ``build.jobs`` — how many archives are
downloaded in parallel. FAOSTATdb.md deliberately refuses to hard-code an
"optimal" number because the best value depends on the machine and the FAO
server's behaviour on the day. This module measures it empirically: it downloads
the same set of archives at several ``jobs`` levels and reports wall-clock time
and throughput, so a user can pick a value with evidence instead of a guess.

Design note — **the scheduling/timing core here is network-free and injectable**.
:func:`run_download_benchmark` takes a ``downloader`` callable and a ``timer``, so
the concurrency logic and the throughput arithmetic can be unit-tested offline
(the real network fetch is only wired in by the CLI). This keeps the promise that
CI never triggers a real FAOSTAT download.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from time import perf_counter
from typing import Callable


@dataclass(frozen=True)
class BenchTask:
    """One archive to (re)download during a benchmark."""

    code: str
    url: str


@dataclass(frozen=True)
class BenchResult:
    """The outcome of downloading the task set once at a given concurrency level."""

    jobs: int
    seconds: float
    total_bytes: int
    files: int
    failures: int

    @property
    def megabytes(self) -> float:
        """Total bytes expressed in decimal megabytes (MB, 1e6 bytes)."""
        return self.total_bytes / 1_000_000

    @property
    def mb_per_second(self) -> float:
        """Aggregate throughput in MB/s (0.0 if the run took no measurable time)."""
        return self.megabytes / self.seconds if self.seconds > 0 else 0.0


def run_download_benchmark(
    tasks: list[BenchTask],
    jobs_levels: list[int],
    *,
    downloader: Callable[[BenchTask], int],
    timer: Callable[[], float] = perf_counter,
    before_level: Callable[[int], None] | None = None,
) -> list[BenchResult]:
    """Download ``tasks`` once per level in ``jobs_levels``; time each level.

    For each concurrency level, ``downloader(task)`` is called for every task in a
    :class:`~concurrent.futures.ThreadPoolExecutor` with ``max_workers`` set to
    that level. ``downloader`` returns the number of bytes fetched (summed for
    throughput); an exception from it counts as a failure and contributes no
    bytes. ``before_level(jobs)`` runs once before each level — the CLI uses it to
    clear the cache directory so every level performs a real, cold download.

    Timing uses ``timer`` (default ``time.perf_counter``); both ``downloader`` and
    ``timer`` are injected so the whole routine is deterministically testable
    offline. Returns one :class:`BenchResult` per level, in the given order.
    """
    results: list[BenchResult] = []
    for jobs in jobs_levels:
        if before_level is not None:
            before_level(jobs)

        total_bytes = 0
        failures = 0
        workers = max(1, jobs)
        start = timer()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(downloader, task) for task in tasks]
            for future in as_completed(futures):
                try:
                    total_bytes += int(future.result())
                except Exception:  # noqa: BLE001 — a failed fetch is a data point
                    failures += 1
        elapsed = timer() - start

        results.append(
            BenchResult(
                jobs=jobs,
                seconds=elapsed,
                total_bytes=total_bytes,
                files=len(tasks) - failures,
                failures=failures,
            )
        )
    return results


def format_bench_table(results: list[BenchResult]) -> str:
    """Render benchmark results as a small aligned text table (no dependencies).

    Marks the fastest (lowest wall-clock) level with a ``*`` so the recommended
    ``--jobs`` value is obvious at a glance.
    """
    if not results:
        return "no benchmark results"

    best = min(results, key=lambda r: r.seconds)
    header = f"{'jobs':>5}  {'wall_s':>8}  {'MB/s':>8}  {'files':>6}  {'fail':>5}"
    lines = [header, "-" * len(header)]
    for r in results:
        marker = " *" if r is best else ""
        lines.append(
            f"{r.jobs:>5}  {r.seconds:>8.2f}  {r.mb_per_second:>8.1f}  "
            f"{r.files:>6}  {r.failures:>5}{marker}"
        )
    lines.append("")
    lines.append(f"fastest: {best.jobs} job(s) at {best.seconds:.2f}s")
    return "\n".join(lines)
