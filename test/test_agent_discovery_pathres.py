"""The agent-spec reader asks the sensitive-path fence off the resolver pool.

``agent_discovery._read_agent_spec`` (and its raising twin
``read_agent_spec_strict``) canonicalises the spec path with
``Path.resolve(strict=True)`` and then asks the fence. Asking through the
bounded ``is_sensitive_path`` submits the same path to the two-worker
``mc-pathres`` pool again, so when that pool is saturated the gate fails closed,
the reader returns ``None`` at DEBUG level, and every surface built on it loses
the agent silently: the native skill projection skips the spec with a bare
``continue`` and later raises ``no prepared skill discovery view``.

The fix hands the fence the resolved path through ``_fence_refuses``, which
delegates to ``security.is_sensitive_canonical_path``: ``is_sensitive_resolved_path``
off the event loop (no submission) and the bounded gate on the loop. These tests
pin:

* the gate receives the ``Path.resolve`` result, never the raw spelling;
* off the loop a spec read completes with the pool refusing every submission and
  never touches the bounded gate;
* the two gates agree on canonical fenced and unfenced controls, and both -- with
  no injected verdict -- refuse a link whose resolved target is a crew secret
  leaf (the home-dirs class) or a keystone publish temp, as do both readers;
* a fenced verdict still refuses (``None`` plus the audit row; the strict twin
  raises ``SensitiveAgentSpecPathError``);
* on the loop the bounded gate answers, on a worker the pre-resolved gate does.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import pytest

from conftest import make_dir_link, requires_symlinks
from kiro_crew import agent_discovery as ad_mod
from kiro_crew import hooks, security
from kiro_crew.agent_discovery import (
    SensitiveAgentSpecPathError,
    _read_agent_spec,
    read_agent_spec_strict,
)


def _spec(directory: Path, name: str = "a") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(json.dumps({"name": name}), encoding="utf-8")
    return path


def _resolved_recorder(monkeypatch) -> list[str]:
    seen: list[str] = []
    real_gate = security.paths.is_sensitive_resolved_path

    def recording(path_str: str) -> bool:
        seen.append(path_str)
        return real_gate(path_str)

    monkeypatch.setattr(security.paths, "is_sensitive_resolved_path", recording)
    return seen


def _bounded_recorder(monkeypatch) -> list[str]:
    seen: list[str] = []
    real_gate = security.paths.is_sensitive_path

    def recording(path_str: str, base_dir=None) -> bool:
        seen.append(path_str)
        return real_gate(path_str, base_dir)

    monkeypatch.setattr(security.paths, "is_sensitive_path", recording)
    return seen


def _forbid_bounded(monkeypatch) -> None:
    monkeypatch.setattr(
        security.paths,
        "is_sensitive_path",
        lambda *a, **k: pytest.fail("the spec reader called is_sensitive_path off the loop"),
    )


def _forbid_resolved(monkeypatch) -> None:
    monkeypatch.setattr(
        security.paths,
        "is_sensitive_resolved_path",
        lambda *a, **k: pytest.fail("the pre-resolved gate ran on the event loop"),
    )


class TestTheReaderHandsTheGateCanonicalPaths:
    """The gate receives ``Path.resolve(strict=True)``, never the spelling."""

    def test_both_readers_ask_about_the_resolved_path(self, tmp_path: Path, monkeypatch) -> None:
        real_dir = tmp_path / "real"
        spec = _spec(real_dir)
        alias = tmp_path / "alias"
        make_dir_link(alias, real_dir)
        spelled = alias / ".." / "alias" / spec.name
        seen = _resolved_recorder(monkeypatch)

        assert _read_agent_spec(spelled, operation="t", source="test") == {"name": "a"}
        assert read_agent_spec_strict(spelled, operation="t", source="test") == {"name": "a"}

        canonical = str(spec.resolve(strict=True))
        assert seen == [canonical, canonical]
        assert all(s == os.path.realpath(s) for s in seen)
        assert str(spelled) not in seen


class TestTheReaderStaysOffThePool:
    """Off the loop a spec read survives a saturated ``mc-pathres`` pool."""

    def test_a_read_completes_with_every_submission_refused(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        spec = _spec(tmp_path / "agents")

        def refuse(expanded, worker, **kwargs):
            raise AssertionError(f"the spec reader reached mc-pathres for {expanded!r}")

        monkeypatch.setattr(security.paths, "_run_resolution_bounded", refuse)
        # Cold target cache: the anchors must be rebuilt inline, not via the pool.
        monkeypatch.setattr(security.paths, "_home_targets_cache", {})
        _forbid_bounded(monkeypatch)

        assert _read_agent_spec(spec, operation="t", source="test") == {"name": "a"}
        assert read_agent_spec_strict(spec, operation="t", source="test") == {"name": "a"}


class TestTheTwoGatesAgree:
    """For a canonical path the pre-resolved gate is the bounded gate's verdict."""

    @staticmethod
    def _fenced_controls() -> list[str]:
        credential = os.path.realpath(os.path.expanduser("~/.aws/credentials"))
        parents = security.paths._home_dir_targets(security.paths._KEYSTONE_ARTIFACT_PARENTS)
        assert parents, "no keystone artifact parent resolved; the fixture home is wrong"
        keystone_temp = os.path.realpath(os.path.join(sorted(parents)[0], "meta.json.tmp"))
        return [credential, keystone_temp]

    def test_fenced_canonical_controls_are_refused_by_both(self) -> None:
        for path in self._fenced_controls():
            assert path == os.path.realpath(path), path
            assert security.is_sensitive_resolved_path(path) is True, path
            assert security.is_sensitive_path(path) is True, path

    def test_unfenced_canonical_controls_pass_both(self, tmp_path: Path) -> None:
        spec = str(_spec(tmp_path / "agents").resolve(strict=True))
        for path in (spec, os.path.realpath(os.sep)):
            assert path == os.path.realpath(path), path
            assert security.is_sensitive_resolved_path(path) is False, path
            assert security.is_sensitive_path(path) is False, path

    @staticmethod
    def _assert_link_refused_by_both_gates_and_both_readers(
        link: Path, target: Path, monkeypatch
    ) -> None:
        resolved = str(link.resolve(strict=True))
        assert resolved == os.path.realpath(str(target))
        # The two real gates, on the resolved target: no injected verdict anywhere.
        assert security.is_sensitive_path(resolved) is True, resolved
        assert security.is_sensitive_resolved_path(resolved) is True, resolved
        assert security.is_sensitive_canonical_path(resolved) is True, resolved
        audited: list[dict[str, str]] = []
        monkeypatch.setattr(ad_mod, "_audit_denied", lambda **kw: audited.append(kw))
        assert _read_agent_spec(link, operation="op", source="src") is None
        with pytest.raises(SensitiveAgentSpecPathError):
            read_agent_spec_strict(link, operation="op", source="src")
        assert [row["resources"] for row in audited] == [resolved, resolved]

    @requires_symlinks
    def test_a_link_to_a_crew_secret_leaf_is_refused_by_both_gates_and_both_readers(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The ``evil.json -> <credential file>`` shape, real gates only.

        The reader resolves the link, so the gate is asked about the TARGET. The
        sibling tests inject a verdict at the reader's gate name, which proves the
        wiring; this one proves the decision on the ``_SENSITIVE_HOME_DIRS`` class.
        The target is the crew home's ``.env`` (a bare secret leaf, fenced through
        the crew-prefix entries of that list): the suite pins ``KIROCREW_HOME`` to a
        per-test tmp dir, so the file is test-owned and the real home, its anchors
        and ``~/.aws`` are never touched or redirected.
        """
        crew_home = Path(os.environ["KIROCREW_HOME"])
        crew_home.mkdir(parents=True, exist_ok=True)
        secret = crew_home / ".env"
        secret.write_text(json.dumps({"name": "leaked", "model": "leaked-value"}))
        agents = tmp_path / "agents"
        agents.mkdir()
        evil = agents / "evil.json"
        evil.symlink_to(secret)
        benign_target = tmp_path / "elsewhere.json"
        benign_target.write_text(json.dumps({"name": "benign"}))
        benign = agents / "benign.json"
        benign.symlink_to(benign_target)

        self._assert_link_refused_by_both_gates_and_both_readers(evil, secret, monkeypatch)
        # Control: a link to an unfenced target agrees the other way, and is read.
        benign_resolved = str(benign.resolve(strict=True))
        assert security.is_sensitive_path(benign_resolved) is False
        assert security.is_sensitive_resolved_path(benign_resolved) is False
        assert _read_agent_spec(benign, operation="op", source="src") == {"name": "benign"}

    @requires_symlinks
    def test_a_link_to_a_keystone_publish_temp_is_refused_by_both_gates_and_both_readers(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The other fenced class: a keystone leaf's publish temp under the data home.

        ``.env`` is a bare keystone leaf, so the crew home root itself is a keystone
        artifact parent and ``<crew home>/<anything>.tmp`` is fenced. The test
        suite pins ``KIROCREW_HOME`` to a per-test tmp dir, so the temp is test-owned.
        """
        crew_home = Path(os.environ["KIROCREW_HOME"])
        crew_home.mkdir(parents=True, exist_ok=True)
        temp = crew_home / "publish.tmp"
        temp.write_text(json.dumps({"name": "leaked"}))
        agents = tmp_path / "agents"
        agents.mkdir()
        link = agents / "evil.json"
        link.symlink_to(temp)

        self._assert_link_refused_by_both_gates_and_both_readers(link, temp, monkeypatch)


