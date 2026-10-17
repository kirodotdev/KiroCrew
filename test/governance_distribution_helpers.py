"""Builders, fakes and fixtures shared by the ``test_governance_distribution*.py`` modules.

The shared constants, the policy-document builders and the fake fetchers, then the
fixtures. ``_hermetic_governance_globals`` is autouse, so every module imports it by
name. The fixtures a test requests by parameter are declared
``@pytest.fixture(name=...)`` and imported under their ``*_fixture`` function names, so
the parameter does not shadow the import.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import traceback
from pathlib import Path

import pytest

from kiro_crew.platform import governance
from kiro_crew.platform import governance_health as health
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform import policy_distribution as pd

#: The import the boot tier must NOT perform on a host that configures no source.
_ENGINE_MODULE = "kiro_crew.platform.policy_distribution"

#: A scheme no built-in handles, so a fake fetcher registered for it cannot be
#: confused with (or shadowed by) the real https/http/file transports.
_TEST_SCHEME = "kctest"
_TEST_SOURCE = f"{_TEST_SCHEME}://policy.example/security_policy.json"

#: A cache age comfortably past the widest fetch window, for the tests whose
#: subject is the fetch rather than the shortcut over it.
_PAST_THE_WINDOW = governance.MIN_REFRESH_INTERVAL_SECS * 100

#: Every status ``refresh_now`` may report. Named states rather than a boolean
#: because the CLI, the viewer and the audit trail all have to tell "nothing
#: changed" apart from "a push was refused".
_REFRESH_STATUSES = (
    pd.REFRESH_NOT_CONFIGURED,
    pd.REFRESH_UNCHANGED,
    pd.REFRESH_APPLIED,
    pd.REFRESH_REJECTED,
    pd.REFRESH_UNREACHABLE,
)


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


def _doc(marker: str = "", **extra: object) -> dict:
    """A minimal valid policy document, tagged by ``identity.issuer``.

    The issuer is the marker because it survives ``parse_policy`` verbatim onto
    ``GovernanceCeiling.identity_issuer``, so a precedence assertion can name the
    document that won without parsing a control out of it.
    """
    body: dict = {"version": 1, "boot": {"fail_closed": True}}
    if marker:
        body["identity"] = {"issuer": marker}
    body.update(extra)
    return body


def _body(marker: str = "", **extra: object) -> bytes:
    return json.dumps(_doc(marker, **extra)).encode("utf-8")


def _write_policy(path: Path, marker: str = "", **extra: object) -> Path:
    path.write_text(json.dumps(_doc(marker, **extra)), encoding="utf-8")
    return path


def _cache(marker: str, source: str, *, age_secs: float = 0.0, etag: str = "") -> None:
    """Seed the last-known-good cache with a document of a chosen age.

    The age is the load path's control flow, not decoration: a copy younger than
    the fetch window is served as a shortcut and no transport is consulted at all,
    so a test about fetching has to age its cache past that window.
    """
    pd.write_cache(_body(marker), source=source, etag=etag, now=time.time() - age_secs)


def _cache_raw(body: bytes, *, source: str, etag: str = "") -> None:
    """Seed the cache with bytes that are NOT a valid policy.

    For the arms that must survive an unusable cached copy, where the point is the
    parse REFUSAL and what it is allowed to say about the source.
    """
    pd.write_cache(body, source=source, etag=etag)


def pd_format_exc(record: logging.LogRecord) -> str:
    """The traceback a handler would print for *record*, or "" when it carries none.

    ``getMessage()`` alone cannot see an ``exc_info=True`` leak: the exception text is
    formatted by the handler, not stored in the message, so a test that reads only the
    message would pass while the log ring printed the source.
    """
    if not record.exc_info:
        return ""
    return "".join(traceback.format_exception(*record.exc_info))


def _file_source(path: Path, marker: str = "", **extra: object) -> str:
    """A published ``file://`` document, returned as its absolute URL.

    Written READ-ONLY because that is a precondition of the transport rather than a
    nicety. The ANCESTOR half of that precondition cannot be satisfied under ``tmp_path``
    at all — ``/tmp`` is world-writable, so the walk always finds a way in — which is the
    control working, not a test problem. Callers whose subject is something else
    (precedence, dispositions, the digest validator) use the ``readonly_chain`` fixture to
    stub the ancestor walk; the control itself is tested against real paths in
    ``TestAFileSourceMustNotBeAgentWritable``.
    """
    written = _write_policy(path, marker, **extra)
    written.chmod(0o444)
    return written.as_uri()


def _sign(doc: dict, secret: str) -> dict:
    """*doc* with a valid ``identity.signature`` for its declared issuer."""
    signed = json.loads(json.dumps(doc))
    signed.setdefault("identity", {})["signature"] = hmac.new(
        secret.encode("utf-8"), governance.policy_signing_payload(signed), hashlib.sha256
    ).hexdigest()
    return signed


def _static_fetcher(result: pd.FetchedPolicy, seen: list | None = None):
    """A fetcher that always answers *result*, recording the requests it saw."""

    def fetch(request: pd.FetchRequest) -> pd.FetchedPolicy:
        if seen is not None:
            seen.append(request)
        return result

    return fetch


def _failing_fetcher(exc: BaseException):
    def fetch(request: pd.FetchRequest) -> pd.FetchedPolicy:
        raise exc

    return fetch


_DECLARED = governance.PolicyDistribution(
    source="https://config.corp.example/policy.json",
    refresh_interval_secs=900,
    timeout_secs=7.0,
    max_cache_age_secs=3600,
    on_unavailable=governance.UNAVAILABLE_FAIL_CLOSED,
)


# ──────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the family's hermetic governance globals, moved verbatim; tracked for a de-flake pass
def _hermetic_governance_globals(monkeypatch):
    """Pin the three process globals this module perturbs.

    ``governance_health`` records the last incident and ``governance_profiles``
    caches profiles, both for the life of the worker, so a mark or a snapshot must
    not leak in either direction. The two env vars are deleted because they select
    a policy tier: an operator (or CI image) with either set would otherwise have
    every precedence assertion here read a file this module never wrote.

    The fetch window is reopened at SETUP, so no test here depends on the previous
    one having torn itself down — the rootdir conftest reopens it after every test,
    which leaves the first test in a worker exposed to whatever ran before pytest
    installed that guard.
    """
    monkeypatch.delenv("KIROCREW_SECURITY_POLICY", raising=False)
    monkeypatch.delenv("KIROCREW_ADMISSION_POLICY", raising=False)
    # Every per-process latch and memo the ladder keeps (last bundled document, the
    # absence audit, the env-inversion warning, the intersect pairs) is reset as one
    # unit so a test states its own tiers and reads its own audit rows.
    governance.reset_process_state()
    pd.reset_fetch_window()
    health.reset()
    gp.reset_store()
    yield
    health.reset()
    gp.reset_store()


@pytest.fixture(name="transport")
def transport_fixture():
    """Register a fake transport, restoring the module's scheme table after.

    ``_FETCHERS`` is a module global shared by every test on this worker, so the
    snapshot/restore is not tidiness — a leaked scheme would silently answer some
    later test's fetch. Yields ``register(fetcher) -> url``.
    """
    snapshot = dict(pd._FETCHERS)
    try:

        def register(fetcher, scheme: str = _TEST_SCHEME) -> str:
            # Assigned directly rather than through ``register_policy_fetcher``: a test
            # that walks several fetchers (each failure mode in turn) re-registers the
            # same scheme, and the public seam RAISES on that by design — the behaviour
            # ``test_a_conflicting_fetcher_registration_raises_rather_than_shadowing``
            # pins. The table is snapshotted and restored below, so this cannot leak.
            with pd._FETCHER_LOCK:
                pd._FETCHERS[scheme] = fetcher
            return f"{scheme}://policy.example/security_policy.json"

        yield register
    finally:
        with pd._FETCHER_LOCK:
            pd._FETCHERS.clear()
            pd._FETCHERS.update(snapshot)


@pytest.fixture(name="readonly_chain")
def readonly_chain_fixture(monkeypatch):
    """Treat a ``tmp_path`` source as non-writable, leaf and ancestors both.

    For tests whose subject is NOT that precondition. Two separate reasons a real
    ``file://`` source under ``tmp_path`` cannot satisfy it: ``/tmp`` is world-writable,
    so the ancestor walk always finds a way in; and off POSIX both predicates answer
    ``True`` unconditionally, because mode bits carry no answer there — so without this
    every ``file://`` test would refuse on Windows for a reason it is not testing.

    Both halves are exercised for real, per platform, by the tests that mean them:
    ``TestAFileSourceMustNotBeAgentWritable`` (POSIX mode bits, real paths) and
    ``TestAnUnverifiableSourceIsTreatedAsWritable`` (the off-POSIX answer).
    """
    monkeypatch.setattr(pd, "path_writable_by_current_user", lambda path: False)
    monkeypatch.setattr(pd, "stat_writable_by_current_user", lambda st: False)


@pytest.fixture(name="install_ceiling")
def install_ceiling_fixture():
    """Install a process context carrying a given ceiling; returns ``install``.

    The base context is composed while the fixture is set up — i.e. BEFORE the
    test body configures a source — because ``current_context()`` builds the
    standalone default by calling ``load_security_policy()``. Composing it lazily
    inside ``apply_ceiling`` would make the tier under test run as a side effect
    of merely reading the context.
    """
    from dataclasses import replace

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform import context as context_mod
    from kiro_crew.platform.bootstrap import build_default_context

    base = build_default_context(KiroCrewConfig.load())

    def install(ceiling):
        context_mod.set_context(replace(base, governance=ceiling))
        return ceiling

    return install


@pytest.fixture(name="profiles_dir")
def profiles_dir_fixture(tmp_path, monkeypatch):
    """An empty profiles directory the store reads instead of the real home."""
    directory = tmp_path / "profiles"
    directory.mkdir()
    monkeypatch.setattr(gp, "_PROFILES_DIR", directory)
    gp.reset_store()
    return directory
