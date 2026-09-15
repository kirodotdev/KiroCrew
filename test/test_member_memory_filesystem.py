"""Member-only Global V1 filesystem separation, including real late-file probes."""

from __future__ import annotations

import ast
import json
import os
import selectors
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import make_dir_link
from kiro_crew import sandbox


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = (tmp_path / "crew").resolve()
    root.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(root))
    monkeypatch.delenv("KIROCREW_MCP_SOCKET", raising=False)
    monkeypatch.delenv("MC_MCP_SOCKET", raising=False)
    monkeypatch.setattr(sandbox, "config_dir", lambda: root)
    monkeypatch.setattr(sandbox, "_private_memory_roots", lambda: [str(root)])
    return root


def test_private_workspace_layout_uses_relative_absolute_tilde_and_overlay_roots(home, monkeypatch):
    relative = home / "projects" / "foo"
    external = home.parent / "external"
    tilde = home.parent / "tilde-workspace"
    for directory in (relative, external, tilde):
        directory.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home.parent))
    monkeypatch.setenv("USERPROFILE", str(home.parent))
    (home / "config.json").write_text(
        json.dumps(
            {"workspaces": {"relative": "projects/foo", "overridden": {"dir": "old-missing"}}}
        )
    )
    (home / "config.local.json").write_text(
        json.dumps(
            {"workspaces": {"overridden": {"dir": str(external)}, "tilde": "~/tilde-workspace"}}
        )
    )

    layout = sandbox._private_memory_layout()

    assert set(layout.workspaces) == {
        str(home / "workspace"),
        str(relative),
        str(external),
        str(tilde),
    }
    assert not (home / "workspace").exists()
    assert not (home / "old-missing").exists()
    # A captured layout is used for both the path denies and hardlink scan.
    (home / "config.local.json").write_text(json.dumps({"workspaces": {"later": "missing"}}))
    rules = "\n".join(sandbox._private_memory_seatbelt_rules(layout=layout))
    for directory in (relative, external, tilde):
        assert f'(literal "{directory}")' in rules
    sandbox._validate_private_memory_hardlinks(layout)
    with pytest.raises(RuntimeError, match="configured workspace.*unavailable"):
        sandbox._private_memory_layout()


@pytest.mark.parametrize("defect", ["missing", "file", "bad_type", "bad_overlay"])
def test_private_workspace_layout_refuses_unverifiable_explicit_roots_before_log_creation(
    home, defect
):
    directory = home / "projects" / "foo"
    if defect == "file":
        directory.parent.mkdir()
        directory.write_text("not a directory")
    entry = 7 if defect == "bad_type" else "projects/foo"
    (home / "config.json").write_text(json.dumps({"workspaces": {"foo": entry}}))
    if defect == "bad_overlay":
        (home / "config.local.json").write_text("{")
    with pytest.raises(RuntimeError, match="memory_unavailable:.*workspace"):
        sandbox._prepare_private_log_dir()
    assert not (home / "memory_stores" / ".execution-logs").exists()
    assert directory.is_file() if defect == "file" else not directory.exists()
    assert "memory($|[._/])" not in sandbox._build_seatbelt_profile("standard")


def test_private_workspace_layout_covers_known_legacy_home_overlay(home, monkeypatch):
    legacy = home.parent / "legacy"
    workspace = legacy / "projects" / "foo"
    workspace.mkdir(parents=True)
    (legacy / "config.local.json").write_text(json.dumps({"workspaces": {"foo": "projects/foo"}}))
    monkeypatch.setattr(sandbox, "_private_memory_roots", lambda: [str(home), str(legacy)])
    layout = sandbox._private_memory_layout()
    assert str(workspace) in layout.workspaces
    assert layout.homes == (str(home), str(legacy))


@pytest.mark.parametrize("relative", ["memory/preferences.md", "memory_index.db", "lessons.jsonl"])
def test_private_workspace_hardlink_refusal_includes_configured_external_store(home, relative):
    workspace = home.parent / "external"
    source = workspace / relative
    source.parent.mkdir(parents=True)
    source.write_text("workspace memory")
    (home / "config.json").write_text(
        json.dumps({"workspaces": {"external": {"dir": str(workspace)}}})
    )
    alias = home / "ordinary-project-alias.txt"
    alias.hardlink_to(source)
    with pytest.raises(RuntimeError, match="protected memory has a hardlink"):
        sandbox._prepare_private_log_dir()
    alias.unlink()
    assert Path(sandbox._prepare_private_log_dir()).is_dir()
    assert source.read_text() == "workspace memory"


