"""Tests for ``kiro_crew.apps.builtins.code_intel``: LSP framing, result conversion and the server pool.

The pool tests drive a real child, ``fake_lsp_server.py``, over stdio, so the
JSON-RPC client, document sync and server-request handling are exercised end to
end. The sandbox wrap and the provenance check are replaced: they need a booted
platform context, and what they enforce is covered by their own suites.
"""

from __future__ import annotations

import io
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.apps.builtins.code_intel import manager as mgr
from kiro_crew.apps.builtins.code_intel import protocol
from kiro_crew.apps.builtins.code_intel.servers import (
    ServerSpec,
    find_project_root,
    resolve_argv,
    spec_by_id,
    spec_for_path,
)

_FAKE_SERVER = str(Path(__file__).with_name("fake_lsp_server.py"))


# ── protocol ──────────────────────────────────────────────────────────────


def test_frame_round_trip():
    payload = {"jsonrpc": "2.0", "id": 1, "result": {"text": "é"}}
    assert protocol.read_message(io.BytesIO(protocol.encode_message(payload))) == payload


def test_a_lone_surrogate_is_sent_as_one_unit():
    from kiro_crew.apps.builtins.code_intel.protocol import encode_message

    body = encode_message({"text": "a\udc00b"}).split(b"\r\n\r\n", 1)[1]
    assert json.loads(body) == {"text": "a?b"}


def test_read_message_eof_and_bad_frames():
    assert protocol.read_message(io.BytesIO(b"")) is None
    with pytest.raises(protocol.FramingError):
        protocol.read_message(io.BytesIO(b"X: 1\r\n\r\n{}"))
    with pytest.raises(protocol.FramingError):
        protocol.read_message(io.BytesIO(b"Content-Length: 2\r\n\r\n[]"))
    too_big = protocol.MAX_MESSAGE_BYTES + 1
    with pytest.raises(protocol.FramingError):
        protocol.read_message(io.BytesIO(b"Content-Length: %d\r\n\r\n" % too_big))


def test_uri_to_path_accepts_only_local_files():
    assert protocol.uri_to_path("file:///tmp/a%20b.py") == "/tmp/a b.py"
    assert protocol.uri_to_path("jdt://contents/x.class") is None
    assert protocol.uri_to_path("file://otherhost/tmp/a.py") is None


def test_locations_of_every_shape():
    rng = {"start": {"line": 1, "character": 2}, "end": {"line": 1, "character": 5}}
    assert protocol.locations_of(None) == []
    assert protocol.locations_of({"uri": "file:///a", "range": rng}) == [
        ("file:///a", (1, 2, 1, 5))
    ]
    link = {"targetUri": "file:///b", "targetRange": {}, "targetSelectionRange": rng}
    assert protocol.locations_of([link]) == [("file:///b", (1, 2, 1, 5))]


def test_hover_markdown_shapes():
    assert protocol.hover_markdown(None) == ""
    assert protocol.hover_markdown({"contents": "plain"}) == "plain"
    assert protocol.hover_markdown({"contents": {"kind": "markdown", "value": "**x**"}}) == "**x**"
    marked = {"contents": [{"language": "python", "value": "def f()"}, "doc"]}
    assert protocol.hover_markdown(marked) == "```python\ndef f()\n```\n\ndoc"


def test_flatten_symbols_hierarchical_and_flat():
    def sym(name, line, children=()):
        rng = {"start": {"line": line, "character": 0}, "end": {"line": line + 2, "character": 0}}
        return {
            "name": name,
            "kind": 5,
            "range": rng,
            "selectionRange": rng,
            "children": list(children),
        }

    items, truncated = protocol.flatten_symbols([sym("A", 0, [sym("m", 1)])], 10)
    assert [(i["name"], i["depth"], i["line"], i["kind"]) for i in items] == [
        ("A", 0, 1, "class"),
        ("m", 1, 2, "class"),
    ]
    assert truncated is False
    flat = [
        {
            "name": n,
            "kind": 12,
            "containerName": "C",
            "location": {
                "uri": "file:///a",
                "range": {
                    "start": {"line": ln, "character": 0},
                    "end": {"line": ln, "character": 1},
                },
            },
        }
        for n, ln in (("b", 5), ("a", 2))
    ]
    items, _ = protocol.flatten_symbols(flat, 10)
    assert [(i["name"], i["container"]) for i in items] == [("a", "C"), ("b", "C")]
    _, truncated = protocol.flatten_symbols([sym("A", 0), sym("B", 3)], 1)
    assert truncated is True


# ── servers ───────────────────────────────────────────────────────────────


