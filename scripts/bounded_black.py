#!/usr/bin/env python3
"""Run the pinned native Black CLI with one formatting task per child process."""

from __future__ import annotations

import sys
from concurrent.futures import ProcessPoolExecutor
from functools import wraps
from multiprocessing import get_context

BLACK_VERSION = "26.3.1"
# Retention, not concurrency, is what the measured cgroup OOM came from: a worker
# retired after one file holds one file's tree, so its peak does not grow with the
# pool. Raising this ceiling therefore buys wall time without reintroducing that
# accumulation, and the per-worker ceiling below still bounds each child.
MAX_WORKERS = 8
# Per-process virtual address space, NOT a promise about whole-job cgroup RSS.
MEMORY_BYTES = 2 * 1024**3


def limit_memory() -> None:
    # This standalone build tool must not import the application's runtime.
    # RLIMIT_AS is enforceable on Linux; macOS/Windows retain recycling only.
    if sys.platform == "linux":
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        limit = min(
            value for value in (soft, hard, MEMORY_BYTES) if value != resource.RLIM_INFINITY
        )
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


def recycling_executor(max_workers: int) -> ProcessPoolExecutor:
    try:
        return ProcessPoolExecutor(
            max_workers=min(max_workers, MAX_WORKERS),
            mp_context=get_context("spawn"),
            max_tasks_per_child=1,
        )
    except (ImportError, NotImplementedError, OSError) as exc:
        # Black's native factory fallback must not put retained trees in a thread.
        raise RuntimeError("cannot create recycling Black executor") from exc


def run_black(black, concurrency, argv: list[str] | None) -> int:
    verdict = None

    def checked(function, many=False):
        @wraps(function)
        def run(**kwargs):
            nonlocal verdict
            report = kwargs["report"]
            expected = len(kwargs["sources"]) if many else 1
            before = report.change_count + report.same_count + report.failure_count
            function(**kwargs)
            after = report.change_count + report.same_count + report.failure_count
            # Native cancellation omits cancelled futures from the report. Never
            # publish that partial result as clean or as a formatting verdict.
            if after - before != expected:
                raise RuntimeError("incomplete Black report")
            verdict = report.return_code

        return run

    hooks = (
        (black, "reformat_one", checked(black.reformat_one)),
        (concurrency, "reformat_many", checked(concurrency.reformat_many, many=True)),
        (concurrency, "ProcessPoolExecutor", recycling_executor),
        (concurrency, "ThreadPoolExecutor", recycling_executor),
    )
    originals = [(module, name, getattr(module, name)) for module, name, _ in hooks]
    try:
        for module, name, replacement in hooks:
            setattr(module, name, replacement)
        result = black.main.main(args=argv, standalone_mode=False)
        # Click validation also returns 1. Only a complete native Report earns
        # that verdict; exceptions, including those after findings, escape to main.
        if result == 1 and verdict != 1:
            raise RuntimeError("Black exited without a complete formatting verdict")
        return result if result in (0, 1, 123) else 123
    finally:
        for module, name, original in originals:
            setattr(module, name, original)


def main(argv: list[str] | None = None) -> int:
    try:
        # Set before importing Black; spawn inherits the limit across exec. The
        # coordinator is capped too, including native singleton formatting.
        limit_memory()
        import black
        import black.concurrency as concurrency

        if black.__version__ != BLACK_VERSION:
            raise RuntimeError(f"bounded Black requires {BLACK_VERSION}, got {black.__version__}")
        return run_black(black, concurrency, argv)
    except (Exception, KeyboardInterrupt, SystemExit) as exc:
        print(f"bounded Black failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 123


if __name__ == "__main__":
    raise SystemExit(main())