def test_private_workspace_layout_refuses_dangling_implicit_alias(home):
    target = home.parent / "absent-workspace"
    target.mkdir()
    make_dir_link(home / "workspace", target)
    target.rmdir()
    with pytest.raises(RuntimeError, match="implicit workspace.*dangling link"):
        sandbox._private_memory_layout()


@pytest.mark.parametrize("origin", ["configured", "implicit_alias"])
@pytest.mark.parametrize("defect", ["missing", "file", "unreadable"])
def test_private_workspace_snapshot_refuses_a_required_root_lost_before_preflight(
    home, monkeypatch, origin, defect
):
    workspace = home.parent / "external"
    workspace.mkdir()
    if origin == "configured":
        (home / "config.json").write_text(json.dumps({"workspaces": {"external": str(workspace)}}))
    else:
        make_dir_link(home / "workspace", workspace)
    layout = sandbox._private_memory_layout()
    if defect == "unreadable":
        original = Path.is_dir

        def is_dir(path):
            if path == workspace:
                raise PermissionError("workspace no longer readable")
            return original(path)

        monkeypatch.setattr(Path, "is_dir", is_dir)
    else:
        workspace.rmdir()
        if defect == "file":
            workspace.write_text("not a directory")

    with pytest.raises(RuntimeError, match="memory_unavailable:.*workspace directory") as refusal:
        sandbox._prepare_private_log_dir(layout)

    assert str(workspace) in str(refusal.value)
    assert not (home / "memory_stores" / ".execution-logs").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher/profile preparation")
@pytest.mark.parametrize("backend", ["namespace", "seatbelt"])
def test_private_spawn_uses_one_workspace_snapshot_for_preflight_and_policy(
    home, monkeypatch, backend
):
    workspace = home / "projects" / "foo"
    workspace.mkdir(parents=True)
    config = home / "config.json"
    config.write_text(json.dumps({"workspaces": {"foo": "projects/foo"}}))
    original = sandbox._private_memory_layout
    calls = []

    def capture():
        layout = original()
        calls.append(layout)
        config.write_text(json.dumps({"workspaces": {"later": "not-created"}}))
        return layout

    monkeypatch.setattr(sandbox, "_private_memory_layout", capture)
    if backend == "namespace":
        argv = sandbox.namespace_argv([sys.executable], "standard", private_memory=True)
        artifact = sandbox._launcher_script_of(argv)
    else:
        _, artifact = sandbox.sandbox_exec_argv([sys.executable], "standard", private_memory=True)
    assert artifact is not None
    try:
        assert len(calls) == 1
        policy = Path(artifact).read_text(encoding="utf-8")
        assert str(workspace) in policy
        assert "not-created" not in policy
    finally:
        Path(artifact).unlink(missing_ok=True)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher")
def test_default_launcher_remains_identical_and_private_view_is_explicit(home):
    default = sandbox._build_launcher_script("standard")
    assert default == sandbox._build_launcher_script("standard", private_memory=False)
    assert "_private_cwd" not in default
    private = sandbox._build_launcher_script("standard", private_memory=True)
    ast.parse(private)
    assert "_private_cwd" in private and "os.chdir(_private_cwd)" in private


def test_private_seatbelt_rules_override_visible_carveout_and_preserve_v1(home):
    default = sandbox._build_seatbelt_profile("standard")
    assert default == sandbox._build_seatbelt_profile("standard", private_memory=False)
    private = sandbox._build_seatbelt_profile(
        "standard", private_memory=True, extra_visible_dirs=(str(home),)
    )
    for rule in sandbox._private_memory_seatbelt_rules():
        assert rule in private
        if "(regex " in rule:
            assert rule not in default
    assert "lessons" in private and "backups" in private


def test_private_seatbelt_global_regex_exempts_only_its_execution_logs(home):
    own = str(home / "memory_stores" / ".execution-logs" / "member-own")
    rules = sandbox._private_memory_seatbelt_rules(own)
    global_denies = [rule for rule in rules if "memory($|[._/])" in rule]
    assert len(global_denies) == 6
    for rule in global_denies:
        assert f"(require-not (subpath {json.dumps(own)}))" in rule


@pytest.mark.parametrize("relative", ["memory.db", "workspace/memory/preferences.md"])
def test_private_spawn_refuses_existing_memory_hardlink_without_scanning_projects(home, relative):
    source = home / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("private memory")
    project = home / "workspace" / "project"
    project.mkdir(parents=True, exist_ok=True)
    (project / "alias.txt").hardlink_to(source)
    with pytest.raises(RuntimeError, match="memory_unavailable: protected memory has a hardlink"):
        sandbox._prepare_private_log_dir()
    # Removing the alias restores admission; no memory contents are changed.
    (project / "alias.txt").unlink()
    directory = Path(sandbox._prepare_private_log_dir())
    assert directory.is_dir()
    assert source.read_text() == "private memory"


