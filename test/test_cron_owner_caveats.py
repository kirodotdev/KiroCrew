"""A cron whose owner cannot outlive it says so at creation, not weeks later.

``cron_add`` stamps the caller's session key as the owner and every mutating tool
then demands an exact match, so two callers write a row they can never manage: a
sub-agent (whose conversation the parent cannot present, and which is reaped) and a
cron running with ``persistent_session=False`` (whose key carries a fresh per-run
id). A third case is the job being created rather than the creator: an agent job
with ``persistent_session=False`` can never satisfy an ownership check on a later
run, including against jobs it scheduled itself.

All three succeeded silently before this change. These tests pin the warning AND
the fact that it is only a warning -- execution is untouched and the durable
callers stay quiet, which is what keeps the noise off the paths that already work.

See issue #8772.
"""

from __future__ import annotations

import uuid

import pytest

from kiro_crew.cron import CronService, cron_job_id_from_session_key, cron_session_key_is_stable
from kiro_crew.mcp_cron import (
    _SCOPED_EMPTY,
    _call_tool_inner,
    _ephemeral_authority_caveat,
    _not_found,
    _owner_unusable_caveat,
)


def _unique_name() -> str:
    return f"test-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _isolate_cron_store(monkeypatch, tmp_path):
    """Route all CronService() instances to tmp_path for test isolation."""
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    monkeypatch.setattr("kiro_crew.mcp_cron.config_dir", lambda: tmp_path)


class TestSessionKeyStabilityPredicate:
    """The two three-segment ``cron:`` keys mean opposite things.

    ``build_cron_session_context`` mints ``cron:<id>:<run_id>`` with a fresh uuid
    per fire (ephemeral); the sequential-agent path in the Slack gateway mints
    ``cron:<id>:<agent>`` for an ``agent_sequence`` job (durable, and it ignores
    ``persistent_session``). Separator counting cannot tell them apart, which is
    why the caveat asks the job record.
    """

    def test_a_default_job_is_stable(self):
        svc = CronService()
        job = svc.add_job(name=_unique_name(), message="hi", every_secs=120)
        assert cron_session_key_is_stable(job) is True

    def test_an_ephemeral_single_agent_job_is_not_stable(self):
        svc = CronService()
        job = svc.add_job(
            name=_unique_name(), message="hi", every_secs=120, persistent_session=False
        )
        assert cron_session_key_is_stable(job) is False

    def test_a_multi_agent_job_is_stable_even_with_persistence_off(self):
        """The regression GPT caught: this shape has 2 colons and is DURABLE."""
        svc = CronService()
        job = svc.add_job(
            name=_unique_name(),
            message="hi",
            every_secs=120,
            persistent_session=False,
            agent_sequence=["alpha", "beta"],
        )
        assert cron_session_key_is_stable(job) is True

    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("cron:abc12345", "abc12345"),
            ("cron:abc12345:run5678", "abc12345"),
            ("cron:abc12345:some-agent", "abc12345"),
            ("dashboard:chat-3-1712793600", ""),
            ("subagent:conv-7", ""),
            ("", ""),
        ],
    )
    def test_job_id_extraction(self, key, expected):
        assert cron_job_id_from_session_key(key) == expected


