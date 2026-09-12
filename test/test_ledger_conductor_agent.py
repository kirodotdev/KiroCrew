"""The deprecated ``kirocrew-ledger-conductor`` alias spec.

The work-ledger flow is ``kirocrew-conductor`` now, and this name is kept for one
release because it is a public, user-facing string: it is what a running session
records as its agent, what a seed prompt names for a second-level conductor, and
what an operator typed into a cron. So one property carries this whole file:
**the alias emits the SAME spec, differing in ``name`` and ``description`` and in
nothing else.** An alias that emitted a different spec would silently change what
an in-flight session can do, which is the one failure the alias exists to prevent.

Everything the spec itself has to be — no file-writing tool, no whole-server
auto-approve, grants filtered through the governance ceiling, the KAS block
derived from the FILTERED list, the work mount on all four surfaces — is asserted
once in ``test_conductor_agent.py``, against the spec both installers share. It is
not re-asserted here: the identity test below is what carries it across.

The ceiling stub is the same shape as the sibling installer tests': the default is
an ungoverned host, and the governed case gets its own test.
"""

import json
from pathlib import Path

from kiro_crew import agent
from kiro_crew.agent_files import (
    CONDUCTOR_AGENT_FILENAME,
    LEDGER_CONDUCTOR_AGENT_FILENAME,
    OWNED_KIRO_AGENT_FILES,
    REQUIRED_KIRO_AGENT_FILES,
)

SKILL_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "goal-ledger-conductor"
)


def _stub(tmp_path, monkeypatch, *, may_auto_approve=None):
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(
        agent,
        "build_agent_config",
        lambda: {
            "name": "kirocrew",
            "prompt": "file://x",
            "mcpServers": {
                "kirocrew-core": {"command": "/resolved/kirocrew", "args": ["mcp-core"]},
                "builder-mcp": {"command": "/x/builder", "args": []},
            },
            "tools": ["fs_write", "@kirocrew-core"],
            "allowedTools": ["@kirocrew-core"],
        },
    )
    monkeypatch.setattr(
        agent,
        "_kirocrew_mcp_invocation",
        lambda sub: ("/resolved/kirocrew", [sub]),
    )
    monkeypatch.setattr(agent, "_may_auto_approve", may_auto_approve or (lambda ref: True))


def _install(tmp_path, monkeypatch, *, may_auto_approve=None):
    _stub(tmp_path, monkeypatch, may_auto_approve=may_auto_approve)
    agent._install_ledger_conductor_agent()
    return json.loads((tmp_path / LEDGER_CONDUCTOR_AGENT_FILENAME).read_text(encoding="utf-8"))


# ── the one property: same spec, different name ───────────────────────────


def test_the_alias_spec_is_identical_apart_from_name_and_description(tmp_path, monkeypatch):
    """Both installers under one stub, then compare the emitted JSON.

    Field by field rather than "both call the same helper", because a helper is
    exactly what a later edit routes around: an installer that grew one extra
    ``config[...] = ...`` line after the call would still share the helper and
    still emit a different spec. Only ``name`` and ``description`` may differ,
    and the description must SAY it is deprecated — an operator reading the
    roster is the one person who cannot see the code.
    """
    _stub(tmp_path, monkeypatch)
    agent._install_conductor_agent()
    agent._install_ledger_conductor_agent()
    live = json.loads((tmp_path / CONDUCTOR_AGENT_FILENAME).read_text(encoding="utf-8"))
    alias = json.loads((tmp_path / LEDGER_CONDUCTOR_AGENT_FILENAME).read_text(encoding="utf-8"))

    assert live["name"] == "kirocrew-conductor"
    assert alias["name"] == "kirocrew-ledger-conductor"
    assert alias["description"].startswith(
        "Deprecated alias of kirocrew-conductor (removed next release)."
    )
    assert alias["description"] != live["description"]
    # The charter sentence survives the prefix, so the roster entry still says
    # what the agent is and not only that it is going away.
    assert live["description"] in alias["description"]

    assert {k for k, v in live.items() if alias.get(k) != v} == {"name", "description"}
    assert set(alias) == set(live)
    # Spelled out for the surfaces a reader cares about most, so a failure names
    # the drifted field instead of dumping two dicts.
    for field in ("prompt", "tools", "allowedTools", "mcpServers", "permissions"):
        assert alias[field] == live[field], field


def test_the_governance_ceiling_reaches_the_alias_the_same_way(tmp_path, monkeypatch):
    """A ceiling is host state, not per-spec state, so it must strip the same ref
    from both. Pinned on a work-ledger ref: the alias is the spec an unmigrated
    session is running under, and a ceiling that reached only the new name would
    leave that session auto-approving a verb the operator revoked."""
    _stub(tmp_path, monkeypatch, may_auto_approve=lambda ref: ref != "@kirocrew-work/work_report")
    agent._install_conductor_agent()
    agent._install_ledger_conductor_agent()
    live = json.loads((tmp_path / CONDUCTOR_AGENT_FILENAME).read_text(encoding="utf-8"))
    alias = json.loads((tmp_path / LEDGER_CONDUCTOR_AGENT_FILENAME).read_text(encoding="utf-8"))
    assert alias["allowedTools"] == live["allowedTools"]

    governed = _install(
        tmp_path,
        monkeypatch,
        may_auto_approve=lambda ref: ref != "@kirocrew-work/work_ledger_record",
    )
    assert "@kirocrew-work/work_ledger_record" not in governed["allowedTools"]
    assert "@kirocrew-work/work_ledger_read" in governed["allowedTools"]
    # Stripped from the derived KAS block with it, rather than surviving there.
    match = governed["permissions"]["rules"][0]["match"]
    assert "kirocrew-work/work_ledger_record" not in match
    assert "kirocrew-work/work_ledger_read" in match
    # Still MOUNTED — a governed ref prompts, it is not unmounted.
    assert "@kirocrew-work" in governed["tools"]


