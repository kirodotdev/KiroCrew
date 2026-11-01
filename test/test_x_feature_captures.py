"""The X-capture rule in ``scripts/check_harness_parity.py``.

A non-standard capability set must cite, per member, a live capture under
``test/fixtures/acp_frames/<backend>/`` that a test loads; KAS cites ``kas/``.
Today's gaps sit in a shrink-only allowlist. Contract:
``docs/ci/harness-parity-gate.md``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys

_GATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "scripts", "check_harness_parity.py"
)
_spec = importlib.util.spec_from_file_location("check_harness_parity_x", _GATE_PATH)
gate = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = gate
_spec.loader.exec_module(gate)

# The ceiling only moves down. Raising it is the change this test exists to stop.
X_CAPTURE_CEILING = 4
KIRO_KAS_PAIR_CEILING = 0
X_REGISTRATION_PENDING_CEILING = 6


def test_tree_passes() -> None:
    assert gate.enforce_x_captures(None) == 0


def test_allowlists_stay_under_their_ceiling() -> None:
    assert len(gate.X_CAPTURE_ALLOWLIST) <= X_CAPTURE_CEILING
    assert len(gate.KIRO_KAS_PAIR_ALLOWLIST) <= KIRO_KAS_PAIR_CEILING
    assert all(why.strip() for why in gate.X_CAPTURE_ALLOWLIST.values())
    assert len(gate.X_REGISTRATION_PENDING) <= X_REGISTRATION_PENDING_CEILING


def _with_set(monkeypatch, name: str) -> None:
    sets = dict(gate.live_sets(), **{name: frozenset({"pi"})})
    monkeypatch.setattr(gate, "live_sets", lambda: sets)


def test_an_unclassified_new_set_fails(monkeypatch, capsys) -> None:
    """A new ACP_BACKENDS_* set in none of the three lists cannot ship green."""
    _with_set(monkeypatch, "ACP_BACKENDS_BRAND_NEW")
    assert gate.enforce_x_captures(None) == 1
    out = capsys.readouterr().out
    assert "ACP_BACKENDS_BRAND_NEW: unclassified" in out and "HARNESS_EXEMPT_SETS" in out
    monkeypatch.setitem(gate.HARNESS_EXEMPT_SETS, "ACP_BACKENDS_BRAND_NEW", "routing-only")
    assert gate.enforce_x_captures(None) == 0
    monkeypatch.setitem(gate.HARNESS_EXEMPT_SETS, "ACP_BACKENDS_BRAND_NEW", " ")
    assert gate.enforce_x_captures(None) == 1


def test_a_set_in_two_lists_fails(monkeypatch) -> None:
    monkeypatch.setitem(gate.HARNESS_EXEMPT_SETS, "ACP_BACKENDS_STEER", "standard")
    assert any("keep it in one" in e for e in gate.check_coverage(gate.live_sets()))


def test_pending_only_shrinks_into_x_feature_sets(monkeypatch) -> None:
    sets, prior = gate.live_sets(), set(gate.X_REGISTRATION_PENDING)
    assert gate._pending_ratchet(prior, sets) == []
    name = "ACP_BACKENDS_HOOKS_LIST"
    monkeypatch.delitem(gate.X_REGISTRATION_PENDING, name)
    monkeypatch.setitem(gate.HARNESS_EXEMPT_SETS, name, "moved to dodge the capture")
    left = [f"{name}: left X_REGISTRATION_PENDING without joining X_FEATURE_SETS for every member"]
    assert gate._pending_ratchet(prior, sets) == left
    monkeypatch.delitem(gate.HARNESS_EXEMPT_SETS, name)
    # An empty scope would check no member at all.
    monkeypatch.setitem(gate.X_FEATURE_SETS, name, frozenset())
    assert gate._pending_ratchet(prior, sets) == left
    assert f"{name}: X_FEATURE_SETS scope checks none of its members" in gate.check_coverage(sets)
    monkeypatch.setitem(gate.X_FEATURE_SETS, name, None)
    assert gate._pending_ratchet(prior, sets) == []
    assert gate._pending_ratchet(prior - {"ACP_BACKENDS_NATIVE_TODOS"}, sets) == [
        "ACP_BACKENDS_NATIVE_TODOS: X_REGISTRATION_PENDING entry added; that list only shrinks"
    ]


def test_the_gate_holds_pending_to_the_base_ref(monkeypatch, capsys) -> None:
    """The shrink-only rule runs through enforce_x_captures, not only its helper."""
    prior = set(gate.X_REGISTRATION_PENDING)
    monkeypatch.setattr(gate, "_base_allowlists", lambda base: None)
    monkeypatch.setattr(gate, "_base_pending", lambda base: prior)
    assert gate.enforce_x_captures("base") == 0
    name = "ACP_BACKENDS_HOOKS_LIST"
    monkeypatch.delitem(gate.X_REGISTRATION_PENDING, name)
    monkeypatch.setitem(gate.HARNESS_EXEMPT_SETS, name, "moved to dodge the capture")
    assert gate.enforce_x_captures("base") == 1
    assert f"{name}: left X_REGISTRATION_PENDING" in capsys.readouterr().out
    smaller = set(gate.X_REGISTRATION_PENDING) - {"ACP_BACKENDS_NATIVE_TODOS"}
    monkeypatch.setattr(gate, "_base_pending", lambda base: smaller)
    assert gate.enforce_x_captures("base") == 1
    assert (
        "ACP_BACKENDS_NATIVE_TODOS: X_REGISTRATION_PENDING entry added" in capsys.readouterr().out
    )


def test_a_listed_set_no_longer_defined_fails(monkeypatch, capsys) -> None:
    for list_name in ("X_REGISTRATION_PENDING", "HARNESS_EXEMPT_SETS"):
        monkeypatch.setitem(getattr(gate, list_name), "ACP_BACKENDS_GONE", "was real")
        assert gate.enforce_x_captures(None) == 1
        out = capsys.readouterr().out
        assert f"ACP_BACKENDS_GONE: in {list_name} but no longer defined" in out
        monkeypatch.delitem(getattr(gate, list_name), "ACP_BACKENDS_GONE")


def test_base_pending_is_read_back_from_the_gate_source(monkeypatch) -> None:
    with open(_GATE_PATH, encoding="utf-8") as fh:
        source = fh.read()
    monkeypatch.setattr(gate, "git", lambda args: source)
    assert gate._base_pending("base") == set(gate.X_REGISTRATION_PENDING)
    plain = 'X_REGISTRATION_PENDING = {"ACP_BACKENDS_STEER": "v"}\n'
    monkeypatch.setattr(gate, "git", lambda args: plain)
    assert gate._base_pending("base") == {"ACP_BACKENDS_STEER"}
    monkeypatch.setattr(gate, "git", lambda args: "X = 1\n")
    assert gate._base_pending("base") is None


def test_pi_in_steering_request_without_its_capture_fails() -> None:
    """pi's STEERING_REQUEST membership whose comment cites no pi capture."""
    sets = gate.live_sets()
    name = "ACP_BACKENDS_STEERING_REQUEST"
    assert "pi" in sets[name]
    comments = gate.set_comments()
    assert "acp_frames/pi/steering-live.jsonl" in comments[name]
    comments[name] = comments[name].replace("acp_frames/pi/", "acp_frames/opencode/")
    gaps = gate.check_x_captures(sets, comments)
    assert (name, "pi") in gaps
    assert (name, "pi") not in gate.X_CAPTURE_ALLOWLIST
    assert (name, "pi") not in gate.check_x_captures(sets, gate.set_comments())


