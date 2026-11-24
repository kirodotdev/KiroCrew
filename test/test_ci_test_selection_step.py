"""Contract tests for scripts/ci_test_selection_step.py and its two steps in ci.yml.

Pinned:

* TRUST -- a bundle is taken only from a run GitHub reports as a successful
  nightly `test-map.yml` run on this repository's main, and only when the file
  names that run's commit, holds every table and is fresh.
* FAIL CLOSED -- every other answer is verdict `full` with the reason, in at
  most three requests, none repeated, and the token goes to the API alone.
* SHADOW -- the steps run on pull requests only, come last, set no output,
  cannot fail the job, and write a verdict the miss ledger reads.
"""

from __future__ import annotations

import copy
import gzip
import io
import json
import socketserver
import subprocess
import threading
import zipfile
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS
from scripts import ci_test_map as ctm
from scripts import ci_test_selection as cts
from scripts import ci_test_selection_ledger as ledger
from scripts import ci_test_selection_step as step
from scripts import ci_test_selection_windows as windows

ROOT = Path(__file__).resolve().parents[1]
REPO = "octo/crew"
TOKEN = "token-for-the-api"
BLOB_URL = "https://blobs.example.invalid/archive?sig=1"
RUN_SHA = "a" * 40
# A run as the API lists it, with the fields the trust rule reads.
RUN = {
    "id": 7,
    "path": f".github/workflows/{step.WORKFLOW}",
    "head_branch": step.BRANCH,
    "status": "completed",
    "conclusion": "success",
    "event": step.EVENTS[0],
    "head_sha": RUN_SHA,
    "repository": {"full_name": REPO},
    "head_repository": {"full_name": REPO},
}
# For each field of the rule, values a run that must not be trusted can carry.
UNTRUSTED = {
    "id": [None, "7"],
    "path": [".github/workflows/ci.yml", f"{step.WORKFLOW}", None],
    "head_branch": ["feature", "release/1", None],
    "status": ["in_progress", None],
    "conclusion": ["failure", "cancelled", None],
    "event": ["pull_request", "pull_request_target", "push", None],
    "repository": [{"full_name": "fork/crew"}, None, "octo/crew"],
    "head_repository": [{"full_name": "fork/crew"}, None, {}],
}


