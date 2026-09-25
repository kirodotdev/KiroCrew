"""Tests for the operator harness descriptor schema, parse, and loader.

These pin the DATA and the two pure operations over it — validation and argv
rendering — plus the ``harnesses.json`` loader's tolerance. The recurring theme
is what must NOT happen: a capability nobody granted (or a kiro-only one), a
routing that silently becomes selectable, an argv that execs an unchecked
program, or a malformed file that costs the boot instead of its own row.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.acp.harness.descriptor import (
    ARGV_PLACEHOLDERS,
    DESCRIPTOR_KEYS,
    HARNESS_ID_MAX_LEN,
    MCP_DELIVERIES,
    MCP_DELIVERY_AGENT_FILE,
    MCP_DELIVERY_DEFAULT,
    MCP_DELIVERY_SESSION_ARRAY,
    MODEL_SOURCE_ACP_ADVERTISED,
    MODEL_SOURCE_STATIC,
    OPERATOR_HARNESSES_LEAF,
    PERMISSION_CONFIG_KEYS,
    ROUTING_AGENT_SPEC,
    ROUTING_SESSION_CONFIG,
    ROUTINGS,
    HarnessDescriptor,
    PermissionConfig,
    descriptor_from_mapping,
    load_operator_descriptors,
    operator_harnesses_path,
    render_argv,
    validate_descriptor,
)


def _operator_descriptor(executable: str = "/opt/tool", **overrides) -> dict:
    payload = {"executable": executable, "argv": ["{executable}", "acp"]}
    payload.update(overrides)
    return payload


# ── Schema surface ──


def test_no_adapter_key_in_the_schema():
    # The `adapter` key is dropped entirely — a descriptor never names code.
    assert "adapter" not in DESCRIPTOR_KEYS


def test_the_schema_key_set_is_exactly_what_is_documented():
    assert DESCRIPTOR_KEYS == frozenset(
        {
            "id",
            "display_name",
            "executable",
            "argv",
            "agent_args",
            "model_args",
            "model_source",
            "models",
            "mcp_delivery",
            "routing",
            "permission_config",
        }
    )


def test_no_capabilities_key_in_the_schema():
    # A descriptor cannot claim a session-path capability: membership in every
    # ``ACP_BACKENDS_*`` set is a code-reviewed literal (harness-parity H6/H7).
    assert "capabilities" not in DESCRIPTOR_KEYS


@pytest.mark.parametrize(
    "claim",
    [
        {"session_mcp_array": True},
        {"harness_owned_sessions": True},
        {"load_without_modes": True},
        {"model_via_config_option": True},
        {"advertised_model_selection": True},
        {"internal_sandbox": True},
        {"session_sharing": True},
    ],
)
def test_a_capabilities_block_is_refused_as_an_unknown_key(claim):
    d, reasons = descriptor_from_mapping(_operator_descriptor(capabilities=claim), harness_id="x")
    assert d is None
    assert any("1 unknown field(s)" in r for r in reasons), reasons
    assert not any("capabilities" in r for r in reasons), reasons  # key names are never echoed


def test_argv_placeholder_vocabulary():
    assert ARGV_PLACEHOLDERS == frozenset({"{executable}", "{agent}", "{model}", "{workdir}"})


def test_routing_and_delivery_vocabularies():
    assert ROUTINGS == frozenset({"agent_spec", "session_config"})
    assert MCP_DELIVERIES == frozenset({"agent_file", "session_array"})
    assert MCP_DELIVERY_DEFAULT == MCP_DELIVERY_AGENT_FILE
    assert PERMISSION_CONFIG_KEYS == frozenset({"option", "value"})


# ── validate_descriptor: identifier rules ──


def test_valid_minimal_descriptor():
    d = HarnessDescriptor(id="my-tool", executable="/opt/tool", argv=("{executable}", "acp"))
    assert validate_descriptor(d) == []


def test_empty_id_is_rejected():
    d = HarnessDescriptor(id="", executable="/opt/tool", argv=("{executable}",))
    assert any("identifier is empty" in r for r in validate_descriptor(d))


@pytest.mark.parametrize("bad_id", ["Upper", "has_underscore", "space bar", "kébab", "a/b"])
def test_id_charset_rules(bad_id):
    d = HarnessDescriptor(id=bad_id, executable="/opt/tool", argv=("{executable}",))
    assert any("lowercase letters, digits, and hyphens" in r for r in validate_descriptor(d))


def test_id_length_cap():
    d = HarnessDescriptor(
        id="a" * (HARNESS_ID_MAX_LEN + 1), executable="/opt/tool", argv=("{executable}",)
    )
    assert any("longer than" in r for r in validate_descriptor(d))


def test_id_at_the_cap_is_allowed():
    d = HarnessDescriptor(
        id="a" * HARNESS_ID_MAX_LEN, executable="/opt/tool", argv=("{executable}",)
    )
    assert validate_descriptor(d) == []


def test_taken_id_is_rejected():
    d = HarnessDescriptor(id="dup", executable="/opt/tool", argv=("{executable}",))
    assert any("already registered" in r for r in validate_descriptor(d, taken_ids=("dup",)))


# ── validate_descriptor: executable + argv ──


def test_empty_executable_is_rejected():
    d = HarnessDescriptor(id="x", executable="", argv=("{executable}",))
    assert any("executable is empty" in r for r in validate_descriptor(d))


def test_empty_argv_is_rejected():
    d = HarnessDescriptor(id="x", executable="/opt/tool", argv=())
    assert any("argv template is empty" in r for r in validate_descriptor(d))


def test_argv_must_start_with_executable_placeholder():
    d = HarnessDescriptor(id="x", executable="/opt/tool", argv=("/opt/tool", "acp"))
    reasons = validate_descriptor(d)
    assert any("argv[0] must be {executable}" in r for r in reasons)
    assert not any("/opt/tool" in r for r in reasons), reasons  # the token is never echoed


def test_unknown_placeholder_is_rejected_by_position_without_echoing_it():
    """The braced text is operator-authored and may be a secret typed as a
    placeholder; the reason names the slot, never the text."""
    d = HarnessDescriptor(
        id="x", executable="/opt/tool", argv=("{executable}", "{sk-live-0123456789}")
    )
    reasons = validate_descriptor(d)
    assert any("argv[1] uses an unknown placeholder" in r for r in reasons)
    assert not any("sk-live" in r for r in reasons), reasons


def test_unbalanced_brace_is_rejected():
    d = HarnessDescriptor(id="x", executable="/opt/tool", argv=("{executable}", "--dir={workdir"))
    assert any("unbalanced brace" in r for r in validate_descriptor(d))


def test_argv_reasons_name_the_token_by_position_never_by_content():
    """A validation reason must not echo an argv literal.

    Reasons are rendered by ``GET /api/backends``, which is deliberately not
    owner-gated, and an argv literal is operator text that may carry a
    credential. A malformed ``--api-key=sk-...{`` token is reported as
    ``argv[1]``; the secret never leaves the file.
    """
    secret = "sk-live-0123456789abcdef"
    d = HarnessDescriptor(
        id="x",
        executable="/opt/tool",
        argv=("{executable}", f"--api-key={secret}{{", f"--token={secret}{{nope}}"),
        model_args=(f"--m={secret}{{agent}}",),
    )
    reasons = validate_descriptor(d)
    assert any("argv[1] has an unbalanced brace" in r for r in reasons)
    assert any("argv[2] uses an unknown placeholder" in r for r in reasons)
    assert not any("nope" in r for r in reasons), reasons
    assert any("model_args[0] uses {agent}" in r for r in reasons)
    assert not any(secret in r for r in reasons), reasons


def test_workdir_and_executable_are_legal_in_argv():
    d = HarnessDescriptor(
        id="x", executable="/opt/tool", argv=("{executable}", "--cwd", "{workdir}")
    )
    assert validate_descriptor(d) == []


def test_model_placeholder_outside_model_args_is_rejected():
    d = HarnessDescriptor(id="x", executable="/opt/tool", argv=("{executable}", "{model}"))
    assert any("only meaningful in model_args" in r for r in validate_descriptor(d))


def test_agent_placeholder_outside_agent_args_is_rejected():
    d = HarnessDescriptor(
        id="x", executable="/opt/tool", argv=("{executable}",), model_args=("--m", "{agent}")
    )
    assert any("only meaningful in agent_args" in r for r in validate_descriptor(d))


def test_convention_placeholders_are_legal_in_their_own_blocks():
    d = HarnessDescriptor(
        id="x",
        executable="/opt/tool",
        argv=("{executable}", "acp"),
        agent_args=("--agent", "{agent}"),
        model_args=("--model", "{model}"),
    )
    assert validate_descriptor(d) == []


def test_bare_string_argv_is_rejected_as_shape():
    # A str is iterable and would otherwise pass every per-token check char-wise.
    d = HarnessDescriptor(id="x", executable="/opt/tool", argv="my-tool acp")  # type: ignore[arg-type]
    reasons = validate_descriptor(d)
    assert any("argv must be a sequence of tokens, not a string" in r for r in reasons)
    # Shape failure short-circuits the placeholder checks for that block.
    assert not any("argv[0] must be {executable}" in r for r in reasons)


# ── validate_descriptor: model_source + models ──


def test_static_without_models_is_rejected():
    d = HarnessDescriptor(
        id="x", executable="/opt/tool", argv=("{executable}",), model_source=MODEL_SOURCE_STATIC
    )
    assert any("no models are declared" in r for r in validate_descriptor(d))


def test_static_with_models_is_valid():
    d = HarnessDescriptor(
        id="x",
        executable="/opt/tool",
        argv=("{executable}",),
        model_source=MODEL_SOURCE_STATIC,
        models=("m1", "m2"),
    )
    assert validate_descriptor(d) == []


def test_unknown_model_source_is_rejected():
    d = HarnessDescriptor(
        id="x", executable="/opt/tool", argv=("{executable}",), model_source="carrier-pigeon"
    )
    assert any("model_source" in r and "is not one of" in r for r in validate_descriptor(d))


def test_acp_advertised_is_the_default_model_source():
    assert HarnessDescriptor(id="x").model_source == MODEL_SOURCE_ACP_ADVERTISED


def test_empty_model_entry_is_rejected():
    d = HarnessDescriptor(
        id="x",
        executable="/opt/tool",
        argv=("{executable}",),
        model_source=MODEL_SOURCE_STATIC,
        models=("",),
    )
    assert any("models[0] is not a non-empty string" in r for r in validate_descriptor(d))


# ── validate_descriptor: mcp_delivery ──


def test_mcp_delivery_default_and_values():
    assert HarnessDescriptor(id="x").mcp_delivery == MCP_DELIVERY_AGENT_FILE
    for delivery in (MCP_DELIVERY_AGENT_FILE, MCP_DELIVERY_SESSION_ARRAY):
        d = HarnessDescriptor(
            id="x", executable="/opt/tool", argv=("{executable}",), mcp_delivery=delivery
        )
        assert validate_descriptor(d) == []


def test_unknown_mcp_delivery_is_rejected():
    d = HarnessDescriptor(
        id="x", executable="/opt/tool", argv=("{executable}",), mcp_delivery="wire_fed"
    )
    assert any("mcp_delivery" in r and "is not one of" in r for r in validate_descriptor(d))


# ── validate_descriptor: routing / permission_config coupling ──


def test_no_routing_is_valid_but_unselectable():
    d = HarnessDescriptor(id="x", executable="/opt/tool", argv=("{executable}",))
    assert validate_descriptor(d) == []
    assert d.selectable is False


def test_agent_spec_routing_is_selectable():
    d = HarnessDescriptor(
        id="x",
        executable="/opt/tool",
        argv=("{executable}",),
        agent_args=("--agent", "{agent}"),
        routing=ROUTING_AGENT_SPEC,
    )
    assert validate_descriptor(d) == []
    assert d.selectable is True


def test_agent_spec_routing_requires_agent_args_that_deliver_the_agent():
    # The mirror of session_config/permission_config: the routing says the
    # permission decision travels as an agent selection on the command line, so
    # a descriptor with nothing that can carry ``{agent}`` would register as
    # selectable and then spawn the default agent with no activation to verify.
    # Empty block, and a block of fixed flags without the placeholder, are the
    # same refusal.
    for agent_args in ((), ("--profile", "review")):
        d = HarnessDescriptor(
            id="x",
            executable="/opt/tool",
            argv=("{executable}",),
            agent_args=agent_args,
            routing=ROUTING_AGENT_SPEC,
        )
        reasons = validate_descriptor(d)
        assert any(
            "routing is 'agent_spec' but agent_args does not carry {agent}" in r for r in reasons
        ), (
            agent_args,
            reasons,
        )


def test_agent_args_without_agent_spec_routing_stays_valid():
    # The coupling is one-directional on this side: an agent_args block is
    # legal on a session_config descriptor (the binary may still take an
    # agent flag); only the CLAIM of agent_spec routing demands the block.
    d = HarnessDescriptor(
        id="x",
        executable="/opt/tool",
        argv=("{executable}",),
        agent_args=("--agent", "{agent}"),
        routing=ROUTING_SESSION_CONFIG,
        permission_config=PermissionConfig(option="permission_mode", value="acceptEdits"),
    )
    assert validate_descriptor(d) == []


def test_session_config_routing_requires_permission_config():
    d = HarnessDescriptor(
        id="x", executable="/opt/tool", argv=("{executable}",), routing=ROUTING_SESSION_CONFIG
    )
    reasons = validate_descriptor(d)
    assert any("no permission_config" in r for r in reasons)


def test_session_config_with_permission_config_is_valid_and_selectable():
    d = HarnessDescriptor(
        id="x",
        executable="/opt/tool",
        argv=("{executable}",),
        routing=ROUTING_SESSION_CONFIG,
        permission_config=PermissionConfig(option="permission_mode", value="acceptEdits"),
    )
    assert validate_descriptor(d) == []
    assert d.selectable is True


def test_permission_config_without_session_config_routing_is_rejected():
    d = HarnessDescriptor(
        id="x",
        executable="/opt/tool",
        argv=("{executable}",),
        agent_args=("--agent", "{agent}"),
        routing=ROUTING_AGENT_SPEC,
        permission_config=PermissionConfig(option="o", value="v"),
    )
    reasons = validate_descriptor(d)
    assert any("only meaningful when routing is 'session_config'" in r for r in reasons)
    assert len(reasons) == 1, reasons


def test_unrecognized_routing_is_rejected_with_the_unselectable_hint():
    d = HarnessDescriptor(
        id="x", executable="/opt/tool", argv=("{executable}",), routing="carrier-pigeon"
    )
    reasons = validate_descriptor(d)
    assert any("routing is not one of" in r for r in reasons)
    assert not any("carrier-pigeon" in r for r in reasons), reasons  # values are never echoed
    assert any("known-but-unselectable" in r for r in reasons)


def test_permission_config_empty_fields_are_rejected():
    d = HarnessDescriptor(
        id="x",
        executable="/opt/tool",
        argv=("{executable}",),
        routing=ROUTING_SESSION_CONFIG,
        permission_config=PermissionConfig(option="", value=""),
    )
    reasons = validate_descriptor(d)
    assert any("permission_config.option is empty" in r for r in reasons)
    assert any("permission_config.value is empty" in r for r in reasons)


# ── descriptor_from_mapping: parse-level rules ──


def test_from_mapping_valid():
    d, reasons = descriptor_from_mapping(_operator_descriptor(), harness_id="my-tool")
    assert reasons == []
    assert d is not None
    assert d.id == "my-tool"


def test_from_mapping_non_object():
    d, reasons = descriptor_from_mapping(["not", "a", "map"], harness_id="x")
    assert d is None
    assert any("must be an object" in r for r in reasons)


def test_from_mapping_unknown_key_is_rejected():
    d, reasons = descriptor_from_mapping(_operator_descriptor(adapter="kiro"), harness_id="x")
    assert d is None
    assert any("1 unknown field(s)" in r for r in reasons)
    assert not any("adapter" in r for r in reasons), reasons


def test_from_mapping_id_mismatch_is_rejected():
    d, reasons = descriptor_from_mapping(_operator_descriptor(id="other"), harness_id="my-tool")
    assert d is None
    assert any("does not match its registry key" in r for r in reasons)


def test_from_mapping_bare_string_argv_is_rejected():
    d, reasons = descriptor_from_mapping(
        {"executable": "/opt/tool", "argv": "my-tool acp"}, harness_id="x"
    )
    assert d is None
    assert any("argv must be an array of strings" in r for r in reasons)


def test_from_mapping_parses_permission_config():
    d, reasons = descriptor_from_mapping(
        _operator_descriptor(
            routing="session_config",
            permission_config={"option": "permission_mode", "value": "acceptEdits"},
        ),
        harness_id="x",
    )
    assert reasons == []
    assert d is not None
    assert d.permission_config == PermissionConfig(option="permission_mode", value="acceptEdits")
    assert d.selectable is True


def test_from_mapping_permission_config_unknown_key_is_rejected():
    d, reasons = descriptor_from_mapping(
        _operator_descriptor(
            routing="session_config",
            permission_config={"option": "o", "value": "v", "extra": 1},
        ),
        harness_id="x",
    )
    assert d is None
    assert any("permission_config has 1 unknown field(s)" in r for r in reasons)
    assert not any("extra" in r for r in reasons), reasons


def test_from_mapping_permission_config_non_object_is_rejected():
    d, reasons = descriptor_from_mapping(
        _operator_descriptor(routing="session_config", permission_config="nope"),
        harness_id="x",
    )
    assert d is None
    assert any("permission_config must be an object" in r for r in reasons)


def test_from_mapping_reasons_are_deduplicated():
    # A malformed static model_source reported by parse and by shape stays once.
    d, reasons = descriptor_from_mapping(
        _operator_descriptor(model_source="static"), harness_id="x"
    )
    assert d is None
    assert len(reasons) == len(set(reasons))


# ── render_argv ──


def test_render_argv_substitutes_executable():
    d = HarnessDescriptor(id="x", executable="/opt/tool", argv=("{executable}", "acp"))
    assert render_argv(d) == ["/opt/tool", "acp"]


def test_render_argv_executable_override():
    d = HarnessDescriptor(id="x", executable="tool", argv=("{executable}", "acp"))
    assert render_argv(d, executable="/resolved/tool") == ["/resolved/tool", "acp"]


def test_render_argv_emits_convention_blocks_only_when_present():
    d = HarnessDescriptor(
        id="x",
        executable="/opt/tool",
        argv=("{executable}", "acp"),
        agent_args=("--agent", "{agent}"),
        model_args=("--model", "{model}"),
    )
    assert render_argv(d) == ["/opt/tool", "acp"]
    assert render_argv(d, agent="dev") == ["/opt/tool", "acp", "--agent", "dev"]
    assert render_argv(d, agent="dev", model="m1") == [
        "/opt/tool",
        "acp",
        "--agent",
        "dev",
        "--model",
        "m1",
    ]


def test_render_argv_is_shell_free_single_element_per_value():
    d = HarnessDescriptor(
        id="x", executable="/opt/tool", argv=("{executable}",), model_args=("--model", "{model}")
    )
    rendered = render_argv(d, model="a; rm -rf / #")
    assert rendered == ["/opt/tool", "--model", "a; rm -rf / #"]


def test_render_argv_single_pass_does_not_recurse_into_substituted_values():
    d = HarnessDescriptor(
        id="x", executable="/opt/tool", argv=("{executable}",), model_args=("{model}",)
    )
    # A model id that literally contains a placeholder is passed through as bytes.
    assert render_argv(d, model="{workdir}", workdir="/w") == ["/opt/tool", "{workdir}"]


# ── loader tolerance ──


def test_loader_missing_file_is_empty(tmp_path):
    valid, invalid = load_operator_descriptors(path=tmp_path / "absent.json")
    assert valid == ()
    assert invalid == ()


def test_loader_malformed_json_is_a_whole_file_invalid_entry(tmp_path):
    p = tmp_path / "harnesses.json"
    p.write_text("{not json", encoding="utf-8")
    valid, invalid = load_operator_descriptors(path=p)
    assert valid == ()
    assert len(invalid) == 1
    harness_id, reasons = invalid[0]
    assert harness_id == ""
    assert any("not valid JSON" in r for r in reasons)


def test_loader_oversized_file_is_a_whole_file_invalid_entry(tmp_path):
    """The read is bounded: a file past the byte cap costs one diagnosable row,
    not a boot-time parse of an arbitrarily large body."""
    from kiro_crew.acp.harness.descriptor import HARNESSES_FILE_MAX_BYTES

    p = tmp_path / "harnesses.json"
    # Valid JSON (so the size, not the syntax, is what refuses it), one byte over.
    filler = "x" * (HARNESSES_FILE_MAX_BYTES - 20)
    p.write_text('{"pad": "' + filler + '"}' + " " * 40, encoding="utf-8")
    assert p.stat().st_size > HARNESSES_FILE_MAX_BYTES
    valid, invalid = load_operator_descriptors(path=p)
    assert valid == ()
    assert invalid[0][0] == ""
    assert any("exceeds" in r and "bytes" in r for r in invalid[0][1]), invalid


def test_loader_too_many_descriptors_is_a_whole_file_invalid_entry(tmp_path):
    """The descriptor count is bounded: one file cannot create an unbounded number
    of registrations, harness objects and diagnostic rows."""
    from kiro_crew.acp.harness.descriptor import HARNESSES_MAX_DESCRIPTORS

    p = tmp_path / "harnesses.json"
    too_many = {
        f"h{i}": {
            "executable": "x",
            "argv": ["{executable}"],
            "agent_args": ["--agent", "{agent}"],
            "routing": "agent_spec",
        }
        for i in range(HARNESSES_MAX_DESCRIPTORS + 1)
    }
    p.write_text(json.dumps(too_many), encoding="utf-8")
    valid, invalid = load_operator_descriptors(path=p)
    assert valid == ()
    assert invalid[0][0] == ""
    assert any("at most" in r for r in invalid[0][1]), invalid
    # Exactly at the cap is fine.
    at_cap = dict(list(too_many.items())[:HARNESSES_MAX_DESCRIPTORS])
    p.write_text(json.dumps(at_cap), encoding="utf-8")
    valid, invalid = load_operator_descriptors(path=p)
    assert len(valid) == HARNESSES_MAX_DESCRIPTORS
    assert invalid == ()


def test_loader_duplicate_key_refuses_the_file_instead_of_keeping_the_last(tmp_path):
    """``json.loads`` alone is last-wins, which would silently serve only the
    second body for a repeated id (and the later value for a repeated field),
    past the reach of the id-uniqueness check. A repeated key at any level is a
    whole-file refusal that says a key repeats -- never WHICH: the key is operator
    text (a pasted secret where an id belongs is the case that matters) and the
    listing that shows the reason is not owner-gated."""
    p = tmp_path / "harnesses.json"
    first = '"dup": {"executable": "/opt/a", "argv": ["{executable}"], "agent_args": ["--agent", "{agent}"], "routing": "agent_spec"}'
    second = '"dup": {"executable": "/opt/b", "argv": ["{executable}"], "agent_args": ["--agent", "{agent}"], "routing": "agent_spec"}'
    p.write_text("{" + first + ", " + second + "}", encoding="utf-8")
    valid, invalid = load_operator_descriptors(path=p)
    assert valid == ()
    assert invalid[0][0] == ""
    assert any("a key is repeated" in r for r in invalid[0][1]), invalid
    assert not any("dup" in r for r in invalid[0][1]), invalid

    # Nested: a descriptor that names ``executable`` twice is refused the same way.
    p.write_text(
        '{"one": {"executable": "/opt/a", "executable": "/opt/b", '
        '"argv": ["{executable}"], "agent_args": ["--agent", "{agent}"], "routing": "agent_spec"}}',
        encoding="utf-8",
    )
    valid, invalid = load_operator_descriptors(path=p)
    assert valid == ()
    assert any("a key is repeated" in r for r in invalid[0][1]), invalid
    assert not any("executable" in r for r in invalid[0][1]), invalid


def test_loader_top_level_not_object_is_invalid(tmp_path):
    p = tmp_path / "harnesses.json"
    p.write_text(json.dumps(["a", "b"]), encoding="utf-8")
    valid, invalid = load_operator_descriptors(path=p)
    assert valid == ()
    assert invalid[0][0] == ""
    assert any("must be a JSON object" in r for r in invalid[0][1])


def test_loader_non_dict_entry_is_listed_by_position_never_by_key(tmp_path):
    """An invalid entry is listed under a synthetic position id and its reasons
    locate it by that position: the operator's key is never repeated. The key
    of an invalid entry is exactly the field that may be wrong -- here a token
    that must not reach the un-gated listing."""
    p = tmp_path / "harnesses.json"
    p.write_text(
        json.dumps({"good": _operator_descriptor(), "sk-live-SECRET": 42}), encoding="utf-8"
    )
    valid, invalid = load_operator_descriptors(path=p)
    assert [d.id for d in valid] == ["good"]
    assert len(invalid) == 1
    assert invalid[0][0] == "#2"
    assert any(r.startswith("entry #2: ") and "must be an object" in r for r in invalid[0][1])
    assert not any("SECRET" in r for r in invalid[0][1]), invalid


def test_loader_parses_valid_entries(tmp_path):
    p = tmp_path / "harnesses.json"
    p.write_text(
        json.dumps(
            {
                "my-tool": _operator_descriptor(
                    display_name="My Tool",
                    agent_args=["--agent", "{agent}"],
                    routing="agent_spec",
                ),
            }
        ),
        encoding="utf-8",
    )
    valid, invalid = load_operator_descriptors(path=p)
    assert invalid == ()
    assert len(valid) == 1
    assert valid[0].id == "my-tool"
    assert valid[0].display_name == "My Tool"
    assert valid[0].selectable is True


def test_loader_rejects_duplicate_ids_in_order(tmp_path):
    # A descriptor carrying an id that collides with an earlier entry's id.
    p = tmp_path / "harnesses.json"
    p.write_text(
        json.dumps(
            {
                "first": _operator_descriptor(),
                "second": _operator_descriptor(id="first"),
            }
        ),
        encoding="utf-8",
    )
    valid, invalid = load_operator_descriptors(path=p)
    assert [d.id for d in valid] == ["first"]
    assert len(invalid) == 1
    assert invalid[0][0] == "#2"
    assert any("declared id does not match" in r for r in invalid[0][1]), invalid


def test_loader_invalid_entry_never_starves_valid_siblings(tmp_path):
    p = tmp_path / "harnesses.json"
    p.write_text(
        json.dumps(
            {
                "broken": {"executable": "/opt/tool"},  # empty argv
                "ok": _operator_descriptor(),
            }
        ),
        encoding="utf-8",
    )
    valid, invalid = load_operator_descriptors(path=p)
    assert [d.id for d in valid] == ["ok"]
    assert invalid[0][0] == "#1"


# ── path resolution ──


def test_operator_harnesses_path_sits_beside_config(monkeypatch, tmp_path):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    from kiro_crew.config import paths as paths_mod

    # Reset the memoized home so the env override takes effect for this test.
    paths_mod._config_dir_memo = None
    resolved = operator_harnesses_path()
    from kiro_crew.config.paths import config_dir

    assert str(resolved) == str(config_dir() / OPERATOR_HARNESSES_LEAF)
    assert str(resolved).endswith(OPERATOR_HARNESSES_LEAF)
