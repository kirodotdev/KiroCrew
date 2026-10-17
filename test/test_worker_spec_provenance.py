"""The derivation refuses to overwrite a ``kirocrew-worker.json`` it did not write.

The mirror path is a NAME, and a name can be claimed. A crew shared through the
Fargate runtime installs its own spec into ``~/.kiro/agents``, and the crew the first
deployment ships is called ``kirocrew-worker``. A freshness gate that reasoned from
existence alone would find a file not matching the default spec's generation, judge it
a stale mirror, re-derive it, and leave the shared crew's prompt and tool surface gone
with nothing raised and nothing logging the crew's name.

The installer's namespace (``crew-<name>.json``, see the container suite) keeps the
crew off this path in the first place. This file covers the other half: the derivation
attributes a file before replacing it, so provenance rather than existence decides.
That holds for a spec placed by hand, by an older exporter, or by a future installer
bug -- none of which the namespace covers.

What must NOT break is ordinary operation, so the positive controls are here beside the
refusals: a real mirror is still re-derived, and a broken file at that path is still
replaced rather than becoming a permanent refusal.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import agent
from kiro_crew.agent_files import AGENT_FILENAME, WORKER_AGENT_FILENAME

CREW_PROMPT = "I am the shared crew's own prompt, and no derivation may replace it."


def _default_spec() -> dict[str, Any]:
    return {
        "name": "kirocrew",
        "description": "the default agent",
        "prompt": "the default prompt",
        "tools": ["fs_read"],
        "allowedTools": ["fs_read"],
        "mcpServers": {},
    }


def _crew_spec(name: str = "kirocrew-worker") -> dict[str, Any]:
    """A shared crew's spec, as the container installs one: its own prompt and servers."""
    return {
        "name": name,
        "description": "a crew somebody shared",
        "prompt": CREW_PROMPT,
        "tools": ["fs_read"],
        "allowedTools": [],
        "mcpServers": {"the-crews-own-server": {"command": "x", "args": []}},
    }


def _write(path: Path, spec: dict[str, Any]) -> None:
    path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")


@pytest.fixture()
def agents_dir(tmp_path, monkeypatch) -> Path:
    """A throwaway agents dir with the default spec installed, as a real host has."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    _write(tmp_path / AGENT_FILENAME, _default_spec())
    return tmp_path


class TestForeignSpecIsNotOverwritten:
    def test_the_spawn_gate_refuses_a_crew_spec_parked_on_the_mirror(self, agents_dir) -> None:
        """The reproduction from the issue, inverted into a pin.

        One call to ``require_fresh_derived_spec`` is enough to reach the write, so it
        must refuse, and the file must still be the crew's afterwards.
        """
        victim = agents_dir / WORKER_AGENT_FILENAME
        _write(victim, _crew_spec())
        before = victim.read_text(encoding="utf-8")

        with pytest.raises(agent.ForeignAgentSpec) as caught:
            agent.require_fresh_derived_spec("kirocrew-worker", None)

        assert victim.read_text(encoding="utf-8") == before
        assert CREW_PROMPT in victim.read_text(encoding="utf-8")
        message = str(caught.value)
        assert str(victim) in message
        assert "did not write" in message

    def test_the_installer_refuses_the_same_file(self, agents_dir) -> None:
        """The boot path, which is the other way the derivation reaches that write.

        Guarding only the spawn gate would leave the boot installer overwriting the
        crew on the next task start, which is when the crew is least able to complain.
        """
        victim = agents_dir / WORKER_AGENT_FILENAME
        _write(victim, _crew_spec())
        before = victim.read_text(encoding="utf-8")

        with pytest.raises(agent.ForeignAgentSpec):
            agent._install_worker_agent()

        assert victim.read_text(encoding="utf-8") == before

    def test_a_spec_declaring_some_other_agent_is_also_refused(self, agents_dir) -> None:
        """A misnamed spec at that path is somebody's file too, whatever it declares."""
        victim = agents_dir / WORKER_AGENT_FILENAME
        _write(victim, _crew_spec(name="acme-support"))

        with pytest.raises(agent.ForeignAgentSpec) as caught:
            agent._install_worker_agent()
        assert "acme-support" in str(caught.value)

    def test_the_refusal_names_the_crew_namespace_in_the_log(self, agents_dir, caplog) -> None:
        """At ERROR, because the boot installer's caller swallows the exception at debug.

        Without the log line the one event an operator needs -- a shared crew occupying
        the mirror's filename -- is invisible at any ordinary level, and silence is
        the whole failure this guard exists to end.
        """
        _write(agents_dir / WORKER_AGENT_FILENAME, _crew_spec())
        with caplog.at_level("ERROR", logger="kiro_crew.agent"):
            with pytest.raises(agent.ForeignAgentSpec):
                agent._install_worker_agent()
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert WORKER_AGENT_FILENAME in logged
        assert "crew-kirocrew-worker.json" in logged

    @pytest.mark.parametrize("bad", [1, True, {"a": 1}, "fs_read"])
    def test_a_malformed_list_field_refuses_rather_than_raising(self, agents_dir, bad) -> None:
        """A spec whose ``tools`` is not a list must reach a REFUSAL, never a TypeError.

        The attribution iterates those fields, and a ``TypeError`` out of it is not a
        ``DerivedSpecStale``: the spawn-path handlers catch that class by name, so the
        session would end on an unhandled error instead of declining the dispatch, and
        the boot installer would leave the mirror unwritten with no verdict at all. A
        value of the wrong type carries no reference to the work server, so the file is
        judged foreign -- which is the right answer for a hand-edited spec, and is a
        verdict rather than a crash.
        """
        victim = agents_dir / WORKER_AGENT_FILENAME
        spec = _crew_spec()
        spec["tools"] = bad
        _write(victim, spec)
        before = victim.read_text(encoding="utf-8")

        with pytest.raises(agent.ForeignAgentSpec):
            agent._install_worker_agent()
        assert victim.read_text(encoding="utf-8") == before

    def test_a_mirror_whose_work_reference_is_only_a_grant_is_still_ours(self, agents_dir) -> None:
        """The mark is a REFERENCE, wherever a release put it.

        A mirror left by an older build carries ``@kirocrew-work`` in ``tools`` and a
        per-tool grant in ``allowedTools`` without mounting the server in
        ``mcpServers``. Requiring the mount would refuse those files instead of healing
        them, which is the upgrade path ``test_worker_agent.py`` pins.
        """
        mirror = agents_dir / WORKER_AGENT_FILENAME
        _write(
            mirror,
            {
                "name": "kirocrew-worker",
                "description": "an older build's description",
                "prompt": "an older build's prompt",
                "tools": ["fs_read", "@kirocrew-work"],
                "allowedTools": ["@kirocrew-work/work_brief"],
                "mcpServers": {},
            },
        )
        agent._install_worker_agent()
        assert (
            json.loads(mirror.read_text(encoding="utf-8"))["prompt"] == agent._WORKER_SYSTEM_PROMPT
        )

    def test_the_refusal_is_a_derived_spec_stale_so_the_spawn_declines(self) -> None:
        """Every spawn-path caller catches ``DerivedSpecStale`` by name.

        A sibling exception class would reach ``acp/client.py``, ``acp/runtime.py`` and
        ``acp/harness/kas.py`` as an unhandled error and END the session rather than
        decline the spawn. The subclassing is what makes the refusal a refusal, so it
        is pinned rather than left to a reader to notice.
        """
        assert issubclass(agent.ForeignAgentSpec, agent.DerivedSpecStale)