@pytest.mark.parametrize(
    "leaf", ["mcp-gateway", "kirocrew-mcp-gateway.sock", "mc-mcp-gateway.sock"]
)
def test_private_seatbelt_blocks_shared_broker_file_and_socket_access(home, leaf):
    default = sandbox._build_seatbelt_profile("standard")
    private = sandbox._build_seatbelt_profile(
        "standard", private_memory=True, extra_visible_dirs=(str(home),)
    )
    predicate = "subpath" if leaf == "mcp-gateway" else "literal"
    for operation in ("file-read*", "file-write*", "file-link"):
        rule = f"(deny {operation} ({predicate} {json.dumps(str(home) + '/' + leaf)}))"
        assert rule in private
        assert rule not in default
    socket_rule = f"(deny network-outbound (remote unix-socket ({predicate} {json.dumps(str(home) + '/' + leaf)})))"
    assert socket_rule in private
    assert socket_rule not in default
    assert json.dumps(str(home) + "/workspace/" + leaf) not in private
    assert "(deny network-outbound (subpath " + json.dumps(str(home) + "/run") not in private


@pytest.mark.parametrize(
    "endpoint",
    [
        "mcp-gateway/gateway.sock",
        "mcp-gateway/nested/custom.sock",
        "kirocrew-mcp-gateway.sock",
        "mc-mcp-gateway.sock",
    ],
)
def test_private_broker_endpoint_accepts_reserved_paths_in_each_data_home(
    home, monkeypatch, endpoint
):
    alternate = home.parent / "alternate"
    alternate.mkdir()
    monkeypatch.setattr(sandbox, "_private_memory_roots", lambda: [str(home), str(alternate)])
    for root in (home, alternate):
        sandbox._validate_private_mcp_gateway_socket(str(root / endpoint))


@pytest.mark.parametrize(
    "source", ["constructor", "persisted", "KIROCREW_MCP_SOCKET", "MC_MCP_SOCKET", "child"]
)
def test_private_broker_endpoint_refuses_uncontained_custom_socket(home, monkeypatch, source):
    project = home.parent / "project"
    project.mkdir()
    code = project / "code.py"
    code.write_text("project code")
    endpoint = str(project / "custom.sock")
    overrides = ()
    argument = ""
    if source == "constructor":
        argument = endpoint
    elif source == "persisted":
        (home / "config.json").write_text(
            json.dumps({"mcp_gateway": {"stub_servers": [], "socket_path": endpoint}})
        )
    elif source == "child":
        overrides = (str(home / "mcp-gateway" / "safe.sock"), endpoint)
    else:
        monkeypatch.setenv(source, endpoint)
    with pytest.raises(RuntimeError, match="reserved mcp-gateway"):
        sandbox._validate_private_mcp_gateway_socket(argument, overrides)
    assert code.read_text() == "project code"
    assert not (project / "custom.sock").exists()


@pytest.mark.parametrize("stub_servers", [[], ["builder"]])
def test_private_broker_validates_persisted_socket_independently_of_routing(home, stub_servers):
    (home / "config.json").write_text(
        json.dumps(
            {
                "mcp_gateway": {
                    "stub_servers": stub_servers,
                    "socket_path": str(home / "mcp-gateway" / "custom.sock"),
                }
            }
        )
    )
    sandbox._validate_private_mcp_gateway_socket(
        socket_overrides=(str(home / "mc-mcp-gateway.sock"),)
    )


def test_private_broker_validates_other_known_data_homes(home, monkeypatch):
    alternate = home.parent / "alternate"
    alternate.mkdir()
    (alternate / "config.json").write_text(
        json.dumps(
            {"mcp_gateway": {"stub_servers": [], "socket_path": str(home.parent / "old.sock")}}
        )
    )
    monkeypatch.setattr(sandbox, "_private_memory_roots", lambda: [str(home), str(alternate)])
    with pytest.raises(RuntimeError, match="reserved mcp-gateway"):
        sandbox._validate_private_mcp_gateway_socket()


def test_private_broker_refuses_relative_child_paths_even_when_gateway_cwd_is_safe(
    home, monkeypatch
):
    monkeypatch.chdir(home)
    with pytest.raises(RuntimeError, match="using an absolute path"):
        sandbox._validate_private_mcp_gateway_socket(socket_overrides=("mcp-gateway/gateway.sock",))


