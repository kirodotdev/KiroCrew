"""Tests for the ACP harness descriptor: shape validation and argv rendering."""

from __future__ import annotations

import dataclasses
import pathlib

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kiro_crew.acp.harness_descriptor import (
    ADAPTERS,
    CAPABILITY_NAMES,
    DESCRIPTOR_KEYS,
    HARNESS_ID_MAX_LEN,
    MCP_DELIVERY_DEFAULT,
    MCP_DELIVERY_FILE_FED,
    MCP_DELIVERY_WIRE_FED,
    MODEL_SOURCE_ACP_ADVERTISED,
    MODEL_SOURCE_STATIC,
    PLACEHOLDER_AGENT,
    PLACEHOLDER_EXECUTABLE,
    PLACEHOLDER_MODEL,
    PLACEHOLDER_WORKDIR,
    CapabilitySet,
    HarnessDescriptor,
    capability_names,
    descriptor_from_mapping,
    render_argv,
    validate_descriptor,
)

# A descriptor in the shape the bundled kiro-cli entry will take: base argv plus
# both optional convention blocks.
KIRO_LIKE = HarnessDescriptor(
    id="kiro",
    display_name="Kiro CLI",
    executable="kiro-cli",
    argv=("{executable}", "acp"),
    agent_args=("--agent", "{agent}"),
    model_args=("--model", "{model}"),
    mcp_delivery=MCP_DELIVERY_FILE_FED,
)


# ── Defaults ──


def test_capabilities_all_default_disabled():
    caps = CapabilitySet()
    assert caps.as_dict() == {name: False for name in CAPABILITY_NAMES}
    assert all(not caps.has(name) for name in CAPABILITY_NAMES)


def test_capability_names_match_the_dataclass_fields():
    assert capability_names() == CAPABILITY_NAMES


def test_unknown_capability_lookup_raises_rather_than_answering_false():
    with pytest.raises(ValueError, match="unknown harness capability"):
        CapabilitySet().has("teleportation")


def test_minimal_mapping_gets_safe_defaults():
    desc, reasons = descriptor_from_mapping(
        {"executable": "my-tool", "argv": ["{executable}", "acp"]},
        harness_id="my-tool",
    )
    assert reasons == []
    assert desc is not None
    assert desc.mcp_delivery == MCP_DELIVERY_DEFAULT == MCP_DELIVERY_WIRE_FED
    assert desc.model_source == MODEL_SOURCE_ACP_ADVERTISED
    assert desc.capabilities == CapabilitySet()
    assert desc.models == ()
    assert desc.agent_args == ()
    assert desc.model_args == ()
    # Absent display name falls back to the id so a listing is never blank.
    assert desc.label == "my-tool"


def test_capabilities_are_opt_in_one_at_a_time():
    desc, reasons = descriptor_from_mapping(
        {
            "executable": "my-tool",
            "argv": ["{executable}"],
            "capabilities": {"steer": True},
        },
        harness_id="my-tool",
    )
    assert reasons == []
    assert desc is not None
    assert desc.capabilities.has("steer") is True
    assert desc.capabilities.has("internal_sandbox") is False


# ── Validation reasons ──


def test_unknown_field_is_a_diagnosable_reason():
    desc, reasons = descriptor_from_mapping(
        {"executable": "x", "argv": ["{executable}"], "adaptor": "kiro"},
        harness_id="x",
    )
    assert desc is None
    assert any("'adaptor'" in r for r in reasons)


def test_unknown_placeholder_names_the_placeholder_and_the_token():
    desc, reasons = descriptor_from_mapping(
        {"executable": "x", "argv": ["{executable}", "--home={home}"]},
        harness_id="x",
    )
    assert desc is None
    assert any("{home}" in r and "--home={home}" in r for r in reasons)


def test_unbalanced_brace_is_rejected():
    desc, reasons = descriptor_from_mapping(
        {"executable": "x", "argv": ["{executable}", "--dir={workdir"]},
        harness_id="x",
    )
    assert desc is None
    assert any("unbalanced brace" in r for r in reasons)


