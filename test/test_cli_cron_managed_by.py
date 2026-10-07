"""``kirocrew cron add --managed-by KEY`` and ``cron remove --managed-by KEY``.

An external installer owns a cron under a key it can recompute without reading
Kiro Crew state. These tests pin the contract it relies on, against the real
store: the key is unique (a second add refuses with nothing written), uninstall
by key removes exactly that job, and a re-install is remove then add, which
mints a fresh id. There is deliberately no replace-in-place.
"""

from __future__ import annotations

import argparse
import json
import sys
from unittest.mock import patch

import pytest

from kiro_crew.cli_commands import _cron
from kiro_crew.config.loader import config_dir
from kiro_crew.cron import CronService
from kiro_crew.cron_service.fields import validate_managed_by

KEY = "acme:owner/version-check"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    monkeypatch.setenv("KIROCREW_HOME", str(root))
    (config_dir() / "crons").mkdir(parents=True)
    with patch("kiro_crew.cli_commands.sel"):
        yield root


def _add(**overrides) -> argparse.Namespace:
    base = dict(
        cron_action="add",
        name="version-check",
        message="check the version",
        every=3600,
        cron_expr=None,
        at=None,
        timezone="",
        channel=None,
        agent="",
        script="",
        shell_command="",
        timeout=None,
        timeout_secs=None,
        model="",
        persistent_session=True,
        minimal_context=False,
        hide_in_chat=False,
        silent=False,
        folder="",
        approval_mode="",
        managed_by=KEY,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _rows() -> list[dict]:
    path = config_dir() / "crons.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["jobs"] if isinstance(data, dict) else data


def _refused(args, capsys) -> str:
    with pytest.raises(SystemExit) as exc:
        _cron(args)
    assert exc.value.code == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("Error: ")
    return err


def _added_id(capsys, verb="Added job") -> str:
    out = capsys.readouterr().out
    assert out.startswith(f"{verb}: "), out
    return out.split()[2]


class TestAdd:
    def test_key_is_persisted_and_survives_a_reload(self, home, capsys):
        _cron(_add())
        job_id = _added_id(capsys)
        (row,) = _rows()
        assert row["id"] == job_id
        assert row["managed_by"] == KEY
        reloaded = CronService(base_dir=config_dir()).list_jobs(include_disabled=True)
        assert [j.managed_by for j in reloaded] == [KEY]

    def test_second_add_with_the_same_key_refuses_and_writes_nothing(self, home, capsys):
        _cron(_add())
        job_id = _added_id(capsys)
        before = _rows()
        err = _refused(_add(message="something else"), capsys)
        assert KEY in err and job_id in err and "cron remove --managed-by" in err
        assert _rows() == before

    def test_without_a_key_add_is_unchanged(self, home, capsys):
        _cron(_add(managed_by=None))
        _cron(_add(managed_by=None))
        capsys.readouterr()
        rows = _rows()
        assert len(rows) == 2
        # Unset is not written at all, so an unmanaged store keeps its bytes.
        assert all("managed_by" not in r for r in rows)

    @pytest.mark.parametrize(
        "bad",
        [" acme:x", "acme:x ", "-acme", "acme x", "acme\nx", "ａcme", "a" * 201, ""],
        ids=[
            "lead-space",
            "trail-space",
            "flag-like",
            "inner-space",
            "newline",
            "fullwidth",
            "too-long",
            "empty",
        ],
    )
    def test_malformed_keys_are_refused(self, bad, home, capsys):
        # "" included: an installer whose $KEY is unset passes an explicit
        # empty key, which must refuse rather than store a keyless job that
        # its own `remove --managed-by ""` could never find.
        with pytest.raises(ValueError):
            validate_managed_by(bad)
        _refused(_add(managed_by=bad), capsys)
        assert _rows() == []

    @pytest.mark.parametrize(
        "good", ["a", "acme:owner/asset-name", "x.y_z+1@host:2/p-q", "a" * 200]
    )
    def test_well_formed_keys_are_accepted(self, good):
        assert validate_managed_by(good) == good


class TestReinstall:
    """There is no replace-in-place: a re-install is remove then add."""

    def test_remove_then_add_reinstalls_under_a_fresh_id(self, home, capsys):
        # A fresh id is the point: every run-driven removal (a script raising
        # Done, a consumed one-shot, a deferred removal) deletes BY ID, so an
        # in-flight run of the old job can only remove the dead old id.
        _cron(_add())
        old_id = _added_id(capsys)
        _cron(argparse.Namespace(cron_action="remove", job_id=None, managed_by=KEY))
        capsys.readouterr()
        _cron(_add(message="v2"))
        new_id = _added_id(capsys)
        assert new_id != old_id
        (row,) = _rows()
        assert row["id"] == new_id and row["message"] == "v2" and row["managed_by"] == KEY

    def test_a_store_holding_the_key_twice_still_refuses_an_add(self, home, capsys):
        _cron(_add())
        capsys.readouterr()
        path = config_dir() / "crons.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        jobs = data["jobs"] if isinstance(data, dict) else data
        jobs.append(dict(jobs[0], id="deadbeef"))
        path.write_text(json.dumps(data), encoding="utf-8")
        before = _rows()
        err = _refused(_add(message="v2"), capsys)
        assert "deadbeef" in err
        assert _rows() == before


class TestRemove:
    def _remove(self, **kw) -> argparse.Namespace:
        return argparse.Namespace(
            cron_action="remove",
            job_id=kw.get("job_id"),
            managed_by=kw.get("managed_by"),
        )

    def test_remove_by_key_removes_only_that_job(self, home, capsys):
        _cron(_add())
        job_id = _added_id(capsys)
        _cron(_add(managed_by="acme:other", name="other"))
        capsys.readouterr()
        _cron(self._remove(managed_by=KEY))
        assert capsys.readouterr().out.strip() == f"Removed job: {job_id} (managed by {KEY})"
        assert [r.get("managed_by") for r in _rows()] == ["acme:other"]

    @pytest.mark.parametrize("bad", ["", " ", "-x", "a b"], ids=["empty", "space", "flag", "inner"])
    def test_remove_by_an_empty_or_malformed_key_refuses_and_deletes_nothing(
        self, bad, home, capsys
    ):
        # Every unmanaged job carries managed_by == "", so an installer whose
        # $KEY is unset must not be able to delete all of them.
        _cron(_add(managed_by=None))
        _cron(_add(managed_by=None, name="other"))
        capsys.readouterr()
        before = _rows()
        with pytest.raises(SystemExit) as exc:
            _cron(self._remove(managed_by=bad))
        assert exc.value.code == 1
        out, err = capsys.readouterr()
        assert out == "" and err.startswith("Error: ")
        assert _rows() == before

    def test_remove_by_unknown_key_says_so_and_writes_nothing(self, home, capsys):
        _cron(_add())
        capsys.readouterr()
        before = _rows()
        _cron(self._remove(managed_by="acme:nobody"))
        assert capsys.readouterr().out.strip() == "No job is managed by: acme:nobody"
        assert _rows() == before

    def test_remove_by_key_over_an_unreadable_store_refuses(self, home, capsys):
        (config_dir() / "crons.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            _cron(self._remove(managed_by=KEY))
        assert exc.value.code == 1
        assert capsys.readouterr().err.startswith("Error: ")

    def test_remove_by_id_still_works(self, home, capsys):
        _cron(_add())
        job_id = _added_id(capsys)
        _cron(self._remove(job_id=job_id))
        assert capsys.readouterr().out.strip() == f"Removed job: {job_id}"
        assert _rows() == []


class TestArgparse:
    def _parse(self, argv, monkeypatch):
        from kiro_crew import cli

        seen: dict[str, argparse.Namespace] = {}
        monkeypatch.setattr("kiro_crew.cli_commands._cron", lambda args: seen.update(args=args))
        monkeypatch.setattr(sys, "argv", ["kirocrew", "cron", *argv])
        cli.main()
        return seen.get("args")

    def test_add_flags_reach_the_handler(self, monkeypatch):
        args = self._parse(
            ["add", "n", "m", "--every", "300", "--managed-by", KEY],
            monkeypatch,
        )
        assert args.managed_by == KEY

    def test_add_defaults_mean_unmanaged(self, monkeypatch):
        args = self._parse(["add", "n", "m", "--every", "300"], monkeypatch)
        assert args.managed_by is None

    def test_there_is_no_replace_flag(self, monkeypatch, capsys):
        # Replace-in-place is deliberately absent until run-driven removals
        # are fenced by definition generation (see the cli spec row).
        with pytest.raises(SystemExit) as exc:
            self._parse(
                ["add", "n", "m", "--every", "300", "--managed-by", KEY, "--replace"], monkeypatch
            )
        assert exc.value.code == 2
        assert "--replace" in capsys.readouterr().err

    def test_remove_takes_a_key(self, monkeypatch):
        args = self._parse(["remove", "--managed-by", KEY], monkeypatch)
        assert args.managed_by == KEY and args.job_id is None

    def test_remove_still_takes_an_id(self, monkeypatch):
        args = self._parse(["remove", "abc12345"], monkeypatch)
        assert args.job_id == "abc12345" and args.managed_by is None

    @pytest.mark.parametrize(
        "argv",
        [["remove"], ["remove", "abc12345", "--managed-by", KEY]],
        ids=["neither", "both"],
    )
    def test_remove_needs_exactly_one_target(self, argv, monkeypatch):
        with pytest.raises(SystemExit) as exc:
            self._parse(argv, monkeypatch)
        assert exc.value.code == 2
