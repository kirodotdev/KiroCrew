"""The installed crew spec lives in the crew namespace, and the front addresses it there.

An install at ``<agents>/<crew_name>.json`` would collide with Kiro Crew's own
agent-spec derivation, which owns three of those names. The crew this deployment ships
is called ``kirocrew-worker``, so such an install lands ON the mirror
``rebuild_agent_config`` derives from the default spec, and the next spawn-path
re-derivation replaces the shared crew's prompt and tools with Kiro Crew's own -- no
error, no log line naming the crew.

Two properties are pinned here, and they are one fix rather than two:

* the install goes to ``crew-<crew_name>.json`` and DECLARES ``crew-<crew_name>``,
  because the declared name is what dispatches -- kiro-cli and Kiro Crew's snapshot of
  dispatchable agents both enumerate by it, so namespacing only the filename leaves the
  spec reachable under no id at all;
* the front forwards that SAME id, so the process that installs the spec and the
  process that addresses it cannot drift apart. A deployment where they disagree is
  precisely the silent-default failure ``bundle.py`` exists to prevent, and it would
  pass every other test in this suite.

The reserved-namespace pin at the bottom is the third copy of a rule the container
cannot import: ``crew-`` must stay free of the names Kiro Crew manages.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from container import common
from container.common import ConfigError
from container.front.app import _forward_body, judge_addressed_crew
from container.supervisor import bundle as bundle_mod

from .test_supervisor_bundle import build_bundle, make_settings

# The crew that broke: the deployed name, which is also the mirror's own.
COLLIDING_CREW = "kirocrew-worker"


def _installed(agents: Path) -> list[str]:
    return sorted(path.name for path in agents.glob("*.json"))


def test_the_installed_spec_lands_in_the_crew_namespace(tmp_path: Path) -> None:
    """Filename AND declared name carry the prefix; nothing else about the spec moves."""
    bundle_dir = build_bundle(tmp_path, crew_name="frontdesk")
    settings = make_settings(tmp_path, crew_name="frontdesk")
    agents = tmp_path / "agents"

    bundle_mod.install_bundle(settings, agents_dir=agents)

    assert _installed(agents) == ["crew-frontdesk.json"]
    installed = json.loads((agents / "crew-frontdesk.json").read_text(encoding="utf-8"))
    assert installed["name"] == "crew-frontdesk"

    # Every other key is the bundle's own. The name is the ONE field the install
    # rewrites, so a second rewrite creeping in -- a stripped prompt, a filtered tool
    # list -- would be a crew that is not the one the operator shared.
    shipped = json.loads((bundle_dir / "agent.json").read_text(encoding="utf-8"))
    assert set(installed) == set(shipped)
    assert {k: v for k, v in installed.items() if k != "name"} == {
        k: v for k, v in shipped.items() if k != "name"
    }


def test_the_crew_named_after_the_mirror_does_not_land_on_the_mirror(tmp_path: Path) -> None:
    """The reproduction from the issue, as a pin: ``kirocrew-worker`` is now installable.

    The deployed crew IS called this -- its ECR repository, its secret and its
    cloud.json block all carry the name -- so the fix had to keep the name legal rather
    than refuse it. What must not exist afterwards is a file at the mirror's path.
    """
    build_bundle(tmp_path, crew_name=COLLIDING_CREW)
    settings = make_settings(tmp_path, crew_name=COLLIDING_CREW)
    agents = tmp_path / "agents"

    payload = bundle_mod.install_bundle(settings, agents_dir=agents)

    assert _installed(agents) == ["crew-kirocrew-worker.json"]
    assert not (agents / "kirocrew-worker.json").exists()
    assert not (agents / "kirocrew.json").exists()
    # The crew keeps its own name everywhere the operator sees it; only the agent id is
    # namespaced.
    assert payload["crew_name"] == COLLIDING_CREW


def test_the_front_addresses_the_id_the_installer_wrote(tmp_path: Path) -> None:
    """The coordination this fix rests on, measured across both processes at once.

    Route 3 costs a coordinated change: whatever asks for the agent must ask for the
    file that was installed. So this reads the installed spec off disk and compares it
    with what the front puts on the wire, rather than asserting a literal in each half
    -- two literals agree until one of them is edited.
    """
    build_bundle(tmp_path, crew_name=COLLIDING_CREW)
    settings = make_settings(tmp_path, crew_name=COLLIDING_CREW)
    agents = tmp_path / "agents"

    bundle_mod.install_bundle(settings, agents_dir=agents)
    (installed_path,) = list(agents.glob("*.json"))
    declared = json.loads(installed_path.read_text(encoding="utf-8"))["name"]

    body, _, _ = _forward_body(
        {"model": COLLIDING_CREW, "messages": [{"role": "user", "content": "hi"}]},
        COLLIDING_CREW,
    )
    assert body["model"] == declared == installed_path.stem
    # And it is NOT the bare crew name, which is what resolved to the mirror.
    assert body["model"] != COLLIDING_CREW


def test_a_crew_name_too_long_for_the_namespace_is_refused_at_install(tmp_path: Path) -> None:
    """A name that cannot become an agent id fails the BOOT, not every turn.

    Kiro Crew caps a dispatchable agent name at 64 characters. Without this check the
    task boots reporting a healthy install and then answers 400 to every request,
    which is a fault with no visible cause on the only surface an operator reads.
    """
    over = "c" * (common.MAX_CREW_AGENT_ID_LEN - len(common.CREW_AGENT_ID_PREFIX) + 1)
    build_bundle(tmp_path, crew_name=over)
    settings = make_settings(tmp_path, crew_name=over)
    agents = tmp_path / "agents"

    with pytest.raises(ConfigError) as caught:
        bundle_mod.install_bundle(settings, agents_dir=agents)
    message = str(caught.value)
    assert "crew agent id fits the name grammar" in message
    assert str(common.MAX_CREW_AGENT_ID_LEN) in message
    assert _installed(agents) == []


def test_the_longest_name_that_does_fit_still_installs(tmp_path: Path) -> None:
    """The boundary from the other side, so the refusal cannot be an off-by-one ban."""
    longest = "c" * (common.MAX_CREW_AGENT_ID_LEN - len(common.CREW_AGENT_ID_PREFIX))
    build_bundle(tmp_path, crew_name=longest)
    settings = make_settings(tmp_path, crew_name=longest)
    agents = tmp_path / "agents"

    bundle_mod.install_bundle(settings, agents_dir=agents)
    assert _installed(agents) == [f"crew-{longest}.json"]


def test_the_crew_namespace_is_free_of_every_name_kirocrew_manages() -> None:
    """The two copies of the rule, pinned together.

    The container installs no ``kiro_crew``, so it cannot import the filenames the
    derivation owns; it mirrors the prefix instead, the way it already mirrors the
    agents-dir resolver. What makes the mirror sound is that ``crew-`` collides with
    nothing on the other side -- so this test imports both and says so. A managed spec
    added tomorrow under a ``crew-`` name reddens here rather than silently taking a
    crew's filename back.

    The length cap is mirrored the same way and checked against the grammar itself, so
    a widened agent-name regex does not leave the installer refusing names Kiro Crew
    would accept.
    """
    from kiro_crew import agent_files, validation

    managed = [Path(name).stem for name in agent_files.OWNED_KIRO_AGENT_FILES]
    assert managed, "control: the owned-filenames list must not be empty"
    offenders = [
        name
        for name in managed + sorted(agent_files.KAS_RESERVED_AGENT_IDS)
        if name.startswith(common.CREW_AGENT_ID_PREFIX)
    ]
    assert offenders == [], (
        f"{offenders} sit inside the {common.CREW_AGENT_ID_PREFIX!r} namespace reserved "
        "for installed crew specs, so a crew of that name would collide with it"
    )

    # The mirrored cap is the grammar's, read off the pattern rather than restated.
    at_cap = "c" * common.MAX_CREW_AGENT_ID_LEN
    assert validation.is_registered_agent_name(at_cap)
    assert not validation.is_registered_agent_name(at_cap + "c")


# --- the namespace does not leave this process ------------------------------
#
# The front sends the crew's agent id as ``model`` and the backend echoes the value it
# was given, so without a translation on the way out a customer reads back an id this
# deployment answers 404 for. The round trip is the property, so these read the
# RESPONSE side against ``judge_addressed_crew`` rather than against a literal.


def test_a_completion_carries_the_name_the_customer_addressed(tmp_path: Path) -> None:
    from container.front import backend as backend_mod

    crew = "acme-support"
    upstream = json.dumps(
        {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "model": common.crew_agent_id(crew),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
        }
    ).encode("utf-8")

    rewritten = json.loads(backend_mod._with_customer_model(upstream, crew))
    assert rewritten["model"] == crew
    # Everything else is the backend's own answer.
    assert rewritten["choices"] == json.loads(upstream)["choices"]
    assert rewritten["id"] == "chatcmpl-1"

    # And what comes back is an address this deployment serves, which is the point.
    addressed, refusal = judge_addressed_crew({"model": rewritten["model"]}, crew)
    assert refusal is None and addressed == crew


def test_a_body_the_rewriter_does_not_understand_is_relayed_verbatim() -> None:
    from container.front import backend as backend_mod

    for payload in (b"not json at all", b"[1, 2, 3]", b'{"model": 7}', b"{}"):
        assert backend_mod._with_customer_model(payload, "acme-support") == payload


def test_a_streamed_chunk_carries_the_name_the_customer_addressed() -> None:
    from container.front import stream as stream_mod

    crew = "acme-support"
    chunk = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "model": common.crew_agent_id(crew),
        "choices": [{"index": 0, "delta": {"content": "hi"}}],
    }
    frame = b"data: " + json.dumps(chunk).encode("utf-8")

    projected = stream_mod.project_frame(frame, crew_name=crew)
    assert projected is not None
    payload = json.loads(projected.decode("utf-8").split("data: ", 1)[1])
    assert payload["model"] == crew
    assert payload["choices"] == chunk["choices"]
    addressed, refusal = judge_addressed_crew({"model": payload["model"]}, crew)
    assert refusal is None and addressed == crew


def test_the_projection_still_drops_what_it_dropped_before() -> None:
    """The rewrite rides on the safety verdict; it must not become a way past it."""
    from container.front import stream as stream_mod

    crew = "acme-support"
    unsafe = [
        b"event: tool_result\ndata: " + json.dumps({"model": "x", "secret": 1}).encode(),
        b"data: not json",
        b'data: {"object": "something.else", "model": "x"}',
        b"data: [1, 2, 3]",
    ]
    for frame in unsafe:
        assert stream_mod.project_frame(frame, crew_name=crew) is None
    # The frames that were always safe still pass, unchanged where there is nothing to
    # rewrite: a keepalive and the terminal sentinel carry no model.
    assert stream_mod.project_frame(b": keepalive", crew_name=crew) == b": keepalive\n\n"
    assert stream_mod.project_frame(b"data: [DONE]", crew_name=crew) == b"data: [DONE]\n\n"


def test_the_non_streamed_relay_calls_the_model_rewriter(tmp_path: Path, monkeypatch) -> None:
    """The relay itself, not just the rewriter: a call site that skips it is the bug.

    ``_with_customer_model`` can be correct while ``forward_completion`` hands back
    ``resp.content``, and that is the shape the customer would actually meet. So this
    drives the relay with a backend answer that carries the agent id and reads the
    Response it returns.
    """
    import asyncio

    from container.front import backend as backend_mod

    from ._settings_helper import make_settings

    crew = "crew1"
    settings = make_settings(tmp_path, crew=crew)
    monkeypatch.setattr(backend_mod, "_read_secret", lambda _s: "secret")

    upstream = json.dumps(
        {
            "object": "chat.completion",
            "model": common.crew_agent_id(crew),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
        }
    ).encode("utf-8")

    class _Resp:
        status_code = 200
        content = upstream
        headers: dict[str, str] = {"content-type": "application/json"}

    class _Client:
        async def post(self, *a, **k):
            return _Resp()

    response = asyncio.run(backend_mod.forward_completion(_Client(), settings, {"messages": []}))
    assert json.loads(response.body)["model"] == crew