def test_placeholder_reasons_cover_every_template_block():
    desc, reasons = descriptor_from_mapping(
        {
            "executable": "x",
            "argv": ["{executable}"],
            "agent_args": ["{persona}"],
            "model_args": ["{llm}"],
        },
        harness_id="x",
    )
    assert desc is None
    assert any("agent_args" in r and "{persona}" in r for r in reasons)
    assert any("model_args" in r and "{llm}" in r for r in reasons)


def test_model_placeholder_in_argv_is_rejected():
    # {model} in the ungated argv block would render to an empty argument when no
    # model is pinned (e.g. "--model=" or a bare ""), a silent spawn footgun. It
    # is only legal in model_args, whose emission is gated on a model value.
    desc, reasons = descriptor_from_mapping(
        {"executable": "x", "argv": ["{executable}", "--model={model}"]},
        harness_id="x",
    )
    assert desc is None
    assert any("{model}" in r and "model_args" in r for r in reasons)


def test_agent_placeholder_in_argv_is_rejected():
    desc, reasons = descriptor_from_mapping(
        {"executable": "x", "argv": ["{executable}", "{agent}"]},
        harness_id="x",
    )
    assert desc is None
    assert any("{agent}" in r and "agent_args" in r for r in reasons)


def test_convention_placeholders_are_rejected_in_the_other_conventions_block():
    # {agent} belongs to agent_args and {model} to model_args; each is rejected
    # in the other's block.
    desc, reasons = descriptor_from_mapping(
        {
            "executable": "x",
            "argv": ["{executable}"],
            "agent_args": ["--agent", "{model}"],
            "model_args": ["--model", "{agent}"],
        },
        harness_id="x",
    )
    assert desc is None
    assert any("agent_args" in r and "{model}" in r for r in reasons)
    assert any("model_args" in r and "{agent}" in r for r in reasons)


def test_convention_placeholders_are_legal_in_their_own_block():
    # {agent} in agent_args and {model} in model_args — where emission is gated —
    # validate cleanly, as every bundled descriptor uses them.
    desc, reasons = descriptor_from_mapping(
        {
            "executable": "x",
            "argv": ["{executable}", "acp"],
            "agent_args": ["--agent", "{agent}"],
            "model_args": ["--model", "{model}"],
        },
        harness_id="x",
    )
    assert desc is not None, reasons
    assert reasons == []


def test_executable_and_workdir_placeholders_are_legal_in_argv():
    # The ungated placeholders carry no per-value gating and stay legal anywhere.
    desc, reasons = descriptor_from_mapping(
        {"executable": "x", "argv": ["{executable}", "--cwd={workdir}"]},
        harness_id="x",
    )
    assert desc is not None, reasons
    assert reasons == []


@pytest.mark.parametrize(
    "bad_id",
    ["Kiro", "my_tool", "my tool", "", "kiro!", "ki.ro", "a" * (HARNESS_ID_MAX_LEN + 1)],
)
def test_identifier_charset_and_length_are_enforced(bad_id):
    reasons = validate_descriptor(
        HarnessDescriptor(id=bad_id, executable="x", argv=("{executable}",))
    )
    assert any("identifier" in r for r in reasons)


def test_identifier_uniqueness_is_reported_against_taken_ids():
    reasons = validate_descriptor(
        HarnessDescriptor(id="kiro", executable="x", argv=("{executable}",)),
        taken_ids={"kiro"},
    )
    assert any("already registered" in r for r in reasons)


def test_empty_executable_is_reported():
    reasons = validate_descriptor(HarnessDescriptor(id="x", argv=("{executable}",)))
    assert any("executable is empty" in r for r in reasons)


def test_empty_argv_template_is_reported():
    reasons = validate_descriptor(HarnessDescriptor(id="x", executable="x"))
    assert any("argv template is empty" in r for r in reasons)