class TestTheDecisionMovedGatesNotVerdicts:
    """A fenced verdict refuses exactly as before; only the fenced path."""

    def test_a_fenced_verdict_refuses_both_readers_and_only_the_fenced_path(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        fenced = _spec(tmp_path / "agents", "fenced")
        benign = _spec(tmp_path / "agents", "benign")
        fenced_real = str(fenced.resolve(strict=True))
        audited: list[dict[str, str]] = []
        monkeypatch.setattr(ad_mod, "_audit_denied", lambda **kw: audited.append(kw))
        monkeypatch.setattr(
            security.paths, "is_sensitive_resolved_path", lambda p: p == fenced_real
        )
        _forbid_bounded(monkeypatch)

        assert _read_agent_spec(fenced, operation="op", source="src") is None
        with pytest.raises(SensitiveAgentSpecPathError):
            read_agent_spec_strict(fenced, operation="op", source="src")
        assert _read_agent_spec(benign, operation="op", source="src") == {"name": "benign"}
        assert read_agent_spec_strict(benign, operation="op", source="src") == {"name": "benign"}

        assert len(audited) == 2
        for row in audited:
            assert row["resources"] == fenced_real
            assert row["error"] == "sensitive path rejected"
            assert (row["operation"], row["source"]) == ("op", "src")


class TestTheFenceFollowsTheThread:
    """On the loop the bounded gate answers; on a worker the pre-resolved gate does."""

    @pytest.mark.asyncio
    async def test_on_the_event_loop_the_bounded_gate_answers(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # Dashboard handlers call the reader synchronously from a coroutine: the
        # pre-resolved gate would resolve its anchors inline on the loop, so the
        # bounded gate must answer there -- exactly as it did before.
        spec = _spec(tmp_path / "agents")
        seen = _bounded_recorder(monkeypatch)
        _forbid_resolved(monkeypatch)

        assert _read_agent_spec(spec, operation="t", source="test") == {"name": "a"}
        assert read_agent_spec_strict(spec, operation="t", source="test") == {"name": "a"}

        canonical = str(spec.resolve(strict=True))
        assert seen == [canonical, canonical]

    @pytest.mark.asyncio
    async def test_off_the_event_loop_the_pre_resolved_gate_answers(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # The native skill projection runs the reader via asyncio.to_thread: a
        # worker thread earns the off-pool gate.
        spec = _spec(tmp_path / "agents")
        seen = _resolved_recorder(monkeypatch)
        _forbid_bounded(monkeypatch)

        assert await asyncio.to_thread(_read_agent_spec, spec, operation="t", source="test") == {
            "name": "a"
        }
        assert await asyncio.to_thread(
            read_agent_spec_strict, spec, operation="t", source="test"
        ) == {"name": "a"}

        canonical = str(spec.resolve(strict=True))
        assert seen == [canonical, canonical]


class TestTheSpecReadIsPinnedAndCapped:
    """The read after the fence is pinned to its descriptor and size-capped."""

    def test_a_spec_over_the_cap_is_refused_as_too_large(self, tmp_path: Path, monkeypatch, caplog):
        monkeypatch.setattr(hooks, "MAX_FILE_BYTES", 64)
        big = tmp_path / "agents" / "big.json"
        big.parent.mkdir()
        big.write_text(json.dumps({"name": "big", "pad": "x" * 512}), encoding="utf-8")
        real = big.resolve(strict=True)

        with pytest.raises(hooks.FileTooLargeError):
            ad_mod._read_spec_bytes(real)
        with caplog.at_level(logging.DEBUG, logger=ad_mod.logger.name):
            assert _read_agent_spec(big, operation="t", source="test") is None
        assert "Skipping oversized agent config" in caplog.text
        with pytest.raises(ValueError, match="safety cap"):
            read_agent_spec_strict(big, operation="t", source="test")

    def test_a_spec_at_the_cap_is_read_whole(self, tmp_path: Path, monkeypatch) -> None:
        body = json.dumps({"name": "exact"}).encode("utf-8")
        monkeypatch.setattr(hooks, "MAX_FILE_BYTES", len(body))
        spec = tmp_path / "agents" / "exact.json"
        spec.parent.mkdir()
        spec.write_bytes(body)

        assert ad_mod._read_spec_bytes(spec.resolve(strict=True)) == body
        assert _read_agent_spec(spec, operation="t", source="test") == {"name": "exact"}

    @requires_symlinks
    def test_a_link_planted_at_the_resolved_name_is_refused_not_followed(
        self, tmp_path: Path, caplog
    ) -> None:
        # The reader judged ``real`` while it was a regular file; by the time the
        # open runs the name is a link (the swap a stale realpath cannot see).
        # ``_read_spec_bytes`` receives the judged name and must refuse.
        target = tmp_path / "protected.json"
        target.write_text(json.dumps({"name": "linked", "model": "leaked"}), encoding="utf-8")
        agents = tmp_path / "agents"
        agents.mkdir()
        planted = agents / "swapped.json"
        os.symlink(target, planted)

        with pytest.raises(OSError, match="refusing to read through a link"):
            ad_mod._read_spec_bytes(planted)

        class StalePath(type(Path())):
            """A path whose ``resolve`` answers with the name it was given."""

            def resolve(self, strict: bool = False) -> Path:
                return Path(str(self))

        stale = StalePath(planted)
        with caplog.at_level(logging.WARNING, logger=ad_mod.logger.name):
            assert _read_agent_spec(stale, operation="t", source="test") is None
        # The refusal is an operator-visible line naming the file and the reason,
        # not the DEBUG skip a plain unreadable file gets.
        warning = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning) == 1, caplog.text
        message = warning[0].getMessage()
        assert "Skipping agent config" in message
        assert "refusing to read through a link" in message
        # The path and the reason are rendered with ``%r`` (a newline in the
        # name cannot open a second record), so the record carries the repr of
        # the object the reader was handed, whatever the platform's separator.
        assert repr(stale) in message, message
        assert planted.name in message
        with pytest.raises(OSError) as info:
            read_agent_spec_strict(stale, operation="t", source="test")
        assert info.value.errno == ad_mod.errno.EACCES
        assert not isinstance(info.value, ValueError)

    @requires_symlinks
    @pytest.mark.skipif(os.name == "nt", reason="a newline is not a legal NTFS filename byte")
    def test_a_newline_in_a_refused_name_cannot_forge_a_second_log_record(
        self, tmp_path: Path, caplog
    ) -> None:
        # The WARNING quotes an untrusted filename and the refusal message that
        # repeats it. Rendered raw, a newline in the name would end the record
        # and start a forged one; ``%r`` escapes it inside a single record.
        target = tmp_path / "protected.json"
        target.write_text(json.dumps({"name": "linked"}), encoding="utf-8")
        agents = tmp_path / "agents"
        agents.mkdir()
        planted = agents / "evil\nWARNING forged record.json"
        os.symlink(target, planted)

        class StalePath(type(Path())):
            def resolve(self, strict: bool = False) -> Path:
                return Path(str(self))

        with caplog.at_level(logging.WARNING, logger=ad_mod.logger.name):
            assert _read_agent_spec(StalePath(planted), operation="t", source="test") is None
        warning = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning) == 1, caplog.text
        message = warning[0].getMessage()
        assert "\n" not in message, message
        # Positive control: the newline was in the input and is present escaped.
        assert "\\n" in message, message
        assert "refusing to read through a link" in message

    def test_a_hardlink_to_another_file_is_refused(self, tmp_path: Path) -> None:
        target = tmp_path / "protected.json"
        target.write_text(json.dumps({"name": "linked"}), encoding="utf-8")
        agents = tmp_path / "agents"
        agents.mkdir()
        alias = agents / "alias.json"
        os.link(target, alias)

        with pytest.raises(OSError, match="refusing to read a hardlinked file"):
            ad_mod._read_spec_bytes(alias.resolve(strict=True))
        assert _read_agent_spec(alias, operation="t", source="test") is None


class TestTheUncTrustedRootGateStays:
    """A UNC spelling outside the trusted roots is refused before and after ``resolve``.

    ``hooks.validate_file_path`` applied this gate to every spec read before the
    readers left ``safe_read_file_bytes``; the pinned read keeps it, and asks it
    before ``Path.resolve`` too, because on Windows that resolve is itself the
    outbound SMB probe. Modelled on POSIX by flipping the module's platform flag
    and the trusted-root predicate; ``is_unc_shape`` itself is the real one.
    """

    @staticmethod
    def _arm(monkeypatch) -> list[dict[str, str]]:
        monkeypatch.setattr(ad_mod, "_WINDOWS", True)
        monkeypatch.setattr(ad_mod, "unc_probe_allowed", lambda raw: False)
        audited: list[dict[str, str]] = []
        monkeypatch.setattr(ad_mod, "_audit_denied", lambda **kw: audited.append(kw))
        _forbid_bounded(monkeypatch)
        _forbid_resolved(monkeypatch)
        return audited

    def test_a_unc_spelling_is_refused_before_the_resolve_probe(self, monkeypatch) -> None:
        audited = self._arm(monkeypatch)

        class ProbingPath(type(Path())):
            """A path whose ``resolve`` is the probe the gate must prevent."""

            def resolve(self, strict: bool = False) -> Path:
                pytest.fail("resolve ran on a UNC spelling outside the trusted roots")

        share = ProbingPath("//evil/share/agents/x.json")
        assert _read_agent_spec(share, operation="op", source="src") is None
        with pytest.raises(SensitiveAgentSpecPathError, match="outside the trusted roots"):
            read_agent_spec_strict(share, operation="op", source="src")
        assert [row["error"] for row in audited] == ["untrusted UNC path rejected"] * 2
        assert all(row["resources"] == str(share) for row in audited)

    def test_a_local_link_into_a_share_is_refused_after_resolve(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        audited = self._arm(monkeypatch)
        spec = _spec(tmp_path / "agents")

        share = Path("//evil/share/x.json")

        class LinkedIntoShare(type(Path())):
            """A local spelling whose resolve lands on a share."""

            def resolve(self, strict: bool = False) -> Path:
                return share

        assert _read_agent_spec(LinkedIntoShare(spec), operation="op", source="src") is None
        with pytest.raises(SensitiveAgentSpecPathError, match="resolves to a share"):
            read_agent_spec_strict(LinkedIntoShare(spec), operation="op", source="src")
        # The audit row carries the resolved spelling as the platform spells it
        # (backslashes on Windows), so compare against str(Path), not a literal.
        assert [row["resources"] for row in audited] == [str(share)] * 2

    def test_a_trusted_share_and_a_local_path_pass(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(ad_mod, "_WINDOWS", True)
        monkeypatch.setattr(ad_mod, "unc_probe_allowed", lambda raw: True)
        spec = _spec(tmp_path / "agents")
        assert _read_agent_spec(spec, operation="op", source="src") == {"name": "a"}
        assert ad_mod._unc_refused("//trusted/share/x.json") is False
        monkeypatch.setattr(ad_mod, "unc_probe_allowed", lambda raw: False)
        assert ad_mod._unc_refused(str(spec)) is False
        assert ad_mod._unc_refused("//evil/share/x.json") is True
        monkeypatch.setattr(ad_mod, "_WINDOWS", False)
        assert ad_mod._unc_refused("//evil/share/x.json") is False


class TestTheSharedEntryPointPicksTheGateByThread:
    """``security.is_sensitive_canonical_path`` is where the thread decides."""

    @pytest.mark.asyncio
    async def test_on_the_loop_the_bounded_gate_answers(self, monkeypatch) -> None:
        seen: list[str] = []
        monkeypatch.setattr(
            security.paths, "is_sensitive_path", lambda p, b=None: seen.append(p) or False
        )
        monkeypatch.setattr(
            security.paths,
            "is_sensitive_resolved_path",
            lambda p: pytest.fail("the pre-resolved gate ran on the event loop"),
        )
        assert security.is_sensitive_canonical_path("/tmp/x") is False
        assert seen == ["/tmp/x"]

    @pytest.mark.asyncio
    async def test_off_the_loop_the_pre_resolved_gate_answers(self, monkeypatch) -> None:
        seen: list[str] = []
        monkeypatch.setattr(
            security.paths, "is_sensitive_resolved_path", lambda p: seen.append(p) or True
        )
        monkeypatch.setattr(
            security.paths,
            "is_sensitive_path",
            lambda p, b=None: pytest.fail("the bounded gate ran on a worker"),
        )
        assert await asyncio.to_thread(security.is_sensitive_canonical_path, "/tmp/x") is True
        assert seen == ["/tmp/x"]
