"""Session storage: agent-controlled names in log lines (the log-forgery class).

A trash batch name, a manifest ``uid`` and a co-tenant directory name can each embed a
newline. The log sites, refusal texts and error messages pinned here render such a name
escaped, and so does the manifest rollback site for a session id, so one record cannot
forge others.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest
from session_storage_helpers import (  # noqa: F401 -- fixtures are requested by name
    _NOW,
    _cli_half,
    _fresh_scan_cache,
    _index,
    _pin_a_fake_default_home,
    stores_fixture,
)

from kiro_crew import session_storage
from kiro_crew.session_storage import SessionStorageError


class TestTrashBatchNamesAreLogSafe:
    """A batch directory name is agent-controlled and can embed a newline; every
    ``list_trash`` log line that carries it must escape it, or one record forges
    additional records (the log-forgery class).
    """

    _FORGED = "20240101T000000-deadbeef\nWARNING forged: batch cleared by operator"

    @staticmethod
    def _make_forged_dir(name: str) -> Path:
        forged = session_storage.trash_root() / name
        try:
            forged.mkdir(parents=True)
        except OSError:
            pytest.skip("this filesystem refuses a newline in a directory name")
        return forged

    def test_missing_manifest_log_escapes_the_batch_name(
        self, stores: tuple[Path, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        self._make_forged_dir(self._FORGED)

        with caplog.at_level(logging.DEBUG, logger="kiro_crew.session_storage"):
            assert session_storage.list_trash() == []

        messages = [
            record.getMessage()
            for record in caplog.records
            if "no readable manifest" in record.getMessage()
        ]
        assert messages, "the missing-manifest site did not log at all"
        # The rendered record stays one line: the name appears repr'd, and the
        # injected second line never starts a record of its own.
        assert all("\n" not in message for message in messages)
        assert any(repr(self._FORGED) in message for message in messages)

    def test_batch_id_disagreement_log_escapes_the_batch_name(
        self, stores: tuple[Path, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        # Renaming the directory makes its manifest header disagree with it,
        # which is exactly the warning path under test.
        original = session_storage.trash_root() / batch.batch_id
        try:
            original.rename(session_storage.trash_root() / self._FORGED)
        except OSError:
            pytest.skip("this filesystem refuses a newline in a directory name")

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            assert session_storage.list_trash() == []

        messages = [
            record.getMessage()
            for record in caplog.records
            if "claiming batch id" in record.getMessage()
        ]
        assert messages, "the batch-id-disagreement site did not log at all"
        assert all("\n" not in message for message in messages)
        assert any(repr(self._FORGED) in message for message in messages)

    def test_both_sites_escape_without_touching_the_filesystem(
        self,
        stores: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Windows refuses a newline in a real directory name, which would skip
        the two filesystem tests above on those shards. Injecting the forged
        name at the manifest-read seam pins both log sites on every platform.
        """
        session_storage.trash_root().mkdir(parents=True)

        class _ForgedDir:
            name = self._FORGED

            @staticmethod
            def is_dir() -> bool:
                return True

        # Shaped for `_summarize_manifest` — (header, sessions, staged_bytes) —
        # because that is the seam `list_trash` reads. Patching
        # `_read_manifest` instead lets the real `_summarize_manifest` run, and it
        # does `batch / MANIFEST_NAME`, which a name-only double cannot support.
        manifests: dict[str, tuple[dict[str, object], int, int] | None] = {
            "missing": None,
            "disagreeing": ({"batch_id": "someone-else", "created_at": _NOW}, 0, 0),
        }

        # Forge ONLY the trash root's own enumeration, via a Path subclass whose
        # `iterdir` yields the double. Patching `session_storage.Path.iterdir`
        # (i.e. `pathlib.Path.iterdir`) globally leaks across the xdist worker:
        # a sibling test in the same process that legitimately iterates a real
        # directory then reaches the unpatched `_summarize_manifest`, which does
        # `<_ForgedDir> / MANIFEST_NAME` and raises TypeError.
        class _ForgedRoot(type(session_storage.trash_root())):
            def iterdir(self):  # type: ignore[override]
                return iter([_ForgedDir()])

        forged_root = _ForgedRoot(session_storage.trash_root())

        for label, parsed in manifests.items():
            caplog.clear()
            monkeypatch.setattr(session_storage, "trash_root", lambda: forged_root)
            monkeypatch.setattr(
                session_storage.platform_compat, "is_link_or_junction", lambda p: False
            )
            monkeypatch.setattr(
                session_storage, "_summarize_manifest", lambda batch, parsed=parsed: parsed
            )

            with caplog.at_level(logging.DEBUG, logger="kiro_crew.session_storage"):
                assert session_storage.list_trash() == []

            rendered = [record.getMessage() for record in caplog.records]
            carrying = [message for message in rendered if repr(self._FORGED) in message]
            assert carrying, f"the {label}-manifest site did not log the name repr'd"
            assert all("\n" not in message for message in rendered)


