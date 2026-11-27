"""Connected computers: the ``kirocrew connected-computer`` verbs' refusals, views and dispatch.

The remote verbs run against a registry in a ``tmp_path`` vault and the local
verbs against the in-memory protector of the protected-store tests, so no SSH,
keyring or terminal is touched.
"""

from __future__ import annotations

import argparse
import io
import json

import pytest
import test_connected_computers_protected as helpers

from kiro_crew.connected_computers import cli
from kiro_crew.connected_computers.devices import DeviceRegistry
from kiro_crew.connected_computers.local_store import LocalGrantStore
from kiro_crew.connected_computers.vault_doc import VaultDocError
from kiro_crew.secrets.vault import SecretVault

vault = helpers.vault
protector = helpers.protector
store = helpers.store
FINGERPRINT = "ab" * 32


@pytest.fixture
def remote(tmp_path, monkeypatch):
    """A remote gateway's registry, with the CLI's config and identity reads stubbed."""
    from kiro_crew import gateway_identity
    from kiro_crew.connected_computers import flag, vault_doc

    remote_vault = SecretVault(tmp_path / "remote-vault")
    monkeypatch.setattr(flag, "preview_on", lambda *a, **k: True)
    monkeypatch.setattr(vault_doc, "open_vault", lambda *a, **k: remote_vault)
    monkeypatch.setattr(gateway_identity, "gateway_id", lambda **_kw: "gateway-1")
    monkeypatch.setattr(cli.getpass, "getuser", lambda: "owner")
    return DeviceRegistry(remote_vault)


@pytest.fixture
def printed(monkeypatch):
    replies: list[dict] = []
    monkeypatch.setattr(cli, "_print", replies.append)
    return replies


def _enroll(registry, fingerprint=FINGERPRINT):
    return registry.enroll(base="laptop", fingerprint=fingerprint, label="Laptop", owner="owner")


class TestReadStdin:
    @pytest.mark.parametrize("raw", ["not json", "[1, 2]", '"text"', ""])
    def test_anything_but_a_json_object_is_none(self, monkeypatch, raw):
        monkeypatch.setattr("sys.stdin", io.StringIO(raw))
        assert cli._read_stdin() is None

    def test_a_json_object_is_returned(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO('{"device_id": "x"}'))
        assert cli._read_stdin() == {"device_id": "x"}


class TestRemoteEnrollRefusals:
    def test_no_json_request_is_refused(self, remote, printed, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO("hello"))
        assert cli._remote_enroll() == 2
        assert printed == [{"ok": False, "error": "a JSON request is required on stdin"}]
        assert remote.list() == []

    def test_a_gateway_with_no_id_is_refused(self, remote, printed, monkeypatch):
        from kiro_crew import gateway_identity

        monkeypatch.setattr(gateway_identity, "gateway_id", lambda **_kw: "")
        monkeypatch.setattr(cli, "_read_stdin", lambda: {"fingerprint": FINGERPRINT})
        assert cli._remote_enroll() == 2
        assert printed == [{"ok": False, "error": "this gateway has no id yet; start it once"}]
        assert remote.list() == []

    def test_a_removed_computer_is_refused_with_its_code(self, remote, printed, monkeypatch):
        record = _enroll(remote)
        remote.remove(record.device_id)
        monkeypatch.setattr(
            cli,
            "_read_stdin",
            lambda: {"base": "laptop", "fingerprint": FINGERPRINT, "label": "Laptop"},
        )
        replies = []
        monkeypatch.setattr(cli, "_reply_on_channel", replies.append)
        assert cli._remote_enroll() == 2
        assert replies == [], "no credential is sent"
        assert printed[0]["ok"] is False and printed[0]["code"] == "removed"
        assert "removed" in printed[0]["error"]
        assert not remote.get(record.device_id).active

    def test_an_invalid_fingerprint_is_refused_without_a_code(self, remote, printed, monkeypatch):
        monkeypatch.setattr(cli, "_read_stdin", lambda: {"base": "laptop", "fingerprint": "nope"})
        assert cli._remote_enroll() == 2
        assert printed == [{"ok": False, "error": "device fingerprint is invalid"}]
        assert remote.list() == []


