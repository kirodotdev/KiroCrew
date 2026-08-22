"""`backup status` and `backup list` -- the two subcommands nothing exercised."""

from __future__ import annotations

import argparse

import pytest

from kiro_crew import backup_cli
from kiro_crew import snapshot_remote as remote

DEST = remote.Destination(
    bucket="kirocrew-backup-123456789012-us-west-2",
    region="us-west-2",
    account="123456789012",
    created_at="2026-01-01T00:00:00Z",
)


def _args(**kw) -> argparse.Namespace:
    base = {"aws_profile": None, "offline": False, "backup_cmd": None}
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(remote, "load_destination", lambda: DEST)
    monkeypatch.setattr(backup_cli, "_profile_and_region", lambda a: ("prof", "us-west-2"))


class TestStatusReportsWhatIsConfigured:
    def test_it_refuses_when_nothing_is_configured(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        def not_configured():
            raise remote.DestinationNotConfigured("run `kirocrew backup setup` first")

        monkeypatch.setattr(remote, "load_destination", not_configured)

        rc = backup_cli.status_main(_args())

        assert rc == 1
        assert "No backup destination configured" in capsys.readouterr().out

    def test_offline_reports_the_record_without_touching_the_network(
        self, configured, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        def must_not_run(*a, **kw):  # pragma: no cover - the assertion is that it is not called
            raise AssertionError("offline status made a live call")

        monkeypatch.setattr(remote, "verify_bucket_private", must_not_run)

        rc = backup_cli.status_main(_args(offline=True))

        out = capsys.readouterr().out
        assert rc == 0
        assert DEST.bucket in out
        assert DEST.account in out

    def test_an_unreachable_bucket_is_a_warning_not_a_failure(
        self, configured, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """The recorded destination is still true when the network is not answering."""

        def unreachable(*a, **kw):
            raise OSError("connection reset")

        monkeypatch.setattr(remote, "verify_bucket_private", unreachable)

        rc = backup_cli.status_main(_args())

        assert rc == 0, "a network problem was reported as a misconfigured destination"
        assert "Could not check the live bucket" in capsys.readouterr().out

    def test_a_bucket_that_lost_its_controls_fails_and_says_how_to_fix_it(
        self, configured, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        monkeypatch.setattr(remote, "verify_bucket_private", lambda *a, **kw: {"public": True})
        monkeypatch.setattr(remote, "is_fully_private", lambda r: False)

        rc = backup_cli.status_main(_args())

        out = capsys.readouterr().out
        assert rc == 1
        assert "backup setup" in out, "the failure does not say how to re-apply the controls"

    def test_a_healthy_bucket_passes(
        self, configured, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        monkeypatch.setattr(remote, "verify_bucket_private", lambda *a, **kw: {})
        monkeypatch.setattr(remote, "is_fully_private", lambda r: True)

        assert backup_cli.status_main(_args()) == 0
        assert "private, encrypted, versioned" in capsys.readouterr().out


class TestListShowsWhatIsInTheBucket:
    def test_it_refuses_when_nothing_is_configured(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        def not_configured():
            raise remote.DestinationNotConfigured("nothing recorded")

        monkeypatch.setattr(remote, "load_destination", not_configured)

        assert backup_cli.list_main(_args()) == 1
        assert "nothing recorded" in capsys.readouterr().out

    def test_an_empty_bucket_is_not_an_error(
        self, configured, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        monkeypatch.setattr(remote, "list_backups", lambda d, p: {})

        rc = backup_cli.list_main(_args())

        assert rc == 0, "having taken no backups yet is not a failure"
        assert "No backups yet" in capsys.readouterr().out

    def test_a_transfer_failure_is_reported_as_one(
        self, configured, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        def boom(d, p):
            raise remote.UPLOAD_FAILURES[0]("denied")

        monkeypatch.setattr(remote, "list_backups", boom)

        assert backup_cli.list_main(_args()) == 1
        assert "denied" in capsys.readouterr().out

    def test_this_host_is_marked_and_older_entries_are_summarised(
        self, configured, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        mine = "thishost"
        monkeypatch.setattr(remote, "host_id", lambda: mine)
        monkeypatch.setattr(
            remote,
            "list_backups",
            lambda d, p: {
                mine: [f"backups/{mine}/snap-{i}.tar.gz" for i in range(7)],
                "otherhost": ["backups/otherhost/snap-a.tar.gz"],
            },
        )

        rc = backup_cli.list_main(_args())

        out = capsys.readouterr().out
        assert rc == 0
        assert "(this host)" in out
        assert "otherhost" in out
        assert "2 older" in out, "the tail was neither shown nor counted"

    def test_a_hostile_object_key_cannot_drive_the_terminal(
        self, configured, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """Host segments and keys come from S3, which any bucket writer controls."""
        monkeypatch.setattr(remote, "host_id", lambda: "thishost")
        monkeypatch.setattr(
            remote,
            "list_backups",
            lambda d, p: {"ev\x1b[2Jil": ["backups/ev\x1b[2Jil/\x1b[31msnap.tar.gz"]},
        )

        rc = backup_cli.list_main(_args())

        out = capsys.readouterr().out
        assert rc == 0
        assert "\x1b" not in out, "a raw escape sequence from an object key reached the terminal"


class TestTheSubcommandDispatch:
    @pytest.mark.parametrize("sub", ["setup", "status", "list"])
    def test_each_subcommand_reaches_its_own_handler(
        self, sub: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called: list[str] = []
        for name in ("setup_main", "status_main", "list_main"):
            monkeypatch.setattr(backup_cli, name, lambda a, _n=name: (called.append(_n), 0)[1])

        assert backup_cli.backup_main(_args(backup_cmd=sub)) == 0
        assert called == [f"{sub}_main"]

    def test_an_unknown_subcommand_prints_usage_and_refuses(self, capsys) -> None:
        rc = backup_cli.backup_main(_args(backup_cmd="nonsense"))

        assert rc == 2
        assert "setup|status|list" in capsys.readouterr().out