def test_a_withheld_grant_names_this_installer_in_the_audit(tmp_path, monkeypatch):
    """Withholding a grant is a permission DECISION, and the two installers must be
    tellable apart in the feed: an operator reading one event cannot otherwise know
    whether the live spec or the alias lost the grant."""
    events: list[dict] = []

    class _Sel:
        def log_api_access(self, **kwargs):
            events.append(kwargs)

    monkeypatch.setattr(agent, "sel", lambda: _Sel())
    _install(
        tmp_path,
        monkeypatch,
        may_auto_approve=lambda ref: ref != "@kirocrew-work/work_ledger_read",
    )
    (event,) = [e for e in events if e["operation"] == "mcp_auto_approve_withheld"]
    assert event["source"] == "_install_ledger_conductor_agent"
    assert "@kirocrew-work/work_ledger_read" in event["resources"]


def test_audit_failure_does_not_break_the_install(tmp_path, monkeypatch):
    """The audit must never be what stops an agent from being installable."""

    class _Boom:
        def log_api_access(self, **kwargs):
            raise RuntimeError("audit down")

    monkeypatch.setattr(agent, "sel", lambda: _Boom())
    data = _install(tmp_path, monkeypatch, may_auto_approve=lambda ref: False)
    assert data["allowedTools"] == []


# ── registration, which is what makes the old name still resolve ───────────


def test_spec_is_registered_as_kirocrew_owned_but_not_required(tmp_path, monkeypatch):
    """Owned so the boot self-heal sweep and the Playwright convergence sweep see
    it; NOT required, because its installer degrades to ``logger.debug`` and only
    its own feature stops working — the same split as its siblings."""
    assert LEDGER_CONDUCTOR_AGENT_FILENAME == "kirocrew-ledger-conductor.json"
    assert LEDGER_CONDUCTOR_AGENT_FILENAME in OWNED_KIRO_AGENT_FILES
    assert LEDGER_CONDUCTOR_AGENT_FILENAME not in REQUIRED_KIRO_AGENT_FILES
    _install(tmp_path, monkeypatch)
    assert (tmp_path / LEDGER_CONDUCTOR_AGENT_FILENAME).is_file()


def test_the_boot_installer_writes_this_spec():
    """``session_create`` refuses an agent it cannot resolve, and resolution reads a
    boot-time in-memory snapshot that no spec write refreshes — so a lazily
    materialized spec is invisible to the validation that runs ahead of the spawn.
    A session already running under the old name resolves it on every dispatch,
    which is exactly what the alias is for, so eager installation is load-bearing
    rather than tidy."""
    source = Path(agent.__file__).read_text(encoding="utf-8")
    assert "_install_ledger_conductor_agent()" in source


# ── the deprecated skill ──────────────────────────────────────────────────


def test_the_skill_is_a_pointer_and_not_a_second_procedure():
    """Two copies of a procedure are two procedures the moment one is edited. The
    body has to send the reader to ``goal-conductor`` and stop, and it must not
    carry the dispatch steps that would let a model follow it instead."""
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "name: goal-ledger-conductor" in body
    assert "deprecated" in body.lower()
    assert "`goal-conductor`" in body
    # The procedure itself must be gone, not merely prefixed with a notice.
    for step in ("action=bind", "action=verdict", "### Which agent", "## Patrol"):
        assert step not in body, step
    assert len(body.splitlines()) < 40


def test_the_index_line_sends_a_reader_to_the_live_skill():
    """Per-message trigger matching is off by default, so the ``## Available
    Skills`` line is the only thing an agent reads before deciding to load a
    skill. A deprecation that shows up as a normal-looking procedure summary gets
    loaded; one whose first sentence says where the procedure went does not."""
    from kiro_crew.skills import SkillsLoader

    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    desc = SkillsLoader._parse_frontmatter_text(body).get("description") or ""
    line = SkillsLoader._short_desc(" ".join(desc.split()))
    assert "Deprecated" in line
    assert "goal-conductor" in line


def test_the_skill_ships_only_the_evaluator():
    """The item-entry codec belongs to a conductor with no item store. This flow
    has a store, so shipping the codec here would invite the double-bookkeeping
    the procedure forbids."""
    scripts = {p.name for p in (SKILL_DIR / "scripts").glob("*.py")}
    assert scripts == {"accept_eval.py"}


def test_the_evaluator_is_a_regular_file_not_a_symlink():
    """The builtin-skill scope gate refuses a symlink before any read, and a wheel
    carries one poorly. The copy is deliberate."""
    script = SKILL_DIR / "scripts" / "accept_eval.py"
    assert script.is_file()
    assert not script.is_symlink()


def test_the_evaluator_is_byte_identical_to_the_shipped_one():
    """Two copies that drift are two acceptance bars. The RFC's whole reason for
    storing ``acceptance`` verbatim is that ONE script parses it, so a divergence
    here is a defect rather than a variant — and a session that loaded the old
    skill name keeps a working evaluator until the name is removed."""
    shipped = SKILL_DIR.parent / "goal-conductor" / "scripts" / "accept_eval.py"
    assert (SKILL_DIR / "scripts" / "accept_eval.py").read_bytes() == shipped.read_bytes()