@pytest.mark.parametrize(
    "contents", ["{broken", "[]", '{"mcp_gateway": null}', '{"mcp_gateway": {"socket_path": null}}']
)
def test_private_broker_refuses_unverifiable_persisted_config(home, contents):
    (home / "config.json").write_text(contents)
    with pytest.raises(RuntimeError, match="cannot verify the configured MCP socket"):
        sandbox._validate_private_mcp_gateway_socket()


def test_private_broker_refuses_unreadable_persisted_config(home, monkeypatch):
    original = Path.read_text

    def read(path, *args, **kwargs):
        if path == home / "config.json":
            raise PermissionError("fixture cannot read config")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises(RuntimeError, match="cannot verify the configured MCP socket"):
        sandbox._validate_private_mcp_gateway_socket()


def test_v1_profile_does_not_apply_private_broker_config_validation(home):
    before = sandbox._build_seatbelt_profile("standard")
    (home / "config.json").write_text("{broken")
    assert sandbox._build_seatbelt_profile("standard") == before


def test_private_broker_endpoint_refuses_redirected_reserved_directory(home):
    outside = home.parent / "outside"
    outside.mkdir()
    make_dir_link(home / "mcp-gateway", outside)
    with pytest.raises(RuntimeError, match="reserved mcp-gateway"):
        sandbox._validate_private_mcp_gateway_socket()


def test_private_broker_endpoint_resolves_alias_into_hidden_namespace(home):
    broker = home / "mcp-gateway"
    broker.mkdir()
    make_dir_link(home / "broker-alias", broker)
    sandbox._validate_private_mcp_gateway_socket(str(home / "broker-alias" / "gateway.sock"))


def test_private_broker_endpoint_fails_closed_when_origin_cannot_be_resolved(home, monkeypatch):
    unresolved = home / "mcp-gateway" / "unreadable.sock"
    original = Path.resolve

    def resolve(path, *args, **kwargs):
        if path == unresolved:
            raise PermissionError("fixture cannot resolve endpoint")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(RuntimeError, match="cannot resolve the shared MCP socket"):
        sandbox._validate_private_mcp_gateway_socket(str(unresolved))


@pytest.mark.parametrize(
    "mode,backend,nested",
    [("off", "namespace", False), ("auto", "none", False), ("auto", "namespace", True)],
)
def test_actual_private_spawn_cannot_use_unconfined_or_nested_bypass(
    monkeypatch, mode, backend, nested
):
    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: "")
    monkeypatch.setattr(sandbox, "_inside_kirocrew_sandbox", lambda: nested)
    monkeypatch.setattr(sandbox, "detect_backend", lambda **_: backend)
    monkeypatch.setattr(sandbox, "kiro_internal_sandbox_enabled", lambda: False)
    with pytest.raises(RuntimeError, match="Private member memory"):
        sandbox.wrap_argv(
            [sys.executable], mode=mode, private_memory=True, first_party_fixed_argv=True
        )


@pytest.mark.asyncio
async def test_async_wrapper_passes_private_flag_only_when_true():
    calls = []

    def prepare(argv, **kwargs):
        calls.append(kwargs)
        return argv, None

    await sandbox.wrap_argv_async([sys.executable], _prepare=prepare)
    await sandbox.wrap_argv_async([sys.executable], private_memory=True, _prepare=prepare)
    await sandbox.wrap_argv_async(
        [sys.executable],
        private_memory=True,
        private_mcp_gateway_socket="reserved.sock",
        private_mcp_gateway_socket_overrides=("other-reserved.sock",),
        _prepare=prepare,
    )
    await sandbox.wrap_argv_async(
        [sys.executable],
        private_mcp_gateway_socket="ignored-for-v1.sock",
        private_mcp_gateway_socket_overrides=("also-ignored-for-v1.sock",),
        _prepare=prepare,
    )
    assert calls == [
        {"mode": "auto"},
        {"mode": "auto", "private_memory": True},
        {
            "mode": "auto",
            "private_memory": True,
            "private_mcp_gateway_socket": "reserved.sock",
            "private_mcp_gateway_socket_overrides": ("other-reserved.sock",),
        },
        {"mode": "auto"},
    ]