class TestOrdinaryDerivationStillWorks:
    def test_a_real_mirror_is_rederived(self, agents_dir) -> None:
        """The positive control. A guard that refused everything would pass the tests above.

        The mirror written by the derivation itself must be replaceable, so the spawn
        gate can still repair a mirror that predates a trust revocation.
        """
        agent._install_worker_agent()
        mirror = agents_dir / WORKER_AGENT_FILENAME
        assert mirror.is_file()
        first = json.loads(mirror.read_text(encoding="utf-8"))
        assert first["name"] == "kirocrew-worker"

        # The default spec changes out of band, the way a writer other than the rebuild
        # changes it. ``tools`` is the surface read here because the mirror copies a
        # plain tool name through unfiltered: ``allowedTools`` passes the governance
        # ceiling, which on a host with no grants configured withholds everything the
        # default listed, and an ``@server`` ref can be an opt-in set the mirror drops
        # on purpose. Neither would tell a re-derive from a refusal.
        probe = "a_tool_the_default_lists"
        assert probe not in first["tools"]
        changed = _default_spec()
        changed["tools"] = ["fs_read", probe]
        _write(agents_dir / AGENT_FILENAME, changed)

        agent._install_worker_agent()
        second = json.loads(mirror.read_text(encoding="utf-8"))
        assert probe in second["tools"]

    def test_the_spawn_gate_still_passes_on_a_fresh_mirror(self, agents_dir) -> None:
        agent._install_worker_agent()
        snapshot = agent.require_fresh_derived_spec("kirocrew-worker", None)
        assert snapshot is not None
        assert snapshot.spec is not None
        assert snapshot.spec["name"] == "kirocrew-worker"

    def test_an_unparseable_file_at_the_mirror_path_is_replaced(self, agents_dir) -> None:
        """A broken file is nobody's work, and refusing it would be permanent.

        A truncated write at that path must not leave the worker undispatchable for
        good: there is no provenance to protect and no operator action that would be
        obvious. A bundle's spec is digest-verified before install and therefore
        parses, so this case cannot be a crew.
        """
        mirror = agents_dir / WORKER_AGENT_FILENAME
        mirror.write_text('{"name": "kirocrew-worker", "prompt": ', encoding="utf-8")

        agent._install_worker_agent()

        assert json.loads(mirror.read_text(encoding="utf-8"))["name"] == "kirocrew-worker"

    def test_an_absent_mirror_is_written(self, agents_dir) -> None:
        assert not (agents_dir / WORKER_AGENT_FILENAME).exists()
        agent._install_worker_agent()
        assert (agents_dir / WORKER_AGENT_FILENAME).is_file()