class TestRemoteForget:
    def _body(self, monkeypatch, body):
        monkeypatch.setattr(cli, "_read_stdin", lambda: body)

    def test_an_unknown_enrollment_is_no_such_computer(self, remote, printed, monkeypatch):
        record = _enroll(remote)
        self._body(
            monkeypatch,
            {"device_id": record.device_id, "fingerprint": "cd" * 32, "cgen": record.cgen},
        )
        assert cli._remote_forget() == 2
        assert printed == [{"ok": False, "error": "no such computer"}]
        assert remote.get(record.device_id).active

    def test_a_broken_registry_is_reported(self, remote, printed, monkeypatch):
        record = _enroll(remote)

        def broken(self, *_args):
            raise VaultDocError("connected computers state is unreadable")

        monkeypatch.setattr(DeviceRegistry, "forget_enrollment", broken)
        self._body(
            monkeypatch,
            {"device_id": record.device_id, "fingerprint": FINGERPRINT, "cgen": record.cgen},
        )
        assert cli._remote_forget() == 2
        assert printed == [{"ok": False, "error": "connected computers state is unreadable"}]

    def test_no_body_is_already_replaced(self, remote, printed, monkeypatch):
        record = _enroll(remote)
        self._body(monkeypatch, None)
        assert cli._remote_forget() == 0
        assert printed == [{"ok": True, "status": "already replaced"}]
        assert remote.get(record.device_id).active


class TestRemoteViews:
    def test_devices_as_json_never_shows_the_fingerprint(self, remote, printed):
        record = _enroll(remote)
        assert cli._remote_devices(True) == 0
        assert printed == [{"devices": [record.public()]}]
        assert FINGERPRINT not in json.dumps(printed)

    def test_devices_as_text_is_one_line_each(self, remote, capsys):
        first = _enroll(remote)
        second = _enroll(remote, "cd" * 32)
        assert cli._remote_devices(False) == 0
        lines = capsys.readouterr().out.splitlines()
        expected = sorted([first, second], key=lambda r: r.device_id)
        assert lines == [f"{r.device_id}\t{r.status}\t{r.label}" for r in expected]

    def test_ending_an_unknown_computer_fails(self, remote, capsys):
        assert cli._remote_end("nobody", remove=True) == 1
        captured = capsys.readouterr()
        assert "no computer 'nobody' is enrolled here" in captured.err
        assert captured.out == ""

    def test_removing_a_computer_reports_its_status(self, remote, capsys):
        record = _enroll(remote)
        assert cli._remote_end(record.device_id, remove=True) == 0
        assert capsys.readouterr().out.startswith(f"{record.device_id}: removed;")
        assert not remote.get(record.device_id).active


class TestLocalViews:
    def test_the_local_store_opens_the_gateway_vault(self, tmp_path, monkeypatch):
        from kiro_crew.connected_computers import vault_doc

        local_vault = SecretVault(tmp_path / "local")
        monkeypatch.setattr(vault_doc, "open_vault", lambda *a, **k: local_vault)
        opened = cli._store()
        assert isinstance(opened, LocalGrantStore)
        assert opened._doc._vault is local_vault
        assert opened.list() == []

    def test_status_as_json_lists_each_grant_without_secrets(self, store, printed, monkeypatch):
        grant = store.add(helpers._grant())
        monkeypatch.setattr(cli, "_store", lambda: store)
        assert cli._local_status(True) == 0
        assert printed == [{"grants": [store.get(grant.grant_id).public()]}]
        assert "cred-secret" not in json.dumps(printed)

    def test_status_as_text_is_one_line_each(self, store, capsys, monkeypatch):
        grant = store.add(helpers._grant())
        monkeypatch.setattr(cli, "_store", lambda: store)
        assert cli._local_status(False) == 0
        assert capsys.readouterr().out.splitlines() == [
            f"{grant.grant_id}\tactive\tlaptop\t/work/proj"
        ]

    def test_the_instance_lookup_is_the_registry_get(self, monkeypatch):
        from kiro_crew.instances import registry

        class _Registry:
            def get(self, instance_id):
                return ("found", instance_id)

        monkeypatch.setattr(registry, "InstancesRegistry", _Registry)
        assert cli._instance_lookup()("inst-1") == ("found", "inst-1")


class _Stopped:
    """A store whose Off cannot be saved."""

    def revoke_all(self):
        raise VaultDocError("connected computers state could not be saved")

    def pending_forgets(self):
        raise AssertionError("nothing is read after a failed Off")


