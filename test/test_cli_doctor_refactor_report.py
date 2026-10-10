"""The whole ``kirocrew doctor`` report, frozen as text.

The section tests beside this file each pin one section. What none of them pins is
the ORCHESTRATION: which sections run, in what order, which headers and rows the
orchestrator prints itself, which values it threads from one section into the next
(the resolved kiro-cli binary, the project directory, the dashboard host and port,
the loaded credentials), how every section's issues aggregate into the closing
verdict, and the exit status. This file pins those, byte for byte, on four hosts.

Every section the orchestrator calls BY NAME is replaced with a marker through
``kiro_crew.cli_doctor`` -- the patch target every other doctor test uses -- so a
marker that stops printing is also a patch seam that stopped reaching its call site.
The rows the orchestrator composes from its own probes run for real, against probes
pinned here. Linux only: the rows describe a Linux host, and the platform branches
each section takes are the section tests' subject.
"""

from __future__ import annotations

import subprocess
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import cli_doctor
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.doctor_checks import access
from kiro_crew.platform import PlatformCompositionError

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the frozen report describes a Linux host"
)

#: Every section the orchestrator calls by name, in call order on a host where all
#: of them run. A marker prints ``<<name>>`` in the report where the section's own
#: output would be.
_SECTIONS = (
    "_doctor_headless_auth",
    "_doctor_claude_backend",
    "_doctor_agent_auth",
    "_doctor_effective_model",
    "_doctor_member_dispatchability",
    "_doctor_member_memory_bindings",
    "render_doctor_section",
    "_doctor_managed_service_policy",
    "_doctor_data_home",
    "_doctor_cron_script_sources",
    "_doctor_skill_currency",
    "_doctor_deprecated_agent_specs",
    "_doctor_path_launcher",
    "_doctor_trust_root",
    "_doctor_signing_key",
    "_doctor_name_grant_platform_scope",
    "_doctor_strict_identity",
    "_doctor_mcp_gateway_daemon",
    "_doctor_unresolved_mcp_refs",
    "_doctor_backend_ability_cards",
    "_doctor_selected_backend_projection",
    "_doctor_credentials",
    "_doctor_agents_janitor",
    "_doctor_kas",
    "_doctor_pod_session_bus",
    "_doctor_sandbox",
    "_doctor_live_target_pointer",
    "_doctor_masked_credential_aliases",
    "_doctor_memory_pressure",
    "_doctor_runtime_tmpfs",
    "_doctor_cli_installer_residue",
    "_doctor_cron_health",
    "_doctor_task_store",
    "_doctor_overload_resilience",
    "doctor_dead_paths",
    "_doctor_mcp_tools",
    "_doctor_mcp_governance",
    "_doctor_import_path",
    "_doctor_source_checkout",
    "_doctor_model_url_reachable",
    "_doctor_discord",
    "_doctor_whatsapp",
)

#: The sections that take the shared ``issues`` list, so a marker can add to it.
_TAKES_ISSUES = frozenset(_SECTIONS) - {
    "_doctor_claude_backend",
    "_doctor_agent_auth",
    "_doctor_data_home",
    "_doctor_path_launcher",
    "_doctor_trust_root",
    "_doctor_name_grant_platform_scope",
    "_doctor_strict_identity",
    "_doctor_unresolved_mcp_refs",
    "_doctor_backend_ability_cards",
    "_doctor_selected_backend_projection",
    "_doctor_overload_resilience",
    "_doctor_source_checkout",
}

#: Sections defined in a family AFTER the move. The facade forwards only the names
#: it held before, so these are stubbed on their family module.
_FAMILY_ONLY = {
    "_doctor_signing_key": access,
}

_GATED_OFF = frozenset({"kirocrew-computer"})
_KIRO = "/opt/kiro/bin/kiro-cli"


#: Where a section's signature puts the shared ``issues`` list, for the ones that do
#: not take it first.
_ISSUES_POSITION = {
    "_doctor_effective_model": 2,
    "_doctor_member_dispatchability": 1,
    "_doctor_member_memory_bindings": 1,
    "_doctor_deprecated_agent_specs": 1,
    "_doctor_mcp_tools": 1,
    "_doctor_mcp_governance": 1,
    "_doctor_discord": 3,
    "_doctor_whatsapp": 1,
}