def test_kas_citing_a_kiro_capture_fails() -> None:
    comments = gate.set_comments()
    name = "ACP_BACKENDS_STEER"
    assert "acp_frames/kas/steering-live.jsonl" in comments[name]
    comments[name] = comments[name].replace("acp_frames/kas/", "acp_frames/kiro/")
    gaps = gate.check_x_captures(gate.live_sets(), comments)
    assert "kiro-cli is not KAS" in gaps[(name, "kas")]


def _relabeled(tmp_path, src_dir: str, dst_dir: str, header_from: str, header_to: str) -> str:
    """Copy ``<src_dir>/steering-live`` into a scratch corpus as ``<dst_dir>/``, relabeled."""
    import shutil

    for d in ("kiro", "kas"):
        (tmp_path / gate._FIXTURES / d).mkdir(parents=True)
    src = os.path.join(gate.REPO_ROOT, gate._FIXTURES, src_dir, "steering-live")
    dst = tmp_path / gate._FIXTURES / dst_dir / "steering-live"
    with open(src + ".jsonl", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    assert header_from in lines[0]
    lines[0] = lines[0].replace(header_from, header_to, 1)
    (dst.parent / "steering-live.jsonl").write_text("\n".join(lines) + "\n")
    shutil.copy(src + ".expected.json", str(dst) + ".expected.json")
    return str(tmp_path)


def test_a_kiro_capture_relabeled_kas_fails(tmp_path) -> None:
    """The kiro-cli steer capture, header rewritten to ``backend: kas``, cited for KAS."""
    root = _relabeled(tmp_path, "kiro", "kas", '"backend": ""', '"backend": "kas"')
    # The name check alone would refuse it; drop the header name to reach the handshake.
    path = os.path.join(root, gate._FIXTURES, "kas", "steering-live.jsonl")
    with open(path, encoding="utf-8") as fh:
        text = fh.read().replace(', "agent_name": "Kiro CLI Agent"', "", 1)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    gaps = gate.check_x_captures(gate.live_sets(), gate.set_comments(), root=root)
    assert "holds no KAS initialize" in gaps[("ACP_BACKENDS_STEER", "kas")]


def test_a_kas_capture_relabeled_kiro_fails(tmp_path) -> None:
    """The KAS steer capture, header rewritten to ``backend: ""``, cited for kiro-cli."""
    root = _relabeled(tmp_path, "kas", "kiro", '"backend": "kas"', '"backend": ""')
    gaps = gate.check_x_captures(gate.live_sets(), gate.set_comments(), root=root)
    assert "holds no kiro-cli initialize" in gaps[("ACP_BACKENDS_STEER", "kiro")]


def test_another_adapters_capture_in_copilot_fails(tmp_path) -> None:
    """The copilot compact capture, renamed to another adapter, cited for copilot."""
    import shutil

    name = "ACP_BACKENDS_COMPACT"
    assert "acp_frames/copilot/compact-live.jsonl" in gate.set_comments()[name]
    src = os.path.join(gate.REPO_ROOT, gate._FIXTURES, "copilot", "compact-live")
    dst = tmp_path / gate._FIXTURES / "copilot"
    dst.mkdir(parents=True)
    with open(src + ".jsonl", encoding="utf-8") as fh:
        text = fh.read()
    assert '"Copilot"' in text
    (dst / "compact-live.jsonl").write_text(text.replace('"Copilot"', '"OpenCode"'))
    shutil.copy(src + ".expected.json", dst / "compact-live.expected.json")
    gaps = gate.check_x_captures(gate.live_sets(), gate.set_comments(), root=str(tmp_path))
    assert "is a capture of 'OpenCode', not ['Copilot']" in gaps[(name, "copilot")]


def test_every_cited_kiro_and_kas_capture_carries_its_own_handshake() -> None:
    comments = gate.set_comments()
    cited = {
        (d, f)
        for name in gate.X_FEATURE_SETS
        for d, f in gate._CITE.findall(comments.get(name, ""))
        if d in gate.HANDSHAKE_IDENTITY
    }
    assert {d for d, _ in cited} == {"kiro", "kas"}
    for d, f in sorted(cited):
        meta, _ = gate.read_capture(gate.REPO_ROOT, f"{d}/{f}")
        assert gate.handshake_matches(d, meta), f"{d}/{f}"


def test_a_capture_is_read_with_its_replay_snapshot() -> None:
    meta, name = gate.read_capture(gate.REPO_ROOT, "kas/steering-live.jsonl")
    assert meta["recorded"] == "live" and meta["backend"] == "kas" and meta["replayed"]
    assert gate.read_capture(gate.REPO_ROOT, "pi/steer-live.jsonl") is None
    _, name = gate.read_capture(gate.REPO_ROOT, "pi/session-live.jsonl")
    assert name == "pi-acp"  # from ``initialize.agentInfo`` when the header has none


def test_the_self_test_passes() -> None:
    assert gate._x_capture_self_test() == 0


def test_an_added_allowlist_entry_is_refused(monkeypatch) -> None:
    entries = set(gate.X_CAPTURE_ALLOWLIST)
    entries.discard(("ACP_BACKENDS_SIDE_READONLY", "goose"))
    pairs, x_sets = set(gate.KIRO_KAS_PAIR_ALLOWLIST), dict(gate.X_FEATURE_SETS)
    monkeypatch.setattr(gate, "_base_allowlists", lambda base: (entries, pairs, x_sets))
    assert gate.enforce_x_captures("base") == 1


def test_dropping_an_x_set_is_refused(monkeypatch) -> None:
    prior = (
        set(gate.X_CAPTURE_ALLOWLIST),
        set(gate.KIRO_KAS_PAIR_ALLOWLIST),
        dict(gate.X_FEATURE_SETS),
    )
    monkeypatch.setattr(gate, "_base_allowlists", lambda base: prior)
    assert gate.enforce_x_captures("base") == 0
    monkeypatch.delitem(gate.X_FEATURE_SETS, "ACP_BACKENDS_MATE_WORKER_KIT")
    assert gate.enforce_x_captures("base") == 1


def test_a_proven_member_left_on_the_allowlist_is_refused(monkeypatch) -> None:
    monkeypatch.setitem(gate.X_CAPTURE_ALLOWLIST, ("ACP_BACKENDS_STEER", "kas"), "stale")
    assert gate.enforce_x_captures(None) == 1


def test_base_allowlists_are_read_back_from_the_gate_source(monkeypatch) -> None:
    with open(_GATE_PATH, encoding="utf-8") as fh:
        source = fh.read()
    monkeypatch.setattr(gate, "git", lambda args: source)
    x, pairs, x_sets = gate._base_allowlists("base")
    assert x == set(gate.X_CAPTURE_ALLOWLIST)
    assert pairs == set(gate.KIRO_KAS_PAIR_ALLOWLIST)
    assert x_sets == {k: (None if v is None else set(v)) for k, v in gate.X_FEATURE_SETS.items()}


def test_narrowing_an_x_set_scope_is_refused(monkeypatch) -> None:
    """Scoping pi out of STEERING_REQUEST would let it join with no capture."""
    prior = (
        set(gate.X_CAPTURE_ALLOWLIST),
        set(gate.KIRO_KAS_PAIR_ALLOWLIST),
        dict(gate.X_FEATURE_SETS),
    )
    monkeypatch.setattr(gate, "_base_allowlists", lambda base: prior)
    name = "ACP_BACKENDS_STEERING_REQUEST"
    monkeypatch.setitem(gate.X_FEATURE_SETS, name, frozenset({"claude", "codex", "goose"}))
    assert gate.enforce_x_captures("base") == 1
    monkeypatch.setitem(gate.X_FEATURE_SETS, name, None)
    monkeypatch.setitem(gate.X_FEATURE_SETS, "ACP_BACKENDS_MEMBER_DISPATCH", None)
    assert gate._narrowed(prior[2]) == []  # widening a scope is always allowed


def test_an_emptied_allowlist_is_read_back_as_empty(monkeypatch) -> None:
    with open(_GATE_PATH, encoding="utf-8") as fh:
        source = fh.read()
    assert "KIRO_KAS_PAIR_ALLOWLIST: frozenset[str] = frozenset()\n" in source
    monkeypatch.setattr(gate, "git", lambda args: source)
    _, pairs, _ = gate._base_allowlists("base")
    assert pairs == set()


def test_the_shrink_check_reads_the_merge_base_not_the_base_tip(monkeypatch) -> None:
    seen = []
    monkeypatch.setenv("HARNESS_BASE_REF", "origin/main")
    monkeypatch.setattr(gate, "diff_base", lambda base: "forkpoint")
    monkeypatch.setattr(gate, "enforce_diff", lambda base: 0)
    monkeypatch.setattr(gate, "enforce_x_captures", lambda base: seen.append(base) or 0)
    assert gate.main([]) == 0
    assert seen == ["forkpoint"]


def _frames(rel: str) -> list[dict]:
    import json

    with open(os.path.join(gate.REPO_ROOT, gate._FIXTURES, rel), encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _reply_and_tool_tags(rel: str) -> tuple[str, list[str]]:
    """The model's reply text and the tool tags of the LAST ``_kiro/tools/didChange``."""
    reply, tags = "", None
    for f in _frames(rel)[1:]:
        params = f.get("params") or {}
        if f.get("method") == "_kiro/tools/didChange":
            tags = [t["tag"] for t in params["tags"]]
        update = params.get("update") or {}
        if update.get("sessionUpdate") == "agent_message_chunk":
            reply += update["content"]["text"]
    assert tags is not None, rel
    return reply, tags


def test_kas_custom_agents_are_proven_by_their_own_captures() -> None:
    """The markdown and worker-kit captures are cited, pass, and are off the allowlist."""
    comments = gate.set_comments()
    for name, rel in (
        ("ACP_BACKENDS_MARKDOWN_AGENT_SPECS", "kas/markdown-agent-live.jsonl"),
        ("ACP_BACKENDS_MATE_WORKER_KIT", "kas/worker-kit-live.jsonl"),
    ):
        assert name in gate.X_FEATURE_SETS
        assert (name, "kas") not in gate.X_CAPTURE_ALLOWLIST
        assert f"acp_frames/{rel}" in comments[name]
        gaps = gate.check_x_captures(gate.live_sets(), comments)
        assert (name, "kas") not in gaps
        assert (name, "kas") in gate.check_x_captures(
            gate.live_sets(), {**comments, name: comments[name].replace(rel, "kas/absent.jsonl")}
        )


def test_the_markdown_agent_capture_shows_its_prompt_and_tools_applied() -> None:
    reply, tags = _reply_and_tool_tags("kas/markdown-agent-live.jsonl")
    assert reply.strip() == "markdown agent saffron"
    assert tags == ["read"]


def test_the_worker_kit_capture_shows_the_crewmate_prompt_and_limit_applied() -> None:
    reply, tags = _reply_and_tool_tags("kas/worker-kit-live.jsonl")
    assert reply.rstrip().endswith("worker kit juniper")
    assert "read_file" in reply and "execute_bash" not in reply
    assert tags == ["read"]  # the crewmate file excluded ``shell`` from the base


def test_every_live_kas_capture_names_the_engine_version() -> None:
    """KAS is versioned apart from kiro-cli; a live capture records the engine's own."""
    import re

    kas = os.path.join(gate.REPO_ROOT, gate._FIXTURES, "kas")
    live = []
    for f in sorted(os.listdir(kas)):
        if not f.endswith(".jsonl"):
            continue
        meta = _frames(f"kas/{f}")[0].get("_meta") or {}
        if meta.get("recorded") != "live":
            continue
        live.append(f)
        version = meta.get("agent_version", "")
        assert re.fullmatch(r"\d+\.\d+\.\d+", version), f
        if "kiro_cli_version" in meta:
            assert version != meta["kiro_cli_version"], f
    assert {"markdown-agent-live.jsonl", "worker-kit-live.jsonl"} <= set(live)


def _updates(rel: str, kind: str) -> list[dict]:
    return [
        (f.get("params") or {}).get("update") or {}
        for f in _frames(rel)[1:]
        if ((f.get("params") or {}).get("update") or {}).get("sessionUpdate") == kind
    ]


def _methods(rel: str, method: str) -> list[dict]:
    return [f for f in _frames(rel)[1:] if f.get("method") == method]


def test_kiro_and_kas_refuse_every_tool_natively_on_a_zero_tool_spec() -> None:
    """Four asks on ``"tools": []``, every permission answered allow: no call starts."""
    for rel in ("kiro/zero-tools-live.jsonl", "kas/zero-tools-live.jsonl"):
        assert not _updates(rel, "tool_call"), rel
        assert not _methods(rel, "session/request_permission"), rel
        ends = [f for f in _frames(rel)[1:] if (f.get("result") or {}).get("stopReason")]
        assert len(ends) == 4, rel
    _, tags = _reply_and_tool_tags("kas/zero-tools-live.jsonl")
    assert tags == []


def test_opencode_zero_tool_requests_are_refused_before_any_consumer() -> None:
    rel = "opencode/zero-tools-refused-live.jsonl"
    asked = _methods(rel, "session/request_permission")
    assert len(asked) == 3
    failed = [
        u
        for u in _updates(rel, "tool_call_update")
        if "rejected permission" in json.dumps(u.get("content"))
    ]
    assert len(failed) == 3


def test_codex_and_opencode_reach_the_server_on_the_session_array() -> None:
    """The saved spec's server and the member entry, each called once and answered."""
    for rel, server, tool in (
        ("codex/saved-spec-projection-live.jsonl", "probe", "probe_codeword"),
        ("codex/member-dispatch-live.jsonl", "kirocrew-dashboard", "session_status"),
    ):
        (call,) = _updates(rel, "tool_call")
        assert call["rawInput"] == {"server": server, "tool": tool, "arguments": {}}, rel
        assert len(_methods(rel, "session/request_permission")) == 1, rel
        assert any(u.get("status") == "completed" for u in _updates(rel, "tool_call_update")), rel
    done = _updates("codex/saved-spec-projection-live.jsonl", "tool_call_update")[-1]
    assert done["rawOutput"]["result"]["content"][0]["text"] == "tangerine-42"
    rel = "opencode/member-dispatch-live.jsonl"
    (call,) = _updates(rel, "tool_call")
    assert call["title"] == "kirocrew-dashboard_session_status"
    assert len(_methods(rel, "session/request_permission")) == 1


def test_opencode_second_prompt_joins_the_running_turn() -> None:
    """The steer is read by the same turn, and both prompts end together."""
    frames = _frames("opencode/concurrent-prompt-steer-live.jsonl")[1:]
    assert [f.get("id") for f in frames[-2:]] == [3, 4]
    assert all(f["result"]["stopReason"] == "end_turn" for f in frames[-2:])
    reply = "".join(
        u["content"]["text"]
        for u in _updates("opencode/concurrent-prompt-steer-live.jsonl", "agent_message_chunk")
    )
    assert reply.strip() == "first-done pineapple"


def _responses(rel: str) -> dict[int, dict]:
    """Every id-matched response frame in a capture, keyed by request id."""
    return {f["id"]: f for f in _frames(rel)[1:] if "id" in f and "method" not in f}


def test_session_eviction_captures_show_the_id_dying_on_teardown() -> None:
    """Each member: a live request answered, the teardown answered, the SAME id 'not found'."""
    for rel, live_id, teardown_id, gone_id in (
        ("kiro/session-evict-live.jsonl", 3, 4, 5),
        ("kas/session-evict-live.jsonl", 3, 4, 5),
        ("codex/session-evict-live.jsonl", 3, 4, 5),
    ):
        r = _responses(rel)
        assert "error" not in r[live_id], f"{rel}: the session was not live before teardown"
        assert "error" not in r[teardown_id], f"{rel}: the teardown verb was refused"
        gone = r[gone_id]
        assert "error" in gone, f"{rel}: the id still answered after teardown"
        text = json.dumps(gone["error"])
        assert "not found" in text.lower(), f"{rel}: {text}"


def test_client_meta_settings_capture_shows_tool_origin_from_the_client() -> None:
    """KAS read settings.toolSearch.neverDefer: the probe tool_call is toolOrigin client."""
    rel = "kas/client-meta-settings-live.jsonl"
    (call,) = _updates(rel, "tool_call")
    assert (call.get("_meta") or {}).get("kiro", {}).get("toolOrigin") == "client"
    assert call["title"] == "@probe/probe_codeword"
    reply = "".join(u["content"]["text"] for u in _updates(rel, "agent_message_chunk"))
    assert reply.strip() == "tangerine-42"


def test_host_auth_callback_capture_shows_the_engine_asking_for_a_token() -> None:
    """KAS spawned without --auth-method cli sends _kiro/auth/getAccessToken, no secret in it."""
    rel = "kas/host-auth-callback-live.jsonl"
    asks = _methods(rel, "_kiro/auth/getAccessToken")
    assert len(asks) == 1, rel
    assert asks[0].get("params") in ({}, None), "the token request must carry no secret"
    assert "id" in asks[0], "it is a request the client answers, so it carries an id"


def test_spec_servers_off_wire_captures_run_the_spec_server_off_an_empty_array() -> None:
    """kiro --agent off disk and KAS via customAgents: the probe tool ran and returned the codeword."""
    for rel in (
        "kiro/spec-servers-off-wire-live.jsonl",
        "kas/client-meta-settings-live.jsonl",
    ):
        (call,) = _updates(rel, "tool_call")
        assert "probe_codeword" in json.dumps(call), rel
        reply = "".join(u["content"]["text"] for u in _updates(rel, "agent_message_chunk"))
        assert reply.strip() == "tangerine-42", rel


def _info(rel: str, kind: str) -> list[dict]:
    """The ``_meta.kiro`` blob of every KAS ``session_info_update`` of *kind*."""
    out = []
    for u in _updates(rel, "session_info_update"):
        kiro = (u.get("_meta") or {}).get("kiro") or {}
        if kiro.get("kind") == kind:
            out.append(kiro)
    return out


def test_open_external_url_capture_shows_the_consent_url_request() -> None:
    """After a resetServer sign-in, KAS asks the client to open the server's consent URL."""
    rel = "kas/open-external-url-live.jsonl"
    (ask,) = _methods(rel, "_kiro/openExternalUrl")
    assert "id" in ask, "it is a request the client answers, so it carries an id"
    assert set(ask["params"]) == {"url"}
    url = ask["params"]["url"]
    assert "/authorize?" in url and "response_type=code" in url
    assert "code_challenge_method=S256" in url and "redirect_uri=" in url


def test_auto_compaction_capture_shows_the_engine_summarizing_on_its_own() -> None:
    """One prompt, no compact request: KAS sends started then completed, then ends the turn."""
    rel = "kas/auto-compaction-live.jsonl"
    frames = _frames(rel)[1:]
    assert not any(str(f.get("method", "")).startswith("_kiro/session/compact") for f in frames)
    kinds = [
        ((f["params"]["update"].get("_meta") or {}).get("kiro") or {}).get("kind")
        for f in frames
        if (f.get("params") or {}).get("update", {}).get("sessionUpdate") == "session_info_update"
    ]
    started = kinds.index("summarization_started")
    assert kinds[started + 1] == "summarization_completed"
    (done,) = _info(rel, "summarization_completed")
    assert done["summarization"]["status"] == "success"
    assert frames[-1]["result"]["stopReason"] == "end_turn"


def test_report_usage_captures_each_carry_the_turn_bill() -> None:
    """kiro and KAS bill in credits; claude, opencode and goose in prompt-result tokens."""
    rel = "kiro/compaction-live.jsonl"
    metered = [
        e
        for f in _methods(rel, "_kiro.dev/metadata")
        for e in (f["params"].get("meteringUsage") or [])
    ]
    assert metered and all(e["unit"] == "credit" and e["value"] > 0 for e in metered)
    (done,) = _info("kas/turn-usage-live.jsonl", "turn_completion")
    (entry,) = done["promptTurnSummaries"]
    assert entry["unit"] == "credit" and entry["usage"] > 0
    for rel in (
        "claude/session.jsonl",
        "opencode/session-live.jsonl",
        "goose/turn-live.jsonl",
    ):
        results = [r.get("result") or {} for r in _responses(rel).values()]
        ends = [r for r in results if "stopReason" in r]
        assert ends and all(e["usage"]["outputTokens"] > 0 for e in ends), rel