def _configured_workspace_memory(home):
    """Real V1 documents, indexes and lessons in three configured workspaces."""
    from kiro_crew.learn import Lesson, LessonStore
    from kiro_crew.memory import MemoryStore

    roots = [
        home / "projects" / "foo",
        home.parent / "external-workspace",
        home / "overlays" / "bar",
    ]
    for root in roots:
        project = root / "project"
        project.mkdir(parents=True)
        (project / "code.py").write_text("configured project code", encoding="utf-8")
        memory = MemoryStore(workspace=root)
        memory.init()
        memory.write_preferences("V1 workspace preferences")
        lessons = LessonStore(base_dir=root)
        assert lessons.path == root / "lessons.jsonl"
        lessons.save(
            Lesson(ts="2026-09-09T00:00:00Z", rule="V1 workspace rule", category="preference")
        )
        assert (root / "memory_index.db").is_file()
    (home / "config.json").write_text(
        json.dumps(
            {"workspaces": {"relative": "projects/foo", "external": {"dir": str(roots[1])}}}
        ),
        encoding="utf-8",
    )
    (home / "config.local.json").write_text(
        json.dumps({"workspaces": {"overlay": {"dir": "overlays/bar"}}}), encoding="utf-8"
    )
    # An ancestor alias must resolve through the final nested view, not a
    # pre-view bind of projects/. Root and leaf aliases cover both path kinds.
    (home / "projects-alias").symlink_to(home / "projects", target_is_directory=True)
    (home / "configured-alias").symlink_to(roots[1], target_is_directory=True)
    (home / "configured-leaf").symlink_to(roots[1] / "memory" / "preferences.md")
    paths = [
        str(root / relative)
        for root in roots
        for relative in (
            "memory/preferences.md",
            "memory_index.db",
            "lessons.jsonl",
            "memory_index.db-wal",
        )
    ]
    paths.extend(
        str(home / relative)
        for relative in (
            "projects-alias/foo/memory/preferences.md",
            "configured-alias/memory/preferences.md",
            "configured-leaf",
        )
    )
    return roots, paths


_CHILD = """import json,logging,os,sys
from pathlib import Path
h=Path(os.environ['KIROCREW_HOME']); out={}
from kiro_crew.config.paths import private_runtime_log_dir
out['diagnostic_route_valid']=(private_runtime_log_dir()==h/'agent-logs') if sys.argv[1]=='private' else private_runtime_log_dir() is None
if sys.argv[1] == 'private':
 from kiro_crew.cli import _setup_cli_logging
 from kiro_crew.config.paths import ensure_data_home
 from kiro_crew.sel import sel
 ensure_data_home()
 _setup_cli_logging('status',1)
 logging.getLogger('kiro_crew').warning('private namespace log canary')
 out['private_log']='private namespace log canary' in (h/'agent-logs'/f'member-{os.getpid()}.log').read_text()
 sel().log_api_access(caller='filesystem-probe',operation='private.canary',outcome='allowed',critical=True)
 out['private_audit']='private.canary' in (h/'agent-logs'/f'audit-{os.getpid()}'/'security_events.jsonl').read_text()
print('READY',flush=True);sys.stdin.readline()
from kiro_crew.member_memory_auth import _verified_global_process, verified_member_session_for_pid
out['runtime_identity']=(_verified_global_process(os.getpid()) if sys.argv[1]=='v1'
 else verified_member_session_for_pid(os.getpid())=='dashboard:filesystem-probe')
for label,alias in (
 ('proc_launcher_root',Path(f'/proc/{os.getppid()}/root')/str(h).lstrip('/')/'memory.db'),
 ('proc_launcher_cwd',Path(f'/proc/{os.getppid()}/cwd')/'..'/'memory.db'),
 ('proc_host_fd',Path(f'/proc/{sys.argv[2]}/fd/{sys.argv[3]}'))):
 try: alias.read_bytes();out[label]=False
 except PermissionError:out[label]=True
for name in ('memory.db','memory.db-wal','memory.db.superseded.future','tmpfuture.tmp',
             'lessons.jsonl','workspace/memory/preferences.md','backups/memory.old.db',
             'home-alias/memory.db','temporary-alias','logs-alias/member-other/other.log',
             'agent-logs/member-other/other.log','mcp-gateway/late.txt','broker-alias/late.txt',
             'kirocrew-mcp-gateway.sock','mc-mcp-gateway.sock',
             'snapshots/global.tar.gz','sessions/global/messages.jsonl'):
 try: (h/name).read_bytes();out[name]=True
 except OSError:out[name]=False
try: (h/'memory.db').write_text('member wrote global');out['global_write']=True
except OSError:out['global_write']=False
out['late_binding']=(h/'member-memory-bindings'/'late.txt').read_text()=='binding'
out['late_listener']=(h/'run'/'gateway.secret').read_text()=='secret'
try: (h/'memory_stores'/'.execution-logs'/'member-other'/'other.log').read_bytes();out['hidden_logs_denied']=False
except OSError:out['hidden_logs_denied']=True
out['code_read']=(h/'workspace'/'project'/'code.py').read_text()=='original'
out['broker_named_project']=(h/'workspace'/'mcp-gateway'/'code.py').read_text()=='project code'
(h/'workspace'/'mcp-gateway'/'new.py').write_text('member code')
(h/'workspace'/'project'/'new.py').write_text('member code')
try: Path('../memory.db').read_bytes();out['relative_global_read']=True
except OSError:out['relative_global_read']=False
for name in json.loads(sys.argv[4]):
 for operation in ('read','write'):
  try:
   if operation=='read': Path(name).read_bytes()
   else:
    with Path(name).open('ab') as stream: stream.write(b'child write')
   out['configured:'+name+':'+operation]=True
  except OSError:out['configured:'+name+':'+operation]=False
for root in json.loads(sys.argv[5]):
 project=Path(root)/'project'
 assert (project/'code.py').read_text()=='configured project code'
 (project/'new.py').write_text('member code')
out['configured_project_write']=True
print(json.dumps(out),flush=True)
"""


