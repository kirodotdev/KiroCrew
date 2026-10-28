"""``kirocrew cron add/update/list --folder``: parity with the ``cron_add`` / ``cron_update`` tools.

The CLI files a job into a Schedule-page folder through the SAME resolver the MCP
tools run (``mcp_cron._resolve_cron_folder_via``), and a missing name is created
through the running gateway's ``POST /api/cron-folders`` -- the dashboard handler
that owns ``cron_folders.json``. These tests pin that contract without a gateway:
the CLI's create leg, ``cli_commands._create_cron_folder``, is replaced by a fake
that records each call, and every test asserts the folder file the CLI must never
write is byte-for-byte unchanged.

Every store lives under a per-test ``KIROCREW_HOME`` in ``tmp_path``.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
from unittest.mock import MagicMock

import pytest

from kiro_crew import cli_commands, mcp_cron
from kiro_crew.config.loader import config_dir
from kiro_crew.cron import CronService

_FOLDERS = [
    {"id": "aaaa1111", "name": "Veille", "order": 0},
    {"id": "bbbb2222", "name": "Apps", "order": 1},
]


def _folders_file():
    return config_dir() / "cron_folders.json"


def _write_folders(entries) -> None:
    _folders_file().write_text(json.dumps(entries), encoding="utf-8")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    root = tmp_path / "kirocrew-home"
    root.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(root))
    config_dir().mkdir(parents=True, exist_ok=True)
    _write_folders(_FOLDERS)
    return root


@pytest.fixture()
def creates(monkeypatch):
    """Replace the CLI's gateway create leg; record every name it is asked to create."""
    calls: list[str] = []

    def _fake(name: str) -> dict:
        calls.append(name)
        return {"id": "dddd4444", "name": name, "order": 2}

    monkeypatch.setattr(cli_commands, "_create_cron_folder", _fake)
    return calls


@pytest.fixture()
def no_create(monkeypatch):
    def _boom(name: str) -> dict:
        raise AssertionError(f"folder create issued for {name!r}")

    monkeypatch.setattr(cli_commands, "_create_cron_folder", _boom)


@pytest.fixture()
def audit(monkeypatch):
    log = MagicMock()
    monkeypatch.setattr(cli_commands, "sel", lambda: log)
    return log


def _add_args(name: str, folder: str) -> argparse.Namespace:
    return argparse.Namespace(
        cron_action="add",
        name=name,
        message="go",
        every=300,
        cron_expr=None,
        channel=None,
        approval_mode="",
        agent="",
        silent=False,
        folder=folder,
    )


def _update_args(job_id: str, folder, **over) -> argparse.Namespace:
    fields = {
        "cron_action": "update",
        "job_id": job_id,
        "name": None,
        "message": None,
        "every_secs": None,
        "cron_expr": None,
        "channel": None,
        "approval_mode": None,
        "folder": folder,
    }
    fields.update(over)
    return argparse.Namespace(**fields)


def _jobs(name: str):
    return [
        j
        for j in CronService(base_dir=config_dir()).list_jobs(include_disabled=True)
        if j.name == name
    ]


def _seed_job(name: str, folder_id: str = "") -> str:
    svc = CronService(base_dir=config_dir())
    return svc.add_job(name, "go", every_secs=300, folder_id=folder_id).id


# ---------------------------------------------------------------------------
# cron add --folder
# ---------------------------------------------------------------------------