def _issues_arg(name: str, args: tuple) -> list[str]:
    """The ``issues`` list a section was handed."""
    return args[_ISSUES_POSITION.get(name, 0)]


class _Host:
    """One pinned host: the probes the orchestrator reads, and a call log."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp_path
        self.mp = monkeypatch
        self.calls: list[tuple[str, tuple, dict]] = []
        self.issue_from: set[str] = set()
        self.which: dict[str, str] = {}
        self.runs: dict[tuple[str, ...], subprocess.CompletedProcess] = {}
        self.urls: dict[str, object] = {}
        self.home = tmp_path / "datahome"
        self.home.mkdir()
        self.agents = tmp_path / "agents"
        self.agents.mkdir()
        self.cfg = KiroCrewConfig()
        self.cfg.stt.enabled = False
        self.cfg.agent.model = "auto"
        self.cfg.agent.approval_mode = "reads"
        self.cfg.dashboard.url = "http://localhost:8765"
        self.creds: dict[str, str] = {}
        self.readiness: list[SimpleNamespace] = []
        self.install()

    def install(self) -> None:
        mp = self.mp
        for var in (
            "KIROCREW_PROJECT_DIR",
            "SSH_CONNECTION",
            "SSH_CLIENT",
            "KIROCREW_PORT",
            "LLAMA_CPP_LIB_PATH",
        ):
            mp.delenv(var, raising=False)
        mp.setattr(cli_doctor, "__file__", str(self.tmp / "pkg" / "src" / "kiro_crew" / "x.py"))
        mp.setattr(cli_doctor, "KIRO_AGENTS_DIR", self.agents)
        mp.setattr(cli_doctor, "config_dir", lambda: self.home)
        mp.setattr(cli_doctor, "data_home", lambda: self.home)
        mp.setattr(cli_doctor, "warm_backend", lambda timeout=None: None)
        mp.setattr(cli_doctor, "_mc_version", "9.9.9")
        mp.setattr(cli_doctor, "MIN_NODE_VERSION", (20, 0, 0))
        mp.setattr(KiroCrewConfig, "load", classmethod(lambda cls: self.cfg))
        mp.setattr(KiroCrewConfig, "load_credentials", lambda cfg: dict(self.creds))
        mp.setattr(cli_doctor, "resolve_kiro_cli", lambda: self.kiro)
        mp.setattr(cli_doctor.shutil, "which", lambda name, **_kw: self.which.get(name))
        mp.setattr(cli_doctor.subprocess, "run", self._run)
        mp.setattr(cli_doctor.urllib.request, "urlopen", self._urlopen)
        mp.setattr(cli_doctor, "current_context", lambda: self.ctx)
        mp.setattr(cli_doctor, "_agent_spec_model_problems", self._model_pins)
        mp.setattr(cli_doctor, "_doctor_gated_off_mcps", lambda: _GATED_OFF)
        mp.setattr(cli_doctor, "_source_checkout_root", lambda: self.source_root)
        mp.setattr(cli_doctor, "_venv_deps_ok", lambda venv_py: self.venv_deps_ok)
        mp.setattr(cli_doctor, "pip_install_channel_available", lambda: self.pip_ok)
        mp.setattr(cli_doctor, "pip_install_command_for", lambda *s: "PIP " + " ".join(s))
        mp.setattr(cli_doctor, "pip_install_command", lambda extra: f"PIP-EXTRA {extra}")
        mp.setattr(cli_doctor, "_load_llama_class", lambda: self.llama)
        mp.setattr(cli_doctor, "_platform_libs_dirname", lambda: self.libs_dir)
        mp.setattr(cli_doctor, "verify_vendored_libs", lambda: self.vendored)
        mp.setattr(cli_doctor, "resolve_custom_model", lambda: self.custom_model)
        mp.setattr(cli_doctor, "model_file_present", lambda path=None: self.model_present)
        mp.setattr(cli_doctor, "default_model_path", lambda: self.tmp / "model.gguf")
        mp.setattr(cli_doctor, "availability_detail", lambda stt_config=None: self.stt_engine)
        mp.setattr(cli_doctor, "ensure_ffmpeg_in_path", lambda: None)
        mp.setattr(cli_doctor, "_find_ffmpeg", lambda: self.ffmpeg)
        mp.setattr(cli_doctor.stt, "resolve_model", lambda name: self.stt_model)
        mp.setattr(cli_doctor.stt, "is_present", lambda model: False)
        mp.setattr(cli_doctor.stt, "models_dir", lambda: self.tmp / "stt")
        mp.setattr(cli_doctor.platform_compat, "is_bundled_interpreter", lambda: False)
        mp.setattr(cli_doctor._plat, "system", lambda: "Linux")
        mp.setattr(cli_doctor, "get_dumps_dir", lambda: self.tmp / "dumps")
        mp.setattr(cli_doctor, "newest_dump_with_stacks", lambda d=None: self.dump)
        mp.setattr(cli_doctor, "dump_age_seconds", lambda p: self.dump_age)
        mp.setattr(cli_doctor, "dumps_with_stacks", lambda d=None: self.stalls)
        mp.setattr(cli_doctor, "dump_superseded", lambda p, d=None: self.superseded)
        mp.setattr(
            cli_doctor, "dump_first_stack_lines", lambda p, max_lines=5: ['File "loop.py", 1']
        )
        mp.setattr(cli_doctor, "attribute_dump", lambda p, base: self.attribution)
        mp.setattr(cli_doctor, "describe", lambda a: ["surface: cron", "job: nightly"])
        mp.setattr(cli_doctor, "job_pause_state_from_disk", lambda job_id: self.paused)
        mp.setattr(cli_doctor, "machine_hostname", lambda: "devbox")
        mp.setattr(cli_doctor, "is_local_only", lambda host, has_slack: self.local)
        import kiro_crew._sqlite_compat as sqlite_compat
        import kiro_crew.browser_cli.install as browser_install
        import kiro_crew.channels as channels

        mp.setattr(sqlite_compat, "fts5_available", lambda: self.fts5)
        # The browser-bootstrap rows resolve through the patched ``shutil.which``;
        # pin the writability probe too, so the host's real files never decide.
        mp.setattr(
            browser_install,
            "bootstrap_tool_provenance",
            lambda path: {
                "path": path,
                "real_path": path,
                "writable_at": "",
                "own_toolchain": False,
                "shared_write": False,
            },
        )
        mp.setattr(channels, "channel_readiness", lambda cfg, creds: tuple(self.readiness))
        mp.setitem(sys.modules, "faiss", None)
        mp.setitem(sys.modules, "amazon_transcribe", None)
        mp.setitem(sys.modules, "amazon_transcribe.client", None)
        mp.setitem(sys.modules, "boto3", None)
        for name in _SECTIONS:
            if name in _FAMILY_ONLY:
                # A section added after the move is not re-exported through the
                # facade (see test_cli_doctor_refactor_facade), so it is stubbed
                # on the family that defines it.
                mp.setattr(_FAMILY_ONLY[name], name, self._marker(name))
                continue
            mp.setattr(cli_doctor, name, self._marker(name))

        self.kiro: str | None = _KIRO
        self.ctx = SimpleNamespace(
            profile="standalone",
            jail=SimpleNamespace(
                status_detail=lambda: "no jail backend (public build)", available=lambda: False
            ),
            slack_gate=SimpleNamespace(validate_enterprise=lambda token, extra_ids: True),
        )
        self.pins: list[tuple[str, str, str]] | None = []
        self.source_root: Path | None = None
        self.venv_deps_ok = True
        self.pip_ok = True
        self.llama: object = object
        self.libs_dir: str | None = "linux-x86_64"
        self.vendored: dict[str, list[str]] = {}
        self.custom_model: object = None
        self.model_present = True
        self.stt_engine = SimpleNamespace(ok=True, detail="", code="")
        self.ffmpeg: str | None = "/usr/bin/ffmpeg"
        self.stt_model = SimpleNamespace(
            name="base.en", filename="base.en.bin", size_bytes=148_000_000
        )
        self.dump: Path | None = None
        self.dump_age = 0.0
        self.stalls = 0
        self.superseded = False
        self.attribution = SimpleNamespace(is_cron=False, job=None)
        self.paused: str | None = None
        self.local = True
        self.fts5 = True

    def _marker(self, name: str):
        def marker(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            print(f"  <<{name}>>")
            if name in self.issue_from and name in _TAKES_ISSUES:
                _issues_arg(name, args).append(f"{name} issue")

        return marker

    def _model_pins(self, project_dir=None, provider="acp"):
        self.calls.append(("_agent_spec_model_problems", (), {"project_dir": project_dir}))
        return self.pins

    def _run(self, argv, *args, **kwargs):
        key = tuple(str(part) for part in argv)
        if key not in self.runs:
            raise AssertionError(f"doctor spawned an unpinned command: {key}")
        return self.runs[key]

    def _urlopen(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        answer = self.urls.get(url, urllib.error.URLError("nothing listens here"))
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def called(self, name: str) -> tuple[tuple, dict]:
        matches = [(a, k) for n, a, k in self.calls if n == name]
        assert len(matches) == 1, (name, len(matches))
        return matches[0]

    def run(self, capsys, **kwargs) -> tuple[str, int | None]:
        code: int | None = None
        try:
            cli_doctor._doctor(**kwargs)
        except SystemExit as exc:
            code = exc.code
        out = capsys.readouterr().out
        out = out.replace(str(self.tmp), "<TMP>")
        out = out.replace(sys.executable, "<PYTHON>").replace(sys.version.split()[0], "<PYVER>")
        return out, code


def _done(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


@pytest.fixture
def host(tmp_path, monkeypatch) -> _Host:
    return _Host(tmp_path, monkeypatch)


_READY = """\
Kiro Crew Doctor 👻

