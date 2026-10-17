# Fixtures for semgrep/test-determinism.yaml, exercised by `semgrep --test` in the
# SAST job. `ruleid:` asserts the NEXT line MUST match; `ok:` asserts it must NOT.
# Each rule has a shape it refuses and the shape the Determinism contract asks for.

import asyncio
import datetime
import importlib
import os
import random
import sys
import time
import unittest.mock
from datetime import date, timezone
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