class TestCliAddFolder:
    def test_existing_name_files_the_job_without_a_create(self, home, no_create, capsys):
        before = _folders_file().read_bytes()
        cli_commands._cron_dispatch(_add_args("by-name", "veille"))
        assert [j.folder_id for j in _jobs("by-name")] == ["aaaa1111"]
        assert _folders_file().read_bytes() == before

    def test_existing_id_files_the_job_without_a_create(self, home, no_create, capsys):
        cli_commands._cron_dispatch(_add_args("by-id", "bbbb2222"))
        assert [j.folder_id for j in _jobs("by-id")] == ["bbbb2222"]

    def test_missing_name_is_created_once_through_the_gateway_then_filed(
        self, home, creates, capsys
    ):
        before = _folders_file().read_bytes()
        cli_commands._cron_dispatch(_add_args("fresh-job", "Fresh"))
        assert creates == ["Fresh"]
        assert [j.folder_id for j in _jobs("fresh-job")] == ["dddd4444"]
        # The dashboard owns the file: the CLI's create went to the gateway only.
        assert _folders_file().read_bytes() == before

    def test_id_shaped_miss_is_refused_with_no_create_and_no_job(self, home, no_create, capsys):
        with pytest.raises(SystemExit) as exc:
            cli_commands._cron_dispatch(_add_args("orphan", "deadbeef"))
        assert exc.value.code == 1
        assert "minted" in capsys.readouterr().err
        assert _jobs("orphan") == []

    def test_ambiguous_name_is_refused_with_no_create(self, home, no_create, capsys):
        _write_folders(_FOLDERS + [{"id": "cccc3333", "name": "VEILLE", "order": 2}])
        with pytest.raises(SystemExit):
            cli_commands._cron_dispatch(_add_args("orphan", "Veille"))
        assert "pass the folder id" in capsys.readouterr().err
        assert _jobs("orphan") == []

    def test_unreadable_store_is_refused_with_no_create(self, home, no_create, capsys):
        _folders_file().write_text("{}", encoding="utf-8")
        with pytest.raises(SystemExit):
            cli_commands._cron_dispatch(_add_args("orphan", "Fresh"))
        assert "unreadable" in capsys.readouterr().err
        assert _jobs("orphan") == []
        assert _folders_file().read_text(encoding="utf-8") == "{}"

    def test_gateway_failure_is_reported_and_leaves_no_job(self, home, monkeypatch, capsys):
        monkeypatch.setattr(
            cli_commands,
            "_create_cron_folder",
            lambda name: {"error": "gateway not running: start it or pass an existing folder"},
        )
        with pytest.raises(SystemExit) as exc:
            cli_commands._cron_dispatch(_add_args("orphan", "Fresh"))
        assert exc.value.code == 1
        assert "gateway not running" in capsys.readouterr().err
        assert _jobs("orphan") == []

    def test_a_refused_job_never_creates_a_folder(self, home, no_create, capsys):
        args = _add_args("bad-channel", "Fresh")
        args.channel = "not a channel id!"
        with pytest.raises(SystemExit):
            cli_commands._cron_dispatch(args)
        assert _jobs("bad-channel") == []


# ---------------------------------------------------------------------------
# cron update --folder
# ---------------------------------------------------------------------------


class TestCliUpdateFolder:
    def test_refiles_into_an_existing_folder(self, home, no_create, audit, capsys):
        job_id = _seed_job("mover", "aaaa1111")
        cli_commands._cron_dispatch(_update_args(job_id, "Apps"))
        assert [j.folder_id for j in _jobs("mover")] == ["bbbb2222"]
        assert capsys.readouterr().out.startswith(f"Updated job: {job_id}")
        resources = audit.log_api_access.call_args.kwargs["resources"]
        assert "fields=folder_id" in resources
        assert "folder=bbbb2222" in resources

    def test_missing_name_is_created_then_filed(self, home, creates, audit, capsys):
        job_id = _seed_job("mover")
        before = _folders_file().read_bytes()
        cli_commands._cron_dispatch(_update_args(job_id, "Fresh"))
        assert creates == ["Fresh"]
        assert [j.folder_id for j in _jobs("mover")] == ["dddd4444"]
        assert _folders_file().read_bytes() == before

    def test_empty_string_ungroups(self, home, no_create, audit, capsys):
        job_id = _seed_job("mover", "aaaa1111")
        cli_commands._cron_dispatch(_update_args(job_id, ""))
        assert [j.folder_id for j in _jobs("mover")] == [""]
        assert "folder=(ungrouped)" in audit.log_api_access.call_args.kwargs["resources"]

    def test_other_fields_ride_in_the_same_update(self, home, no_create, audit, capsys):
        job_id = _seed_job("mover")
        cli_commands._cron_dispatch(_update_args(job_id, "Veille", name="renamed"))
        (job,) = _jobs("renamed")
        assert job.folder_id == "aaaa1111"

    def test_unknown_job_creates_no_folder(self, home, no_create, audit, capsys):
        cli_commands._cron_dispatch(_update_args("nosuchjob", "Fresh"))
        assert "Job not found: nosuchjob" in capsys.readouterr().out

    def test_unresolvable_ref_exits_1_and_leaves_the_job_alone(
        self, home, no_create, audit, capsys
    ):
        job_id = _seed_job("mover", "aaaa1111")
        with pytest.raises(SystemExit) as exc:
            cli_commands._cron_dispatch(_update_args(job_id, "deadbeef"))
        assert exc.value.code == 1
        assert "minted" in capsys.readouterr().err
        assert [j.folder_id for j in _jobs("mover")] == ["aaaa1111"]

    def test_no_folder_flag_leaves_the_folder_untouched(self, home, no_create, audit, capsys):
        job_id = _seed_job("mover", "aaaa1111")
        cli_commands._cron_dispatch(_update_args(job_id, None, name="still-filed"))
        assert [j.folder_id for j in _jobs("still-filed")] == ["aaaa1111"]


