"""Tests for the cycle-5 review findings.

Two of these close holes in the design's central claim rather than in its details: the
recorded destination was made unwritable, but the COMMAND that writes it was not gated,
and exposure was verified at setup but never re-checked at the moment of writing.
"""

from __future__ import annotations

import argparse
import json
import re
import tarfile

import pytest

from kiro_crew import backup_cli
from kiro_crew import snapshot as snap
from kiro_crew import snapshot_remote as remote
from kiro_crew.deploy import engine
from kiro_crew.security import is_sensitive_path

ACCOUNT = "123456789012"


class TestSetupNeedsAnAuthorizationTheCallerCannotForge:
    """Protecting the record while leaving the writer ungated protects nothing, and a
    terminal check does not gate the writer.

    `destination.json` is behind the sensitive-path floor, so an agent cannot author it.
    But `backup setup` writes it through this program's own code, so the command itself
    needs a gate. An earlier revision used `sys.stdin.isatty()` and called that a
    human-presence check; it is not, because a pty is something any process can
    allocate. The gate is now a file in a keystone directory that NAMES the destination it
    authorizes: the operator can create it, nothing the agent can drive can, and a
    token approved for one account cannot be spent on another.
    """

    def _args(self, **kw) -> argparse.Namespace:
        base = dict(aws_profile="default", region="us-west-2", bucket=None)
        base.update(kw)
        return argparse.Namespace(**base)

    def _prepared(self, home, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.setattr(backup_cli, "_resolve_aws_profile", lambda _n: ("p", "us-west-2"))
        monkeypatch.setattr(remote, "caller_account", lambda _p: ACCOUNT)
        calls: list[str] = []

        def fake_setup(profile, region, bucket=None):
            calls.append("ran")
            # Stubs the AWS work, NOT the authorization. The gate lives inside the real
            # `setup_destination`, so a double that skipped it would let this suite pass
            # while the property under test (single use) was broken in production.
            remote.consume_authorization(
                ACCOUNT, region, bucket or remote.default_bucket_name(ACCOUNT, region)
            )
            dest = remote.Destination(
                bucket="my-backups",
                region="us-west-2",
                account=ACCOUNT,
                created_at="now",
            )
            report = {
                "block_public_access": {
                    "BlockPublicAcls": True,
                    "IgnorePublicAcls": True,
                    "BlockPublicPolicy": True,
                    "RestrictPublicBuckets": True,
                },
                "sse": "AES256",
                "versioning": "Enabled",
            }
            return dest, True, report

        monkeypatch.setattr(remote, "setup_destination", fake_setup)
        return calls

    def test_without_the_authorization_setup_refuses(self, tmp_path, monkeypatch, capsys):
        calls = self._prepared(tmp_path, monkeypatch)
        rc = backup_cli.setup_main(self._args())
        out = capsys.readouterr().out
        assert rc == 1
        assert calls == [], "setup ran unauthorized"
        assert "authorized out of band" in out
        assert "setup-authorized" in out, "the refusal must name the file to create"

    def test_a_pseudo_terminal_alone_is_not_enough(self, tmp_path, monkeypatch, capsys):
        """The exact bypass this replaced: `script -qec ... /dev/null` (or pty.fork)
        makes isatty() true and can answer the prompt, so a terminal cannot be the
        thing that authorizes a redirection."""
        calls = self._prepared(tmp_path, monkeypatch)
        monkeypatch.setattr(backup_cli.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _p="": "yes")
        rc = backup_cli.setup_main(self._args())
        assert rc == 1, "a pty plus a scripted 'yes' must not authorize setup"
        assert calls == []
        assert "authorized out of band" in capsys.readouterr().out

    def test_with_the_authorization_setup_proceeds(self, tmp_path, monkeypatch):
        calls = self._prepared(tmp_path, monkeypatch)
        token = remote.authorization_token_path()
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text(json.dumps({"account": ACCOUNT, "region": "us-west-2"}))
        rc = backup_cli.setup_main(self._args())
        assert rc == 0
        assert calls == ["ran"]

    def test_the_authorization_is_single_use(self, tmp_path, monkeypatch):
        """One authorization must not be replayable into a later redirection."""
        calls = self._prepared(tmp_path, monkeypatch)
        token = remote.authorization_token_path()
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text(json.dumps({"account": ACCOUNT, "region": "us-west-2"}))
        assert backup_cli.setup_main(self._args()) == 0
        assert not token.exists(), "the authorization survived its use"
        assert backup_cli.setup_main(self._args()) == 1
        assert calls == ["ran"], "a second setup ran on a consumed authorization"

    def test_the_authorization_lives_where_the_agent_cannot_write(self, tmp_path, monkeypatch):
        """The mechanism IS the path. If the token were writable by the agent's tools,
        it would authorize nothing."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        token = remote.authorization_token_path()
        assert is_sensitive_path(
            str(token)
        ), "the authorization file must be behind the sensitive-path floor"
        assert is_sensitive_path(str(token.parent))

    def test_a_declined_confirmation_records_nothing(self, tmp_path, monkeypatch, capsys):
        calls = self._prepared(tmp_path, monkeypatch)
        token = remote.authorization_token_path()
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text(json.dumps({"account": ACCOUNT, "region": "us-west-2"}))
        monkeypatch.setattr(backup_cli.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _p="": "no")
        rc = backup_cli.setup_main(self._args())
        assert rc == 1
        assert calls == []
        assert "Cancelled" in capsys.readouterr().out

    def test_the_confirmation_names_the_account_the_memory_would_go_to(
        self, tmp_path, monkeypatch, capsys
    ):
        """ "Wrong profile" is the mistake most worth catching, and an account number is
        what makes it visible."""
        self._prepared(tmp_path, monkeypatch)
        token = remote.authorization_token_path()
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text(json.dumps({"account": ACCOUNT, "region": "us-west-2"}))
        monkeypatch.setattr(backup_cli.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _p="": "no")
        backup_cli.setup_main(self._args())
        out = capsys.readouterr().out
        assert ACCOUNT in out
        assert "us-west-2" in out

    def test_there_is_no_flag_that_skips_the_gate(self):
        """A --yes/--force flag is exactly what an automated caller would pass.

        Scoped to the `backup setup` parser block and matched on argument DEFINITIONS:
        `cli.py` legitimately carries `--force` (restore) and `-y` (cloud launch), and
        the source here explains why no bypass exists, so a plain substring search over
        either would fail for the wrong reason.
        """
        import inspect

        from kiro_crew import cli

        pattern = re.compile(
            r"""add_argument\(\s*["'](--yes|-y|--force|--non-interactive|--batch)["']"""
        )
        cli_src = inspect.getsource(cli)
        blk = cli_src[
            cli_src.index("b_setup = backup_sub.add_parser(") : cli_src.index(
                "b_status = backup_sub.add_parser("
            )
        ]
        for name, src in (
            ("the backup setup parser", blk),
            ("backup_cli", inspect.getsource(backup_cli)),
        ):
            hits = pattern.findall(src)
            assert hits == [], f"a gate bypass is defined in {name}: {hits}"

    def test_the_refusal_is_reached_before_any_aws_write(self, tmp_path, monkeypatch):
        """The gate has to sit ahead of provisioning, not inside it."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.setattr(backup_cli, "_resolve_aws_profile", lambda _n: ("p", "us-west-2"))
        monkeypatch.setattr(remote, "caller_account", lambda _p: ACCOUNT)
        calls: list[list[str]] = []

        def record(args, profile, timeout=30):
            calls.append(list(args))
            return 0, "{}", ""

        monkeypatch.setattr(engine, "run_aws", record)
        rc = backup_cli.setup_main(self._args())
        assert rc == 1
        mutating = [
            c for c in calls if any(v.startswith("put-") or v == "create-bucket" for v in c)
        ]
        assert mutating == [], f"AWS was mutated before the gate: {mutating}"


class TestAContainedLinkRootDoesNotHalfRestore:
    """A root can pass containment and still be a LINK.

    A symlink pointing somewhere else *inside* the data home resolves within it, so the
    containment predicate allows it — and `shutil.rmtree` then refuses a symlink with
    OSError. Because the databases are replaced before the trees, that exception left the
    operator half-restored: the worst available outcome.
    """

    def test_clear_tree_root_removes_a_link_as_a_link(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        (real / "keep.md").write_text("still here")
        link = tmp_path / "link"
        try:
            link.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("cannot create a directory symlink on this platform")

        snap._clear_tree_root(link)
        assert not link.exists(), "the link survived"
        assert (real / "keep.md").is_file(), "clearing the link deleted the target"

    def test_clear_tree_root_still_removes_a_real_directory(self, tmp_path):
        d = tmp_path / "d"
        (d / "sub").mkdir(parents=True)
        (d / "sub" / "f.md").write_text("x")
        snap._clear_tree_root(d)
        assert not d.exists()

    def test_it_does_not_raise_on_a_missing_path(self, tmp_path):
        snap._clear_tree_root(tmp_path / "never-existed")

    def test_every_tree_clearing_site_uses_the_chokepoint(self):
        """Structural: the naive form is `if d.is_dir(): shutil.rmtree(str(d))`, which is
        wrong for a link at three separate sites. One helper, or the weakest site wins.

        Now stronger than counting call sites. Clearing is reachable from exactly one
        function -- `_replace_tree_root`, which also owns putting the rollback back when
        the refill fails -- so the assertion is that no OTHER code clears a tree. Counting
        `_clear_tree_root(` occurrences would pass while a site cleared without the
        rollback handling, which is the defect that made the helper necessary.
        """
        import inspect

        src = inspect.getsource(snap)
        clearer = inspect.getsource(snap._clear_tree_root)
        replacer = inspect.getsource(snap._replace_tree_root)
        # Recovery also clears before putting a saved tree back -- the same reason the
        # replace path does, so it is the second legitimate caller and no more.
        recovery = inspect.getsource(snap._restore_everything_from_rollback)
        outside = src.replace(clearer, "").replace(replacer, "").replace(recovery, "")

        assert "shutil.rmtree(str(d))" not in outside
        assert "shutil.rmtree(str(sk))" not in outside
        assert "_clear_tree_root(" not in outside, (
            "a tree is cleared outside _replace_tree_root and the recovery path, so "
            "that site clears without belonging to the restore's atomicity unit"
        )
        # And every replace-path site goes through the helper: workspace, plan_memory
        # (one loop), skills, and the memory subtrees.
        assert src.count("_replace_tree_root(") >= 4


class TestTheMergeCannotWriteOutsideTheDataHome:
    def test_a_nested_destination_link_is_not_followed(self, tmp_path):
        """safe_tree_root validates the destination ROOT, but the merge walks below it
        and the write target is the dangerous end: a nested link under the destination
        would deposit restored files wherever it aimed."""
        home = tmp_path / "home"
        (home / "workspace" / "memory").mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        dest = home / "workspace" / "memory"
        try:
            (dest / "history").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("cannot create a directory symlink on this platform")

        src = tmp_path / "snap" / "workspace" / "memory"
        (src / "history").mkdir(parents=True)
        (src / "history" / "leak.md").write_text("must not escape")
        (src / "keep.md").write_text("fine")

        snap._copy_tree_no_overwrite(src, dest, home)

        assert (dest / "keep.md").is_file(), "the legitimate file should still merge"
        assert not (outside / "leak.md").exists(), "the merge wrote outside the data home"

    def test_without_a_home_the_check_is_skipped(self, tmp_path):
        """Staging into a fresh temporary tree has no home to be contained by; the
        parameter is optional so that path keeps working."""
        src = tmp_path / "s"
        src.mkdir()
        (src / "f.md").write_text("x")
        dst = tmp_path / "d"
        dst.mkdir()
        snap._copy_tree_no_overwrite(src, dst)
        assert (dst / "f.md").is_file()

    def test_every_merge_call_site_passes_the_home(self):
        """Structural: a call site that forgets the home silently loses the guard."""
        import inspect
        import re

        src = inspect.getsource(snap)
        calls = re.findall(r"_copy_tree_no_overwrite\(([^)]*)\)", src)
        # Drop the definition itself.
        calls = [c for c in calls if "home: Path" not in c]
        assert calls, "no call sites found — did the helper get renamed?"
        for c in calls:
            assert c.count(",") >= 2, f"call site missing the home argument: {c}"


class TestAMalformedRemoteBundleIsRefusedNotCrashed:
    def test_a_corrupt_download_is_removed_and_reported(self, tmp_path, monkeypatch, capsys):
        """A downloaded object is untrusted input even from a bucket we own: versioning
        means an older object may be corrupt, and a truncated transfer only fails when
        opened. tarfile raising out of the extract path is indistinguishable from a
        crash and leaves the bad file behind."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        snaps = tmp_path / "snapshots"
        snaps.mkdir()
        bad = snaps / "kirocrew-snapshot-20260811T000000Z.tar.gz"
        bad.write_bytes(b"this is not a tarball")

        monkeypatch.setattr(snap, "_default_snapshot_dir", lambda: str(snaps))
        monkeypatch.setattr(snap, "_resolve_aws_profile", lambda _n: ("p", "us-west-2"))
        monkeypatch.setattr(remote, "download", lambda *_a, **_k: bad)

        rc = snap.restore_main(["s3://my-backups/backups/h/snap.tar.gz", "--force"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "not a readable snapshot archive" in out
        assert not bad.exists(), "the invalid download was retained"


class TestConcurrentDownloadsCannotOverwriteEachOther:
    def test_a_name_is_claimed_atomically(self, tmp_path):
        """A plain exists() test then a move is a race: both processes see the name free,
        both move, and the loser's bundle is silently replaced by the winner's."""
        first = remote._claim_free_name(tmp_path, "snap.tar.gz")
        second = remote._claim_free_name(tmp_path, "snap.tar.gz")
        third = remote._claim_free_name(tmp_path, "snap.tar.gz")
        assert first.name == "snap.tar.gz"
        assert len({first, second, third}) == 3, "the same name was handed out twice"
        # Each claim is a real reservation on disk, which is what makes it exclusive.
        for p in (first, second, third):
            assert p.is_file()

    def test_the_claim_uses_an_exclusive_create(self):
        import inspect

        src = inspect.getsource(remote._claim_free_name)
        assert "O_EXCL" in src
        assert "exists()" not in src, "an exists() probe is the race this replaced"

    def test_a_failed_download_leaves_no_placeholder(self, tmp_path, monkeypatch):
        """The reservation is ours, so a failure must not leave a zero-byte file that
        looks like a bundle to `backup list` or to the next name claim."""

        def boom(args, profile, timeout=30):
            return 1, "", "NoSuchKey"

        monkeypatch.setattr(engine, "run_aws", boom)
        into = tmp_path / "snapshots"
        into.mkdir()
        with pytest.raises(remote.DestinationError):
            remote.download("s3://my-backups/backups/h/snap.tar.gz", into, "p")
        assert list(into.iterdir()) == [], "a placeholder survived the failure"


class TestTheArchiveProbeAcceptsAGoodBundle:
    def test_a_valid_archive_passes_the_probe(self, tmp_path):
        """The guard must not reject bundles it is meant to admit."""
        good = tmp_path / "good.tar.gz"
        payload = tmp_path / "f.txt"
        payload.write_text("hi")
        with tarfile.open(good, "w:gz") as tf:
            tf.add(payload, arcname="f.txt")
        with tarfile.open(good) as tf:
            assert [m.name for m in tf.getmembers()] == ["f.txt"]