Platform
  edition:     ✅ standalone
  jail:        ⏭  no jail backend (public build)
Dependencies
  kiro-cli:    ✅ /opt/kiro/bin/kiro-cli
  <<_doctor_headless_auth>>
  <<_doctor_claude_backend>>
  <<_doctor_agent_auth>>
  git:         ✅ /usr/bin/git
  node:        ✅ /usr/bin/node (v22.3.0)
  browser npm: ⏹ not found (the browser install needs Node.js with npm)
  browser node: ✅ /usr/bin/node
               These rows resolve from this shell's PATH; the gateway logs the npm and Node it
               actually ran at install time.

Project
  source dir:  ✅ <TMP>/checkout (Kiro Crew source checkout)
  git repo:    ✅

Agent
  config:      ✅ <TMP>/agents/kirocrew.json
  model pins:  ✅ no unusable spellings in agent specs

Configuration
  config dir:  ✅ <TMP>/datahome
  provider:    acp
  model:       auto
  approval:    reads
  dashboard:   http://localhost:8765
  bind:        127.0.0.1 (local-only, SSH tunnel for remote)
  auth:        token required — loopback is not exempt (CLI/MCP use the local secret)
  <<_doctor_effective_model>>
  <<_doctor_member_dispatchability>>
  <<_doctor_member_memory_bindings>>
  <<render_doctor_section>>
  <<_doctor_managed_service_policy>>
  <<_doctor_data_home>>
  <<_doctor_cron_script_sources>>
  <<_doctor_skill_currency>>
  <<_doctor_deprecated_agent_specs>>
  <<_doctor_path_launcher>>
  <<_doctor_trust_root>>
  <<_doctor_signing_key>>
  <<_doctor_name_grant_platform_scope>>
  <<_doctor_strict_identity>>
  <<_doctor_mcp_gateway_daemon>>
  <<_doctor_unresolved_mcp_refs>>
  <<_doctor_backend_ability_cards>>
  <<_doctor_selected_backend_projection>>
  <<_doctor_credentials>>
  <<_doctor_agents_janitor>>
  <<_doctor_kas>>
  <<_doctor_pod_session_bus>>
  <<_doctor_sandbox>>
  <<_doctor_live_target_pointer>>
  <<_doctor_masked_credential_aliases>>
  <<_doctor_memory_pressure>>
  <<_doctor_runtime_tmpfs>>
  <<_doctor_cli_installer_residue>>
  <<_doctor_cron_health>>
  <<_doctor_task_store>>
  <<_doctor_overload_resilience>>
  <<doctor_dead_paths>>