class TestLocalOff:
    def test_an_off_that_cannot_be_saved_fails(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_store", _Stopped)
        assert cli._local_off() == 2
        assert "could not turn access off" in capsys.readouterr().err

    def test_an_off_with_nothing_queued_does_not_wait(self, store, monkeypatch, capsys):
        waited = []
        monkeypatch.setattr(cli, "_store", lambda: store)
        monkeypatch.setattr(cli, "_wait_for_local_stop", lambda: waited.append(True))
        generation = store.off_generation()
        assert cli._local_off() == 0
        assert store.off_generation() == generation + 1
        assert waited == []
        out = capsys.readouterr().out
        assert "Turned off 0 grant(s)" in out and "Waiting" not in out

    def test_unreachable_remotes_stay_queued_and_are_reported(self, store, monkeypatch, capsys):
        store.install_key()
        store.add(helpers._grant("inst-a"))
        store.add(helpers._grant("inst-b"))
        steps = []
        monkeypatch.setattr(cli, "_store", lambda: store)
        monkeypatch.setattr(cli, "_wait_for_local_stop", lambda: steps.append("waited"))

        def tell(current):
            steps.append("told")
            assert current is store
            return 1, 1

        monkeypatch.setattr(cli, "_tell_remotes", tell)
        assert cli._local_off() == 0
        assert steps == ["waited", "told"]
        assert store.active() == []
        out = capsys.readouterr().out
        assert "Turned off 2 grant(s)" in out
        assert "Told 1 remote gateway(s)." in out
        assert "1 remote gateway(s) could not be reached" in out


class _Terminal(io.StringIO):
    def __init__(self, text: str = "", *, tty: bool = True) -> None:
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class TestResetProtection:
    @pytest.mark.parametrize("stdin_tty,stdout_tty", [(False, True), (True, False)])
    def test_it_needs_an_interactive_terminal(self, monkeypatch, stdin_tty, stdout_tty, store):
        out = _Terminal(tty=stdout_tty)
        err = _Terminal(tty=False)
        monkeypatch.setattr(cli.sys, "stdin", _Terminal(tty=stdin_tty))
        monkeypatch.setattr(cli.sys, "stdout", out)
        monkeypatch.setattr(cli.sys, "stderr", err)
        monkeypatch.setattr(cli, "_store", lambda: store)
        monkeypatch.setattr("builtins.input", pytest.fail)
        assert cli._local_reset_protection() == 2
        assert "interactive terminal" in err.getvalue()
        assert out.getvalue() == ""

    def test_anything_but_reset_cancels(self, monkeypatch, store):
        grant = store.add(helpers._grant())
        out = _Terminal()
        monkeypatch.setattr(cli.sys, "stdin", _Terminal())
        monkeypatch.setattr(cli.sys, "stdout", out)
        monkeypatch.setattr(cli, "_store", lambda: store)
        monkeypatch.setattr("builtins.input", lambda _prompt: "  yes ")
        assert cli._local_reset_protection() == 1
        assert "Cancelled; nothing changed." in out.getvalue()
        assert store.get(grant.grant_id).active

    def test_typing_reset_turns_every_grant_off(self, monkeypatch, store):
        store.install_key()
        grant = store.add(helpers._grant())
        generation = store.off_generation()
        prompts = []
        out = _Terminal()
        monkeypatch.setattr(cli.sys, "stdin", _Terminal())
        monkeypatch.setattr(cli.sys, "stdout", out)
        monkeypatch.setattr(cli, "_store", lambda: store)
        monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or " reset\n")
        assert cli._local_reset_protection() == 0
        assert len(prompts) == 1 and "Type reset" in prompts[0]
        assert "Turned off 1 grant(s) and reset protection." in out.getvalue()
        assert not store.get(grant.grant_id).active
        assert store.off_generation() == generation + 1
        assert [p.instance_id for p in store.pending_forgets()] == ["inst-1"]


class TestRun:
    @pytest.fixture
    def verbs(self, monkeypatch):
        from kiro_crew.connected_computers import desktop_host

        monkeypatch.setattr(cli, "_remote_enroll", lambda: "enroll")
        monkeypatch.setattr(cli, "_remote_forget", lambda: "forget")
        monkeypatch.setattr(cli, "_remote_devices", lambda as_json: ("devices", as_json))
        monkeypatch.setattr(
            cli, "_remote_end", lambda device_id, *, remove: ("end", device_id, remove)
        )
        monkeypatch.setattr(cli, "_local_status", lambda as_json: ("status", as_json))
        monkeypatch.setattr(cli, "_local_off", lambda: "off")
        monkeypatch.setattr(cli, "_local_reset_protection", lambda: "reset")
        monkeypatch.setattr(desktop_host, "run_desktop_host", lambda: "desktop")

    @pytest.mark.parametrize(
        "argv,expected",
        [
            (["enroll", "--stdin"], "enroll"),
            (["forget", "--stdin"], "forget"),
            (["devices", "--json"], ("devices", True)),
            (["devices"], ("devices", False)),
            (["revoke", "dev-1"], ("end", "dev-1", False)),
            (["remove", "dev-1"], ("end", "dev-1", True)),
            (["status", "--json"], ("status", True)),
            (["off"], "off"),
            (["reset-protection"], "reset"),
            (["desktop-host"], "desktop"),
        ],
    )
    def test_each_parsed_verb_reaches_its_handler(self, verbs, argv, expected):
        parser = argparse.ArgumentParser()
        cli.register_parser(parser.add_subparsers(dest="command"))
        args = parser.parse_args(["connected-computer", *argv])
        assert cli.run(args) == expected

    def test_an_unknown_verb_exits_2(self, verbs):
        with pytest.raises(SystemExit) as info:
            cli.run(argparse.Namespace(cc_command="share"))
        assert info.value.code == 2
