"""The workspace side of the gh proxy, in process: what ``gh`` prints and sends.

``test_remote_gh_proxy`` drives this file end to end as a child process; these
cases call it directly so each branch it takes is pinned on its own.
"""

from __future__ import annotations

import base64
import http.server
import io
import json
import shutil
import subprocess
import sys
import threading

import pytest
from loopback_servers import LoopbackHTTPServer

from kiro_crew.remote_session import gh_proxy

GIT = shutil.which("git")
SECRET = "b" * 64


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


@pytest.fixture
def note(tmp_path, monkeypatch):
    path = tmp_path / "note"
    path.write_text("")
    monkeypatch.setenv(gh_proxy.ENV_NOTE, str(path))
    monkeypatch.setattr(gh_proxy, "workspace_repo", lambda: "acme/widgets")
    monkeypatch.setattr(gh_proxy, "current_branch", lambda: "feat/x")
    return path


def test_a_command_the_gateway_ran_prints_its_output_and_notes_it(note, monkeypatch, capsys):
    sent = []
    answer = {
        "code": 3,
        "stdout": _b64("https://github.com/acme/widgets/pull/7\n"),
        "stderr": _b64("warning from gh\n"),
        "login": "octo",
        "repo": "acme/widgets",
        "ran": True,
    }
    monkeypatch.setattr(gh_proxy, "ask", lambda request: sent.append(request) or answer)
    assert gh_proxy.main(["pr", "view", "7"]) == 3
    out, err = capsys.readouterr()
    assert out == "https://github.com/acme/widgets/pull/7\n"
    assert err.startswith("warning from gh\n")
    assert "gh-proxy: ran on the gateway machine as @octo for acme/widgets" in err
    assert sent == [
        {
            "op": "run",
            "argv": ["pr", "view", "7"],
            "workspace_repo": "acme/widgets",
            "branch": "feat/x",
            "files": {},
        }
    ]
    assert note.read_text() == "octo\tacme/widgets\tgh pr view\n"


def test_an_api_call_is_noted_without_its_path(note, monkeypatch):
    answer = {"code": 0, "stdout": "", "stderr": "", "login": "octo", "repo": "a/b", "ran": True}
    monkeypatch.setattr(gh_proxy, "ask", lambda request: answer)
    assert gh_proxy.main(["api", "repos/a/b/pulls"]) == 0
    assert note.read_text() == "octo\ta/b\tgh api\n"


@pytest.mark.parametrize("code", [None, -1, 300, "1"])
def test_an_unusable_exit_code_reads_as_failure(note, monkeypatch, code):
    answer = {"code": code, "stdout": "", "stderr": "", "login": "o", "repo": "a/b", "ran": False}
    monkeypatch.setattr(gh_proxy, "ask", lambda request: answer)
    assert gh_proxy.main(["pr", "list"]) == 1
    assert note.read_text() == ""


def test_a_refusal_says_what_the_proxy_is_and_whose_login_it_uses(note, monkeypatch, capsys):
    refused = {"refused": "gh pr merge is refused", "login": "octo", "repositories": ["a/b"]}
    monkeypatch.setattr(gh_proxy, "ask", lambda request: refused)
    assert gh_proxy.main(["pr", "merge", "1"]) == 1
    err = capsys.readouterr().err.splitlines()
    assert err == [
        "gh-proxy: refused: gh pr merge is refused",
        "gh-proxy: gh-proxy (Kiro Crew) runs on the gateway machine as @octo, scoped to a/b; "
        "run gh help for what it allows",
    ]


def test_no_gateway_is_a_refusal_not_a_crash(note, monkeypatch, capsys):
    def down(request):
        raise ConnectionError("the session's git proxy is not running")

    monkeypatch.setattr(gh_proxy, "ask", down)
    assert gh_proxy.main(["pr", "list"]) == 1
    assert "not connected to the gateway" in capsys.readouterr().err
    assert gh_proxy.main(["--version"]) == 0
    assert "Right now it refuses every command: not connected" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [[], ["help"], ["--help"], ["pr", "view", "--help"]])
def test_help_names_the_proxy_its_owner_and_what_it_allows(monkeypatch, capsys, argv):
    about = {"login": "octo", "repositories": ["acme/widgets"]}
    monkeypatch.setattr(gh_proxy, "ask", lambda request: about)
    assert gh_proxy.main(argv) == 0
    out = capsys.readouterr().out
    assert "gh-proxy (Kiro Crew): Kiro Crew's gh proxy" in out and "not GitHub's gh" in out
    assert "as @octo, scoped to acme/widgets" in out
    assert "Allowed: " in out and "Refused: " in out and "--jq" in out


def test_auth_status_reports_the_owner_without_a_token(monkeypatch, capsys):
    monkeypatch.setattr(gh_proxy, "ask", lambda request: {"login": "octo", "repositories": []})
    assert gh_proxy.main(["auth", "status"]) == 0
    assert "as @octo" in capsys.readouterr().out
    monkeypatch.setattr(gh_proxy, "ask", lambda request: {"login": "", "refused": "off"})
    assert gh_proxy.main(["auth", "status"]) == 1


def test_a_body_file_outside_the_workspace_stops_before_asking(tmp_path, note, monkeypatch):
    asked = []
    monkeypatch.setattr(gh_proxy, "ask", lambda request: asked.append(request) or {})
    monkeypatch.setenv(gh_proxy.ENV_ROOT, str(tmp_path / "ws"))
    assert gh_proxy.main(["pr", "comment", "1", "-F", str(tmp_path / "note")]) == 1
    assert asked == []