@pytest.mark.parametrize(
    "raw,needle",
    [
        ({"capabilities": {"warp_drive": True}}, "unknown capability"),
        ({"capabilities": {"steer": "true"}}, "must be true or false"),
        ({"capabilities": ["steer"]}, "capabilities must be an object"),
        ({"model_source": "guess"}, "model_source"),
        ({"mcp_delivery": "carrier_pigeon"}, "mcp_delivery"),
        ({"argv": "my-tool acp"}, "argv must be an array of strings"),
        ({"models": [3]}, "models entry"),
        ({"display_name": 7}, "display_name must be a string"),
    ],
)
def test_field_shape_problems_are_reported_not_guessed(raw, needle):
    payload = {"executable": "x", "argv": ["{executable}"], **raw}
    desc, reasons = descriptor_from_mapping(payload, harness_id="x")
    assert desc is None
    assert any(needle in r for r in reasons), reasons


def test_declared_id_must_match_its_registry_key():
    desc, reasons = descriptor_from_mapping(
        {"id": "other", "executable": "x", "argv": ["{executable}"]},
        harness_id="x",
    )
    assert desc is None
    assert any("does not match its registry key" in r for r in reasons)


def test_every_reason_names_the_harness_it_belongs_to():
    _, reasons = descriptor_from_mapping({"argv": ["{nope}"]}, harness_id="my-tool")
    assert reasons
    assert all("my-tool" in r for r in reasons)


def test_a_string_capability_value_never_enables_the_flag():
    # "false" is truthy in Python; guessing here would enable a capability the
    # operator meant to disable.
    desc, reasons = descriptor_from_mapping(
        {"executable": "x", "argv": ["{executable}"], "capabilities": {"steer": "false"}},
        harness_id="x",
    )
    assert desc is None
    assert reasons


def test_the_literal_adapter_key_is_not_an_operator_field():
    # A bundled descriptor may name a reviewed Python entry point; configuration
    # must never be able to. The literal key is rejected as an unknown field, so
    # extending DESCRIPTOR_KEYS cannot open this path without failing here.
    assert "adapter" not in DESCRIPTOR_KEYS
    desc, reasons = descriptor_from_mapping(
        {"executable": "x", "argv": ["{executable}"], "adapter": "kiro"},
        harness_id="x",
    )
    assert desc is None
    assert any("'adapter'" in r for r in reasons)


def test_a_parsed_descriptor_never_carries_an_adapter():
    desc, reasons = descriptor_from_mapping(
        {"executable": "x", "argv": ["{executable}"]}, harness_id="x"
    )
    assert reasons == []
    assert desc is not None
    assert desc.adapter == ""


@pytest.mark.parametrize("block", ["argv", "agent_args", "model_args", "models"])
def test_a_bare_string_sequence_is_rejected_on_a_code_built_descriptor(block):
    # A string is iterable, so every per-token check passes by iterating ONE
    # CHARACTER AT A TIME and the descriptor validates — then renders
    # ["m", "y", "-", ...] and dies at exec. The parse path already refuses the
    # shape; this is the same refusal for a descriptor built in code.
    fields_ = {"id": "x", "executable": "x", "argv": ("{executable}",)}
    fields_[block] = "my-tool acp"
    reasons = validate_descriptor(HarnessDescriptor(**fields_))
    assert any(f"{block} must be a sequence" in r for r in reasons), reasons


def test_a_static_model_source_with_no_models_is_rejected():
    # 'static' declares "here are my models"; an empty list leaves the composer
    # with nothing to offer and no way to obtain anything, so the harness could
    # never serve a session.
    reasons = validate_descriptor(
        HarnessDescriptor(
            id="x", executable="x", argv=("{executable}",), model_source=MODEL_SOURCE_STATIC
        )
    )
    assert any("no models are declared" in r for r in reasons)

    desc, reasons = descriptor_from_mapping(
        {"executable": "x", "argv": ["{executable}"], "model_source": MODEL_SOURCE_STATIC},
        harness_id="x",
    )
    assert desc is None
    assert any("no models are declared" in r for r in reasons)


def test_a_static_model_source_with_models_is_accepted():
    reasons = validate_descriptor(
        HarnessDescriptor(
            id="x",
            executable="x",
            argv=("{executable}",),
            model_source=MODEL_SOURCE_STATIC,
            models=("m1",),
        )
    )
    assert reasons == []


