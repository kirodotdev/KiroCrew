"""The branches of the sandbox, task-queue and liveness rows, pinned by their output.

Driven through ``kiro_crew.cli_doctor``, the name every caller uses, and through the
probes the rows read: module attributes shared with the owning subsystem
(``cli_doctor.sandbox.<probe>``, ``platform_compat.IS_LINUX``) or the seams the
facade forwards (``cli_doctor._read_linux_proc_self``). The expected text is the
report as it printed before the rows moved into ``kiro_crew.doctor_checks``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from kiro_crew import cli_doctor, platform_compat

EXPECTED: dict[str, str] = {
    "aliases:accounted-confined": (
        "\n"
        "Masked Credential Leaves\n"
        "  alias:       ⚠️  masked for each spawn — /data/op/.kiro/crew/.env (2 links)\n"
        "               find -samefile /data/op/.kiro/crew/.env\n"
        "               No spawn is refused for these. Where every other name was located, those names\n"
        "               are masked for each spawn too, so the bytes are unreachable from inside one.\n"
        "               Remove the extra link anyway: a name no mask covers leaves the bytes readable,\n"
        "               and this report is the only notice.\n"
    ),
    "aliases:in-live-unconfined": (
        "\n"
        "Masked Credential Leaves\n"
        "  alias:       ⚠️  will refuse spawns once confined — /data/op/.kiro/crew/.env (3 links)\n"
        "               cannot mask /data/op/.kiro/crew/.env: 3\n"
        "               This is not what stops a spawn on this host yet: the launcher reaches the mask\n"
        "               only when it WRAPS a child, and this host hands the command over unwrapped or\n"
        "               refuses it for a different reason. Remove the extra link before the host starts\n"
        "               confining spawns, or the first one that does fails closed.\n"
    ),
    "aliases:mixed-confined": (
        "\n"
        "Masked Credential Leaves\n"
        "  alias:       ⚠️  masked for each spawn — /data/op/.kiro/crew/.env (2 links)\n"
        "               find -samefile /data/op/.kiro/crew/.env\n"
        "  alias:       ⚠️  reported, spawns proceed — /srv/old/.env (2 links)\n"
        "               cannot mask /srv/old/.env: 2\n"
        "               No spawn is refused for these. Where every other name was located, those names\n"
        "               are masked for each spawn too, so the bytes are unreachable from inside one.\n"
        "               Where a name could not be located, the leaf is outside the live data home, which\n"
        "               the launcher reports rather than refusing on, so that a file inside a home this\n"
        "               install does not use cannot stop every launch. Remove the extra link anyway: a\n"
        "               name no mask covers leaves the bytes readable, and this report is the only\n"
        "               notice.\n"
    ),
    "aliases:mixed-unconfined": (
        "\n"
        "Masked Credential Leaves\n"
        "  alias:       ⚠️  masked for each spawn — /data/op/.kiro/crew/.env (2 links)\n"
        "               find -samefile /data/op/.kiro/crew/.env\n"
        "  alias:       ⚠️  reported, spawns proceed — /srv/old/.env (2 links)\n"
        "               cannot mask /srv/old/.env: 2\n"
        "               This is not what stops a spawn on this host yet: the launcher reaches the mask\n"
        "               only when it WRAPS a child, and this host hands the command over unwrapped or\n"
        "               refuses it for a different reason. Remove the extra link before the host starts\n"
        "               confining spawns, or the first one that does fails closed.\n"
    ),
    "aliases:outside-confined": (
        "\n"
        "Masked Credential Leaves\n"
        "  alias:       ⚠️  reported, spawns proceed — /srv/old/.env (2 links)\n"
        "               cannot mask /srv/old/.env: 2\n"
        "               No spawn is refused for these. Where a name could not be located, the leaf is\n"
        "               outside the live data home, which the launcher reports rather than refusing on,\n"
        "               so that a file inside a home this install does not use cannot stop every launch.\n"
        "               Remove the extra link anyway: a name no mask covers leaves the bytes readable,\n"
        "               and this report is the only notice.\n"
    ),
    "aliases:probe-raises": (
        "\n"
        "Masked Credential Leaves\n"
        "  aliases:     ⚠️  could not check (OSError('stat refused'))\n"
    ),
    "aliases:refusing": (
        "\n"
        "Masked Credential Leaves\n"
        "  alias:       ❌ agent spawns will be REFUSED — /data/op/.kiro/crew/.env (3 links)\n"
        "               cannot mask /data/op/.kiro/crew/.env: 3\n"
        "               Until this is fixed every agent spawn on this host fails closed, and the only\n"
        "               other notice is a warning in the gateway log.\n"
    ),
    "aliases:unreadable": (
        "\n"
        "Masked Credential Leaves\n"
        "  alias:       ⚠️  reported, spawns proceed — /data/op/.kiro/crew/.env (3 links)\n"
        "               cannot mask /data/op/.kiro/crew/.env: 3\n"
        "               No spawn is refused for these. Where a name could not be located, the leaf is\n"
        "               outside the live data home, which the launcher reports rather than refusing on,\n"
        "               so that a file inside a home this install does not use cannot stop every launch.\n"
        "               Remove the extra link anyway: a name no mask covers leaves the bytes readable,\n"
        "               and this report is the only notice.\n"
    ),
    "backend:foreign": (
        "  backend:     ⚠️  an outer sandbox already confines this process\n"
        "               Kiro Crew cannot nest its own sandbox inside it. Launch the gateway outside that\n"
        "               sandbox to hand isolation back to Kiro Crew's own profile.\n"
    ),
    "backend:linux-refusal:False": ("  backend:     ❌ none — denied by policy\n"),
    "backend:linux-refusal:True": (
        "  backend:     ❌ none — denied by policy\n"
        "               Set user.max_user_namespaces above zero.\n"
    ),
    "backend:probe-raises": ("  backend:     ⚠️  could not probe (probe exploded)\n"),
    "backend:transient": (
        "  backend:     ⚠️  probe failed transiently — not cached; the next spawn re-probes\n"
        "               (EAGAIN)\n"
    ),
    "backend:transient-no-reason": (
        "  backend:     ⚠️  probe failed transiently — not cached; the next spawn re-probes\n"
        "               (no probe detail recorded)\n"
    ),
    "backend:userns-vantage": (
        "  backend:     ⏭  cannot be verified from this shell\n"
        "               This shell is already confined inside a child user namespace with seccomp\n"
        "               filtering, so its nested CLONE_NEWUSER refusal cannot establish the host's\n"
        "               support; run `kirocrew doctor` from an unconfined shell instead.\n"
    ),
    "backend:working": ("  backend:     ✅ namespace\n"),
    "posture:operator": (
        "  backend:     ⏭  no OS-level sandbox backend on this platform\n"
        "  exec:        ⚠️  unconfined by declaration — sandbox_allow_unsandboxed_exec=true\n"
        "               The operator declared this opt-in, so agent subprocesses run without OS-level\n"
        "               isolation and every such spawn is audited. Remove the key to fall back to this\n"
        "               platform's default. A governance sandbox.min_level floor overrides the\n"
        "               declaration and makes such spawns fail closed.\n"
    ),
    "posture:platform": (
        "  backend:     ⏭  no OS-level sandbox backend on this platform\n"
        "  exec:        ⚠️  configured to run agent subprocesses UNCONFINED\n"
        "               This is the default for a platform with no backend to install: ~/.aws, ~/.ssh\n"
        "               and the rest of your home directory are readable by an agent subprocess, and\n"
        "               only the bypassable app-level checks remain. Every such spawn is audited. To\n"
        "               refuse them instead, set agent.sandbox_allow_unsandboxed_exec=false. A\n"
        "               governance sandbox.min_level floor overrides this and makes such spawns fail\n"
        "               closed, so a managed host refuses them despite this line.\n"
    ),
    "posture:refused-declared": (
        "  backend:     ⏭  no OS-level sandbox backend on this platform\n"
        "  exec:        ⛔ agent subprocesses are REFUSED on this host\n"
        "               agent.sandbox_allow_unsandboxed_exec is set to false, so MCP servers, app\n"
        "               backends and the provider CLIs will report a sandbox error. Remove the key to\n"
        "               accept this platform's default, or set it to true to allow unconfined execution.\n"
    ),
    "posture:refused-undeclared": (
        "  backend:     ⏭  no OS-level sandbox backend on this platform\n"
        "  exec:        ⛔ agent subprocesses are REFUSED on this host\n"
        "               No backend is available and no opt-in is declared, so MCP servers, app backends\n"
        "               and the provider CLIs will report a sandbox error. Set\n"
        "               agent.sandbox_allow_unsandboxed_exec=true to allow unconfined execution, or run\n"
        "               `kirocrew setup` to be walked through the decision.\n"
    ),
}


def _check(key: str, out: str) -> None:
    assert out == EXPECTED[key]


# ── /proc/self readers ────────────────────────────────────────────────────────


def _fake_proc(monkeypatch: pytest.MonkeyPatch, answers: dict[str, object]) -> None:
    """Answer ``Path.read_text`` for the named ``/proc`` files and nothing else."""
    real = Path.read_text

    def read_text(self: Path, *args, **kwargs) -> str:
        key = self.as_posix()
        if key in answers:
            answer = answers[key]
            if isinstance(answer, BaseException):
                raise answer
            return str(answer)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)


def test_the_per_lsm_apparmor_label_is_read_first(monkeypatch) -> None:
    _fake_proc(
        monkeypatch,
        {
            "/proc/self/attr/apparmor/current": "kirocrew-userns (enforce)\x00\n",
            "/proc/self/attr/current": AssertionError("read the fallback"),
        },
    )
    assert cli_doctor._process_apparmor_confinement() == "kirocrew-userns (enforce)"


def test_the_bare_attr_is_the_fallback_label(monkeypatch) -> None:
    _fake_proc(
        monkeypatch,
        {
            "/proc/self/attr/apparmor/current": OSError("no such lsm"),
            "/proc/self/attr/current": "unconfined\n",
        },
    )
    assert cli_doctor._process_apparmor_confinement() == "unconfined"


def test_an_unreadable_label_is_empty(monkeypatch) -> None:
    _fake_proc(
        monkeypatch,
        {
            "/proc/self/attr/apparmor/current": OSError("gone"),
            "/proc/self/attr/current": OSError("gone"),
        },
    )
    assert cli_doctor._process_apparmor_confinement() == ""


def test_a_proc_self_file_is_read_as_ascii(monkeypatch) -> None:
    _fake_proc(monkeypatch, {"/proc/self/status": "Seccomp:\t2\n"})
    assert cli_doctor._read_linux_proc_self("status") == "Seccomp:\t2\n"


def test_a_proc_self_status_whose_name_is_not_ascii_is_read(monkeypatch, tmp_path) -> None:
    """The ``Name:`` line is this process's raw comm; the ``Seccomp:`` line survives it."""
    status = tmp_path / "status"
    status.write_bytes(b"Name:\tkc-\xc3\xa9t\xff\nSeccomp:\t2\n")
    real = Path.read_text

    def read_text(self: Path, *args, **kwargs) -> str:
        target = status if self.as_posix() == "/proc/self/status" else self
        return real(target, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    assert cli_doctor._read_linux_proc_self("status").endswith("Seccomp:\t2\n")


@pytest.mark.parametrize(
    ("uid_map", "status", "expected"),
    [
        ("0 0 1\n", "Name:\tx\nSeccomp:\t2\n", True),
        ("0 1000 1\n", "Seccomp:\t2\n", False),
        ("0 0 1\n1 1 1\n", "Seccomp:\t2\n", False),
        ("0 0 1\n", "Seccomp:\t0\n", False),
        ("0 0\n", "Seccomp:\t2\n", None),
        ("a b c\n", "Seccomp:\t2\n", None),
        ("", "Seccomp:\t2\n", None),
        ("0 0 1\n", "Name:\tx\n", None),
        ("0 0 1\n", "Seccomp:\tstrict\n", None),
    ],
)
def test_the_confined_agent_shell_shape(monkeypatch, uid_map, status, expected) -> None:
    monkeypatch.setattr(platform_compat, "IS_LINUX", True)
    files = {"uid_map": uid_map, "status": status}
    monkeypatch.setattr(cli_doctor, "_read_linux_proc_self", lambda name: files[name])
    assert cli_doctor._process_userns_vantage_confined() is expected


def test_an_unreadable_proc_self_keeps_the_host_verdict(monkeypatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_LINUX", True)

    def refuse(name: str) -> str:
        raise OSError(name)

    monkeypatch.setattr(cli_doctor, "_read_linux_proc_self", refuse)
    assert cli_doctor._process_userns_vantage_confined() is None


def test_the_vantage_check_does_not_apply_off_linux(monkeypatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    monkeypatch.setattr(cli_doctor, "_read_linux_proc_self", lambda name: pytest.fail(name))
    assert cli_doctor._process_userns_vantage_confined() is None


# ── Sandbox backend verdict ───────────────────────────────────────────────────


def _backend(monkeypatch, *, kind="x", reason="denied by policy", remedy="", platform="linux"):
    sb = cli_doctor.sandbox
    if isinstance(kind, BaseException):

        def boom() -> str:
            raise kind

        monkeypatch.setattr(sb, "unavailable_kind", boom)
    else:
        monkeypatch.setattr(sb, "unavailable_kind", lambda: kind)
    monkeypatch.setattr(sb, "unavailable_reason", lambda: reason)
    monkeypatch.setattr(sb, "unavailable_remedy", lambda: remedy)
    monkeypatch.setattr(sb, "detect_backend", lambda config_mode="auto": "namespace")
    monkeypatch.setattr(sys, "platform", platform)


@pytest.mark.parametrize(
    ("key", "setup"),
    [
        ("probe-raises", {"kind": RuntimeError("probe exploded")}),
        ("working", {"kind": ""}),
        ("transient", {"kind": "transient", "reason": "EAGAIN"}),
        ("transient-no-reason", {"kind": "transient", "reason": None}),
        ("foreign", {"kind": "foreign_sandbox"}),
    ],
)
def test_the_backend_verdict_rows(monkeypatch, capsys, key, setup) -> None:
    _backend(monkeypatch, **setup)
    issues: list[str] = []

    cli_doctor._doctor_sandbox_backend(issues)

    _check(f"backend:{key}", capsys.readouterr().out)
    assert issues == []


def test_a_denied_userns_seen_from_a_confined_shell_is_unverifiable(monkeypatch, capsys) -> None:
    _backend(monkeypatch, kind="permanent", remedy=cli_doctor.sandbox.REMEDY_USERNS_DENIED)
    monkeypatch.setattr(cli_doctor, "_process_userns_vantage_confined", lambda: True)
    issues: list[str] = []

    cli_doctor._doctor_sandbox_backend(issues)

    _check("backend:userns-vantage", capsys.readouterr().out)
    assert issues == []


@pytest.mark.parametrize("guidance", ["Set user.max_user_namespaces above zero.", ""])
def test_a_named_linux_refusal_is_an_issue(monkeypatch, capsys, guidance) -> None:
    _backend(monkeypatch, kind="permanent", remedy="max_user_namespaces")
    monkeypatch.setattr(cli_doctor.sandbox, "remedy_guidance", lambda remedy: guidance)
    monkeypatch.setattr(cli_doctor, "_process_userns_vantage_confined", lambda: None)
    issues: list[str] = []

    cli_doctor._doctor_sandbox_backend(issues)

    _check(f"backend:linux-refusal:{bool(guidance)}", capsys.readouterr().out)
    assert issues == ["sandbox backend"]


@pytest.mark.parametrize(
    ("key", "permitted_by", "declared"),
    [
        ("platform", "UNSANDBOXED_BY_PLATFORM", False),
        ("operator", "UNSANDBOXED_BY_OPERATOR", False),
        ("refused-declared", None, True),
        ("refused-undeclared", None, False),
    ],
)
def test_the_no_backend_posture_rows(monkeypatch, capsys, key, permitted_by, declared) -> None:
    _backend(monkeypatch, kind="permanent", remedy="none", platform="win32")
    sb = cli_doctor.sandbox
    value = getattr(sb, permitted_by) if permitted_by else ""
    monkeypatch.setattr(sb, "unsandboxed_exec_permitted_by", lambda: value)
    monkeypatch.setattr(cli_doctor, "unsandboxed_exec_declared", lambda: declared)
    issues: list[str] = []

    cli_doctor._doctor_sandbox_backend(issues)

    _check(f"posture:{key}", capsys.readouterr().out)
    assert issues == []


# ── Masked credential leaves ──────────────────────────────────────────────────


def _aliases(monkeypatch, *, rows=None, raises=None, confined=True, home="/data/op/.kiro/crew"):
    sb = cli_doctor.sandbox
    monkeypatch.setattr(sys, "platform", "linux")
    if raises is not None:

        def boom() -> list:
            raise raises

        monkeypatch.setattr(sb, "masked_credential_leaf_aliases", boom)
    else:
        monkeypatch.setattr(sb, "masked_credential_leaf_aliases", lambda: rows)
    if isinstance(confined, BaseException):

        def refuse(mode: str) -> bool:
            raise confined

        monkeypatch.setattr(sb, "credential_mask_applies", refuse)
    else:
        monkeypatch.setattr(sb, "credential_mask_applies", lambda mode: confined)
    monkeypatch.setattr(sb, "configured_sandbox_mode", lambda: "auto")
    if isinstance(home, BaseException):

        def no_home() -> Path:
            raise home

        monkeypatch.setattr(cli_doctor, "config_dir", no_home)
    else:
        # The live home is compared as text, so hand it over as the text it is on
        # every host rather than as a path that renders per flavour.
        monkeypatch.setattr(cli_doctor, "config_dir", lambda: home)
    monkeypatch.setattr(sb, "_masked_leaf_alias_search_hint", lambda path: f"find -samefile {path}")
    monkeypatch.setattr(
        sb, "_masked_leaf_multilink_detail", lambda path, links: f"cannot mask {path}: {links}"
    )


_LIVE = "/data/op/.kiro/crew"
_ROWS = {
    "accounted": [(f"{_LIVE}/.env", 2, _LIVE, True)],
    "refusing": [(f"{_LIVE}/.env", 3, _LIVE, False)],
    "outside": [("/srv/old/.env", 2, "/srv/old", False)],
    "mixed": [(f"{_LIVE}/.env", 2, _LIVE, True), ("/srv/old/.env", 2, "/srv/old", False)],
}


@pytest.mark.parametrize(
    ("key", "rows", "confined", "issue"),
    [
        ("refusing", "refusing", True, True),
        ("in-live-unconfined", "refusing", False, False),
        ("accounted-confined", "accounted", True, False),
        ("outside-confined", "outside", True, False),
        ("mixed-confined", "mixed", True, False),
        ("mixed-unconfined", "mixed", False, False),
    ],
)
def test_the_masked_leaf_rows(monkeypatch, capsys, key, rows, confined, issue) -> None:
    _aliases(monkeypatch, rows=_ROWS[rows], confined=confined)
    issues: list[str] = []

    cli_doctor._doctor_masked_credential_aliases(issues)

    _check(f"aliases:{key}", capsys.readouterr().out)
    assert issues == (["masked credential leaf alias"] if issue else [])


def test_a_failed_alias_probe_is_reported_not_raised(monkeypatch, capsys) -> None:
    _aliases(monkeypatch, raises=OSError("stat refused"))
    issues: list[str] = []

    cli_doctor._doctor_masked_credential_aliases(issues)

    _check("aliases:probe-raises", capsys.readouterr().out)
    assert issues == []


def test_an_unreadable_mode_and_home_still_report_the_leaf(monkeypatch, capsys) -> None:
    """An unreadable mode reads as confined; an unresolvable home matches no leaf,
    so nothing can be refused and every leaf reports as outside the live home."""
    _aliases(
        monkeypatch,
        rows=_ROWS["refusing"],
        confined=RuntimeError("mode"),
        home=RuntimeError("home"),
    )
    issues: list[str] = []

    cli_doctor._doctor_masked_credential_aliases(issues)

    _check("aliases:unreadable", capsys.readouterr().out)
    assert issues == []


@pytest.mark.parametrize(("platform", "silent"), [("darwin", False), ("win32", True)])
def test_the_alias_section_runs_where_a_confined_launch_path_exists(
    monkeypatch, capsys, platform, silent
) -> None:
    _aliases(monkeypatch, rows=_ROWS["outside"], confined=True)
    monkeypatch.setattr(sys, "platform", platform)

    cli_doctor._doctor_masked_credential_aliases([])

    assert (capsys.readouterr().out == "") is silent


def test_no_alias_prints_nothing(monkeypatch, capsys) -> None:
    _aliases(monkeypatch, rows=[])

    cli_doctor._doctor_masked_credential_aliases([])

    assert capsys.readouterr().out == ""


# ── Task queue and liveness ───────────────────────────────────────────────────


@pytest.fixture
def task_home(tmp_path, monkeypatch) -> Path:
    import kiro_crew.config.paths as paths

    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    (tmp_path / "tasks").mkdir()
    return tmp_path


def test_a_quarantined_task_store_is_named_once_per_copy(task_home, capsys) -> None:
    tasks = task_home / "tasks"
    for name in ("tasks.db.corrupt-1", "tasks.db.corrupt-1-wal", "tasks.db.corrupt-2"):
        (tasks / name).write_bytes(b"")
    issues: list[str] = []

    cli_doctor._doctor_task_store(issues)

    assert capsys.readouterr().out == (
        "  task store: ⚠️  a corrupt store was quarantined as tasks.db.corrupt-1\n"
        "  task store: ⚠️  a corrupt store was quarantined as tasks.db.corrupt-2\n"
    )
    assert [issue.split(" was found corrupt")[0] for issue in issues] == [
        f"task store {tasks / 'tasks.db'}",
        f"task store {tasks / 'tasks.db'}",
    ]
    assert issues[0].endswith(
        f"quarantined as {tasks / 'tasks.db.corrupt-1'}; work accepted into the old file "
        "was not recovered -- inspect or delete the quarantined copy"
    )


def test_an_unopenable_task_store_is_an_issue(task_home, capsys, monkeypatch) -> None:
    from kiro_crew.taskq import TaskStore, TaskStoreUnavailable

    db = task_home / "tasks" / "tasks.db"
    db.write_bytes(b"")

    def refuse(self) -> None:
        raise TaskStoreUnavailable("locked by another writer")

    monkeypatch.setattr(TaskStore, "open", refuse)
    issues: list[str] = []

    cli_doctor._doctor_task_store(issues)

    assert capsys.readouterr().out == (
        f"  task store: ⚠️  {db} cannot be opened (locked by another writer)\n"
    )
    assert issues == [
        f"task store {db} cannot be opened: accepted subagent work cannot be "
        "persisted or recovered until this is fixed"
    ]


def test_the_task_store_rows_and_warnings(task_home, capsys, monkeypatch) -> None:
    from kiro_crew.taskq import TaskStore

    (task_home / "tasks" / "tasks.db").write_bytes(b"")
    monkeypatch.setattr(
        TaskStore, "open", lambda self: self.warnings.append("journal on a network filesystem")
    )
    monkeypatch.setattr(TaskStore, "close", lambda self: None)
    monkeypatch.setattr(TaskStore, "doctor_lines", lambda self: ["task store: 2 queued"])
    monkeypatch.setattr(
        TaskStore, "count_by_state", lambda self: {"running": 1, "done": 0, "queued": 2}
    )
    issues: list[str] = []

    cli_doctor._doctor_task_store(issues)

    assert capsys.readouterr().out == (
        "  task store: 2 queued\n  task states: queued=2, running=1\n"
    )
    assert issues == ["journal on a network filesystem"]


def test_an_unknown_platform_has_no_process_tree_backend(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "sunos5")
    assert cli_doctor._liveness_platform_line() == (
        "sunos5 — no process-tree backend; UNKNOWN platform_limited"
    )