def test_a_body_from_stdin_is_read_here(tmp_path, monkeypatch):
    monkeypatch.setenv(gh_proxy.ENV_ROOT, str(tmp_path))
    stdin = io.TextIOWrapper(io.BytesIO(b"from stdin"))
    monkeypatch.setattr(sys, "stdin", stdin)
    assert gh_proxy.body_files(["pr", "create", "-t", "x", "--body-file=-"]) == {"4": "from stdin"}


def test_a_body_file_past_the_limit_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(gh_proxy.ENV_ROOT, str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "big.md").write_bytes(b"x" * (gh_proxy.MAX_BODY_FILE_BYTES + 1))
    with pytest.raises(SystemExit):
        gh_proxy.body_files(["pr", "comment", "1", "-F", "big.md"])
    with pytest.raises(SystemExit):
        gh_proxy.body_files(["pr", "comment", "1", "-F", "missing.md"])
    err = capsys.readouterr().err
    assert "is over" in err and "cannot read" in err


@pytest.mark.skipif(GIT is None, reason="git is required")
def test_the_repository_and_branch_come_from_the_checkout(tmp_path, monkeypatch):
    def git(*args):
        subprocess.run([GIT, *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-b", "feat/y")
    monkeypatch.chdir(tmp_path)
    assert gh_proxy.workspace_repo() == ""
    git("remote", "add", "origin", "git@github.com:acme/widgets.git")
    assert gh_proxy.workspace_repo() == "acme/widgets"
    git("remote", "set-url", "origin", "https://gitlab.com/acme/widgets.git")
    assert gh_proxy.workspace_repo() == ""
    assert gh_proxy.current_branch() == "feat/y"


class _Gateway(http.server.BaseHTTPRequestHandler):
    seen: list = []
    status = 200
    reply = b"{}"

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        type(self).seen.append(
            (self.path, self.headers.get("Authorization"), json.loads(self.rfile.read(length)))
        )
        self.send_response(type(self).status)
        self.send_header("Content-Length", str(len(type(self).reply)))
        self.end_headers()
        self.wfile.write(type(self).reply)


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    _Gateway.seen = []
    # The context manager closes the socket on every path, a failed thread
    # start included; shutdown() only once serve_forever is really running.
    with LoopbackHTTPServer(("127.0.0.1", 0), _Gateway) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port_file = tmp_path / "git-proxy.port"
            port_file.write_text(f"{server.server_address[1]}\n")
            monkeypatch.setenv(gh_proxy.ENV_PORT_FILE, str(port_file))
            monkeypatch.setenv(gh_proxy.ENV_SECRET, SECRET)
            yield _Gateway
        finally:
            server.shutdown()
            thread.join()


def test_ask_posts_to_the_git_proxy_with_its_capability(gateway):
    gateway.status, gateway.reply = 200, b'{"login": "octo"}'
    assert gh_proxy.ask({"op": "about"}) == {"login": "octo"}
    assert gateway.seen == [(gh_proxy.PROXY_PATH, f"Bearer {SECRET}", {"op": "about"})]


@pytest.mark.parametrize(
    ("status", "reply"), [(403, b"refused"), (200, b"not json"), (200, b"[1]")]
)
def test_ask_reads_a_bad_answer_as_no_connection(gateway, status, reply):
    gateway.status, gateway.reply = status, reply
    with pytest.raises(ConnectionError):
        gh_proxy.ask({"op": "about"})


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell stand-in for gh")
@pytest.mark.asyncio
async def test_run_gh_runs_the_trusted_gh_in_an_empty_directory_with_stdin(tmp_path, monkeypatch):
    from kiro_crew.remote_session import gh_gateway, git_gateway

    fake = tmp_path / "gh"
    fake.write_text(
        "#!/bin/sh\n"
        'printf "%s|" "$@"; printf "cwd=%s|" "$(ls -A | wc -l | tr -d " ")"\n'
        'printf "host=%s|prompt=%s|" "$GH_HOST" "$GH_PROMPT_DISABLED"\n'
        "cat\n"
        'echo "to stderr" >&2\n'
        "exit 4\n"
    )
    fake.chmod(0o700)
    monkeypatch.setattr(git_gateway, "_binary", lambda name: str(fake))
    code, out, err = await gh_gateway.run_gh(["pr", "view", "1"], b"body text")
    assert code == 4
    assert out.decode() == "pr|view|1|cwd=0|host=github.com|prompt=1|body text"
    assert err.decode() == "to stderr\n"


@pytest.mark.asyncio
async def test_run_gh_refuses_without_a_trusted_gh(monkeypatch):
    from kiro_crew.remote_session import gh_gateway, git_gateway

    monkeypatch.setattr(git_gateway, "_binary", lambda name: None)
    with pytest.raises(gh_gateway.GhRefusal) as refused:
        await gh_gateway.run_gh(["pr", "list"], b"")
    assert refused.value.rule == "no-gh"


def test_the_setting_reader_fails_closed(monkeypatch):
    from kiro_crew.config import loader
    from kiro_crew.remote_session import gh_gateway

    def unreadable():
        raise OSError("config unreadable")

    monkeypatch.setattr(loader.KiroCrewConfig, "load", staticmethod(unreadable))
    assert gh_gateway.gh_proxy_enabled() is False


def test_ask_without_a_proxy_is_no_connection(tmp_path, monkeypatch):
    monkeypatch.setenv(gh_proxy.ENV_PORT_FILE, str(tmp_path / "absent.port"))
    monkeypatch.setenv(gh_proxy.ENV_SECRET, SECRET)
    with pytest.raises(ConnectionError):
        gh_proxy.ask({"op": "about"})