class TestOwnerUnusableCaveat:
    """Which CALLERS write a row they cannot come back for."""

    def test_a_subagent_caller_is_told_the_parent_cannot_manage_the_job(self):
        out = _owner_unusable_caveat(CronService(), "subagent:conv-7", "abc12345")
        assert "sub-agent's conversation" in out
        # The whole point is that the loss is immediate, not deferred to reaping.
        assert "starting now" in out
        assert "kirocrew cron adopt abc12345" in out

    def test_the_subagent_caveat_tells_the_user_rather_than_the_model_to_adopt(self):
        """``self-protection-cron-adopt`` denies the command to the agent.

        A caveat that told the model to RUN it would dead-end at that gate, so the
        remedy has to be relayed to the only principal allowed to execute it.
        """
        out = _owner_unusable_caveat(CronService(), "subagent:conv-7", "abc12345")
        assert "Tell the user" in out

    def test_an_ephemeral_cron_caller_is_told_its_key_will_never_match_again(self):
        svc = CronService()
        caller = svc.add_job(
            name=_unique_name(), message="hi", every_secs=120, persistent_session=False
        )
        out = _owner_unusable_caveat(svc, f"cron:{caller.id}:run5678", "abc12345")
        assert "never match again" in out
        assert "Tell the user" in out
        assert "kirocrew cron adopt abc12345" in out

    def test_a_multi_agent_cron_caller_is_not_warned(self):
        """The false positive GPT flagged: 2 colons, but a DURABLE per-agent key."""
        svc = CronService()
        caller = svc.add_job(
            name=_unique_name(),
            message="hi",
            every_secs=120,
            persistent_session=False,
            agent_sequence=["alpha", "beta"],
        )
        assert _owner_unusable_caveat(svc, f"cron:{caller.id}:alpha", "abc12345") == ""

    def test_an_unknown_caller_job_is_not_warned(self):
        """No record means no evidence; an unsubstantiated warning is the bug."""
        assert _owner_unusable_caveat(CronService(), "cron:deadbeef:run1", "abc12345") == ""

    @pytest.mark.parametrize(
        "session_key",
        [
            "dashboard:chat-3-1712793600",
            "slack:C123:1712793600.1",
            "",
        ],
    )
    def test_a_durable_caller_gets_no_caveat(self, session_key):
        """The paths that already work must stay silent.

        The empty key never reaches here (``cron_add`` refuses first), and is
        included so a future refactor that reorders those checks trips this instead
        of shipping a caveat with no id in it. The Slack key carries its own colons,
        which an earlier separator-counting implementation would have misread.
        """
        assert _owner_unusable_caveat(CronService(), session_key, "abc12345") == ""

    def test_a_durable_cron_caller_gets_no_caveat(self):
        """Load-bearing: a default cron IS a stable owner.

        Warning here would fire on the single working orchestration pattern -- a
        cron managing jobs it created on an earlier fire.
        """
        svc = CronService()
        caller = svc.add_job(name=_unique_name(), message="hi", every_secs=120)
        assert _owner_unusable_caveat(svc, f"cron:{caller.id}", "abc12345") == ""


class TestEphemeralAuthorityCaveat:
    """Whether the job BEING written can manage crons on a later run."""

    def test_an_ephemeral_agent_job_is_warned(self):
        out = _ephemeral_authority_caveat(persistent=False, is_agent_job=True, sequence_len=0)
        assert "persistent_session is false" in out
        assert "including ones it creates itself" in out

    def test_a_persistent_agent_job_is_not_warned(self):
        assert _ephemeral_authority_caveat(persistent=True, is_agent_job=True, sequence_len=0) == ""

    def test_a_multi_agent_job_is_not_warned(self):
        """That path mints a stable per-agent key and ignores the flag."""
        assert (
            _ephemeral_authority_caveat(persistent=False, is_agent_job=True, sequence_len=2) == ""
        )

    @pytest.mark.parametrize("persistent", [True, False])
    def test_a_command_or_script_job_is_never_warned(self, persistent):
        """The flag cannot produce the bad state for these, so the warning is noise.

        A script cron is launched with ``KIROCREW_SESSION_KEY=cron:<job_id>``
        unconditionally and a command cron issues no MCP call at all.
        """
        assert (
            _ephemeral_authority_caveat(persistent=persistent, is_agent_job=False, sequence_len=0)
            == ""
        )


class TestCronAddSurfacesTheCaveat:
    """End to end through the tool, including that it is NOT a refusal."""

    def test_a_subagent_still_gets_its_job_and_also_gets_told(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "subagent:conv-42")
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        name = _unique_name()

        result = _call_tool_inner("cron_add", {"name": name, "message": "hi", "every": 120})

        # Warned, not refused: the job exists and will fire.
        assert "Added job" in result
        assert "Error" not in result
        assert "sub-agent's conversation" in result
        jobs = [j for j in CronService().list_jobs() if j.name == name]
        assert len(jobs) == 1
        assert jobs[0].session_key == "subagent:conv-42"
        # The caveat names the real id, so the fix is copy-pasteable.
        assert f"kirocrew cron adopt {jobs[0].id}" in result

    def test_an_ephemeral_agent_job_is_flagged_at_creation(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:chat-9")
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        name = _unique_name()

        result = _call_tool_inner(
            "cron_add",
            {"name": name, "message": "hi", "every": 120, "persistent_session": False},
        )

        assert "Added job" in result
        assert "persistent_session is false" in result

    def test_the_ordinary_case_stays_exactly_as_it_was(self, monkeypatch):
        """A chat tab creating a default job gets no extra sentence at all."""
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:chat-9")
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        name = _unique_name()

        result = _call_tool_inner("cron_add", {"name": name, "message": "hi", "every": 120})

        assert "Added job" in result
        assert "Note:" not in result

    def test_both_caveats_can_appear_together(self, monkeypatch):
        """A sub-agent scheduling an ephemeral agent job earns both, not one."""
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "subagent:conv-42")
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        name = _unique_name()

        result = _call_tool_inner(
            "cron_add",
            {"name": name, "message": "hi", "every": 120, "persistent_session": False},
        )

        assert "sub-agent's conversation" in result
        assert "persistent_session is false" in result


