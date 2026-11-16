# Fixtures for semgrep/test-determinism.yaml, exercised by `semgrep --test` in the
# SAST job. `ruleid:` asserts the NEXT line MUST match; `ok:` asserts it must NOT.
# Each rule has a shape it refuses and the shape the Determinism contract asks for.

import asyncio
import contextlib
import datetime
import importlib
import os
import random
import sys
import tempfile
import time
import unittest.mock
from datetime import date, timezone
from tempfile import TemporaryDirectory, mkdtemp
from unittest import mock
from unittest.mock import patch

import pytest
import pytest_asyncio


def test_sleeps(patched, done, subject):
    # ruleid: kirocrew.test-sleep-as-barrier
    time.sleep(0.2)
    # ruleid: kirocrew.test-sleep-as-barrier
    subject.time.sleep(0.2)
    # ok: kirocrew.test-sleep-as-barrier
    time.sleep(0)
    with patched:
        # ruleid: kirocrew.test-sleep-as-barrier
        time.sleep(0.1)
    while not done():
        # ok: kirocrew.test-sleep-as-barrier
        time.sleep(0.01)
    for _ in range(50):
        if done():
            break
        # ok: kirocrew.test-sleep-as-barrier
        time.sleep(0.01)
    # A sleep the loop body asserts right after is a barrier run N times, not a poll.
    for _ in range(3):
        # ruleid: kirocrew.test-sleep-as-barrier
        time.sleep(0.05)
        assert done()
    while not done():
        # ruleid: kirocrew.test-sleep-as-barrier
        subject.time.sleep(0.05)
        assert patched


async def test_async_sleeps(subject, done):
    # ruleid: kirocrew.test-sleep-as-barrier
    await asyncio.sleep(0.05)
    # ruleid: kirocrew.test-sleep-as-barrier
    await subject.asyncio.sleep(0.05)
    # ok: kirocrew.test-sleep-as-barrier
    await asyncio.sleep(0)
    while not done():
        # ok: kirocrew.test-sleep-as-barrier
        await asyncio.sleep(0.01)
    for _ in range(3):
        # ruleid: kirocrew.test-sleep-as-barrier
        await asyncio.sleep(0.05)
        assert done()


def slow_stub():
    # ok: kirocrew.test-sleep-as-barrier
    time.sleep(0.5)


def test_undo(monkeypatch):
    # ruleid: kirocrew.test-bare-monkeypatch-undo
    monkeypatch.undo()
    with monkeypatch.context() as mp:
        # ok: kirocrew.test-bare-monkeypatch-undo
        mp.undo()


def test_reload(module):
    # ruleid: kirocrew.test-in-process-reload
    importlib.reload(module)
    # ok: kirocrew.test-in-process-reload
    importlib.import_module("json")
    # An eviction is not a reload; test/test_flake_pattern_ratchet.py's K5 counts it.
    # ok: kirocrew.test-in-process-reload
    del sys.modules["json"]


def test_clock_patches(monkeypatch, subject):
    # ruleid: kirocrew.test-stdlib-clock-rebound
    monkeypatch.setattr(time, "monotonic", lambda: 0.0)
    # ruleid: kirocrew.test-stdlib-clock-rebound
    monkeypatch.setattr("time.time", lambda: 0.0)
    # The time module reached through another module's binding is the same
    # module, so the same whole-worker patch.
    # ruleid: kirocrew.test-stdlib-clock-rebound
    monkeypatch.setattr(subject.time, "monotonic", lambda: 0.0)
    # ruleid: kirocrew.test-stdlib-clock-rebound
    monkeypatch.setattr("kiro_crew.acp.client.time.monotonic", lambda: 0.0)
    # ruleid: kirocrew.test-stdlib-clock-rebound
    mock.patch.object(time, "sleep")
    # ruleid: kirocrew.test-stdlib-clock-rebound
    unittest.mock.patch("time.sleep")
    # ruleid: kirocrew.test-stdlib-clock-rebound
    mock.patch("kiro_crew.acp.client.time.monotonic")
    # ruleid: kirocrew.test-stdlib-clock-rebound
    patch.object(subject.time, "monotonic", lambda: 0.0)
    # Rebinding the module's own `time` name is the shape asked for (D2).
    # ok: kirocrew.test-stdlib-clock-rebound
    monkeypatch.setattr(subject, "time", object())
    # ok: kirocrew.test-stdlib-clock-rebound
    monkeypatch.setattr("kiro_crew.acp.client.time", object())
    # ok: kirocrew.test-stdlib-clock-rebound
    monkeypatch.setattr("kiro_crew.acp.client.lifetime.budget", 3)
    # The rule's patterns do not reach these clock and sleep patches;
    # test/test_flake_pattern_ratchet.py's K3 counts them instead.
    # ok: kirocrew.test-stdlib-clock-rebound
    monkeypatch.setattr(asyncio, "sleep", lambda _seconds: None)
    # ok: kirocrew.test-stdlib-clock-rebound
    monkeypatch.setattr(datetime, "datetime", object())