MCP Tools
  <<_doctor_mcp_tools>>
  <<_doctor_mcp_governance>>

Runtime
  python:      ✅ <PYTHON> (<PYVER>)
  kiro_crew:   ✅ 9.9.9
  deps:        ✅ websockets, slack_sdk, aiohttp available
  <<_doctor_import_path>>
  sqlite fts5: ✅ available

Vector Memory (in-process embeddings)
  runtime:     ✅ vendored llama-cpp-python importable
  faiss:       ⏹ not installed (optional) — episodic recall uses the stdlib fallback; installing faiss-cpu accelerates it
               Install: PIP faiss-cpu
  model:       ✅ <TMP>/model.gguf
  embeddings:  ✅ always-on

Speech-to-Text
  status:      ⏹ disabled (enable from dashboard → Settings → Speech-to-Text)
  ffmpeg:      ✅ available

Slack Integration
  status:      ⏭  not configured (optional)
  setup:       run 'kirocrew setup --slack', or connect any channel
               (Slack, Discord, Telegram, …) from the dashboard
  <<_doctor_discord>>
  <<_doctor_whatsapp>>

Other Channels
  status:      ⏭  none enabled (optional)
  setup:       connect one from the dashboard's Settings > Messaging Channels

Loop-stall Crash Dumps
  dumps:       ✅ no crash dumps found (healthy)
  dump dir:    <TMP>/dumps