# ---------------------------------------------------------------------------
# cron list: folder column and --folder filter (read-only)
# ---------------------------------------------------------------------------


def _list(folder=None) -> argparse.Namespace:
    return argparse.Namespace(cron_action="list", folder=folder)


def _job_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if "[folder: " in line]


class TestCliListFolder:
    def test_each_job_line_names_its_folder(self, home, no_create, capsys):
        _seed_job("in-veille", "aaaa1111")
        _seed_job("loose")
        _seed_job("stale", "eeee5555")  # folder since deleted: show its raw id
        cli_commands._cron_dispatch(_list())
        lines = _job_lines(capsys.readouterr().out)
        names = ("in-veille", "loose", "stale")
        by_name = {n: next(line for line in lines if f"  {n}  " in line) for n in names}
        assert "[folder: Veille]" in by_name["in-veille"]
        assert "[folder: (none)]" in by_name["loose"]
        assert "[folder: eeee5555]" in by_name["stale"]

    def test_filter_by_name_lists_only_that_folder(self, home, no_create, capsys):
        _seed_job("in-veille", "aaaa1111")
        _seed_job("in-apps", "bbbb2222")
        cli_commands._cron_dispatch(_list("veille"))
        lines = _job_lines(capsys.readouterr().out)
        assert len(lines) == 1 and "in-veille" in lines[0]

    @pytest.mark.parametrize("ref", ["-", ""])
    def test_dash_or_empty_lists_ungrouped_jobs(self, home, no_create, capsys, ref):
        _seed_job("in-veille", "aaaa1111")
        _seed_job("loose")
        cli_commands._cron_dispatch(_list(ref))
        lines = _job_lines(capsys.readouterr().out)
        assert len(lines) == 1 and "loose" in lines[0]

    def test_empty_folder_says_so(self, home, no_create, capsys):
        _seed_job("loose")
        cli_commands._cron_dispatch(_list("Apps"))
        assert "No cron jobs in that folder." in capsys.readouterr().out

    @pytest.mark.parametrize("ref", ["Nope", "deadbeef"])
    def test_unknown_folder_is_an_error_never_a_create(self, home, no_create, capsys, ref):
        before = _folders_file().read_bytes()
        with pytest.raises(SystemExit) as exc:
            cli_commands._cron_dispatch(_list(ref))
        assert exc.value.code == 1
        assert "not found" in capsys.readouterr().err
        assert _folders_file().read_bytes() == before

    def test_ambiguous_folder_is_an_error(self, home, no_create, capsys):
        _write_folders(_FOLDERS + [{"id": "cccc3333", "name": "VEILLE", "order": 2}])
        with pytest.raises(SystemExit) as exc:
            cli_commands._cron_dispatch(_list("Veille"))
        assert exc.value.code == 1
        assert "pass the folder id" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# One resolver, one create endpoint: the CLI and the MCP tools share them
# ---------------------------------------------------------------------------


class TestSharedWritePath:
    def test_cli_and_mcp_both_run_the_one_resolver(self, home, monkeypatch, capsys):
        original = mcp_cron._resolve_cron_folder_via
        seen: list[str] = []

        def _spy(ref, create):
            seen.append(ref)
            return original(ref, create)

        monkeypatch.setattr(mcp_cron, "_resolve_cron_folder_via", _spy)
        monkeypatch.setattr(mcp_cron, "_post", lambda *a, **k: {"id": "dddd4444"})
        monkeypatch.setattr(cli_commands, "_create_cron_folder", lambda n: {"id": "dddd4444"})

        cli_commands._cron_dispatch(_add_args("cli-job", "Fresh"))
        assert mcp_cron._resolve_cron_folder("Fresh", session_key="") == ("dddd4444", None)
        assert seen == ["Fresh", "Fresh"]

    @pytest.mark.parametrize("ref", ["Veille", "bbbb2222", "", "deadbeef", "Fresh", "  apps  "])
    def test_cli_and_mcp_resolve_the_same_ref_identically(self, home, monkeypatch, ref):
        posted: list[tuple[str, dict]] = []
        created: list[str] = []

        def _mcp_post(path, body=None, **kwargs):
            posted.append((path, body))
            return {"id": "dddd4444"}

        def _cli_create(name):
            created.append(name)
            return {"id": "dddd4444"}

        monkeypatch.setattr(mcp_cron, "_post", _mcp_post)
        monkeypatch.setattr(cli_commands, "_create_cron_folder", _cli_create)
        assert cli_commands._cron_folder_ref(ref) == mcp_cron._resolve_cron_folder(
            ref, session_key=""
        )
        # Both create legs fire for exactly the same refs, with the same name.
        assert [body["name"] for _path, body in posted] == created
        assert all(path == "/api/cron-folders" for path, _body in posted)

    def test_ambiguous_ref_is_refused_identically(self, home, no_create, monkeypatch):
        _write_folders(_FOLDERS + [{"id": "cccc3333", "name": "VEILLE", "order": 2}])
        monkeypatch.setattr(mcp_cron, "_post", lambda *a, **k: pytest.fail("POST issued"))
        cli = cli_commands._cron_folder_ref("Veille")
        assert cli == mcp_cron._resolve_cron_folder("Veille", session_key="")
        assert cli[0] == "" and "pass the folder id" in (cli[1] or "")