def _id_spec(server_id: str) -> ServerSpec:
    spec = spec_by_id(server_id)
    assert spec is not None
    return spec


def _path_spec(path: str) -> ServerSpec:
    spec = spec_for_path(path)
    assert spec is not None
    return spec


def test_spec_for_path_by_extension():
    assert _path_spec("/x/a.py").id == "python"
    assert _path_spec("/x/a.TSX").id == "typescript"
    assert spec_for_path("/x/a.unknown") is None


def test_find_project_root_prefers_family_marker(tmp_path):
    (tmp_path / ".git").mkdir()
    pkg = tmp_path / "pkg"
    (pkg / "src").mkdir(parents=True)
    (pkg / "pyproject.toml").write_text("")
    target = pkg / "src" / "a.py"
    target.write_text("")
    assert find_project_root(str(target), _path_spec("a.py")) == str(pkg.resolve())
    other = tmp_path / "b.go"
    other.write_text("")
    assert find_project_root(str(other), _path_spec("b.go")) == str(tmp_path.resolve())


def test_resolve_argv_records_refusals():
    spec = _path_spec("a.rs")
    refusals: list[str] = []
    with (
        patch("shutil.which", return_value="/opt/rust-analyzer"),
        patch(
            "kiro_crew.github_runner.validate_provider_executable",
            side_effect=ValueError("world-writable"),
        ),
    ):
        assert resolve_argv(spec, "/r", refusals) is None
    assert refusals == ["/opt/rust-analyzer: world-writable"]


# ── pool, against a real child ────────────────────────────────────────────


@pytest.fixture
def fake_server(tmp_path, monkeypatch):
    log = tmp_path / "lsp.log"
    monkeypatch.setenv("FAKE_LSP_LOG", str(log))
    monkeypatch.setattr(
        mgr, "resolve_argv", lambda spec, root, refusals=None, **_: [sys.executable, _FAKE_SERVER]
    )
    monkeypatch.setattr("kiro_crew.sandbox.wrap_argv", lambda argv, **_: (argv, None))
    monkeypatch.setattr("kiro_crew.sandbox.cgroup_scope_argv", lambda argv: argv)
    monkeypatch.setattr(mgr, "WARMUP_RETRY_SECS", 0)
    pool = mgr.LspManager()
    yield pool, log
    pool.shutdown_all()


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pyproject.toml").write_text("")
    (root / "lib.py").write_text("def helper():\n    return 1\n")
    (root / "main.py").write_text("from lib import helper\n\nprint(helper())\n")
    return root