Connectivity
  kiro-cli:    ✅ kiro-cli 1.2.3
  gateway:     ⏹  not running

✅ Kiro Crew is ready!
"""


def _ready(host: _Host) -> None:
    checkout = host.tmp / "checkout"
    for marker in ("skills", "src/kiro_crew", ".git"):
        (checkout / marker).mkdir(parents=True)
    host.mp.setenv("KIROCREW_PROJECT_DIR", str(checkout))
    (host.agents / "kirocrew.json").write_text("{}", encoding="utf-8")
    host.which.update(git="/usr/bin/git", node="/usr/bin/node")
    host.runs[("/usr/bin/node", "-v")] = _done("v22.3.0\n")
    host.runs[(_KIRO, "--version")] = _done("kiro-cli 1.2.3\n")
    (host.tmp / "dumps").mkdir()
    host.readiness = [
        SimpleNamespace(
            channel_type="telegram",
            enabled=False,
            ready=False,
            missing_credentials=(),
            missing_config=(),
        )
    ]


def test_a_ready_host_prints_every_section_in_order_and_passes(host, capsys) -> None:
    _ready(host)

    out, code = host.run(capsys)

    assert out == _READY
    assert code is None
    # The sections that take a positional input receive the orchestrator's values.
    args, _ = host.called("_doctor_effective_model")
    assert args[0] is host.cfg and args[1] == str(host.tmp / "checkout")
    args, kwargs = host.called("_doctor_mcp_tools")
    assert args[0] == host.agents / "kirocrew.json" and kwargs == {"gated_off": _GATED_OFF}
    args, kwargs = host.called("_doctor_mcp_governance")
    assert args[0] == host.agents / "kirocrew.json" and kwargs["gated_off"] is _GATED_OFF
    args, kwargs = host.called("doctor_dead_paths")
    assert kwargs == {"agents_dir": host.agents}
    args, _ = host.called("_doctor_agents_janitor")
    assert args[1] is host.cfg.agent.sweep_agents_backups
    args, _ = host.called("_doctor_discord")
    assert args[0] is host.cfg and args[1] == {} and args[2] == 8765
    args, _ = host.called("_agent_spec_model_problems")
    assert host.called("_agent_spec_model_problems")[1] == {
        "project_dir": str(host.tmp / "checkout")
    }
    # One issues list threads through every section that reports into it.
    lists = {id(_issues_arg(n, a)) for n, a, _k in host.calls if n in _TAKES_ISSUES}
    assert len(lists) == 1


_BROKEN = """\
Kiro Crew Doctor 👻

Platform
  edition:     ❌ composition failed: companion missing
