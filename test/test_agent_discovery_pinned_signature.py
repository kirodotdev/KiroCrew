"""Tests for the POSIX descriptor-relative pinned scan and project-signature
cache binding/sentinel/traversal guards.

Split out of ``test_agent_discovery.py`` (file-line-cap gate); see that module's
docstring for the full sibling-file map. Shares fixtures/doubles from
``agent_discovery_test_helpers``.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from agent_discovery_test_helpers import _mock_pinned_directory, _project_agents_dir

from conftest import requires_symlinks
from kiro_crew.agent_discovery import clear_project_agent_cache, project_agent_names


class TestPosixPinnedScanIsDescriptorRelative:
    """The POSIX branch must not re-resolve the directory by NAME after checking
    it: a writable project lets an attacker swap ``.kiro/agents`` for a symlink
    into a credential home between the check and the read, and a name-based
    ``glob`` would follow the swap while a descriptor-relative ``scandir`` reads
    the inode that was validated.
    """

    def test_a_mid_enumeration_error_degrades_instead_of_crashing(
        self, tmp_path, monkeypatch
    ) -> None:
        """A `@contextmanager` generator may yield exactly ONCE.

        With a lazy iterator, an `OSError` raised while the CALLER iterates is
        thrown back in at the yield, and yielding again from the handler raises
        `RuntimeError: generator didn't stop after throw()` -- which aborts the
        caller's whole command (Slack agent resolution) instead of degrading to
        "no project agents". Materializing before the yield is what makes the
        failure a value rather than a crash.
        """
        from kiro_crew.agent_discovery import _pinned_scan_dir

        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "repo-dev.json").write_text("{}", encoding="utf-8")
        entry = MagicMock()
        entry.name = "repo-dev.json"
        state = _mock_pinned_directory(monkeypatch, real_path=agents, entries=[entry])

        with _pinned_scan_dir(agents) as entries:
            assert entries is not None
            # The contract that makes this safe: what is handed over is already
            # read, so nothing can fail partway through the caller's loop.
            assert isinstance(entries, list), (
                "entries must be materialized before the yield, or a mid-loop "
                "OSError re-enters the generator and crashes the caller"
            )
            assert [e.name for e in entries] == ["repo-dev.json"]
        assert state["scans"] == [state["fd"]]

    def test_entries_come_from_the_validated_inode_after_a_name_swap(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.agent_discovery import _pinned_scan_dir

        real = tmp_path / "agents"
        real.mkdir()
        (real / "repo-dev.json").write_text("{}", encoding="utf-8")

        decoy = tmp_path / "credentials"
        decoy.mkdir()
        (decoy / "stolen.json").write_text("{}", encoding="utf-8")
        entry = MagicMock()
        entry.name = "repo-dev.json"
        state = _mock_pinned_directory(monkeypatch, real_path=real, entries=[entry])

        with _pinned_scan_dir(real) as entries:
            assert entries is not None
            # The swap lands AFTER the open and before the listing is consumed,
            # which is exactly the window the finding describes.
            real.rename(tmp_path / "moved")
            decoy.rename(tmp_path / "agents")
            names = sorted(e.name for e in entries)

        assert names == ["repo-dev.json"], (
            "enumeration followed the swapped NAME instead of reading the "
            "descriptor it validated"
        )
        assert state["scans"] == [state["fd"]], "enumeration used a path, not the held fd"

    def test_a_missing_directory_is_nothing_here_not_a_denial(self, tmp_path, monkeypatch) -> None:
        """An absent `.kiro/agents` is the ordinary "no project agents" case; a
        denial here would emit a false security audit on every plain checkout."""
        import kiro_crew.agent_discovery as discovery_mod
        from kiro_crew.agent_discovery import _pinned_scan_dir

        monkeypatch.setattr(discovery_mod, "supports_pinned_walk", lambda: True)
        pin_attempts: list[tuple[object, str, dict[str, object]]] = []

        def missing_leaf(parent, name, **kwargs):
            pin_attempts.append((parent, name, kwargs))
            raise FileNotFoundError(name)

        monkeypatch.setattr(discovery_mod, "open_in_pinned_parent", missing_leaf)

        with _pinned_scan_dir(tmp_path / "does-not-exist") as entries:
            assert entries is not None
            assert list(entries) == []
        assert len(pin_attempts) == 1
        assert pin_attempts[0][1] == "does-not-exist"


class TestProjectSignatureSnapshotBinding:
    def test_roster_from_a_different_directory_identity_is_not_cached(
        self, tmp_path, monkeypatch
    ) -> None:
        """A cache entry binds the signature and roster to one directory inode."""
        import contextlib

        import kiro_crew.agent_discovery as discovery_mod

        project = tmp_path / "repo"
        agents_dir = _project_agents_dir(project)
        (agents_dir / "poison.json").write_text(json.dumps({"name": "poison"}))
        clear_project_agent_cache()

        witnesses = [tmp_path / name for name in ("kiro-witness", "old-agents", "new-agents")]
        for witness in witnesses:
            witness.write_text(witness.name)
        fds = [os.open(witness, os.O_RDONLY) for witness in witnesses]
        calls = iter(
            (
                ((), fds[0], False),
                ((), fds[1], False),
                ((SimpleNamespace(name="poison.json"),), fds[2], False),
            )
        )

        @contextlib.contextmanager
        def swapped_scan(_directory, **_kwargs):
            yield next(calls)

        monkeypatch.setattr(discovery_mod, "_pinned_scan_dir_fd", swapped_scan)
        try:
            assert project_agent_names(project) == frozenset()
        finally:
            for fd in fds:
                os.close(fd)

        assert str(project) not in discovery_mod._PROJECT_NAMES_CACHE, (
            "names read from the replacement directory were cached under the "
            "original directory's signature"
        )

    def test_a_repoint_inside_the_request_window_audits_its_denial_and_still_refuses(
        self, tmp_path, monkeypatch
    ) -> None:
        """A scope readable when its signature was taken and sensitive when the
        scan runs is BOTH audited as denied AND refused.

        The identity compare raises on that mismatch, so ordering it ahead of the
        ``entries is None`` audit loses the denial row: the request is refused
        with 503 while SEL carries only the caller's ``allowed`` row, which reads
        as a scan that was permitted. Auditing first must not cost the refusal
        either -- dropping the compare would let a roster built WITHOUT the
        sensitive directory be cached against the pre-repoint signature.

        The DENIAL half is asserted on every platform, because that is the
        finding. The RAISE half needs a recorded directory identity to mismatch,
        which a platform that cannot pin a descriptor never produces -- there the
        scan refuses the directory without a snapshot error, and the test asserts
        that rather than skipping.
        """
        import contextlib

        import kiro_crew.agent_discovery as discovery_mod
        from kiro_crew.agent_discovery import _ProjectSnapshotChanged, _scan_project_agent_files

        proj = tmp_path / "repo"
        agents_dir = _project_agents_dir(proj)
        (agents_dir / "a.json").write_text(json.dumps({"name": "a"}))
        # Taken while the directory is still readable, over the pinned walk the
        # owner-supplied scan uses: only that walk records the identity rows the
        # compare below fails to observe (the by-name walk records none). Where
        # the platform cannot pin, there is no identity to mismatch.
        readable = discovery_mod._project_signature(
            proj, unsupported_ok=not discovery_mod.supports_pinned_walk()
        )
        identity_recorded = discovery_mod._signature_dir_identity(readable[1]) is not None

        @contextlib.contextmanager
        def _now_sensitive(_d, **_kw):
            yield None, None, False

        monkeypatch.setattr(discovery_mod, "_pinned_scan_dir_fd", _now_sensitive)
        denials: list[dict] = []
        monkeypatch.setattr(discovery_mod, "_audit_denied", lambda **kw: denials.append(kw))

        if identity_recorded:
            with pytest.raises(_ProjectSnapshotChanged):
                _scan_project_agent_files(
                    proj, expected_signatures=readable, raise_unverifiable=True
                )
        else:
            specs = _scan_project_agent_files(
                proj, expected_signatures=readable, raise_unverifiable=True
            )
            assert specs == [], (
                "the sensitive directory was refused, so it must contribute no "
                f"spec even without an identity to mismatch: {specs}"
            )

        assert [d["error"] for d in denials] == [
            "sensitive scan dir rejected"
        ], f"the refused scan recorded no denial: {denials}"
        assert denials[0]["resources"] == str(agents_dir)


class TestProjectSignatureSensitiveDirSentinel:
    """A sensitive subdir's cache signature must differ from an empty dir's.

    GPT flagged that ``_project_signature`` returning ``()`` for BOTH "empty"
    and "sensitive, skipped" lets a cache warmed on a legitimately empty
    ``.kiro/agents`` survive an attacker later swapping that dir to a symlink
    into a credential home: the next call's signature is still ``()``, so
    ``project_agent_names`` treats it as an unchanged cache hit and never calls
    ``project_agent_files`` -- whose call is what emits the required SEL denial
    audit for the sensitive dir. The fix is a sentinel that cannot collide with
    any real ``_dir_signature`` output.
    """

    def test_a_refused_dir_is_never_served_from_cache(self, tmp_path, monkeypatch) -> None:
        """Every lookup of a refused directory must re-scan, because the scan is
        what emits the SEL denial.

        A stable sentinel matches the cached signature on the next lookup, so the
        cached result is served and the repeat probe goes unaudited -- the first
        attempt is recorded and an attacker's subsequent ones are silent.
        """
        import contextlib as _contextlib

        import kiro_crew.agent_discovery as discovery_mod
        from kiro_crew.agent_discovery import _project_signature

        proj = tmp_path / "repo"
        (proj / ".kiro" / "agents").mkdir(parents=True)

        @_contextlib.contextmanager
        def _refused(_d, **_kw):
            yield None, None, False

        monkeypatch.setattr(discovery_mod, "_pinned_scan_dir_fd", _refused)

        first = _project_signature(proj)
        second = _project_signature(proj)

        assert first != second, (
            "two lookups of a refused dir produced the SAME signature, so the "
            "second is a cache hit and its denial is never audited"
        )
        # Still distinguishable from a genuinely empty directory, which is the
        # other job this sentinel has to do.
        assert all(part != () for part in first), first


class TestTheRosterSignatureDoesNotTraverseChildEntries:
    """``_entries_signature`` fingerprints entry NAMES without following them."""

    def test_a_dangling_spec_symlink_is_fingerprinted_not_followed(
        self, tmp_path, monkeypatch
    ) -> None:
        """GPT flagged ``DirEntry.stat()``, which FOLLOWS by default.

        On Windows, statting a name that is a symlink to ``\\\\host\\share`` IS the
        outbound SMB/NTLM authentication, and the signature walk reaches every
        child of the scanned directory before ``_read_agent_spec``'s
        resolved-target guard runs. The directory hold protects the scan directory
        from being swapped; it says nothing about an entry planted inside it.

        A DANGLING link is the oracle and needs no Windows host: a following stat
        raises on it, which the old code swallowed into a ``0`` mtime, while a
        non-following stat reads the LINK's own mtime. A ``0`` here would also be
        a correctness bug in its own right — every dangling link would share one
        fingerprint, so repointing one could not invalidate the cache.
        """
        from kiro_crew.agent_discovery import _entries_signature

        entry = MagicMock()
        entry.name = "planted.json"
        entry.path = str(tmp_path / entry.name)
        entry.is_symlink.return_value = True
        entry.stat.side_effect = [
            SimpleNamespace(st_mtime_ns=17),
            FileNotFoundError("simulated dangling target"),
        ]
        monkeypatch.setattr(os, "readlink", lambda _path: str(tmp_path / "absent-target.json"))

        sig = _entries_signature([entry])

        names = {name for name, _ in sig}
        assert "planted.json" in names, "the planted entry was not fingerprinted at all"
        mtimes = {name: m for name, m in sig}
        assert mtimes["planted.json"] != 0, (
            "the mtime came back 0, which means the stat FOLLOWED the link and "
            "failed on its absent target -- on Windows that stat is the SMB "
            "authentication this must not perform"
        )
        assert entry.stat.call_args_list[0].kwargs == {"follow_symlinks": False}

    @requires_symlinks
    def test_editing_a_symlinked_specs_target_invalidates_the_signature(self, tmp_path) -> None:
        """A symlinked spec (``~/.kiro/agents/mine.json`` -> a dotfiles copy)
        whose TARGET is edited must change the directory signature.

        The link's OWN mtime catches a repoint but not an edit to the file it
        resolves to, so fingerprinting the link alone leaves ``_LIST_AGENTS_CACHE``
        and ``_PARSED_SPECS_CACHE`` serving the pre-edit model/tools/prompt for the
        process lifetime -- the very staleness ``agents_dir_revision`` returns
        ``None`` for on a symlinked spec. The fix folds the followed target mtime
        in additively, keeping the non-following link mtime (its SMB safety and its
        repoint detection) intact.
        """
        from kiro_crew.agent_discovery import _dir_signature

        agents = tmp_path / ".kiro" / "agents"
        agents.mkdir(parents=True)
        # The real spec lives OUTSIDE the scanned dir; the scanned entry links to it.
        target = tmp_path / "dotfiles" / "mine.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps({"name": "mine", "model": "old"}))
        (agents / "mine.json").symlink_to(target)

        before = _dir_signature(agents)

        # Edit the TARGET only, and stamp ONLY its mtime to a fixed, past value --
        # the link's own mtime is untouched, so a signature that fingerprints the
        # link alone cannot tell this from no change at all.
        target.write_text(json.dumps({"name": "mine", "model": "new"}))
        os.utime(target, (1_000_000, 1_000_000))

        after = _dir_signature(agents)
        assert after != before, (
            "a symlinked spec's target edit did not change the signature -- the "
            "list-agents and parsed-spec caches would serve the pre-edit spec"
        )

    @requires_symlinks
    def test_pinned_signature_tracks_a_symlinked_specs_target(self, tmp_path) -> None:
        """The project-cache signature must fingerprint a linked spec's target
        through the descriptor-relative scan, not only through ``_dir_signature``.
        """
        from kiro_crew.agent_discovery import _project_signature

        agents = tmp_path / ".kiro" / "agents"
        agents.mkdir(parents=True)
        target = tmp_path / "dotfiles" / "mine.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps({"name": "mine", "model": "old"}))
        (agents / "mine.json").symlink_to(target)

        before = _project_signature(tmp_path)
        target_rows = {name: mtime for name, mtime in before[1]}
        assert (
            "mine.json\0target" in target_rows
        ), "the descriptor-relative signature omitted the linked target row"
        assert target_rows["mine.json\0target"] == target.stat().st_mtime_ns

        target.write_text(json.dumps({"name": "mine", "model": "new"}))
        os.utime(target, (1_000_000, 1_000_000))

        after = _project_signature(tmp_path)
        assert (
            after != before
        ), "editing a linked spec's target did not invalidate the pinned signature"