def test_an_unknown_adapter_is_rejected():
    reasons = validate_descriptor(
        HarnessDescriptor(id="x", executable="x", argv=("{executable}",), adapter="teleporter")
    )
    assert any("adapter" in r for r in reasons)


@pytest.mark.parametrize("adapter", sorted(ADAPTERS))
def test_every_declared_adapter_validates(adapter):
    reasons = validate_descriptor(
        HarnessDescriptor(id="x", executable="x", argv=("{executable}",), adapter=adapter)
    )
    assert reasons == []


@pytest.mark.parametrize("garbage", [None, "kiro-cli acp", 7, [], ["argv"], True])
def test_non_object_descriptor_is_a_reason_not_an_exception(garbage):
    desc, reasons = descriptor_from_mapping(garbage, harness_id="x")
    assert desc is None
    assert any("must be an object" in r for r in reasons)


# ── Argv rendering ──


def test_render_substitutes_executable_agent_and_model():
    assert render_argv(KIRO_LIKE, agent="default", model="auto") == [
        "kiro-cli",
        "acp",
        "--agent",
        "default",
        "--model",
        "auto",
    ]


def test_render_omits_the_whole_block_for_an_empty_value():
    assert render_argv(KIRO_LIKE, agent="default") == [
        "kiro-cli",
        "acp",
        "--agent",
        "default",
    ]
    assert render_argv(KIRO_LIKE, model="auto") == ["kiro-cli", "acp", "--model", "auto"]
    assert render_argv(KIRO_LIKE) == ["kiro-cli", "acp"]


def test_render_prefers_the_resolved_executable_over_the_descriptor_rule():
    argv = render_argv(KIRO_LIKE, executable="/opt/kiro/bin/kiro-cli")
    assert argv[0] == "/opt/kiro/bin/kiro-cli"


def test_render_accepts_a_path_workdir_and_embeds_it_in_a_token():
    descriptor = HarnessDescriptor(id="x", executable="x", argv=("{executable}", "--cwd={workdir}"))
    workdir = pathlib.Path("/tmp/work dir")
    argv = render_argv(descriptor, workdir=workdir)
    # Build the expectation from the SAME Path object: str(Path) renders the
    # platform's own separator (``/tmp/work dir`` on POSIX, ``\tmp\work dir`` on
    # Windows), and the token embeds exactly that, so a literal would only match
    # on POSIX.
    assert argv == ["x", f"--cwd={workdir}"]


def test_a_descriptor_without_a_model_convention_gets_no_substituted_default():
    descriptor = HarnessDescriptor(id="x", executable="x", argv=("{executable}", "acp"))
    assert render_argv(descriptor, model="some-model") == ["x", "acp"]


def test_substituted_values_are_not_rescanned_for_placeholders():
    # A model id containing another placeholder must reach exec as literal bytes.
    argv = render_argv(KIRO_LIKE, model="{workdir}", workdir="/secret")
    assert argv == ["kiro-cli", "acp", "--model", "{workdir}"]


def test_render_leaves_an_unknown_placeholder_alone_rather_than_raising():
    # Validation rejects such a descriptor; rendering stays total so a spawn
    # path can never be handed an exception mid-flight.
    descriptor = HarnessDescriptor(id="x", executable="x", argv=("{executable}", "{home}"))
    assert render_argv(descriptor) == ["x", "{home}"]


# ── Property 1: rendering is deterministic, shell-free, and total ──

_LITERALS = st.text(
    alphabet=st.characters(exclude_characters="{}", exclude_categories=("Cs",)),
    min_size=1,
    max_size=8,
)


def _block_tokens(placeholders):
    """Tokens legal in one template block: literals plus only the placeholders
    that block permits.

    ``{agent}``/``{model}`` are convention placeholders legal ONLY in their own
    gated block (``agent_args`` / ``model_args``); the ungated ``{executable}``
    and ``{workdir}`` are legal everywhere. Drawing per-block keeps the generator
    inside the validator's accepted space so ``test_generated_descriptors_are_valid``
    pins the real shape rather than a superset the validator now rejects.
    """
    ph = st.sampled_from(sorted(placeholders))
    return st.one_of(
        _LITERALS,
        ph,
        st.builds(lambda lit, p: f"{lit}={p}", _LITERALS, ph),
    )