Dependencies
  kiro-cli:    ⏭  not found (the default agent backend)
               Install kiro-cli per its docs, then: kiro-cli login
  <<_doctor_claude_backend>>
  <<_doctor_agent_auth>>
  git:         ❌ not found (needed for kirocrew update)
  node:        ❌ v18.19.0 < v20.0.0
               Fix: Node.js v18.19.0 is too old: Kiro Crew needs v20.0.0 or newer. Update Node.js: install 24 LTS from https://nodejs.org, or run `nvm install 24` / `mise use -g node@24`.
  browser npm: ⏹ not found (the browser install needs Node.js with npm)
  browser node: ✅ /usr/bin/node
               These rows resolve from this shell's PATH; the gateway logs the npm and Node it
               actually ran at install time.

Project
  source dir:  ❌ stale — points to deleted <TMP>/gone
               Fix: rm <TMP>/datahome/project_dir

Agent
  config:      ❌ not found (run kirocrew setup)
  model pin:   ❌ 'kirocrew': 'claude-4' is not a model kiro-cli serves
                  the registry maps that spelling to 'claude-sonnet-4'

Configuration
  config dir:  ✅ <TMP>/datahome
  provider:    acp
  model:       auto
  approval:    reads
  dashboard:   http://0.0.0.0:9999
  bind:        0.0.0.0 (all interfaces)
  auth:        ✅ token auth required (via !dashboard)
  auth:        ⚠️  Slack not configured — token generation unavailable
  <<_doctor_effective_model>>
  <<_doctor_member_dispatchability>>
  <<_doctor_member_memory_bindings>>
  <<render_doctor_section>>
  <<_doctor_managed_service_policy>>
  <<_doctor_data_home>>
  <<_doctor_cron_script_sources>>
  <<_doctor_skill_currency>>
  <<_doctor_deprecated_agent_specs>>
  <<_doctor_path_launcher>>
  <<_doctor_trust_root>>
  <<_doctor_signing_key>>
  <<_doctor_name_grant_platform_scope>>
  <<_doctor_strict_identity>>
  <<_doctor_mcp_gateway_daemon>>
  <<_doctor_unresolved_mcp_refs>>
  <<_doctor_backend_ability_cards>>
  <<_doctor_selected_backend_projection>>
  <<_doctor_credentials>>
  <<_doctor_agents_janitor>>
  <<_doctor_kas>>
  <<_doctor_pod_session_bus>>
  <<_doctor_sandbox>>
  <<_doctor_live_target_pointer>>
  <<_doctor_masked_credential_aliases>>
  <<_doctor_memory_pressure>>
  <<_doctor_runtime_tmpfs>>
  <<_doctor_cli_installer_residue>>
  <<_doctor_cron_health>>
  <<_doctor_task_store>>
  <<_doctor_overload_resilience>>
  <<doctor_dead_paths>>

MCP Tools

Runtime
  python:      ✅ <TMP>/pkg/.venv/bin/python3 (Python 3.12.1)
  deps:        ❌ missing modules (websockets/slack_sdk/aiohttp)
  <<_doctor_import_path>>
  sqlite fts5: ❌ missing (memory/knowledge search will fail)
               Or use a Python whose SQLite was built with FTS5.
  <<_doctor_source_checkout>>

Vector Memory (in-process embeddings)
  runtime:     ❌ vendored runtime failed to load
               Missing native libs for linux-x86_64: libllama.so, libggml.so
               This install's vendored llama.cpp is incomplete (packaging
               defect, not an unsupported platform) — reinstall Kiro Crew
               from a current release to restore vector memory.
  faiss:       ⏹ not installed (optional) — episodic recall uses the stdlib fallback; installing faiss-cpu accelerates it
  model:       ❌ custom model unusable — not a GGUF file
  embeddings:  ✅ always-on

Speech-to-Text
  provider:    ✅ local
  engine:      ❌ install the voice extra
  model:       ⏹ base.en not downloaded yet (148 MB, fetched on first use)
  ffmpeg:      ❌ not found
               Fix: download the audio decoder from the dashboard (Settings → Speech-to-Text), or install ffmpeg into /usr/local/bin

Slack Integration
  status:      ⏭  not configured (optional)
  setup:       run 'kirocrew setup --slack', or connect any channel
               (Slack, Discord, Telegram, …) from the dashboard
  <<_doctor_discord>>
  <<_doctor_whatsapp>>

