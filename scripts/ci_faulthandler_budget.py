"""Dump every thread's stack only for a test that outlived its own timeout.

faulthandler's watchdog thread walks every thread's frames without the GIL.
When the main thread is still running Python, the walk can follow a frame that
was just popped and segfault; that thread blocks every signal, so the kernel
delivers the SIGSEGV with its default action and the process dies without a
report. pytest's ``faulthandler_timeout`` arms one fixed deadline for every
test, so a test allowed a longer ``pytest.mark.timeout`` that is still working
past it can be killed by its own diagnostic.

This plugin arms the dump at the test's own pytest-timeout plus ``--faulthandler-grace``
seconds, over the same span pytest-timeout times (the whole protocol, or the call
alone for ``func_only``). pytest-timeout's SIGALRM has already failed a test that is
still running Python by then, so the dump fires only for a test stuck where SIGALRM
cannot reach it (in C, or without the GIL), and names its frame.
"""

from __future__ import annotations

import faulthandler
import os
import sys

import pytest

_FD_KEY = pytest.StashKey[int]()


def pytest_addoption(parser):
    parser.addoption(
        "--faulthandler-grace",
        type=float,
        default=0.0,
        help="Dump all thread stacks this many seconds after a test's pytest-timeout (0: off)",
    )


def pytest_configure(config):
    if config.getoption("faulthandler_grace") > 0:
        # pytest-xdist replaces sys.stderr with an object that has no real fd.
        assert sys.__stderr__ is not None
        config.stash[_FD_KEY] = os.dup(sys.__stderr__.fileno())


def pytest_unconfigure(config):
    fd = config.stash.get(_FD_KEY, None)
    if fd is not None:
        faulthandler.cancel_dump_traceback_later()
        os.close(fd)
        del config.stash[_FD_KEY]


def dump_after(item, func_only):
    """Seconds after which *item* is dumped in this span, or None for no dump.

    shortcut: a test that times out and then spends more than the grace in a
    Python teardown is dumped too, which can end its worker after the test has
    already failed; widen the grace if a log ever shows that.
    """
    fd = item.config.stash.get(_FD_KEY, None)
    if fd is None:
        return None
    import pytest_timeout

    settings = pytest_timeout._get_item_settings(item)
    if not settings.timeout or settings.timeout <= 0 or bool(settings.func_only) != func_only:
        return None
    return settings.timeout + item.config.getoption("faulthandler_grace")


def _armed(item, func_only):
    seconds = dump_after(item, func_only)
    if seconds is None:
        return False
    faulthandler.dump_traceback_later(seconds, file=item.config.stash[_FD_KEY])
    return True


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    armed = _armed(item, func_only=False)
    try:
        yield
    finally:
        if armed:
            faulthandler.cancel_dump_traceback_later()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    armed = _armed(item, func_only=True)
    try:
        yield
    finally:
        if armed:
            faulthandler.cancel_dump_traceback_later()