_ARGV_TOKENS = _block_tokens({PLACEHOLDER_EXECUTABLE, PLACEHOLDER_WORKDIR})
_AGENT_TOKENS = _block_tokens({PLACEHOLDER_EXECUTABLE, PLACEHOLDER_WORKDIR, PLACEHOLDER_AGENT})
_MODEL_TOKENS = _block_tokens({PLACEHOLDER_EXECUTABLE, PLACEHOLDER_WORKDIR, PLACEHOLDER_MODEL})
_IDS = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=HARNESS_ID_MAX_LEN
)
# Values a hostile or merely awkward operator/model name can take. Every one of
# these would be re-interpreted by a shell, so they are the interesting inputs
# for "argv is a list, never a command line".
_SHELL_HOSTILE = st.sampled_from(
    [
        "a b",
        "; rm -rf /",
        "$(whoami)",
        "`id`",
        "&& echo pwned",
        "| cat /etc/passwd",
        "'quoted'",
        '"quoted"',
        "new\nline",
        "tab\there",
        "*",
        "~",
        "$HOME",
        ">out",
    ]
)
_VALUES = st.one_of(
    _SHELL_HOSTILE,
    st.text(alphabet=st.characters(exclude_categories=("Cs",)), min_size=1, max_size=10),
)


@st.composite
def _descriptors(draw):
    model_source = draw(st.sampled_from([MODEL_SOURCE_ACP_ADVERTISED, MODEL_SOURCE_STATIC]))
    # A 'static' source with no models fails validation (it could never serve a
    # model to the composer), so the generator pairs them — otherwise the
    # properties below would be exercising invalid descriptors.
    min_models = 1 if model_source == MODEL_SOURCE_STATIC else 0
    return HarnessDescriptor(
        id=draw(_IDS),
        display_name=draw(st.text(max_size=10)),
        executable=draw(
            st.text(alphabet=st.characters(exclude_characters="{}"), min_size=1, max_size=10)
        ),
        # argv[0] must be the executable placeholder — the attested executable is
        # the one that has to exec — so the generator pins it and varies the tail.
        argv=("{executable}", *draw(st.lists(_ARGV_TOKENS, max_size=3))),
        agent_args=tuple(draw(st.lists(_AGENT_TOKENS, max_size=3))),
        model_args=tuple(draw(st.lists(_MODEL_TOKENS, max_size=3))),
        capabilities=CapabilitySet(**{name: draw(st.booleans()) for name in CAPABILITY_NAMES}),
        model_source=model_source,
        models=tuple(
            draw(st.lists(st.text(min_size=1, max_size=8), min_size=min_models, max_size=3))
        ),
        mcp_delivery=draw(st.sampled_from([MCP_DELIVERY_FILE_FED, MCP_DELIVERY_WIRE_FED])),
        adapter=draw(st.sampled_from(["", *sorted(ADAPTERS)])),
    )


@given(_descriptors())
def test_generated_descriptors_are_valid(descriptor):
    # Pins the generator to the validator: if a shape the generator produces
    # ever stops validating, the properties below are testing the wrong space.
    assert validate_descriptor(descriptor) == []


@given(_descriptors(), _VALUES, _VALUES, _VALUES)
def test_rendering_is_deterministic(descriptor, agent, model, workdir):
    first = render_argv(descriptor, agent=agent, model=model, workdir=workdir)
    second = render_argv(descriptor, agent=agent, model=model, workdir=workdir)
    assert first == second
    assert isinstance(first, list)
    assert all(isinstance(token, str) for token in first)