Other Channels
  telegram:    ❌ enabled but missing TELEGRAM_BOT_TOKEN and telegram.allowed_chat_ids
               The channel will not start. Set it in Settings > Messaging Channels, or in ~/.kiro/crew/.env

Loop-stall Crash Dumps
  last dump:   ⚠️  crash-1.dump (2.0h ago)
  MainThread stuck at:
    File "loop.py", 1
  attribution:
    surface: cron
    job: nightly
    job is currently auto-paused
  dump dir:    <TMP>/dumps

Connectivity
  kiro-cli:    ⏭  skipped (not installed)
  gateway:     ✅ running (token auth enabled)

  💡 Remote access: Run on your LOCAL machine:
     ssh -NL 9999:localhost:9999 devbox
     Then run: kirocrew token
  auth check:  ❌ external access allowed without token!

❌ Fix these issues: platform composition failed: companion missing, git, node, stale project_dir, agent config, agent model pin, dashboard auth: remote bind without Slack, _doctor_effective_model issue, render_doctor_section issue, _doctor_credentials issue, _doctor_sandbox issue, _doctor_live_target_pointer issue, doctor_dead_paths issue, python deps, _doctor_import_path issue, sqlite fts5, embedding runtime, custom embedding model unusable, speech recogniser (extra_missing), ffmpeg, _doctor_discord issue, _doctor_whatsapp issue, telegram: missing TELEGRAM_BOT_TOKEN and telegram.allowed_chat_ids, loop-stall dump attributed to cron job 'nightly' ('job-7'), recent loop-stall crash dump (2h ago), dashboard auth: no token required on external interface
"""


def test_a_broken_host_reports_every_issue_in_order_and_exits_one(host, capsys) -> None:
    host.kiro = None
    host.which.update(node="/usr/bin/node")
    host.runs[("/usr/bin/node", "-v")] = _done("v18.19.0\n")
    # The frozen report asserts the DEFAULT (nodejs.org / nvm) node remedy. Pin a
    # modern glibc so node_too_old_message does not switch to the old-glibc branch
    # on an AL2 (glibc 2.26) developer host and redden this byte-for-byte report.
    from kiro_crew import constants as _constants

    host.mp.setattr(_constants, "_host_glibc_version", lambda: (2, 35))
    venv_py = host.tmp / "pkg" / ".venv" / "bin" / "python3"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("", encoding="utf-8")
    host.runs[(str(venv_py), "--version")] = _done("Python 3.12.1\n")
    host.venv_deps_ok = False
    (host.home / "project_dir").write_text(str(host.tmp / "gone") + "\n", encoding="utf-8")
    host.pins = [("kirocrew", "claude-4", "claude-sonnet-4")]
    host.cfg.dashboard.url = "http://0.0.0.0:9999"
    host.local = False
    host.cfg.stt.enabled = True
    host.cfg.stt.provider = "local"
    host.stt_engine = SimpleNamespace(
        ok=False, detail="install the voice extra", code="extra_missing"
    )
    host.ffmpeg = None
    host.pip_ok = False
    host.fts5 = False
    host.llama = None
    host.vendored = {"linux-x86_64": ["libllama.so", "libggml.so"]}
    host.custom_model = SimpleNamespace(
        error="not a GGUF file", path=host.tmp / "m.gguf", model_id="m", dim=384
    )
    host.source_root = host.tmp / "repo"
    host.readiness = [
        SimpleNamespace(
            channel_type="telegram",
            enabled=True,
            ready=False,
            missing_credentials=("TELEGRAM_BOT_TOKEN",),
            missing_config=("allowed_chat_ids",),
        )
    ]
    host.dump = host.tmp / "dumps" / "crash-1.dump"
    host.dump_age = 7200.0
    host.stalls = 1
    host.attribution = SimpleNamespace(
        is_cron=True, job=SimpleNamespace(job_id="job-7", name="nightly")
    )
    host.paused = "auto-paused"
    host.urls["http://127.0.0.1:9999/api/status"] = urllib.error.HTTPError(
        "http://127.0.0.1:9999/api/status", 401, "unauthorized", None, None
    )
    host.urls["http://0.0.0.0:9999/api/status"] = _Response(200, b"{}")
    host.mp.setenv("SSH_CONNECTION", "10.0.0.1 22 10.0.0.2 22")
    host.mp.setitem(sys.modules, "slack_sdk", None)
    host.issue_from = {
        "_doctor_headless_auth",
        "_doctor_effective_model",
        "render_doctor_section",
        "_doctor_credentials",
        "_doctor_sandbox",
        "_doctor_live_target_pointer",
        "doctor_dead_paths",
        "_doctor_import_path",
        "_doctor_discord",
        "_doctor_whatsapp",
    }

    out, code = host.run(capsys, platform_boot_error=PlatformCompositionError("companion missing"))

    assert out == _BROKEN
    assert code == 1
    # No kiro-cli means no service-visibility row, and no agent spec means no MCP probe.
    assert not [
        n for n, _a, _k in host.calls if n in ("_doctor_headless_auth", "_doctor_mcp_tools")
    ]
    args, _ = host.called("_doctor_source_checkout")
    assert args == (host.tmp / "repo",)
    args, _ = host.called("_doctor_discord")
    assert args[2] == 9999


_SLACK = """\
Slack Integration
  tokens:      ✅ configured
  owner:       ✅ U123
  workspace:   ❌ not in configured workspace allowlist
               The gateway will refuse to connect.
  <<_doctor_discord>>