class TestCronUpdateSurfacesTheCaveat:
    """The sibling mutation site: the flag is writable here too."""

    def test_flipping_persistence_off_via_update_is_flagged(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:chat-9")
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        name = _unique_name()
        _call_tool_inner("cron_add", {"name": name, "message": "hi", "every": 120})
        job_id = [j for j in CronService().list_jobs() if j.name == name][0].id

        result = _call_tool_inner("cron_update", {"job_id": job_id, "persistent_session": False})

        assert "Updated job" in result
        assert "persistent_session is false" in result

    def test_an_ordinary_update_stays_quiet(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:chat-9")
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        name = _unique_name()
        _call_tool_inner("cron_add", {"name": name, "message": "hi", "every": 120})
        job_id = [j for j in CronService().list_jobs() if j.name == name][0].id

        result = _call_tool_inner("cron_update", {"job_id": job_id, "every": 300})

        assert "Updated job" in result
        assert "Note:" not in result


class TestRefusalsNameTheWayBack:
    def test_not_found_denies_being_proof_of_absence(self):
        out = _not_found("abc12345")
        # Existing callers assert on this substring; keep it.
        assert "job not found" in out.lower()
        assert out.startswith("Error:")
        assert "do not report it as proof the job is gone" in out
        assert "kirocrew cron adopt abc12345" in out

    def test_not_found_asks_the_user_to_adopt(self):
        assert "ask the user to run" in _not_found("abc12345")

    def test_not_found_stays_one_string_for_every_branch(self):
        """No oracle: the message is a pure function of the id it was handed.

        The added sentences hold verbatim whether the id exists, belongs to another
        session, or names an ownerless row -- so two ids must differ only where the
        id itself appears.
        """
        a = _not_found("aaaaaaaa")
        b = _not_found("bbbbbbbb")
        assert a.replace("aaaaaaaa", "X") == b.replace("bbbbbbbb", "X")

    def test_scoped_empty_denies_being_an_empty_registry(self):
        assert "NOT an empty registry" in _SCOPED_EMPTY
        assert "do not report that no cron jobs exist" in _SCOPED_EMPTY
        assert "kirocrew cron adopt" in _SCOPED_EMPTY

    def test_scoped_empty_asks_the_user_to_adopt(self):
        assert "ask the user to run" in _SCOPED_EMPTY

    def test_scoped_empty_still_withholds_the_count(self):
        """Naming the recovery path must not turn into disclosing the volume.

        ``_UNOWNED`` exists because an identified session is not necessarily the
        operator, so "N jobs you may not see" would leak the admin surface's size
        to exactly that principal.
        """
        assert "withheld" not in _SCOPED_EMPTY.lower()
        assert not any(ch.isdigit() for ch in _SCOPED_EMPTY)


class TestAdoptIsNeverAddressedToTheModel:
    """One rule across every string: the agent cannot run ``cron adopt``.

    ``security.py``'s ``self-protection-cron-adopt`` denies it so a session cannot
    assign itself ownership of a scheduled job. Every mention must therefore be
    routed through the user, and this test is what stops a later reword from
    quietly re-pointing one of them at the model.
    """

    def _strings(self):
        svc = CronService()
        ephemeral = svc.add_job(
            name=_unique_name(), message="hi", every_secs=120, persistent_session=False
        )
        return [
            _not_found("abc12345"),
            _SCOPED_EMPTY,
            _owner_unusable_caveat(svc, "subagent:conv-7", "abc12345"),
            _owner_unusable_caveat(svc, f"cron:{ephemeral.id}:run1", "abc12345"),
        ]

    def test_every_adopt_mention_is_user_directed(self):
        for s in self._strings():
            assert "cron adopt" in s, s
            assert ("ask the user" in s.lower()) or ("tell the user" in s.lower()), s