def test_local_time(subject):
    # ruleid: kirocrew.test-naive-local-time
    assert date.today()
    # ruleid: kirocrew.test-naive-local-time
    assert datetime.datetime.now()
    # ruleid: kirocrew.test-naive-local-time
    assert datetime.datetime.today()
    # ruleid: kirocrew.test-naive-local-time
    assert subject.datetime.now()
    # ok: kirocrew.test-naive-local-time
    assert datetime.datetime.now(timezone.utc)
    # ok: kirocrew.test-naive-local-time
    assert subject.datetime.now(timezone.utc)


def test_randomness():
    # ruleid: kirocrew.test-unseeded-random
    random.randint(1, 6)
    # ruleid: kirocrew.test-unseeded-random
    os.urandom(16)
    # ruleid: kirocrew.test-unseeded-random
    random.seed(20261005)
    # ok: kirocrew.test-unseeded-random
    random.Random(20261005).randint(1, 6)


def test_shared_tmp_dir_mkdtemp(tmp_path, np):
    # ruleid: kirocrew.test-shared-tmp-dir
    a = tempfile.mkdtemp()
    # ruleid: kirocrew.test-shared-tmp-dir
    b = mkdtemp(prefix="kc-")
    # ruleid: kirocrew.test-shared-tmp-dir
    c = np.tempfile.mkdtemp()
    # ruleid: kirocrew.test-shared-tmp-dir
    d = tempfile.mkdtemp(dir="/tmp")
    # ok: kirocrew.test-shared-tmp-dir
    e = tempfile.mkdtemp(dir=tmp_path)
    # ok: kirocrew.test-shared-tmp-dir
    f = tempfile.mkdtemp(dir=str(tmp_path))
    # ok: kirocrew.test-shared-tmp-dir
    g = tmp_path / "root"
    return a, b, c, d, e, f, g


def test_shared_tmp_dir_temporary_directory(tmp_path, np, keep):
    # ruleid: kirocrew.test-shared-tmp-dir
    held = tempfile.TemporaryDirectory()
    # ruleid: kirocrew.test-shared-tmp-dir
    name = tempfile.TemporaryDirectory().name
    # ruleid: kirocrew.test-shared-tmp-dir
    entered = tempfile.TemporaryDirectory().__enter__()
    # ruleid: kirocrew.test-shared-tmp-dir
    imported = TemporaryDirectory()
    # ruleid: kirocrew.test-shared-tmp-dir
    chained = np.tempfile.TemporaryDirectory()
    # ok: kirocrew.test-shared-tmp-dir
    with tempfile.TemporaryDirectory() as scoped:
        # ruleid: kirocrew.test-shared-tmp-dir
        nested = tempfile.TemporaryDirectory()
    # ok: kirocrew.test-shared-tmp-dir
    with TemporaryDirectory(prefix="kc-") as scoped_imported, open(scoped) as fh:
        pass
    # ok: kirocrew.test-shared-tmp-dir
    with tempfile.TemporaryDirectory():
        pass
    # ok: kirocrew.test-shared-tmp-dir
    with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
        pass
    with mock.patch("os.getcwd"):
        # ok: kirocrew.test-shared-tmp-dir
        with tempfile.TemporaryDirectory() as inner:
            pass
        # ruleid: kirocrew.test-shared-tmp-dir
        patched = tempfile.TemporaryDirectory()
    # ruleid: kirocrew.test-shared-tmp-dir
    keep.append(tempfile.TemporaryDirectory())
    with contextlib.ExitStack() as stack:
        # ok: kirocrew.test-shared-tmp-dir
        stacked = stack.enter_context(tempfile.TemporaryDirectory())
    keep.extend([held, name, entered, imported, chained, nested, scoped_imported])
    keep.extend([fh, stacked, first, second, inner, patched])
    # ruleid: kirocrew.test-shared-tmp-dir
    return tempfile.TemporaryDirectory()


async def test_shared_tmp_dir_async_with():
    # ok: kirocrew.test-shared-tmp-dir
    async with tempfile.TemporaryDirectory() as scoped:
        return scoped


class TestSharedTmpDirSetUp(unittest.TestCase):
    def setUp(self):
        # ruleid: kirocrew.test-shared-tmp-dir
        self.root = tempfile.mkdtemp()
        # ruleid: kirocrew.test-shared-tmp-dir
        self._td = tempfile.TemporaryDirectory()

    @pytest.fixture(autouse=True)
    # ok: kirocrew.test-autouse-shared-monkeypatch
    def _per_test_dirs(self, tmp_path):
        # ok: kirocrew.test-shared-tmp-dir
        self.root = tmp_path / "root"


@pytest.fixture(autouse=True)
# ruleid: kirocrew.test-autouse-shared-monkeypatch
def _pin_shared(monkeypatch):
    # ok: kirocrew.test-autouse-shared-monkeypatch
    monkeypatch.setenv("A", "1")


@pytest_asyncio.fixture(autouse=True)
# ruleid: kirocrew.test-autouse-shared-monkeypatch
async def _pin_shared_async(monkeypatch):
    monkeypatch.setenv("A", "1")


@pytest.fixture(autouse=True)
# ok: kirocrew.test-autouse-shared-monkeypatch
def _pin_floor(_floor_monkeypatch):
    _floor_monkeypatch.setenv("A", "1")