def _messages(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_a_file_request_starts_no_server(fake_server, project):
    """Cmd-click, F12 and the underline work only on a server the LSP tab started."""
    pool, _log = fake_server
    main = str(project / "main.py")
    for operation in ("definition", "references", "hover", "symbols"):
        with pytest.raises(mgr.CodeIntelError) as caught:
            pool.handle(operation, main, 0, 0, None)
        assert caught.value.code == "not_running"
        assert caught.value.extra == {"server": "python", "root": str(project)}
    assert pool.running() == []
    pool.start("python", str(project))
    pool.start("python", str(project))  # idempotent: one server
    assert len(pool.running()) == 1
    assert pool.handle("hover", main, 0, 0, None)["server"] == "python"


def test_definition_references_hover_symbols(fake_server, project):
    pool, log = fake_server
    pool.start("python", str(project))
    lib, main = str(project / "lib.py"), str(project / "main.py")
    pool.handle("symbols", lib, 0, 0, None)  # opens lib.py so the server can resolve it
    found = pool.handle("definition", main, 2, 8, None)
    assert found["server"] == "python"
    assert found["root"] == str(project.resolve())
    assert found["document"] == str(Path(main).resolve())
    assert found["locations"] == [
        {
            "path": str(Path(lib).resolve()),
            "line": 1,
            "character": 4,
            "endLine": 1,
            "endCharacter": 10,
            "preview": "def helper():",
        }
    ]
    refs = pool.handle("references", main, 2, 8, None)
    assert [(Path(r["path"]).name, r["line"]) for r in refs["locations"]] == [
        ("lib.py", 1),
        ("main.py", 1),
        ("main.py", 3),
    ]
    assert pool.handle("hover", main, 2, 8, None)["contents"] == "**helper**"
    outline = pool.handle("symbols", lib, 0, 0, None)["symbols"]
    assert [(s["name"], s["line"], s["kind"]) for s in outline] == [("helper", 1, "function")]
    methods = [m.get("method") for m in _messages(log)]
    assert methods[:2] == ["initialize", None]  # the configuration answer came back
    assert methods.count("initialized") == 1
    assert methods.count("textDocument/didOpen") == 2


def test_the_oldest_open_document_is_closed_past_the_cap(fake_server, project, monkeypatch):
    from kiro_crew.apps.builtins.code_intel import client as client_mod

    monkeypatch.setattr(client_mod, "MAX_OPEN_DOCUMENTS", 2)
    pool, log = fake_server
    pool.start("python", str(project))
    for name in ("a.py", "b.py", "c.py"):
        (project / name).write_text("x = 1\n")
        pool.handle("hover", str(project / name), 0, 0, None)
    closed = [
        m["params"]["textDocument"]["uri"]
        for m in _messages(log)
        if m.get("method") == "textDocument/didClose"
    ]
    assert [u.rsplit("/", 1)[-1] for u in closed] == ["a.py"]
    (client,) = pool._clients.values()
    assert client.document_text(str(project / "a.py")) is None
    assert client.document_text(str(project / "c.py")) == "x = 1\n"


def test_buffer_text_reaches_the_server_as_a_change(fake_server, project):
    pool, log = fake_server
    pool.start("python", str(project))
    main = str(project / "main.py")
    pool.handle("hover", main, 0, 0, None)
    edited = "def fresh():\n    pass\n"
    assert pool.handle("hover", main, 0, 5, edited)["contents"] == "**fresh**"
    pool.handle("hover", main, 0, 5, edited)  # unchanged text sends no second change
    changes = [m for m in _messages(log) if m.get("method") == "textDocument/didChange"]
    assert len(changes) == 1
    assert changes[0]["params"]["textDocument"]["version"] == 2


def test_external_and_sensitive_locations_are_dropped(fake_server, project):
    pool, _ = fake_server
    pool.start("python", str(project))
    (project / "ext.py").write_text("def external():\n    pass\nexternal()\n")
    ext = str(project / "ext.py")
    answer = pool.handle("definition", ext, 2, 1, None)
    assert answer["locations"] == [] and answer["external"] == 1
    with patch("kiro_crew.security.is_sensitive_path", return_value=True):
        answer = pool.handle("references", ext, 2, 1, None)
    assert answer["locations"] == [] and answer["hidden"] == 2


def test_one_server_per_root_and_lru_eviction(fake_server, tmp_path, monkeypatch):
    pool, _ = fake_server
    monkeypatch.setattr(mgr, "MAX_SERVERS", 1)
    roots = []
    for name in ("one", "two"):
        root = tmp_path / name
        root.mkdir()
        (root / "pyproject.toml").write_text("")
        (root / "a.py").write_text("def a():\n    pass\n")
        roots.append(root)
    pool.start("python", str(roots[0]))
    pool.handle("symbols", str(roots[0] / "a.py"), 0, 0, None)
    assert len(pool.running()) == 1
    pool.start("python", str(roots[1]))
    assert [r["root"] for r in pool.running()] == [str(roots[1].resolve())]


def test_no_server_unsupported_and_status(project, monkeypatch):
    pool = mgr.LspManager()
    monkeypatch.setattr(mgr, "resolve_argv", lambda spec, root, refusals=None: None)
    with pytest.raises(mgr.CodeIntelError) as caught:
        pool.start("python", str(project))
    assert caught.value.code == "no_server"
    install = caught.value.extra["install"]
    assert isinstance(install, list) and install[0] == "pyright-langserver"
    created: list = []
    monkeypatch.setattr(
        mgr, "resolve_argv", lambda spec, root, refusals=None, create=True: created.append(create)
    )
    status = pool.handle("status", str(project / "main.py"), 0, 0, None)
    assert status["available"] is False and status["server"] == "python"
    assert created == [False]  # a probe creates no data directory
    (project / "notes.xyz").write_text("")
    with pytest.raises(mgr.CodeIntelError) as caught:
        pool.handle("hover", str(project / "notes.xyz"), 0, 0, None)
    assert caught.value.code == "unsupported"


def test_failed_start_is_backed_off(project, monkeypatch, tmp_path, caplog):
    broken = tmp_path / "broken.py"
    broken.write_text(
        "import sys\nsys.stderr.write('Could not resolve module: demo\\n')\nsys.exit(1)\n"
    )
    monkeypatch.setattr(
        mgr, "resolve_argv", lambda spec, root, refusals=None: [sys.executable, str(broken)]
    )
    monkeypatch.setattr("kiro_crew.sandbox.wrap_argv", lambda argv, **_: (argv, None))
    monkeypatch.setattr("kiro_crew.sandbox.cgroup_scope_argv", lambda argv: argv)
    pool = mgr.LspManager()
    try:
        pool.start("python", str(project))
        with pytest.raises(mgr.CodeIntelError) as caught:
            pool.handle("hover", str(project / "main.py"), 0, 0, None)
        assert caught.value.code == "server_failed"
        # A file request after the failure says why, rather than "not running".
        with pytest.raises(mgr.CodeIntelError) as caught:
            pool.handle("hover", str(project / "main.py"), 0, 0, None)
        assert caught.value.code == "server_failed"
        assert "failed to start" in str(caught.value)
        # The LSP tab's Start honours the same backoff.
        with pytest.raises(mgr.CodeIntelError) as caught:
            pool.start("python", str(project))
        assert caught.value.code == "server_failed"
        # The server's own last words reach the log, not just "exited" ...
        assert "Could not resolve module: demo" in caplog.text
        # ... and the LSP tab, with the backoff that holds off a retry.
        (entry,) = pool.servers()["servers"]
        assert entry["state"] == "failed"
        assert "Could not resolve module: demo" in entry["stderr"]
        assert 0 < entry["retryInSecs"] <= mgr.FAILED_START_BACKOFF_SECS
    finally:
        pool.shutdown_all()


def test_shut_down_pool_refuses(project):
    pool = mgr.LspManager()
    pool.shutdown_all()
    for attempt in (
        lambda: pool.handle("hover", str(project / "main.py"), 0, 0, None),
        lambda: pool.start("python", str(project)),
    ):
        with pytest.raises(mgr.CodeIntelError) as caught:
            attempt()
        assert caught.value.code == "shutting_down"


def test_the_gateway_pool_stays_shut_after_cleanup(monkeypatch):
    """A request during or after cleanup gets no fresh pool whose servers nothing stops."""
    monkeypatch.setattr(mgr, "_manager", None)
    monkeypatch.setattr(mgr, "_shutdown", False)
    first = mgr.code_intel_manager()
    assert mgr.code_intel_manager() is first
    mgr.shutdown_code_intel()
    with pytest.raises(mgr.CodeIntelError) as caught:
        mgr.code_intel_manager()
    assert caught.value.code == "shutting_down"
    mgr.shutdown_code_intel()  # idempotent


def test_a_server_stopped_while_spawning_is_still_stopped(project, monkeypatch):
    """stop/evict during the spawn finds no process; the start thread then takes it down."""
    import kiro_crew.sandbox as sandbox
    from kiro_crew.apps.builtins.code_intel.client import LspClient

    monkeypatch.setattr("kiro_crew.sandbox.wrap_argv", lambda argv, **_: (argv, None))
    monkeypatch.setattr("kiro_crew.sandbox.cgroup_scope_argv", lambda argv: argv)
    client = LspClient("python", str(project), [sys.executable, _FAKE_SERVER])
    real_popen = sandbox.popen_limited
    spawned: list = []

    def popen_then_stop(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        spawned.append(proc)
        client.kill()  # the pool gave up on this client mid-spawn: nothing to kill yet
        assert proc.poll() is None
        return proc

    monkeypatch.setattr(sandbox, "popen_limited", popen_then_stop)
    client.start(5.0)
    (proc,) = spawned
    assert proc.wait(timeout=5) is not None  # the child did not outlive the stop
    assert client.start_error and not client.alive
    # A stop that lands before the spawn skips it altogether.
    other = LspClient("python", str(project), [sys.executable, _FAKE_SERVER])
    other.kill()
    spawned.clear()
    other.start(5.0)
    assert spawned == [] and other.start_error


# ── LSP tab: pool status, detection, workspace symbols ────────────────────


def _ready(pool, project):
    pool.start("python", str(project))
    pool.handle("hover", str(project / "main.py"), 0, 0, None)
    (client,) = pool._clients.values()
    return client


def test_servers_reports_a_ready_server_until_stopped(fake_server, project):
    pool, _log = fake_server
    client = _ready(pool, project)
    answer = pool.servers()
    (entry,) = answer["servers"]
    assert entry["server"] == "python" and entry["root"] == str(project)
    assert entry["state"] == "ready" and entry["pid"] == client.pid
    assert entry["shutdownInSecs"] <= answer["idleShutdownSecs"]
    assert pool.stop("python", str(project)) is True
    assert pool.servers()["servers"] == []
    assert pool.stop("python", str(project)) is False


def test_restart_replaces_the_server(fake_server, project):
    pool, _log = fake_server
    first = _ready(pool, project)
    pool.restart("python", str(project))
    (second,) = pool._clients.values()
    assert second is not first and second.wait_ready(10)
    assert not first.alive


def test_crash_after_start_logs_the_servers_stderr(fake_server, project, monkeypatch, caplog):
    pool, _log = fake_server
    key = "AKIAIOSFODNN7EXAMPLE"  # the example key the redaction tests use
    monkeypatch.setenv(
        "FAKE_LSP_STDERR", f"java.lang.OutOfMemoryError: demo AWS_ACCESS_KEY_ID={key}"
    )
    client = _ready(pool, project)
    with pytest.raises(mgr.LspError):
        client.request("fake/crash", {}, timeout=5)
    deadline = time.monotonic() + 5
    while "exited (rc=3)" not in caplog.text and time.monotonic() < deadline:
        time.sleep(0.05)
    assert "exited (rc=3)" in caplog.text
    assert "java.lang.OutOfMemoryError: demo" in caplog.text
    assert key not in caplog.text  # the tail quotes the environment; the log is scrubbed


def test_workspace_symbols_and_root_guard(fake_server, project):
    pool, _log = fake_server
    answer = pool.workspace_symbols("python", str(project), "help")
    assert [(s["name"], s["path"], s["line"]) for s in answer["symbols"]] == [
        ("helper", str(project / "lib.py"), 1)
    ]
    assert answer["symbols"][0]["kind"] == "function"
    assert pool.workspace_symbols("python", str(project), "external")["external"] == 1
    # A directory the server would not be rooted at is refused, not started over.
    (project / "sub").mkdir()
    with pytest.raises(mgr.CodeIntelError) as caught:
        pool.workspace_symbols("python", str(project / "sub"), "x")
    assert caught.value.code == "bad_request"
    with pytest.raises(mgr.CodeIntelError) as caught:
        pool.workspace_symbols("cobol", str(project), "x")
    assert caught.value.code == "bad_request"


def test_detect_groups_files_by_project_and_starts_nothing(fake_server, project, tmp_path):
    pool, _log = fake_server
    crate = project / "crate"
    (crate / "src").mkdir(parents=True)
    (crate / "Cargo.toml").write_text("")
    (crate / "src" / "main.rs").write_text("fn main() {}\n")
    (project / "node_modules" / "dep").mkdir(parents=True)
    (project / "node_modules" / "dep" / "index.js").write_text("")
    (project / ".hidden").mkdir()
    (project / ".hidden" / "x.go").write_text("")
    answer = pool.detect(str(tmp_path))
    found = {(p["server"], p["root"]): p for p in answer["projects"]}
    assert set(found) == {("python", str(project)), ("rust", str(crate))}
    assert found[("python", str(project))]["files"] == 2
    assert found[("python", str(project))]["available"] is True
    assert found[("python", str(project))]["state"] == "stopped"
    assert answer["truncated"] is False
    assert pool._clients == {}
    _ready(pool, project)
    states = {p["server"]: p["state"] for p in pool.detect(str(tmp_path))["projects"]}
    assert states["python"] == "ready"


def test_detect_reaches_deep_java_and_skips_fenced_dirs(fake_server, tmp_path, monkeypatch):
    pool, _log = fake_server
    service = tmp_path / "ws" / "src" / "Svc"
    deep = service / "src" / "main" / "java" / "com" / "amazon" / "team" / "lambda"
    deep.mkdir(parents=True)
    (service / "build.gradle").write_text("")
    (deep / "Handler.java").write_text("class Handler {}\n")
    (service / "bin" / "main").mkdir(parents=True)
    (service / "bin" / "main" / "Handler.java").write_text("")
    secret = tmp_path / "ws" / "creds"
    secret.mkdir()
    (secret / "leak.py").write_text("")
    monkeypatch.setattr("kiro_crew.security.paths.path_contains_sensitive", lambda *a, **k: True)
    monkeypatch.setattr(
        "kiro_crew.security.paths.is_sensitive_resolved_path", lambda p: p == str(secret)
    )
    found = {(p["server"], p["root"]): p["files"] for p in pool.detect(str(tmp_path))["projects"]}
    # Nine directories down, and the compiled `bin/` copy is not counted.
    assert found == {("java", str(service)): 1}


def test_java_with_eclipse_files_imports_them_into_a_fresh_workspace(tmp_path, monkeypatch):
    from kiro_crew.apps.builtins.code_intel.servers import (
        data_dir_for,
        initialization_options,
        setup_hint,
    )

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    java = _id_spec("java")
    ws = tmp_path / "ws"
    pkg = ws / "src" / "Svc"
    pkg.mkdir(parents=True)
    (ws / "packageInfo").write_text("")
    (pkg / "build.gradle").write_text("")
    gradle_dir = data_dir_for(java, str(pkg))
    assert initialization_options(java, str(pkg)) == {
        "extendedClientCapabilities": {"classFileContentsSupport": True}
    }
    assert str(ws) in setup_hint(java, str(pkg))
    (pkg / ".project").write_text("")
    (pkg / ".classpath").write_text("")
    # bemol's files switch the importer off Gradle, in a workspace of its own.
    assert data_dir_for(java, str(pkg)) != gradle_dir
    settings = initialization_options(java, str(pkg))["settings"]["java"]["import"]
    assert settings == {"gradle": {"enabled": False}, "maven": {"enabled": False}}
    # A .project rewritten without the Java nature leaves jdtls blind.
    assert "no Java nature" in setup_hint(java, str(pkg))
    (pkg / ".project").write_text(
        "<natures><nature>org.eclipse.jdt.core.javanature</nature></natures>"
    )
    assert setup_hint(java, str(pkg)) == ""
    assert initialization_options(_id_spec("python"), str(pkg)) == {}


def test_jdtls_project_status_error_and_warning_become_notices():
    from kiro_crew.apps.builtins.code_intel.client import LspClient

    def note(client, method, **params):
        client._note({"method": method, "params": params})

    failed = LspClient("java", "/p", ["jdtls"])
    note(
        failed,
        "window/logMessage",
        type=1,
        message="Oct 27, 2026, 5:38:01 PM Could not fetch model",
    )
    note(failed, "language/status", type="ProjectStatus", message="ERROR")
    assert failed.notices() == [
        {"level": "error", "message": "The project did not load: Could not fetch model"}
    ]

    # WARNING means the project loaded; only a reason is worth showing, as a warning.
    loaded = LspClient("java", "/p", ["jdtls"])
    note(loaded, "language/status", type="ProjectStatus", message="WARNING")
    assert loaded.notices() == []
    note(loaded, "window/logMessage", type=2, message="Missing source folder src/test/resources")
    note(loaded, "language/status", type="ProjectStatus", message="WARNING")
    assert loaded.notices() == [
        {
            "level": "warning",
            "message": "The project loaded with a warning: Missing source folder src/test/resources",
        }
    ]
    note(loaded, "language/status", type="ProjectStatus", message="OK")
    assert len(loaded.notices()) == 1


def _sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_a_buffer_named_by_hash_is_not_sent_again(fake_server, project):
    pool, log = fake_server
    pool.start("python", str(project))
    main = str(project / "main.py")
    on_disk = (project / "main.py").read_text()
    # The file as saved: the gateway reads it from disk, the viewer sends no text.
    pool.handle("hover", main, 0, 0, None, _sha(on_disk))
    edited = "def fresh():\n    pass\n"
    # An edited buffer neither the server nor the disk holds: ask for the text.
    with pytest.raises(mgr.CodeIntelError) as refused:
        pool.handle("hover", main, 0, 5, None, _sha(edited))
    assert refused.value.code == "text_required"
    assert pool.handle("hover", main, 0, 5, edited)["contents"] == "**fresh**"
    # The server holds that buffer now: the hash alone answers, with no change sent.
    assert pool.handle("hover", main, 0, 5, None, _sha(edited))["contents"] == "**fresh**"
    methods = [m.get("method") for m in _messages(log)]
    assert methods.count("textDocument/didOpen") == 1
    assert methods.count("textDocument/didChange") == 1


def _eclipse_project(folder: Path, name: str, references: Sequence[str] = ()) -> Path:
    folder.mkdir(parents=True)
    (folder / ".project").write_text(
        f"<?xml version='1.0'?>\n<projectDescription>\n\t<name>{name}</name>\n"
        "\t<natures><nature>org.eclipse.jdt.core.javanature</nature></natures>\n"
        "</projectDescription>\n"
    )
    entries = "".join(
        f'\t<classpathentry combineaccessrules="false" kind="src" path="/{ref}"/>\n'
        for ref in references
    )
    (folder / ".classpath").write_text(
        f'<classpath>\n\t<classpathentry kind="src" path="src"/>\n{entries}'
        '\t<classpathentry kind="lib" path="/opt/jars/x.jar"/>\n</classpath>\n'
    )
    return folder


def test_java_opens_the_projects_its_classpath_references(tmp_path):
    from kiro_crew.apps.builtins.code_intel.servers import missing_references_hint, project_folders

    src = tmp_path / "ws" / "src"
    app = _eclipse_project(src / "App", "App", ["Common", "Gone"])
    # The folder name need not match: references name the project, not the folder.
    common = _eclipse_project(src / "common-lib", "Common", ["Base"])
    base = _eclipse_project(src / "Base", "Base", ["Common"])  # a cycle ends the walk
    _eclipse_project(src / "Unrelated", "Unrelated")
    java = _id_spec("java")
    folders, missing = project_folders(java, str(app))
    assert folders == [str(app), str(common.resolve()), str(base.resolve())]
    assert missing == ["Gone"]
    assert "Gone" in missing_references_hint(missing)
    # A reference whose project sits in the folder of the same name is found by
    # reading that folder's .project alone: the other siblings are not listed.
    # A folder of that name holding another project does not stand in for it.
    (src / "Common").mkdir()
    (src / "Common" / ".project").write_text(
        "<projectDescription><name>Other</name></projectDescription>"
    )
    (src / "Common" / ".classpath").write_text("<classpath/>")
    assert project_folders(java, str(base))[0] == [
        str(base),
        str(common.resolve()),
    ]  # same name, wrong project
    only = _eclipse_project(src / "Only", "Only", ["Unrelated"])
    with patch(
        "kiro_crew.apps.builtins.code_intel.servers.os.scandir",
        side_effect=AssertionError("listed"),
    ):
        assert project_folders(java, str(only))[0] == [
            str(only),
            str((src / "Unrelated").resolve()),
        ]
    # jdtls imports what initializationOptions names, so the folders go there too.
    from kiro_crew.apps.builtins.code_intel.servers import initialization_options

    options = initialization_options(java, str(app), folders)
    assert [u.rsplit("/", 1)[-1] for u in options["workspaceFolders"]] == [
        "App",
        "common-lib",
        "Base",
    ]
    assert "workspaceFolders" not in initialization_options(java, str(app), [str(app)])
    # A package is a Java root by its Eclipse files alone, with no build file.
    from kiro_crew.apps.builtins.code_intel.servers import find_project_root

    (app / "src" / "x").mkdir(parents=True)
    (app / "src" / "x" / "A.java").write_text("class A {}\n")
    assert find_project_root(str(app / "src" / "x" / "A.java"), java) == str(app)
    assert missing_references_hint([]) == ""
    # Other servers, and Java without Eclipse files, open the root alone.
    assert project_folders(_id_spec("python"), str(app)) == ([str(app)], [])
    plain = src / "Plain"
    plain.mkdir()
    assert project_folders(java, str(plain)) == ([str(plain)], [])


def test_a_referenced_project_is_served_by_the_server_that_opened_it(fake_server, tmp_path):
    pool, log = fake_server
    src = tmp_path / "ws" / "src"
    app = _eclipse_project(src / "App", "App", ["Common"])
    common = _eclipse_project(src / "Common", "Common")
    (app / "A.java").write_text("class A {}\n")
    (common / "C.java").write_text("class C {}\n")
    pool.start("java", str(app))
    pool.handle("symbols", str(app / "A.java"), 0, 0, None)
    init = next(m for m in _messages(log) if m.get("method") == "initialize")
    assert [f["name"] for f in init["params"]["workspaceFolders"]] == ["App", "Common"]
    pool.handle("symbols", str(common / "C.java"), 0, 0, None)
    entries = pool.servers()["servers"]
    assert [(Path(e["root"]).name, [Path(f).name for f in e["folders"]]) for e in entries] == [
        ("App", ["Common"])
    ]
    assert sum(m.get("method") == "initialize" for m in _messages(log)) == 1


def test_a_library_class_is_written_out_and_navigable(fake_server, project, tmp_path, monkeypatch):
    from kiro_crew.apps.builtins.code_intel import classfiles

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    pool, log = fake_server
    (project / "use.py").write_text("def library():\n    pass\nlibrary()\n")
    pool.start("python", str(project))
    answer = pool.handle("definition", str(project / "use.py"), 2, 1, None)
    assert answer["external"] == 0
    [location] = answer["locations"]
    written = Path(location["path"])
    assert written.name == "Lib.java"
    assert written.read_text() == "package x;\n\npublic class Lib {}\n"
    assert written.stat().st_mode & 0o222 == 0  # read-only
    origin = classfiles.origin(str(written.resolve()))
    assert origin is not None
    assert origin[0] == "jdt://contents/lib.jar/x/Lib.class?=fake"
    # A second answer reuses the file rather than asking the server again.
    pool.handle("definition", str(project / "use.py"), 2, 1, None)
    assert [m.get("method") for m in _messages(log)].count("java/classFileContents") == 1
    # A request on the written file goes to the Java server for the root that
    # produced it, with the class URI rather than a copy; it starts nothing.
    with pytest.raises(mgr.CodeIntelError) as caught:
        pool.handle("hover", str(written), 2, 14, None)
    assert caught.value.code == "not_running" and caught.value.extra["root"] == str(project)
    pool.start("java", str(project))
    pool.handle("hover", str(written), 2, 14, None)
    hovers = [m for m in _messages(log) if m.get("method") == "textDocument/hover"]
    assert hovers[-1]["params"]["textDocument"]["uri"] == "jdt://contents/lib.jar/x/Lib.class?=fake"
    opened = [
        m["params"]["textDocument"]["uri"]
        for m in _messages(log)
        if m.get("method") == "textDocument/didOpen"
    ]
    assert not any("Lib.java" in uri for uri in opened)
    # A file planted beside it, or anywhere else, has no origin.
    planted = written.parent / "Other.java"
    planted.write_text("class Other {}\n")
    assert classfiles.origin(str(planted)) is None
    assert classfiles.origin(str(project / "use.py")) is None


def test_class_file_fetches_share_one_budget_per_answer(fake_server, project, monkeypatch):
    monkeypatch.setattr(mgr, "CLASS_FILES_BUDGET_SECS", 0.5)
    pool, log = fake_server
    (project / "slow.py").write_text("def slowlib():\n    pass\nslowlib()\n")
    pool.start("python", str(project))
    started = time.monotonic()
    answer = pool.handle("definition", str(project / "slow.py"), 2, 1, None)
    # Three classes the server never delivers: one budget for all, not a timeout each.
    assert time.monotonic() - started < mgr.REQUEST_TIMEOUT_SECS
    assert answer["locations"] == [] and answer["external"] == 3
    asked = [m for m in _messages(log) if m.get("method") == "java/classFileContents"]
    assert 1 <= len(asked) <= 3


def test_symbol_search_merges_running_servers_and_opens_library_classes(
    fake_server, project, tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    pool, _ = fake_server
    other = tmp_path / "other"
    other.mkdir()
    (other / "pyproject.toml").write_text("")
    (other / "more.py").write_text("def helpful():\n    pass\n")
    assert pool.workspace_symbols_all("help")["servers"] == 0  # starts nothing
    pool.start("python", str(project))
    pool.start("python", str(other))
    pool.handle("symbols", str(project / "lib.py"), 0, 0, None)
    pool.handle("symbols", str(other / "more.py"), 0, 0, None)
    merged = pool.workspace_symbols_all("help")
    assert merged["servers"] == 2 and merged["errors"] == []
    assert [(s["name"], Path(s["root"]).name) for s in merged["symbols"]] == [
        ("helper", "proj"),
        ("helpful", "other"),
    ]
    library = pool.workspace_symbols_all("Lib")["symbols"]
    # Both servers found the class: it is listed once, by its URI, with no path yet.
    assert len(library) == 1 and library[0]["path"] == "" and library[0]["uri"].startswith("jdt://")
    opened = pool.open_class(library[0]["server"], library[0]["root"], library[0]["uri"])
    assert Path(opened["path"]).read_text().startswith("package x;")
    with pytest.raises(mgr.CodeIntelError) as caught:
        pool.open_class(library[0]["server"], str(tmp_path), library[0]["uri"])
    assert caught.value.code == "not_running"
    with pytest.raises(mgr.CodeIntelError):
        pool.open_class(library[0]["server"], library[0]["root"], "file:///etc/passwd")


def test_class_file_names():
    from kiro_crew.apps.builtins.code_intel.classfiles import _file_name

    assert (
        _file_name(
            "jdt://contents/d.jar/dagger/Component.java?=P/%5C/x.jar%3Cdagger%28Component.class"
        )
        == "Component.java"
    )
    assert _file_name("jdt://contents/d.jar/x/Lib.class?=fake") == "Lib.java"
    assert _file_name("jdt://contents/d.jar/x/..%2F..%2Fevil.class") == "evil.java"
    assert _file_name("jdt://contents/") == "Class.java"


def test_hint_names_classpath_jars_missing_from_disk(tmp_path):
    from kiro_crew.apps.builtins.code_intel.servers import setup_hint

    pkg = _eclipse_project(tmp_path / "ws" / "src" / "Svc", "Svc")
    (tmp_path / "ws" / "packageInfo").write_text("")
    jar = tmp_path / "present.jar"
    jar.write_text("")
    (pkg / ".classpath").write_text(
        f'<classpath><classpathentry kind="lib" path="{jar}"/>'
        '<classpathentry kind="lib" path="/Volumes/brazil-pkg-cache/x.jar" sourcepath=""/></classpath>'
    )
    hint = setup_hint(_id_spec("java"), str(pkg))
    assert "1 of 2 libraries" in hint and "/Volumes/brazil-pkg-cache/x.jar" in hint
    assert str(tmp_path / "ws") in hint
    (pkg / ".classpath").write_text(
        f'<classpath><classpathentry kind="lib" path="{jar}"/></classpath>'
    )
    assert setup_hint(_id_spec("java"), str(pkg)) == ""