@given(_descriptors(), _VALUES, _VALUES)
def test_rendering_never_splits_or_interprets_a_value(descriptor, agent, model):
    argv = render_argv(descriptor, agent=agent, model=model)
    # One template token renders to exactly one argv element: no value can grow
    # the argument count by containing a space, a pipe, or a substitution.
    expected = len(descriptor.argv) + len(descriptor.agent_args) + len(descriptor.model_args)
    assert len(argv) == expected
    # A bare placeholder token carries the value verbatim — unquoted, unescaped,
    # and unexpanded.
    for token, rendered in zip(descriptor.argv, argv):
        if token == "{agent}":
            assert rendered == agent
        elif token == "{model}":
            assert rendered == model


@given(_descriptors(), _VALUES)
def test_an_empty_model_omits_the_whole_model_block(descriptor, agent):
    without_model = render_argv(descriptor, agent=agent, model="")
    base = render_argv(dataclasses.replace(descriptor, model_args=()), agent=agent)
    # No dangling flag: the argv is exactly what the same descriptor with no
    # model convention at all would produce.
    assert without_model == base


@given(
    st.recursive(
        st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=6)),
        lambda children: st.one_of(
            st.lists(children, max_size=3),
            st.dictionaries(st.text(max_size=6), children, max_size=3),
        ),
        max_leaves=6,
    )
)
def test_parsing_arbitrary_config_never_raises(raw):
    # An operator descriptor is untrusted config; a malformed one costs that
    # harness its listing, never the gateway's boot.
    descriptor, reasons = descriptor_from_mapping(raw, harness_id="probe")
    assert (descriptor is None) == bool(reasons)


# ── Code-only capabilities are refused from configuration (review finding) ──
# Local review (2026-09-02, GPT + Opus lanes, both blocking): every name in
# CAPABILITY_NAMES was config-grantable, so an operator descriptor could claim
# internal_sandbox (Kiro Crew's own sandbox waived for a binary with no internal
# one — the process runs UNCONFINED), acp_runtime_pool (routed onto AcpRuntime,
# which speaks kiro-cli's wire dialect), session_sharing (a foreign process
# multiplexed across trust contexts), or kiro_identity_store (sessions retired
# on a login store the harness never reads). All four are honoured by code
# written for specific bundled harnesses; a config grant points that machinery
# at a process that never earned it.

_CODE_ONLY_CAPABILITIES = (
    "internal_sandbox",
    "acp_runtime_pool",
    "session_sharing",
    "kiro_identity_store",
)


@pytest.mark.parametrize("capability", _CODE_ONLY_CAPABILITIES)
def test_code_only_capabilities_cannot_be_granted_from_config(capability):
    desc, reasons = descriptor_from_mapping(
        {
            "executable": "x",
            "argv": ["{executable}"],
            "capabilities": {capability: True},
        },
        harness_id="my-tool",
    )
    assert desc is None
    assert any(
        capability in r and "cannot be granted from configuration" in r for r in reasons
    ), reasons


@pytest.mark.parametrize("capability", _CODE_ONLY_CAPABILITIES)
def test_code_only_capabilities_are_refused_even_as_false(capability):
    # The key's PRESENCE is the claim; accepting the spelling at all invites
    # flipping it, and a dropped-but-accepted key would read as granted.
    desc, reasons = descriptor_from_mapping(
        {
            "executable": "x",
            "argv": ["{executable}"],
            "capabilities": {capability: False},
        },
        harness_id="my-tool",
    )
    assert desc is None
    assert reasons


def test_operator_grantable_capabilities_still_parse():
    # The refusal is scoped: capabilities an operator adapter can genuinely
    # implement stay grantable.
    desc, reasons = descriptor_from_mapping(
        {
            "executable": "x",
            "argv": ["{executable}"],
            "capabilities": {"steer": True, "reasoning_effort": True, "mcp_tool_search": True},
        },
        harness_id="my-tool",
    )
    assert reasons == []
    assert desc is not None
    assert desc.capabilities.has("steer")
    assert desc.capabilities.has("reasoning_effort")


def test_the_ungrantable_set_only_names_real_capabilities():
    # A retired/renamed capability lingering here would silently stop gating.
    from kiro_crew.acp.harness_descriptor import _CONFIG_UNGRANTABLE_CAPABILITIES

    for name in _CONFIG_UNGRANTABLE_CAPABILITIES:
        assert name in CAPABILITY_NAMES