@pytest.mark.skipif(sys.platform != "linux", reason="real Linux user/mount namespaces")
@pytest.mark.parametrize("private", [False, True])
def test_kernel_hides_replaced_and_future_global_memory_preserving_live_runtime_and_code(
    home, private
):
    if not sandbox.userns_available():
        pytest.skip("Unprivileged user/mount namespaces unavailable")
    workspace = home / "workspace"
    project = workspace / "project"
    project.mkdir(parents=True)
    (project / "code.py").write_text("original")
    broker_named_project = workspace / "mcp-gateway"
    broker_named_project.mkdir()
    (broker_named_project / "code.py").write_text("project code")
    memory = workspace / "memory"
    memory.mkdir()
    (memory / "preferences.md").write_text("GLOBAL PREFERENCES")
    (home / "memory.db").write_text("GLOBAL DATABASE")
    (home / "lessons.jsonl").write_text("GLOBAL LESSONS")
    (home / "config.json").write_text("{}")
    marker = home / ".private-member-runtime"
    marker.write_text("1")
    marker.chmod(0o400)
    (home / "tmpexisting.tmp").write_text("PRIVATE STAGING")
    (home / "home-alias").symlink_to(home, target_is_directory=True)
    (home / "temporary-alias").symlink_to(home / "tmpexisting.tmp")
    logs = home / "agent-logs"
    (logs / "member-other").mkdir(parents=True)
    (logs / "member-other" / "other.log").write_text("OTHER MEMBER LOG")
    hidden_logs = home / "memory_stores" / ".execution-logs" / "member-other"
    hidden_logs.mkdir(parents=True)
    (hidden_logs / "other.log").write_text("PRIVATE OTHER EXECUTION LOG")
    (home / "logs-alias").symlink_to(logs, target_is_directory=True)
    for name in ("backups", "run", "member-memory-bindings", "mcp-gateway"):
        (home / name).mkdir()
    (home / "broker-alias").symlink_to(home / "mcp-gateway", target_is_directory=True)
    (home / "backups" / "memory.old.db").write_text("GLOBAL BACKUP")
    (home / "snapshots").mkdir()
    (home / "snapshots" / "global.tar.gz").write_text("GLOBAL SNAPSHOT")
    (home / "sessions" / "global").mkdir(parents=True)
    (home / "sessions" / "global" / "messages.jsonl").write_text("PEER TRANSCRIPT")
    configured_roots, configured_paths = _configured_workspace_memory(home)
    child = home.parent / "probe.py"
    child.write_text(_CHILD)
    host_handle = (home / "memory.db").open("rb")
    args = sandbox.namespace_argv(
        [
            sys.executable,
            str(child),
            "private" if private else "v1",
            str(os.getpid()),
            str(host_handle.fileno()),
            json.dumps(configured_paths),
            json.dumps([str(root) for root in configured_roots]),
        ],
        "standard",
        private_memory=private,
    )
    process = subprocess.Popen(
        args,
        cwd=workspace,
        text=True,
        encoding="utf-8",
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=20), "Namespace setup did not report readiness"
        ready = process.stdout.readline().strip()
        if ready != "READY":
            _, error = process.communicate(timeout=5)
            pytest.fail(f"Namespace setup failed: {ready} {error}")
        from kiro_crew.member_memory_auth import publish_member_session_pid

        publish_member_session_pid(
            process.pid,
            "dashboard:filesystem-probe",
            home=home,
            memory_store="member-probe" if private else "",
        )
        for name in ("memory.db", "memory.db-wal", "memory.db.superseded.future", "tmpfuture.tmp"):
            incoming = home / "incoming"
            incoming.write_text("LATE GLOBAL")
            incoming.replace(home / name)
        (home / "member-memory-bindings" / "late.txt").write_text("binding")
        (home / "run" / "gateway.secret").write_text("secret")
        (home / "mcp-gateway" / "late.txt").write_text("broker")
        (home / "kirocrew-mcp-gateway.sock").write_text("legacy broker canary")
        (home / "mc-mcp-gateway.sock").write_text("legacy broker canary")
        for root in configured_roots:
            for relative in ("memory/preferences.md", "memory_index.db-wal"):
                incoming = root / "incoming"
                incoming.write_text("LATE WORKSPACE")
                incoming.replace(root / relative)
        output, error = process.communicate("continue\n", timeout=20)
        assert process.returncode == 0, error
        result = json.loads(output)
        for name, value in result.items():
            assert value is (
                True
                if name
                in {
                    "late_binding",
                    "runtime_identity",
                    "proc_launcher_root",
                    "proc_launcher_cwd",
                    "proc_host_fd",
                    "late_listener",
                    "code_read",
                    "broker_named_project",
                    "private_log",
                    "private_audit",
                    "hidden_logs_denied",
                    "diagnostic_route_valid",
                    "configured_project_write",
                }
                else not private
            ), (name, result)
        assert (project / "new.py").read_text() == "member code"
        assert (broker_named_project / "new.py").read_text() == "member code"
        for root in configured_roots:
            assert (root / "project" / "new.py").read_text() == "member code"
            if private:
                assert (root / "memory" / "preferences.md").read_text() == "LATE WORKSPACE"
                assert (root / "memory_index.db-wal").read_text() == "LATE WORKSPACE"
        if private:
            assert (home / "memory.db").read_text() == "LATE GLOBAL"
            assert not (home / "security_events.jsonl").exists()
            assert not (home / "gateway.log").exists()
            execution_logs = [
                path
                for path in (home / "memory_stores" / ".execution-logs").iterdir()
                if path.name != "member-other"
            ]
            assert len(execution_logs) == 1
            assert list(execution_logs[0].glob("member-*.log"))
            assert list(execution_logs[0].glob("audit-*/security_events.jsonl"))
    finally:
        host_handle.close()
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