"""


def test_slack_and_cloud_transcription_rows(host, capsys) -> None:
    _ready(host)
    host.creds = {
        "SLACK_APP_TOKEN": "xapp-1",
        "SLACK_BOT_TOKEN": "xoxb-1",
        "KIROCREW_OWNER_ID": "U123",
    }
    host.ctx.slack_gate = SimpleNamespace(validate_enterprise=lambda token, extra_ids: False)
    host.cfg.stt.enabled = True
    host.cfg.stt.provider = "transcribe"

    out, code = host.run(capsys)

    assert _SLACK in out
    assert (
        "Speech-to-Text\n"
        "  provider:    ✅ transcribe\n"
        "  ffmpeg:      ✅ available\n"
        "  transcribe:  ⏹ optional cloud STT not installed\n"
        "               Install: PIP-EXTRA voice-aws\n"
        "  boto3:       ⏹ optional AWS SDK not installed\n"
        "               Install: PIP-EXTRA voice-aws\n"
        "\n"
    ) in out
    assert out.endswith("❌ Fix these issues: slack workspace: not in allowlist\n")
    assert code == 1
    args, _ = host.called("_doctor_discord")
    assert args[1] == host.creds


_BUNDLE = """\
Kiro Crew Doctor 👻

Collecting diagnostics bundle (secrets are redacted)...

  ✅ bundle: <TMP>/diag.zip
     2 file(s) · 3 secret(s) redacted
     skipped (not found): gateway.log

  Open a GitHub issue (then drag the zip in):
  https://example.invalid/issues/new
"""


def test_bundle_mode_prints_only_the_bundle(host, capsys, monkeypatch) -> None:
    result = SimpleNamespace(
        zip_path=host.tmp / "diag.zip",
        included=["a", "b"],
        total_redactions=3,
        skipped=["gateway.log"],
    )
    monkeypatch.setattr(cli_doctor.diagnostics, "collect_bundle", lambda: result)
    monkeypatch.setattr(
        cli_doctor.diagnostics, "terminal_issue_url", lambda r: "https://example.invalid/issues/new"
    )

    out, code = host.run(capsys, bundle=True)

    assert out == _BUNDLE
    assert code is None
    assert host.calls == []


def test_bundle_mode_exits_one_when_the_bundle_cannot_be_written(host, capsys, monkeypatch) -> None:
    def refuse():
        raise OSError("disk full")

    monkeypatch.setattr(cli_doctor.diagnostics, "collect_bundle", refuse)

    out, code = host.run(capsys, bundle=True)

    assert out.endswith(
        "  ❌ could not write the diagnostics bundle: disk full\n"
        "     Check that ~/.kiro/crew is writable and has free space.\n"
    )
    assert code == 1
