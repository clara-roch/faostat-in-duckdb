"""Tests for the concurrency-benchmark core (offline, deterministic).

The download step is injected, so these exercise the scheduling, byte/throughput
arithmetic, failure counting and table formatting without any network I/O.
"""

from __future__ import annotations

from faostatdb import bench as bench_mod


def _tasks(*codes):
    return [bench_mod.BenchTask(code=c, url=f"http://example/{c}.zip") for c in codes]


def test_benchmark_counts_bytes_and_files():
    # A fake timer that advances by a fixed amount each call, so wall-clock is
    # deterministic: two calls per level (start/stop) => 1.0s per level.
    ticks = iter(range(1000))
    timer = lambda: float(next(ticks))  # noqa: E731

    def downloader(task):
        return 1_000_000  # 1 MB each

    results = bench_mod.run_download_benchmark(
        _tasks("A", "B", "C"), [1, 2], downloader=downloader, timer=timer
    )
    assert [r.jobs for r in results] == [1, 2]
    for r in results:
        assert r.files == 3
        assert r.failures == 0
        assert r.total_bytes == 3_000_000
        assert r.seconds == 1.0
        assert r.mb_per_second == 3.0  # 3 MB / 1 s


def test_benchmark_records_failures_without_bytes():
    def downloader(task):
        if task.code == "B":
            raise RuntimeError("boom")
        return 500

    results = bench_mod.run_download_benchmark(
        _tasks("A", "B", "C"), [2], downloader=downloader, timer=iter(range(10)).__next__
    )
    (r,) = results
    assert r.failures == 1
    assert r.files == 2
    assert r.total_bytes == 1000  # only A and C contributed


def test_before_level_runs_once_per_level():
    seen = []
    bench_mod.run_download_benchmark(
        _tasks("A"),
        [1, 4, 8],
        downloader=lambda t: 1,
        timer=iter(range(100)).__next__,
        before_level=seen.append,
    )
    assert seen == [1, 4, 8]


def test_format_table_marks_fastest():
    results = [
        bench_mod.BenchResult(jobs=1, seconds=4.0, total_bytes=4_000_000, files=1, failures=0),
        bench_mod.BenchResult(jobs=4, seconds=1.0, total_bytes=4_000_000, files=1, failures=0),
        bench_mod.BenchResult(jobs=8, seconds=2.0, total_bytes=4_000_000, files=1, failures=0),
    ]
    table = bench_mod.format_bench_table(results)
    # The 4-job row is fastest and should carry the marker + the summary line.
    assert "*" in table
    assert "fastest: 4 job(s)" in table
    # Throughput math surfaces in the table (4 MB / 1 s = 4.0 MB/s).
    assert "4.0" in table


def test_format_table_empty():
    assert bench_mod.format_bench_table([]) == "no benchmark results"