_DARWIN_CHILD = """import errno,json,os,sys
from pathlib import Path
home=Path(os.environ['KIROCREW_HOME'])
private=sys.argv[1]=='private'
result={}
if private:
 own=Path(os.environ['_KIROCREW_PRIVATE_LOG_DIRECTORY'])
 assert own.parent==home/'memory_stores'/'.execution-logs'
 assert own.name.startswith('member-')
 (own/'canary.log').write_text('own private diagnostic')
 result['own_log']=(own/'canary.log').read_text()=='own private diagnostic'
print('READY',flush=True)
assert sys.stdin.readline()=='continue\\n'
paths=json.loads(sys.argv[2])
for relative in paths:
 path=home/relative
 for operation in ('read','write'):
  try:
   if operation=='read': path.read_bytes()
   else:
    with path.open('ab') as stream: stream.write(b'child write')
   result[relative+':'+operation]=True
  except OSError as exc:
   assert exc.errno in (errno.EACCES,errno.EPERM), (relative,operation,exc)
   result[relative+':'+operation]=False
project=home/'workspace'/'project'
assert (project/'code.py').read_text()=='project code'
(project/'new.py').write_text('member code')
result['project_write']=(project/'new.py').read_text()=='member code'
for root in json.loads(sys.argv[3]):
 project=Path(root)/'project'
 assert (project/'code.py').read_text()=='configured project code'
 (project/'new.py').write_text('member code')
result['configured_project_write']=True
result['binding_read']=(home/'member-memory-bindings'/'late.txt').read_text()=='binding'
print(json.dumps(result),flush=True)
"""