# ---------------------------------------------------------------------------
# _create_cron_folder: the CLI's transport to the dashboard endpoint
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def gateway(monkeypatch):
    """Pin the port and secret; return the list of requests ``_gateway_send`` saw."""
    from kiro_crew import port_resolution

    monkeypatch.setattr(port_resolution, "resolve_client_port_ex", lambda _p: (54321, True))
    monkeypatch.setattr(cli_commands, "_internal_secret", lambda port: "s3cret-for-test")
    return []


class TestCreateCronFolderTransport:
    def test_posts_the_name_to_the_dashboard_endpoint_with_the_secret(
        self, home, gateway, monkeypatch
    ):
        def _send(req, port, timeout, label):
            gateway.append((req, port))
            return _Resp(b'{"id": "dddd4444", "name": "Fresh", "order": 2}')

        monkeypatch.setattr(cli_commands, "_gateway_send", _send)
        assert cli_commands._create_cron_folder("Fresh") == {
            "id": "dddd4444",
            "name": "Fresh",
            "order": 2,
        }
        ((req, port),) = gateway
        assert port == 54321
        assert req.get_method() == "POST"
        assert req.full_url == "http://127.0.0.1:54321/api/cron-folders"
        assert json.loads(req.data) == {"name": "Fresh"}
        assert req.get_header("X-internal-secret") == "s3cret-for-test"

    def test_unreachable_gateway_is_a_clear_error(self, home, gateway, monkeypatch):
        def _send(req, port, timeout, label):
            raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))

        monkeypatch.setattr(cli_commands, "_gateway_send", _send)
        out = cli_commands._create_cron_folder("Fresh")
        assert "gateway not running" in str(out.get("error"))
        assert "pass an existing folder" in str(out.get("error"))

    def test_endpoint_refusal_passes_its_error_through(self, home, gateway, monkeypatch):
        import io

        def _send(req, port, timeout, label):
            raise urllib.error.HTTPError(
                req.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"error": "name too long"}')
            )

        monkeypatch.setattr(cli_commands, "_gateway_send", _send)
        assert cli_commands._create_cron_folder("x" * 500) == {"error": "name too long"}

    def test_unreachable_gateway_fails_cron_add_with_no_job(
        self, home, gateway, monkeypatch, capsys
    ):
        def _send(req, port, timeout, label):
            raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))

        monkeypatch.setattr(cli_commands, "_gateway_send", _send)
        before = _folders_file().read_bytes()
        with pytest.raises(SystemExit) as exc:
            cli_commands._cron_dispatch(_add_args("orphan", "Fresh"))
        assert exc.value.code == 1
        assert "gateway not running" in capsys.readouterr().err
        assert _jobs("orphan") == []
        assert _folders_file().read_bytes() == before


# ---------------------------------------------------------------------------
# Parser: the flags exist and the old "no create" promise is gone
# ---------------------------------------------------------------------------


class TestParser:
    @pytest.mark.parametrize("action", ["add", "update", "list"])
    def test_folder_flag_is_documented(self, action, monkeypatch, capsys):
        import sys

        from kiro_crew.cli import main

        monkeypatch.setattr(sys, "argv", ["kirocrew", "cron", action, "--help"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        flat = " ".join(capsys.readouterr().out.split())
        assert "--folder" in flat
        assert "does not create folders" not in flat

    @pytest.mark.parametrize(
        ("argv", "want"),
        [
            (["cron", "update", "abcd1234", "--name", "n"], None),
            (["cron", "update", "abcd1234", "--folder", ""], ""),
            (["cron", "list"], None),
            (["cron", "list", "--folder", "Veille"], "Veille"),
            (["cron", "add", "j", "m", "--every", "300"], ""),
        ],
    )
    def test_folder_defaults(self, argv, want, monkeypatch):
        # update/list default to None so an absent flag is "not asked", never "ungroup".
        import sys

        from kiro_crew.cli import main

        seen: list[argparse.Namespace] = []
        monkeypatch.setattr(cli_commands, "_cron", seen.append)
        monkeypatch.setattr(sys, "argv", ["kirocrew", *argv])
        main()
        assert [a.folder for a in seen] == [want]
