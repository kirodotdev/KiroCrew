"""The ``kirocrew-ledger-conductor`` spec, and the isolation it exists to provide.

Two properties are pinned here, and they are separate claims:

1. **The spec is a conductor.** It carries every property
   ``_install_conductor_agent``'s docstring argues for — no file-writing tool, no
   whole-server auto-approve, grants filtered through the governance ceiling, the
   KAS block derived from the FILTERED list — because it is a copy of that
   installer rather than of anything else. A copy is where those properties drift,
   so they are re-asserted rather than assumed.
2. **The work-ledger surface lives HERE and only here.** The mirror assertions —
   that ``kirocrew-conductor`` and ``kirocrew-pipeline-conductor`` do NOT carry it
   — live in ``test_conductor_agent.py``, ``test_pipeline_conductor_agent.py`` and
   ``test_worker_agent.py``, next to the specs they constrain.

The ceiling stub is the same shape as the sibling installer tests': the default is
an ungoverned host, and the governed case gets its own test.
"""

import json
from pathlib import Path

from kiro_crew import agent
from kiro_crew.agent_files import (
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


def _install(tmp_path, monkeypatch, *, may_auto_approve=None):
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
    agent._install_ledger_conductor_agent()
    return json.loads((tmp_path / LEDGER_CONDUCTOR_AGENT_FILENAME).read_text(encoding="utf-8"))


# ── identity and registration ─────────────────────────────────────────────


def test_identity_and_charter(tmp_path, monkeypatch):
    data = _install(tmp_path, monkeypatch)
    assert data["name"] == "kirocrew-ledger-conductor"
    assert "work item" in data["prompt"]


def test_spec_is_registered_as_kirocrew_owned_but_not_required(tmp_path, monkeypatch):
    """Owned so the boot self-heal sweep and the Playwright convergence sweep see
    it; NOT required, because its installer degrades to ``logger.debug`` and only
    its own feature stops working — the same split as its six siblings."""
    assert LEDGER_CONDUCTOR_AGENT_FILENAME == "kirocrew-ledger-conductor.json"
    assert LEDGER_CONDUCTOR_AGENT_FILENAME in OWNED_KIRO_AGENT_FILES
    assert LEDGER_CONDUCTOR_AGENT_FILENAME not in REQUIRED_KIRO_AGENT_FILES
    _install(tmp_path, monkeypatch)
    assert (tmp_path / LEDGER_CONDUCTOR_AGENT_FILENAME).is_file()


def test_the_boot_installer_writes_this_spec():
    """``session_create`` refuses an agent it cannot resolve, and resolution reads a
    boot-time in-memory snapshot that no spec write refreshes — so a lazily
    materialized spec is invisible to the validation that runs ahead of the spawn.
    A ledger conductor dispatching a second-level ledger conductor depends on its
    own name resolving, which makes eager installation load-bearing rather than
    tidy."""
    source = Path(agent.__file__).read_text(encoding="utf-8")
    assert "_install_ledger_conductor_agent()" in source


# ── the work-ledger surface, which is the point of the spec ───────────────


def test_the_work_server_is_mounted_without_auto_approve(tmp_path, monkeypatch):
    """An autoApproved MCP tool is approved inside kiro-cli and emits no permission
    request, so ``hooks.on_tool_call`` — the deny floor, the sensitive-path check
    and the governance ceiling — is never reached for it. A store that writes
    agent-authored text into a record the user reads is not the place to break
    that."""
    data = _install(tmp_path, monkeypatch)
    assert "@kirocrew-work" in data["tools"]
    entry = data["mcpServers"]["kirocrew-work"]
    assert entry["args"] == ["mcp-work"]
    assert "autoApprove" not in entry


def test_mcp_surface_is_core_plus_dashboard_plus_work(tmp_path, monkeypatch):
    """Inherited servers this charter has no use for are dropped, exactly as the
    two sibling conductors drop them."""
    data = _install(tmp_path, monkeypatch)
    assert set(data["mcpServers"]) == {
        "kirocrew-core",
        "kirocrew-dashboard",
        "kirocrew-work",
    }
    assert "builder-mcp" not in data["mcpServers"]


def test_the_conductor_half_plus_the_read_only_worker_verb_are_granted(tmp_path, monkeypatch):
    """Per tool rather than whole-server. ``work_brief`` is granted because it only
    reads the caller's own bound item (or answers ``not_bound``) — the same rule
    the two ledger verbs rest on — and it is a nested conductor's mandated first
    call in a session nobody opened. ``work_report`` is NOT granted: it writes
    into the parent's record across a dispatch relationship."""
    allowed = _install(tmp_path, monkeypatch)["allowedTools"]
    assert "@kirocrew-work/work_ledger_read" in allowed
    assert "@kirocrew-work/work_ledger_record" in allowed
    assert "@kirocrew-work/work_brief" in allowed
    assert "@kirocrew-work/work_report" not in allowed
    # A whole-server grant would hand over the worker half by the back door.
    assert "@kirocrew-work" not in allowed


def test_kas_permissions_name_exact_work_resources(tmp_path, monkeypatch):
    """Derived from the FILTERED grant list, and projected to EXACT
    ``server/tool`` resources: a ``kirocrew-work/*`` wildcard would re-grant the
    worker half on the backend where nothing reads ``allowedTools``."""
    match = _install(tmp_path, monkeypatch)["permissions"]["rules"][0]["match"]
    assert "kirocrew-work/work_ledger_read" in match
    assert "kirocrew-work/work_ledger_record" in match
    assert "kirocrew-work/work_brief" in match
    assert "kirocrew-work/*" not in match
    assert "kirocrew-work/work_report" not in match


# ── the conductor properties a copied installer could lose ────────────────


def test_no_file_writing_tool_at_all(tmp_path, monkeypatch):
    """What makes "never does a work item's work itself" a property of the SPEC
    rather than of the prompt. ``code`` counts: governance classes it under
    ``filesystem.write`` because it writes files and can shell out, so mounting it
    would make the whole no-write property false."""
    tools = _install(tmp_path, monkeypatch)["tools"]
    for writer in ("fs_write", "code"):
        assert writer not in tools, writer


def test_mounts_no_tool_the_charter_never_names(tmp_path, monkeypatch):
    """An unused grant is surface the charter cannot account for. Same omissions as
    ``kirocrew-conductor``: nothing names ``web_search``, and ``fs_read`` covers
    every read the charter describes, so ``grep`` / ``glob`` stay out."""
    tools = _install(tmp_path, monkeypatch)["tools"]
    for unmounted in ("web_search", "grep", "glob"):
        assert unmounted not in tools, unmounted


def test_execute_bash_is_mounted_and_never_granted(tmp_path, monkeypatch):
    """The shell runs the bundled evaluator, and ``allowedTools`` is name-scoped
    with no argument matching — so trusting that one script cannot be told apart
    from trusting arbitrary shell. There is no per-argument form of the grant."""
    data = _install(tmp_path, monkeypatch)
    assert "execute_bash" in data["tools"]
    assert "execute_bash" not in data["allowedTools"]


def test_no_mutating_session_verb_is_granted(tmp_path, monkeypatch):
    """The invariant on ``_CONDUCTOR_DASHBOARD_GRANTS``: a granted verb may CREATE
    or READ, never MUTATE something that already exists and is not the agent's
    own. This spec ingests untrusted content on nudge-driven cycles with nobody at
    the keyboard, so the withholds carry the same weight here."""
    allowed = _install(tmp_path, monkeypatch)["allowedTools"]
    for withheld in (
        "@kirocrew-dashboard/session_send",
        "@kirocrew-dashboard/session_stop",
        "@kirocrew-dashboard/chat_folder_move_session",
        "@kirocrew-dashboard/chat_folder_move",
        "@kirocrew-core/task_run",
        "@kirocrew-core/workflow_run",
        "@kirocrew-core/spawn_run",
        "@kirocrew-core/spawn_sub_agents",
    ):
        assert withheld not in allowed, withheld


def test_grants_pass_through_the_governance_ceiling(tmp_path, monkeypatch):
    """``allowedTools`` is the ONE path that never reaches the PreToolUse gate, so a
    ceiling that refuses a ref must strip it here. Pinned on a work-ledger ref
    specifically: a hand-written literal would have survived the filter."""
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


def test_a_withheld_grant_leaves_an_audit_record_naming_this_installer(tmp_path, monkeypatch):
    """Withholding a grant is a permission DECISION. Filtering silently would make
    this the one withhold path with no audit trail, and an operator would have no
    record of why the agent started prompting."""
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
    assert [e for e in events if e["source"] == "_install_ledger_conductor_agent"]
    (event,) = [e for e in events if e["operation"] == "mcp_auto_approve_withheld"]
    assert "@kirocrew-work/work_ledger_read" in event["resources"]


def test_audit_failure_does_not_break_the_install(tmp_path, monkeypatch):
    """The audit must never be what stops an agent from being installable."""

    class _Boom:
        def log_api_access(self, **kwargs):
            raise RuntimeError("audit down")

    monkeypatch.setattr(agent, "sel", lambda: _Boom())
    data = _install(tmp_path, monkeypatch, may_auto_approve=lambda ref: False)
    assert data["allowedTools"] == []


def test_prompt_carries_the_verbosity_placeholder(tmp_path, monkeypatch):
    """A custom agent's prompt comes from its spec rather than ``config/prompt.md``,
    and the token is only expanded where it appears — so without it the dashboard
    verbosity setting never reaches this agent."""
    assert "{{VERBOSITY_BLOCK}}" in _install(tmp_path, monkeypatch)["prompt"]


# ── the procedure the prompt has to carry ─────────────────────────────────


def test_prompt_names_the_ledger_tools(tmp_path, monkeypatch):
    prompt = _install(tmp_path, monkeypatch)["prompt"]
    assert "work_ledger_read" in prompt
    assert "work_ledger_record" in prompt


def test_prompt_fixes_the_dispatch_order_with_bind_before_seed(tmp_path, monkeypatch):
    """A worker that runs before its binding exists gets ``not_bound`` and cannot
    tell an early call from a broken one. The order is the whole reason this
    procedure differs from ``goal-conductor``'s, so the prompt must state it."""
    prompt = _install(tmp_path, monkeypatch)["prompt"]
    assert "action=create" in prompt
    assert "action=bind" in prompt
    assert "session_create" in prompt
    assert "session_send" in prompt
    assert "Bind before you seed" in prompt
    # And in that order, not merely all present.
    assert prompt.index("action=create") < prompt.index("session_create")
    assert prompt.index("session_create") < prompt.index("action=bind")
    assert prompt.index("action=bind") < prompt.index("session_send")


def test_prompt_states_the_per_item_agent_rule(tmp_path, monkeypatch):
    """An omitted ``agent`` inherits the CALLER's, so the child comes up as a second
    ledger conductor with no ``fs_write`` and the item looks stalled rather than
    misconfigured."""
    prompt = _install(tmp_path, monkeypatch)["prompt"]
    assert "kirocrew-worker" in prompt
    assert "kirocrew-ledger-conductor" in prompt
    assert "select_crew" in prompt
    assert "Never leave `agent` unset" in prompt


def test_prompt_makes_done_a_claim_the_evaluator_settles(tmp_path, monkeypatch):
    """Nothing a worker can write reaches ``verdict``. The prompt has to say that a
    ``done`` triggers verification rather than substituting for it."""
    prompt = _install(tmp_path, monkeypatch)["prompt"]
    assert "accept_eval" in prompt
    assert "accept_batch" in prompt
    assert "action=verdict" in prompt
    assert "CLAIM" in prompt


def test_prompt_and_skill_filter_the_batch_to_done_items(tmp_path, monkeypatch):
    """``accept_batch`` is status-blind by design (it is the promotion seam), so the
    procedure must filter it. Unfiltered, a ``progress`` worker whose stub already
    satisfies a ``file`` condition earns a genuine ``pass`` and can be closed under
    itself. Both the prompt and the skill have to state the filter, because the
    agent copies whichever it read last."""
    prompt = _install(tmp_path, monkeypatch)["prompt"]
    assert "Filter the returned" in prompt
    assert "whose status is `done`" in prompt
    body = " ".join((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").split())
    assert "keep only the entries whose item is currently `status: done`" in body
    assert "Never pipe the unfiltered document" in body
    # The old wording that told the reader to send everything is gone.
    assert "every ready item" not in body


def test_prompt_keeps_a_claimed_pr_out_of_the_acceptance_bar(tmp_path, monkeypatch):
    """A worker that could fill in its own acceptance could point it at anybody's
    already-green pull request, which is why promotion is an explicit
    ``action=accept`` write."""
    prompt = _install(tmp_path, monkeypatch)["prompt"]
    assert "action=accept" in prompt


def test_prompt_uses_close_for_state_and_not_the_item_codec(tmp_path, monkeypatch):
    """The ledger is the item store now. Writing items into ``session_ledger``
    artifacts as well would give two records that can disagree."""
    prompt = _install(tmp_path, monkeypatch)["prompt"]
    assert "action=close" in prompt
    assert "ledger_entry" not in prompt
    assert "session_ledger_read" in prompt


def test_prompt_and_skill_make_a_nested_conductor_report_upward(tmp_path, monkeypatch):
    """A second-level ledger conductor is bound as its parent's worker, and the
    parent reads its OWN ledger — so a nested conductor that never calls
    ``work_report`` leaves its parent's item statusless forever, which the parent
    reads as a stall. The worker half is mounted on this spec for exactly this
    caller; the text has to tell it to use it."""
    prompt = _install(tmp_path, monkeypatch)["prompt"]
    assert "If a conductor dispatched you" in prompt
    assert "`work_brief` before you plan" in prompt
    assert "`work_report` `status: progress`" in prompt
    body = " ".join((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").split())
    assert "When a conductor dispatched you" in body
    assert "`work_brief` before Round 0" in body
    assert "`work_report` at round boundaries" in body
    # And the root case is still named, so a root conductor does not treat
    # ``not_bound`` as a failure.
    assert "not_bound" in prompt
    assert "root conductor gets `not_bound`" in body


def test_prompt_notes_the_patrol_gate_is_still_a_timer(tmp_path, monkeypatch):
    """``monitor_start`` gates on one pull-request URL and nothing else today, so a
    cycle fires whether or not anything was reported. The prompt says so, and says
    what to switch to, rather than implying a gate that does not exist."""
    prompt = _install(tmp_path, monkeypatch)["prompt"]
    assert "monitor_start" in prompt
    assert 'watch: "work-ledger"' in prompt


def test_prompt_points_at_its_own_skill_not_the_shipped_one(tmp_path, monkeypatch):
    """``goal-conductor`` is frozen against this flow — it seeds before it records
    and reads transcripts — so naming it here would send the agent to the wrong
    procedure."""
    prompt = _install(tmp_path, monkeypatch)["prompt"]
    assert "goal-ledger-conductor" in prompt
    assert "`goal-conductor`" not in prompt.replace("`goal-ledger-conductor`", "")


# ── the bundled skill ─────────────────────────────────────────────────────


def test_the_skill_ships_only_the_evaluator():
    """``ledger_entry.py`` exists to squeeze an item into a 2000-character
    ``artifacts`` value under an entry cap. This flow has a store, so the codec has
    no job here and shipping it would invite the double-bookkeeping the prompt
    forbids."""
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
    here is a defect rather than a variant."""
    shipped = SKILL_DIR.parent / "goal-conductor" / "scripts" / "accept_eval.py"
    assert (SKILL_DIR / "scripts" / "accept_eval.py").read_bytes() == shipped.read_bytes()


def test_the_skill_body_matches_the_prompt_on_the_load_bearing_rules():
    """The prompt is the summary and the skill is the procedure; a disagreement
    between them is resolved nondeterministically by whichever the model weighs
    more. These four are the rules that make this flow different."""
    body = " ".join((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").split())
    assert "Bind BEFORE you seed" in body
    assert "Never leave `agent` unset" in body
    assert "work_ledger_read` first, every cycle" in body
    assert "action=accept" in body
    # And it must not send the reader to the codec this flow replaced.
    assert "ledger_entry" not in body


def test_the_skill_feeds_the_evaluator_through_a_quoted_heredoc():
    """The acceptance document is built from ingested text, and the skill's example
    is what the agent copies. A ``printf '%s' '<json>'`` form ends its string at
    the first single quote inside a path and hands the remainder to the shell,
    which ``execute_bash`` then runs after one approval. A quoted heredoc is the
    one form the shell copies to stdin without interpreting."""
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "<<'ACCEPT_BATCH'" in body
    assert "printf '%s' '<" not in body
    assert "printf '%s' '{" not in body


def test_the_shipped_conductor_skill_is_untouched_by_this_flow():
    """``goal-conductor`` runs the un-migrated procedure and keeps its codec. If this
    assertion ever fails, the two flows have been merged — which is a decision with
    its own criteria, recorded in the work-ledger RFC's rollout note."""
    sibling = SKILL_DIR.parent / "goal-conductor"
    assert {p.name for p in (sibling / "scripts").glob("*.py")} == {
        "accept_eval.py",
        "ledger_entry.py",
    }
    body = (sibling / "SKILL.md").read_text(encoding="utf-8")
    assert "work_ledger" not in body
    assert "kirocrew-work" not in body