class TestUntrustedNamesAreLogSafeOutsideListTrash:
    """The forgery primitive of :class:`TestTrashBatchNamesAreLogSafe`, at the
    sibling sites outside ``list_trash`` (the log-forgery class).

    Two operands carry it, and they are not equally reachable. A manifest-supplied
    ``uid`` is read off disk with no validation, so its sites take a forged value
    end to end. A batch DIRECTORY name reaches its sites only through
    :func:`_batch_dir`, whose id pattern rejects a newline, so those conversions
    are defence in depth and are pinned at the seam each helper already offers —
    which is also what keeps them covered on Windows shards, where a hostile
    directory name cannot be created at all.
    """

    _FORGED_UID = "aaaa1111\nWARNING forged: session restored by operator"
    _FORGED_NAME = "20240101T000000-deadbeef\nWARNING forged: batch cleared by operator"

    @staticmethod
    def _staged(kiro_home: Path) -> tuple[str, Path]:
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)
        batch = session_storage.move_to_trash(
            ["aaaa1111"], reason="manual", index=_index(), now=_NOW
        )
        return batch.batch_id, session_storage.trash_root() / batch.batch_id

    @classmethod
    def _forge_uid(cls, batch: Path, *, files: object | None = None) -> None:
        """Rewrite the batch's one manifest entry under a forged uid.

        The uid is JSON-escaped on the way to disk, so the manifest itself stays
        one line — the forgery only materializes if a log renders the value raw.
        """
        manifest = batch / session_storage.MANIFEST_NAME
        lines = manifest.read_text(encoding="utf-8").splitlines()
        entry = json.loads(lines[1])
        entry["uid"] = cls._FORGED_UID
        if files is not None:
            entry["files"] = files
        manifest.write_text(f"{lines[0]}\n{json.dumps(entry)}\n", encoding="utf-8")

    @staticmethod
    def _carrying(caplog: pytest.LogCaptureFixture, needle: str) -> list[str]:
        return [record.getMessage() for record in caplog.records if needle in record.getMessage()]

    def _assert_escaped(self, messages: list[str], value: str, site: str) -> None:
        assert messages, f"the {site} site did not log at all"
        # One record stays one record: the value appears repr'd, so the injected
        # second line never starts a record of its own.
        assert all("\n" not in message for message in messages)
        assert any(repr(value) in message for message in messages)

    def test_a_forged_uid_is_escaped_when_a_manifest_record_is_not_an_object(
        self, stores: tuple[Path, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        _, kiro_home = stores
        batch_id, batch = self._staged(kiro_home)
        self._forge_uid(batch, files=["not-an-object"])

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            assert session_storage.restore(batch_id) == 0

        self._assert_escaped(
            self._carrying(caplog, "is not an object"), self._FORGED_UID, "unreadable-record"
        )

    def test_a_forged_uid_is_escaped_when_the_session_was_recreated(
        self,
        stores: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _, kiro_home = stores
        batch_id, batch = self._staged(kiro_home)
        self._forge_uid(batch)
        # The occupant-wins branch: the session came back between the preflight
        # and the move, so the batch keeps its entry.
        monkeypatch.setattr(session_storage, "_move_file_exclusive", lambda src, dst: False)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            assert session_storage.restore(batch_id) == 0

        self._assert_escaped(
            self._carrying(caplog, "was recreated while being restored"),
            self._FORGED_UID,
            "lost-race",
        )

    def test_a_forged_uid_is_escaped_when_the_restore_cannot_finish(
        self,
        stores: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _, kiro_home = stores
        batch_id, batch = self._staged(kiro_home)
        self._forge_uid(batch)

        def _refuse(src: Path, dst: Path) -> bool:
            raise OSError("the store is read-only")

        monkeypatch.setattr(session_storage, "_move_file_exclusive", _refuse)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            assert session_storage.restore(batch_id) == 0

        self._assert_escaped(
            self._carrying(caplog, "could not fully restore session"),
            self._FORGED_UID,
            "restore-failed",
        )

    def test_the_manifest_rollback_site_escapes_the_session_id(
        self,
        stores: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """This uid comes from the caller's validated list, so the conversion is
        defence in depth; the test pins it rather than claiming reachability.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=8, age_days=40)

        def _refuse(handle: object, entry: dict[str, object]) -> None:
            raise OSError("no space left on device")

        monkeypatch.setattr(session_storage, "_append_entry", _refuse)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            with pytest.raises(SessionStorageError):
                session_storage.move_to_trash(
                    ["aaaa1111"], reason="manual", index=_index(), now=_NOW
                )

        self._assert_escaped(
            self._carrying(caplog, "could not record session"), "aaaa1111", "manifest-rollback"
        )

    def test_the_discard_and_incomplete_sites_escape_the_batch_name(
        self,
        stores: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Both keep-the-batch branches and the still-on-disk branch, at the
        ``_unlisted_files`` seam — no hostile directory has to exist on disk.
        """
        forged = session_storage.trash_root() / self._FORGED_NAME
        # No write to patch out any more: the caller clears the manifest before this is
        # reached, so neither branch below writes at all.

        def _refuse(batch: Path) -> list[Path]:
            raise SessionStorageError(f"could not read all of {batch.name!r}")

        monkeypatch.setattr(session_storage, "_unlisted_files", _refuse)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            session_storage._discard_restored_batch(forged, None)
        self._assert_escaped(
            self._carrying(caplog, "keeping trash batch"), self._FORGED_NAME, "unreadable-scan"
        )

        caplog.clear()
        monkeypatch.setattr(
            session_storage, "_unlisted_files", lambda batch: [forged / "orphan.jsonl"]
        )
        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            session_storage._discard_restored_batch(forged, None)
        self._assert_escaped(
            self._carrying(caplog, "keeping trash batch"), self._FORGED_NAME, "leftover-files"
        )

        caplog.clear()

        class _ForgedBatch:
            name = self._FORGED_NAME

            @staticmethod
            def exists() -> bool:
                return True

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            assert (
                session_storage._incomplete_if_present(_ForgedBatch())
                == session_storage.SKIP_INCOMPLETE
            )
        self._assert_escaped(
            self._carrying(caplog, "did not remove it"), self._FORGED_NAME, "still-present"
        )

    def test_the_empty_trash_sites_escape_what_they_name(
        self,
        stores: tuple[Path, Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The three refusals in the sweep, at the ``_batch_dir`` seam."""
        session_storage.trash_root().mkdir(parents=True, exist_ok=True)
        forged = session_storage.trash_root() / self._FORGED_NAME

        class _Batch:
            batch_id = "20240101T000000-deadbeef"

        monkeypatch.setattr(session_storage, "list_trash", lambda: [_Batch()])
        monkeypatch.setattr(session_storage, "_batch_dir", lambda batch_id: forged)

        def _refuse(batch: Path) -> list[Path]:
            raise SessionStorageError("could not read all of it")

        monkeypatch.setattr(session_storage, "_unlisted_files", _refuse)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            assert session_storage.empty_trash() == 0
        self._assert_escaped(
            self._carrying(caplog, "refusing to empty"), self._FORGED_NAME, "unreadable-batch"
        )

        caplog.clear()
        monkeypatch.setattr(
            session_storage, "_unlisted_files", lambda batch: [forged / "orphan.jsonl"]
        )
        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            assert session_storage.empty_trash() == 0
        self._assert_escaped(
            self._carrying(caplog, "refusing to empty"), self._FORGED_NAME, "leftover-files"
        )

        # The outside-the-root refusal names the PATH, so its repr is the Path's.
        caplog.clear()
        outside = tmp_path / self._FORGED_NAME
        monkeypatch.setattr(session_storage, "_batch_dir", lambda batch_id: outside)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            assert session_storage.empty_trash() == 0
        messages = self._carrying(caplog, "outside the trash root")
        assert messages, "the outside-the-root site did not log at all"
        assert all("\n" not in message for message in messages)
        assert any(repr(outside) in message for message in messages)

    def test_the_unreadable_scan_error_escapes_the_batch_name(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The two refusal sites render this exception with ``%s``, so the name has
        to be escaped where the exception is BUILT or the escape at the log site is
        undone by the text it carries.
        """
        forged = session_storage.trash_root() / self._FORGED_NAME

        # The error carries a hostile .filename, which is the half the batch-name
        # escape does not cover: its string form is interpolated into the same
        # message.
        planted = OSError(13, "Permission denied", str(forged / self._FORGED_NAME))

        # ``session_storage.os`` is the global module, so this patch is also seen
        # by other walk users during teardown. Accept the real signature and
        # divert only the walk of the forged batch, delegating every other call
        # to the genuine function. Both mechanisms are patched because
        # ``_unlisted_files`` picks fwalk-vs-walk at call time by availability
        # (fwalk does not exist on Windows).
        real_fwalk = getattr(os, "fwalk", None)
        real_walk = os.walk

        def _divert(real):  # type: ignore[no-untyped-def]
            def inner(top, *args, onerror=None, **kwargs):  # type: ignore[no-untyped-def]
                if Path(top) == forged:
                    assert callable(onerror)
                    onerror(planted)
                    return iter(())
                return real(top, *args, onerror=onerror, **kwargs)

            return inner

        monkeypatch.setattr(session_storage.os, "walk", _divert(real_walk))
        if real_fwalk is not None:
            monkeypatch.setattr(session_storage.os, "fwalk", _divert(real_fwalk))
        monkeypatch.setattr(session_storage, "_manifest_rels", lambda batch: [])

        with pytest.raises(SessionStorageError) as raised:
            session_storage._unlisted_files(forged)

        # Both halves of the message are newline-free: the name because this call
        # site reprs it, and the interpolated OSError because ``OSError.__str__``
        # reprs its own ``.filename``. The second half is why the error operand is
        # correctly left as %s at the sites that render it.
        assert "\n" not in str(raised.value)
        assert repr(self._FORGED_NAME) in str(raised.value)
        assert "Permission denied" in str(raised.value)


class TestCotenantNamesAreLogSafe:
    """A co-tenant directory name is agent-influenced and can embed a newline;
    both ``cotenant_sids`` log sites that carry it must escape it, or one record
    forges additional records (the log-forgery class).
    """

    _FORGED = "wt-evil\nWARNING forged: reclaim authorized by operator"

    def _pod_root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        pod_root = tmp_path / "pods"
        pod_root.mkdir()
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(pod_root))
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIRO_HOME", raising=False)
        _pin_a_fake_default_home(monkeypatch, tmp_path)
        return pod_root

    def _make_forged_pod(self, pod_root: Path) -> Path:
        pod = pod_root / self._FORGED
        try:
            pod.mkdir()
        except OSError:
            pytest.skip("this filesystem refuses a newline in a directory name")
        return pod

    @staticmethod
    def _carrying(caplog: pytest.LogCaptureFixture, marker: str) -> list[str]:
        return [record.getMessage() for record in caplog.records if marker in record.getMessage()]

    def test_malformed_map_log_escapes_the_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        pod = self._make_forged_pod(self._pod_root(tmp_path, monkeypatch))
        (pod / "session_map.json").write_text("not json{", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            _protected, refusals = session_storage.cotenant_sids()

        assert [name for name, _why in refusals] == [self._FORGED]
        messages = self._carrying(caplog, "malformed session map")
        assert messages, "the malformed-map site did not log at all"
        # The rendered record stays one line: the name appears repr'd, and the
        # injected second line never starts a record of its own.
        assert all("\n" not in message for message in messages)
        assert any(repr(self._FORGED) in message for message in messages)

    def test_unreadable_map_log_escapes_the_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        pod = self._make_forged_pod(self._pod_root(tmp_path, monkeypatch))
        # Invalid UTF-8 makes ``safe_read_file`` raise ``UnicodeDecodeError``,
        # which is exactly the unreadable-map warning path under test.
        (pod / "session_map.json").write_bytes(b"\xff\xfe\xfa")

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            _protected, refusals = session_storage.cotenant_sids()

        assert [name for name, _why in refusals] == [self._FORGED]
        messages = self._carrying(caplog, "unreadable session map")
        assert messages, "the unreadable-map site did not log at all"
        assert all("\n" not in message for message in messages)
        assert any(repr(self._FORGED) in message for message in messages)

    def test_exc_info_rendering_does_not_carry_a_raw_newline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The unreadable-map site logs with ``exc_info=True``, and
        ``safe_read_file``'s refusal messages embed the resolved path — which
        keeps a newline-bearing directory name. The FULL rendered record
        (message plus exception text) must stay forge-free, not just the
        ``getMessage()`` line the other tests pin.
        """
        pod = self._make_forged_pod(self._pod_root(tmp_path, monkeypatch))
        (pod / "session_map.json").write_text("{}", encoding="utf-8")
        # Force the refusal arm so the raised PermissionError carries the real
        # resolved path (newline included) into the traceback rendering.
        monkeypatch.setattr(session_storage.hooks, "is_sensitive_path", lambda p: True)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
            _protected, refusals = session_storage.cotenant_sids()

        assert [name for name, _why in refusals] == [self._FORGED]
        assert "unreadable session map" in caplog.text
        # The injected payload never lands at the start of a rendered line,
        # which is what a forged record would need.
        assert not [line for line in caplog.text.splitlines() if line.startswith("WARNING forged")]

    def test_both_sites_escape_without_touching_the_filesystem(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Windows refuses a newline in a real directory name, which would skip
        the two filesystem tests above on those shards. Injecting the forged
        name at the candidate-list seam pins both log sites on every platform.
        """
        self._pod_root(tmp_path, monkeypatch)
        monkeypatch.setattr(session_storage, "_replay_store_cotenants", lambda: [self._FORGED])

        reads: dict[str, object] = {
            "unreadable": PermissionError("refused"),
            "malformed": "not json{",
        }
        for label, outcome in reads.items():

            def _read(path: str, outcome: object = outcome) -> str:
                if isinstance(outcome, Exception):
                    raise outcome
                return str(outcome)

            caplog.clear()
            monkeypatch.setattr(session_storage.hooks, "safe_read_file", _read)

            with caplog.at_level(logging.WARNING, logger="kiro_crew.session_storage"):
                _protected, refusals = session_storage.cotenant_sids()

            assert [name for name, _why in refusals] == [self._FORGED]
            rendered = [record.getMessage() for record in caplog.records]
            carrying = [message for message in rendered if repr(self._FORGED) in message]
            assert carrying, f"the {label}-map site did not log the name repr'd"
            assert all("\n" not in message for message in rendered)


class TestCotenantRefusalTextIsForgeSafe:
    """The raw co-tenant name also exits ``cotenant_sids`` inside the refusals
    tuples and is interpolated into two downstream refusal-text surfaces. Neither
    is a log line today, but one caller-side ``logger.warning(str(exc))`` away
    from re-opening the forgery class — so both must carry the name
    repr'd, exactly like the log sites ``TestCotenantNamesAreLogSafe`` pins.
    """

    _FORGED = "wt-evil\nWARNING forged: reclaim authorized by operator\x1b[31m"
    _REFUSALS = ((_FORGED, "its session map could not be parsed"),)

    def test_reclaim_block_reason_escapes_the_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The listed refusals in the block reason render one-line and escaped.

        Injected at the ``cotenant_sids`` seam rather than through the pod root:
        Windows refuses a newline in a real directory name, and what is under
        test is the CONSUMER's rendering, not discovery.
        """
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIRO_HOME", raising=False)
        _pin_a_fake_default_home(monkeypatch, tmp_path)
        monkeypatch.setattr(
            session_storage, "cotenant_sids", lambda *, cached=False: (frozenset(), self._REFUSALS)
        )

        reason = session_storage.reclaim_block_reason()

        assert "make reclaiming unsafe" in reason
        assert repr(self._FORGED) in reason
        # One line, no raw control bytes: the injected second record and the
        # ANSI payload both survive only inside the repr's escapes.
        assert "\n" not in reason
        assert "\x1b" not in reason

    def test_move_to_trash_refusal_error_escapes_the_name(
        self, stores: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ``SessionStorageError`` raised at the move renders one-line and
        escaped, so a caller logging ``str(exc)`` cannot forge a
        second record.
        """
        _, kiro_home = stores
        _cli_half(kiro_home, "aaaa1111", log_bytes=64, age_days=40)
        monkeypatch.setattr(
            session_storage, "cotenant_sids", lambda *, cached=False: (frozenset(), self._REFUSALS)
        )

        with pytest.raises(SessionStorageError, match="make reclaiming unsafe") as excinfo:
            session_storage.move_to_trash(["aaaa1111"], reason="manual", index=_index(), now=_NOW)

        text = str(excinfo.value)
        assert repr(self._FORGED) in text
        assert "\n" not in text
        assert "\x1b" not in text
        assert (kiro_home / "sessions" / "cli" / "aaaa1111.jsonl").is_file()