def _git(repo: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        + ["-c", "commit.gpgsign=false", *argv],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Two commits; the second edits one mapped `src/` file."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "repo"
    files = {
        "src/kiro_crew/testing/wait.py": "",
        "test/metrics/test_schema.py": "",
        "test/test_gate.py": "",
        "test/test_other.py": "",
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "one")
    (root / "src/kiro_crew/testing/wait.py").write_text("# two\n", encoding="utf-8")
    _git(root, "commit", "-qam", "two")
    return root


def _bundle(repo: Path, age: timedelta = timedelta(hours=5), **changed: Any) -> dict[str, Any]:
    """A whole bundle of the trusted run's commit, built `age` before the checkout's head."""
    doc: dict[str, Any] = {
        "schema": 1,
        "commit": RUN_SHA,
        "built_at": (windows.commit_time("HEAD", repo) - age).isoformat(),
        "tests": {"test/metrics/test_schema.py": 3},
        "files": {"src/kiro_crew/testing/wait.py": [0]},
        "cochange": {"src/kiro_crew/testing/wait.py": ["test/metrics/test_schema.py"]},
        "guard": ["test/test_gate.py"],
        "os_sensitive": {"src/kiro_crew/lock.py": "W2"},
        "windows_targeting": ["test/test_windows_x.py"],
    }
    assert set(ctm.BUNDLE_TABLES) <= set(doc)
    doc.update(changed)
    return {k: v for k, v in doc.items() if v is not None}


def _zip(members: dict[str, bytes]) -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as zipped:
        for name, body in members.items():
            zipped.writestr(name, body)
    return data.getvalue()


def _archive(doc: dict[str, Any]) -> bytes:
    return _zip({step.MEMBER: gzip.compress(json.dumps(doc).encode("utf-8"))})


class Api:
    """The three answers of the API, each replaceable, and every request it was sent."""

    def __init__(self, archive: bytes) -> None:
        self.runs: Any = (200, {}, {"workflow_runs": [copy.deepcopy(RUN)]})
        artifact = {"id": 9, "name": step.ARTIFACT, "expired": False, "workflow_run": {"id": 7}}
        self.artifacts: Any = (200, {}, {"artifacts": [artifact]})
        self.zip: Any = (302, {"location": BLOB_URL}, b"")
        self.blob: Any = (200, {}, archive)
        self.sent: list[tuple[str, str]] = []

    def __call__(self, url: str, token: str) -> tuple[int, dict[str, str], bytes]:
        self.sent.append((url, token))
        if url == BLOB_URL:
            answer = self.blob
        elif "/workflows/" in url:
            answer = self.runs
        elif url.endswith("/zip"):
            answer = self.zip
        else:
            answer = self.artifacts
        status, headers, body = answer
        return status, headers, body if isinstance(body, bytes) else json.dumps(body).encode()


def _score(repo: Path, tmp_path: Path, api: Api, **kwargs: Any) -> dict[str, Any]:
    out = tmp_path / "out" / step.VERDICT_NAME
    out.parent.mkdir(exist_ok=True)
    env = {"GITHUB_REPOSITORY": REPO, "GH_TOKEN": TOKEN}
    verdict, _age = step.score(repo, out, kwargs.pop("env", env), get=api, **kwargs)
    return verdict


def test_only_a_run_github_reports_as_the_nightly_on_main_is_trusted() -> None:
    for event in step.EVENTS:
        assert step.trusted_run({**RUN, "event": event}, REPO)
    assert set(UNTRUSTED) == set(RUN) - {"head_sha"}
    for field, values in UNTRUSTED.items():
        for value in values:
            assert not step.trusted_run({**RUN, field: value}, REPO), (field, value)
        assert not step.trusted_run({k: v for k, v in RUN.items() if k != field}, REPO), field
    # The same run is not this repository's when the step runs for another one.
    assert not step.trusted_run(RUN, "fork/crew")
    assert not step.trusted_run(None, REPO) and not step.trusted_run([RUN], REPO)


def test_a_trusted_bundle_is_scored_in_three_requests(repo: Path, tmp_path: Path) -> None:
    api = Api(_archive(_bundle(repo)))
    # A newer run that is not trusted is passed over, not followed.
    api.runs[2]["workflow_runs"].insert(0, {**RUN, "id": 8, "event": "pull_request"})
    verdict = _score(repo, tmp_path, api)
    assert verdict["verdict"] == "select" and verdict["reason"] is None
    assert verdict["bundle_commit"] == RUN_SHA
    assert [verdict["base"], verdict["head"]] == [
        _git(repo, "rev-parse", "HEAD^1"),
        _git(repo, "rev-parse", "HEAD"),
    ]
    assert verdict["tiers"]["B"] == ["test/metrics/test_schema.py"]
    assert verdict["would_skip"] == ["test/test_other.py"]
    urls = [url for url, _token in api.sent]
    assert len(urls) == len(set(urls)) == 3 + 1  # three API reads and the redirect's target
    assert "/actions/runs/7/artifacts" in urls[1] and "/actions/artifacts/9/zip" in urls[2]
    # The token goes to the API and nowhere else.
    for url, token in api.sent:
        assert token == (TOKEN if url.startswith(step.API_URL + "/") else "")
    assert api.sent[-1] == (BLOB_URL, "")


def _cases(repo: Path) -> dict[str, tuple[Any, str, int]]:
    """Name -> (change to the API's answers, a part of the reason, API reads made)."""
    whole = _bundle(repo)
    limited = (403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1"}, b"")

    def runs(answer: Any) -> Any:
        return lambda api: setattr(api, "runs", answer)

    def artifacts(answer: Any) -> Any:
        return lambda api: setattr(api, "artifacts", answer)

    def blob(body: bytes) -> Any:
        return lambda api: setattr(api, "blob", (200, {}, body))

    def bundle(**changed: Any) -> Any:
        return blob(_archive({**whole, **changed}))

    def without(key: str) -> Any:
        return blob(_archive({k: v for k, v in whole.items() if k != key}))

    one = {"id": 9, "name": step.ARTIFACT, "expired": False, "workflow_run": {"id": 7}}
    cases: dict[str, tuple[Any, str, int]] = {
        "no run": (runs((200, {}, {"workflow_runs": []})), "no trusted", 1),
        "no trusted run": (
            runs((200, {}, {"workflow_runs": [{**RUN, "head_branch": "x"}]})),
            "no trusted",
            1,
        ),
        "runs not a listing": (runs((200, {}, [])), "no trusted", 1),
        "rate limit": (runs(limited), "primary rate limit", 1),
        "server error": (runs((500, {}, b"")), "HTTP 500", 1),
        "not JSON": (runs((200, {}, b"<html>")), "JSONDecodeError", 1),
        "no artifact": (artifacts((200, {}, {"artifacts": []})), "no live", 2),
        "expired": (artifacts((200, {}, {"artifacts": [{**one, "expired": True}]})), "no live", 2),
        "another name": (
            artifacts((200, {}, {"artifacts": [{**one, "name": "test-selection-verdict"}]})),
            "no live",
            2,
        ),
        "another run's": (
            artifacts((200, {}, {"artifacts": [{**one, "workflow_run": {"id": 8}}]})),
            "no live",
            2,
        ),
        "artifacts refused": (artifacts(limited), "primary rate limit", 2),
        "archive refused": (lambda api: setattr(api, "zip", (404, {}, b"")), "HTTP 404", 3),
        "blob refused": (lambda api: setattr(api, "blob", (403, {}, b"")), "HTTP 403", 3),
        "not a zip": (blob(b"not a zip"), "unreadable archive", 3),
        "two members": (
            blob(_zip({step.MEMBER: b"", "other.json": b""})),
            "does not hold exactly",
            3,
        ),
        "another member": (blob(_zip({"evil/../x.gz": b""})), "does not hold exactly", 3),
        "over the size cap": (blob(b"0" * (step.MAX_ARCHIVE_BYTES + 1)), "is over", 3),
        "damaged bundle": (blob(_zip({step.MEMBER: b"\x1f\x8b0"})), "damaged gzip", 3),
        "another schema": (bundle(schema=2), "not a schema-1", 3),
        "another commit": (bundle(commit="b" * 40), "is not of run 7's commit", 3),
        "no build time": (without("built_at"), "does not say when", 3),
        "a build time that is not one": (bundle(built_at="yesterday"), "does not say when", 3),
        "a build time with no offset": (
            bundle(built_at="2026-01-01T00:00:00"),
            "does not say when",
            3,
        ),
        "stale": (
            bundle(
                built_at=_bundle(repo, timedelta(days=step.MAX_BUNDLE_AGE_DAYS, minutes=1))[
                    "built_at"
                ]
            ),
            "bundle_stale",
            3,
        ),
    }
    for table in ctm.BUNDLE_TABLES:
        cases[f"no {table}"] = (without(table), f"no {table} table", 3)
        cases[f"empty {table}"] = (bundle(**{table: type(whole[table])()}), f"no {table} table", 3)
    return cases


def test_every_other_answer_is_a_full_verdict_and_is_not_asked_again(
    repo: Path, tmp_path: Path
) -> None:
    for name, (change, reason, reads) in _cases(repo).items():
        api = Api(_archive(_bundle(repo)))
        change(api)
        verdict = _score(repo, tmp_path, api)
        assert verdict["verdict"] == "full", name
        assert verdict["reason"].startswith("no_bundle: ") and reason in verdict["reason"], (
            name,
            verdict["reason"],
        )
        assert verdict["would_skip"] == [] and verdict["would_run_share"] == 1.0, name
        assert verdict["windows"]["verdict"] == "full", name
        assert verdict["bundle_commit"] is None, name
        urls = [url for url in dict(api.sent) if url.startswith(step.API_URL)]
        assert len(urls) == reads and len(api.sent) == len(set(api.sent)), name
        assert ledger.parse_verdict(json.dumps(verdict)) is not None, name
    # A bundle a day inside the limit is still read.
    fresh = _bundle(repo, timedelta(days=step.MAX_BUNDLE_AGE_DAYS - 1))
    assert _score(repo, tmp_path, Api(_archive(fresh)))["verdict"] == "select"


def test_no_request_is_made_without_a_repository_and_no_scoring_without_time(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = Api(_archive(_bundle(repo)))
    for bad in ("", "octo", "octo/crew/x", "octo/crew?x=1"):
        verdict = _score(repo, tmp_path, api, env={"GITHUB_REPOSITORY": bad, "GH_TOKEN": TOKEN})
        assert verdict["verdict"] == "full" and "GITHUB_REPOSITORY" in verdict["reason"]
    assert api.sent == []
    # The fetch used the step's time: what is left is below the scorer's least.
    ticks = iter([0.0, step.BUDGET_SECS - step.MIN_SCORE_SECS + 1])
    verdict = _score(repo, tmp_path, api, clock=lambda: next(ticks))
    assert verdict["verdict"] == "full" and "were left to score in" in verdict["reason"]
    # The scorer gets what is left, never the whole budget again.
    seen: list[float] = []
    ticks = iter([0.0, 20.0])
    real = cts.run

    def run(args: Any) -> dict[str, Any]:
        seen.append(args.timeout)
        return real(args)

    monkeypatch.setattr(step.selection, "run", run)
    _score(repo, tmp_path, Api(_archive(_bundle(repo))), clock=lambda: next(ticks))
    assert seen == [step.BUDGET_SECS - 20.0]


def test_main_writes_what_the_ledger_reads_and_never_fails(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    api = Api(_archive(_bundle(repo)))
    summary = tmp_path / "summary.md"
    env = {
        "GITHUB_REPOSITORY": REPO,
        "GH_TOKEN": TOKEN,
        "RUNNER_TEMP": str(tmp_path / "runner"),
        "GITHUB_STEP_SUMMARY": str(summary),
    }
    assert step.main(env, repo, api) == 0
    written = tmp_path / "runner" / step.VERDICT_DIR / step.VERDICT_NAME
    # One JSON file in the directory's listing of them, as the ledger takes an artifact.
    assert sorted(written.parent.rglob("*.json")) == [written]
    assert written.stat().st_size <= ledger.MAX_VERDICT_BYTES
    row = ledger.parse_verdict(written.read_text(encoding="utf-8"))
    assert row is not None and row["verdict"] == "select"
    line = summary.read_text(encoding="utf-8")
    assert line == capsys.readouterr().out
    verdict = json.loads(written.read_text(encoding="utf-8"))
    for part in ("verdict `select`", "reason: None", "bundle 5.0 h old"):
        assert part in line
    assert f"{verdict['would_run_share']:.1%}" in line
    # With no bundle the line says so, and the verdict is still the ledger's to read.
    api.runs = (500, {}, b"")
    assert step.main(env, repo, api) == 0
    assert "verdict `full`" in summary.read_text(encoding="utf-8").splitlines()[-1]
    assert "no bundle" in summary.read_text(encoding="utf-8").splitlines()[-1]
    # A long reason is cut in the line, never in the file.
    long = cts.unscored({}, "x" * (step.SUMMARY_REASON_CHARS + 50))
    assert "x" * step.SUMMARY_REASON_CHARS + "," in step.summary_line(long, None)
    # Nowhere to write, or a tree that is not a repository: exit 0, a notice, nothing raised.
    for broken, root in (({}, repo), ({**env, "RUNNER_TEMP": str(summary)}, repo)):
        assert step.main(broken, root, api) == 0
        assert "::notice::Test selection (shadow): no verdict written" in capsys.readouterr().out
    assert step.main({**env, "RUNNER_TEMP": str(tmp_path / "elsewhere")}, tmp_path, api) == 0
    elsewhere = tmp_path / "elsewhere" / step.VERDICT_DIR / step.VERDICT_NAME
    assert json.loads(elsewhere.read_text(encoding="utf-8"))["verdict"] == "full"


class _LoopbackServer(HTTPServer):
    """Binds without the host-name lookup `HTTPServer.server_bind` makes."""

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_port = self.server_address[1]


def _serve(answer: Any) -> tuple[HTTPServer, threading.Thread, list[dict[str, str]]]:
    """A loopback server on a free port that answers one GET with `answer(handler)`."""
    seen: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802  (the handler's fixed method name)
            seen.append({key.lower(): value for key, value in self.headers.items()})
            answer(self)

        def log_message(self, *args: Any) -> None:
            pass

    server = _LoopbackServer(("127.0.0.1", 0), Handler)
    server.timeout = FALLBACK_TIMEOUT_SECS
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    return server, thread, seen


def test_the_archive_redirect_is_followed_without_the_token() -> None:
    archive = _zip({step.MEMBER: b"bundle"})

    def blob(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(archive)))
        handler.end_headers()
        handler.wfile.write(archive)

    store, store_thread, store_seen = _serve(blob)

    def api(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(302)
        handler.send_header("Location", f"http://127.0.0.1:{store.server_port}/blob")
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    front, front_thread, front_seen = _serve(api)
    try:
        status, headers, _body = step.http_get(
            f"http://127.0.0.1:{front.server_port}/zip", TOKEN, schemes=("http",)
        )
        # The transport hands the redirect back; it does not follow it with the token.
        assert status == 302 and store_seen == []
        assert front_seen[0]["authorization"] == f"Bearer {TOKEN}"
        status, _headers, body = step.http_get(headers["location"], "", schemes=("http",))
        assert status == 200 and step.member(body) == b"bundle"
        assert "authorization" not in store_seen[0]
    finally:
        for server, thread in ((front, front_thread), (store, store_thread)):
            thread.join(FALLBACK_TIMEOUT_SECS)
            server.server_close()
    with pytest.raises(step.NoBundle):
        step.http_get(f"http://127.0.0.1:{front.server_port}/zip", TOKEN)


# -- the workflow wiring ----------------------------------------------------


def _ci() -> dict[str, Any]:
    return yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))


def _shadow_steps() -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    steps = _ci()["jobs"]["changes"]["steps"]
    score = [s for s in steps if "ci_test_selection_step.py" in str(s.get("run", ""))]
    upload = [s for s in steps if s.get("with", {}).get("name") == step.VERDICT_DIR]
    assert len(score) == len(upload) == 1
    return steps, score[0], upload[0]


def test_the_shadow_steps_cannot_change_the_job_or_any_other() -> None:
    ci = _ci()
    changes = ci["jobs"]["changes"]
    steps, score, upload = _shadow_steps()
    # Last, in order, on pull requests only, and unable to fail the job. A job's
    # outputs are published when it ends, so the time these steps take is added
    # to every job that waits on this one: the bounds below are that cost.
    assert steps[-2:] == [score, upload]
    for shadow in (score, upload):
        assert shadow["if"] == "github.event_name == 'pull_request'"
        assert shadow["continue-on-error"] is True
        # No id: no output expression, here or in another job, can name the step.
        assert "id" not in shadow
        assert "secrets." not in json.dumps(shadow)
    ids = {s["id"] for s in steps if "id" in s}
    for name, value in changes["outputs"].items():
        assert value.split(".")[1] in ids, name
    for name, job in ci["jobs"].items():
        if name != "changes":
            assert "test-selection" not in json.dumps(job), name
    # The script bounds itself well inside the step's timeout, which is seconds of the job's.
    assert step.BUDGET_SECS + step.REQUEST_TIMEOUT <= score["timeout-minutes"] * 60
    assert score["timeout-minutes"] * 60 <= changes["timeout-minutes"] * 60 // 5
    assert 4 * step.REQUEST_TIMEOUT + step.MIN_SCORE_SECS <= step.BUDGET_SECS
    assert step.BUDGET_SECS <= cts.TIMEOUT_SECS


def test_the_shadow_steps_hold_a_read_token_and_upload_the_ledgers_artifact() -> None:
    changes = _ci()["jobs"]["changes"]
    _steps, score, upload = _shadow_steps()
    # Read scopes only; `actions` is the one the artifact of another run needs.
    assert set(changes["permissions"].values()) == {"read"}
    assert "actions" in changes["permissions"]
    assert score["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert score["run"].strip() == "python3 scripts/ci_test_selection_step.py"
    assert upload["with"]["path"] == (
        "${{ runner.temp }}/" + f"{step.VERDICT_DIR}/{step.VERDICT_NAME}"
    )
    assert upload["with"]["if-no-files-found"] == "ignore"
    assert upload["with"]["overwrite"] is True
    # The name the ledger asks a run for by default.
    assert f'default="{step.VERDICT_DIR}"' in Path(ledger.__file__).read_text(encoding="utf-8")
    # The same pinned action every other upload in the workflow uses.
    pins = {
        s["uses"]
        for job in _ci()["jobs"].values()
        for s in job.get("steps", [])
        if str(s.get("uses", "")).startswith("actions/upload-artifact@")
    }
    assert pins == {upload["uses"]}


def test_the_nightly_workflow_publishes_the_bundle_the_step_asks_for() -> None:
    path = ROOT / ".github" / "workflows" / step.WORKFLOW
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    # The events the step trusts are the only ones that start the publisher.
    assert set(doc.get("on") or doc[True]) == set(step.EVENTS)
    uploads = [
        s["with"]
        for job in doc["jobs"].values()
        for s in job["steps"]
        if "actions/upload-artifact@" in str(s.get("uses", ""))
    ]
    # One artifact carries the step's name, and it holds the one file the step reads.
    assert [u["path"] for u in uploads if u["name"] == step.ARTIFACT] == [step.MEMBER]