@pytest.mark.skipif(sys.platform != "darwin", reason="real Darwin Seatbelt enforcement")
@pytest.mark.parametrize("private", [False, True], ids=["v1", "private"])
def test_darwin_kernel_private_memory_boundary(home, private):
    """Compile the shipped profile and prove positive access alongside denies.

    Private databases, including the caller's own, are gateway-only. The
    execution's diagnostic directory and ordinary project remain writable.
    All child inputs, outputs, aliases and cwd belong to this fixture.
    """
    project = home / "workspace" / "project"
    project.mkdir(parents=True)
    (project / "code.py").write_text("project code", encoding="utf-8")
    paths = [
        "memory.db",
        "memory.db-wal",
        "memory.db.superseded.future",
        "future.tmp",
        "lessons.jsonl",
        "workspace/memory/preferences.md",
        "backups/memory.old.db",
        "snapshots/global.tar.gz",
        "sessions/global/messages.jsonl",
        "memory_stores/member-own/memory.db",
        "memory_stores/member-other/memory.db",
        "memory_stores/.execution-logs/member-other/other.log",
    ]
    late_paths = {"memory.db-wal", "memory.db.superseded.future", "future.tmp"}
    for relative in paths:
        path = home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative not in late_paths:
            path.write_text("fixture private data", encoding="utf-8")
    (home / "member-memory-bindings").mkdir()
    for alias, target in (
        ("global-alias", home),
        ("member-alias", home / "memory_stores" / "member-other"),
        ("log-alias", home / "memory_stores" / ".execution-logs" / "member-other"),
    ):
        (project / alias).symlink_to(target, target_is_directory=True)
    paths.extend(
        [
            "workspace/project/global-alias/memory.db",
            "workspace/project/member-alias/memory.db",
            "workspace/project/log-alias/other.log",
        ]
    )
    configured_roots, configured_paths = _configured_workspace_memory(home)
    paths.extend(configured_paths)
    child = home.parent / "seatbelt-canary.py"
    child.write_text(_DARWIN_CHILD, encoding="utf-8")
    argv, profile = sandbox.sandbox_exec_argv(
        [
            sys.executable,
            str(child),
            "private" if private else "v1",
            json.dumps(paths),
            json.dumps([str(root) for root in configured_roots]),
        ],
        "standard",
        private_memory=private,
        # Exercise the broad visible carve-out too: it must not reopen V2 data.
        extra_visible_dirs=(str(home),),
    )
    assert profile is not None
    process = None
    parent_record = None
    try:
        process = subprocess.Popen(
            argv,
            cwd=project,
            text=True,
            encoding="utf-8",
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=20), "Seatbelt child did not report readiness"
        ready = process.stdout.readline().strip()
        if ready != "READY":
            _, error = process.communicate(timeout=5)
            pytest.fail(f"Seatbelt startup failed: {ready} {error}")
        from kiro_crew import member_memory_auth, platform_compat

        assert platform_compat.process_can_read_under_sandbox(process.pid, home / "memory.db") is (
            not private
        )
        candidate_record = member_memory_auth._binding_path(os.getpid(), home)
        assert not candidate_record.exists()
        parent_record = candidate_record
        member_memory_auth.publish_member_session_pid(
            os.getpid(), "dashboard:global-canary", home=home, memory_store=""
        )
        expected_binding = ("", "") if private else ("dashboard:global-canary", "")
        assert (
            member_memory_auth._protected_member_binding_for_pid(process.pid, home=home)
            == expected_binding
        )
        # Path predicates must still hold when a trusted host replaces memory
        # or publishes a sidecar after the child installed its sandbox.
        for relative in ("memory.db", "memory.db-wal", "memory.db.superseded.future", "future.tmp"):
            incoming = home / "incoming"
            incoming.write_text("late private data", encoding="utf-8")
            incoming.replace(home / relative)
        (home / "member-memory-bindings" / "late.txt").write_text("binding", encoding="utf-8")
        for root in configured_roots:
            for relative in ("memory/preferences.md", "memory_index.db-wal"):
                incoming = root / "incoming"
                incoming.write_text("LATE WORKSPACE", encoding="utf-8")
                incoming.replace(root / relative)
        output, error = process.communicate("continue\n", timeout=20)
        assert process.returncode == 0, error
        result = json.loads(output)
        for relative in paths:
            # Named stores are gateway-only in both modes; V1 keeps global
            # memory access, including through an alias into the project.
            named_store = relative.startswith("memory_stores/") or relative.startswith(
                ("workspace/project/member-alias/", "workspace/project/log-alias/")
            )
            for operation in ("read", "write"):
                expected = not private and not named_store
                assert result[f"{relative}:{operation}"] is expected, (relative, result)
        assert (
            result["project_write"]
            and result["binding_read"]
            and result["configured_project_write"]
        )
        for root in configured_roots:
            assert (root / "project" / "new.py").read_text() == "member code"
            if private:
                assert (root / "memory" / "preferences.md").read_text() == "LATE WORKSPACE"
        if private:
            assert result["own_log"]
            assert (home / "memory.db").read_text(encoding="utf-8") == "late private data"
    finally:
        if parent_record is not None:
            parent_record.unlink(missing_ok=True)
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
        Path(profile).unlink(missing_ok=True)
