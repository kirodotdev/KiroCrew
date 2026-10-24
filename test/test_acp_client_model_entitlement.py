"""Tests for ACP client: model entitlement preflight, the cold-cache fallback and the set_model
context rebase.
"""

from unittest.mock import AsyncMock

import pytest
from acp_client_helpers import _native_projection_for_fake_processes  # noqa: F401 - fixture
from acp_client_helpers import _pin_the_installed_kiro_cli_version  # noqa: F401 - fixture

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import AcpPromptStats


class TestSetModelRebasesContextStats:
    """A mid-session set_model must re-anchor the context-meter stats.

    Without the re-anchor set_model leaves
    last_prompt_stats untouched, so the old model's window (and its
    authoritative context_tokens_from_usage flag) survived the switch. When
    the new model streams only contextUsagePercentage metadata (kiro 2.10+),
    the stale True gated _backfill_context_window forever — pct updated but
    the token text stayed scaled to the OLD model's window.
    """

    def _client(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._session_id = "s1"
        client._send_request = AsyncMock()
        return client

    def _usage_msg(self, used, size):
        from kiro_crew.acp.types import JsonRpcMessage

        return JsonRpcMessage(
            method="session/update",
            params={"update": {"sessionUpdate": "usage_update", "used": used, "size": size}},
        )

    @pytest.mark.asyncio
    async def test_rebases_window_and_clears_usage_flag(self, tmp_path, monkeypatch):
        import kiro_crew.acp.client as c

        monkeypatch.setattr(c.model_registry, "has_known_window", lambda mid: True)
        monkeypatch.setattr(c.model_registry, "model_window", lambda mid, **kw: 272_000)
        client = self._client(tmp_path)
        # Old model reported authoritative counts: 100K / 1M = 10%.
        client._track_usage_update(self._usage_msg(100_000, 1_000_000))
        assert client.last_prompt_stats.context_tokens_from_usage is True

        await client.set_model("gpt-5.6-sol")

        stats = client.last_prompt_stats
        assert stats.context_window_tokens == 272_000
        assert stats.context_used_tokens == 100_000  # transcript unchanged
        assert stats.context_pct == round(100_000 / 272_000 * 100, 1)
        # The old model's usage_update does not describe the session.
        assert stats.context_tokens_from_usage is False

    @pytest.mark.asyncio
    async def test_rebases_to_the_substituted_models_window(self, tmp_path, monkeypatch):
        """The meter describes what is serving, not what was asked for.

        A config-option advisory can substitute the model a switch actually
        got. Keying the rebase on the requested id showed that model's window
        against the substitute's token counts, so the percentage was wrong for
        exactly the sessions that were silently moved.
        """
        import kiro_crew.acp.client as c

        windows = {"gpt-5.6-sol": 272_000, "substitute-model": 128_000}
        monkeypatch.setattr(c.model_registry, "has_known_window", lambda mid: mid in windows)
        monkeypatch.setattr(c.model_registry, "model_window", lambda mid, **kw: windows[mid])
        client = self._client(tmp_path)
        client._track_usage_update(self._usage_msg(100_000, 1_000_000))

        async def _substitute(method, params):
            client._last_substitution_model = "substitute-model"
            return 1

        client._send_request = _substitute

        await client.set_model("gpt-5.6-sol")

        assert client._resolved_model_id == "substitute-model"
        assert client.last_prompt_stats.context_window_tokens == 128_000

    @pytest.mark.asyncio
    async def test_post_switch_metadata_backfills_new_window(self, tmp_path, monkeypatch):
        """Load-bearing: with the rebase reverted, context_tokens_from_usage
        stays True and this metadata pct would be ignored, leaving the window
        at the OLD model's 1M forever."""
        import kiro_crew.acp.client as c
        from kiro_crew.acp.types import JsonRpcMessage

        monkeypatch.setattr(c.model_registry, "has_known_window", lambda mid: True)
        monkeypatch.setattr(c.model_registry, "model_window", lambda mid, **kw: 272_000)
        client = self._client(tmp_path)
        client._track_usage_update(self._usage_msg(100_000, 1_000_000))

        await client.set_model("gpt-5.6-sol")
        client._track_metadata(
            JsonRpcMessage(
                method="_kiro.dev/metadata",
                params={"contextUsagePercentage": 50.0},
            )
        )

        stats = client.last_prompt_stats
        assert stats.context_window_tokens == 272_000
        assert stats.context_used_tokens == 136_000  # 50% of the NEW window
        assert stats.context_pct == 50.0

    @pytest.mark.asyncio
    async def test_unknown_window_zeroes_out(self, tmp_path, monkeypatch):
        """A registry miss must not keep the old window: zero it so downstream
        consumers fall back to their own model-derived value."""
        import kiro_crew.acp.client as c

        monkeypatch.setattr(c.model_registry, "has_known_window", lambda mid: False)
        client = self._client(tmp_path)
        client._track_usage_update(self._usage_msg(100_000, 1_000_000))

        await client.set_model("mystery-model")

        stats = client.last_prompt_stats
        assert stats.context_window_tokens == 0
        # The old model's pct must not survive either — it would ship in the
        # model-switch reset broadcast as a claim about the unknown window.
        assert stats.context_pct == 0.0
        assert stats.context_tokens_from_usage is False

    def test_rebase_clamps_pct_when_used_exceeds_new_window(self):
        """Shrinking the window below used must clamp pct to 100, not overflow."""
        stats = AcpPromptStats(
            context_used_tokens=500_000,
            context_window_tokens=1_000_000,
            context_pct=50.0,
            context_tokens_from_usage=True,
        )
        stats.rebase_to_window(272_000)
        assert stats.context_pct == 100.0
        assert stats.context_window_tokens == 272_000
        assert stats.context_tokens_from_usage is False


class TestModelEntitlementPreflight:
    """An unusable model is stopped BEFORE the wire, not explained afterwards.

    A post-hoc rejection is readable and terminal. These cover the
    turn never failing in the first place: the advertised set is known at
    session/new, so a model the account cannot run has no business being sent.
    """

    @staticmethod
    def _client(advertised, model, is_claude=False):
        from kiro_crew.acp.client import ACP_BACKEND_CLAUDE, AcpClient

        client = AcpClient()
        client._session_id = "sess-1"
        client._model = model
        # _is_claude is derived from the backend seam, not settable directly.
        client._acp_backend = ACP_BACKEND_CLAUDE if is_claude else ""
        client._available_models = [{"modelId": m, "name": m} for m in advertised]

        # The refusal and the withhold first re-ask entitlement on a throwaway
        # probe process. Held to a FAILED probe here (no evidence), so these pin
        # the snapshot's own verdict and never launch a real kiro-cli -- a host
        # with one installed would otherwise answer with its own account's list.
        async def _no_probe_evidence():
            return [], 0.0

        client._probe_advertised_models = _no_probe_evidence
        return client

    @pytest.mark.parametrize(
        ("advertised", "asked", "unusable"),
        [
            pytest.param(
                ["claude-sonnet-4.6"], "claude-opus-4.8", True, id="unadvertised_model_is_unusable"
            ),
            pytest.param(
                ["claude-sonnet-4.6", "claude-opus-4.8"],
                "claude-opus-4.8",
                False,
                id="advertised_model_is_usable",
            ),
            # Empty advertised set means "unknowable", never "nothing is allowed":
            # a backend that omits ``models`` must not have every model withheld.
            pytest.param([], "claude-opus-4.8", False, id="unknown_entitlement_allows_the_send"),
            pytest.param(
                ["Claude-Opus-4.8"],
                "  claude-opus-4.8 ",
                False,
                id="match_is_case_and_space_insensitive",
            ),
        ],
    )
    def test_model_is_unusable(self, advertised, asked, unusable):
        client = self._client(advertised, "claude-opus-4.8")
        assert client._model_is_unusable(asked) is unusable

    @pytest.mark.asyncio
    async def test_startup_withholds_unusable_model(self):
        """The screenshot case: a stale default the account cannot run.

        Nothing goes on the wire and the session is recorded as running the
        backend default, so the turn proceeds instead of dying three retries in.
        """
        from kiro_crew.acp.client import DEFAULT_MODEL

        client = self._client(["claude-sonnet-4.6"], "claude-opus-4.8")
        client._resolved_model_id = "claude-sonnet-4.6"
        sent = []
        client._send_request = _record(sent)

        await client._apply_startup_model()

        assert sent == []
        # Not left holding the id we declined -- the warm-pool re-apply path
        # reads this and would re-offer it on every claim.
        assert client._model == DEFAULT_MODEL
        assert client._resolved_model_id == "claude-sonnet-4.6"

    @pytest.mark.asyncio
    async def test_startup_withhold_records_the_pin_as_refused(self):
        """The withheld pin never ran, so it is recorded in ``model_pin_refused``,
        the field a caller that bills or labels a turn by the pin reads."""
        client = self._client(["claude-sonnet-4.6"], "claude-opus-4.8")
        client._send_request = _record([])

        await client._apply_startup_model()

        assert client.model_pin_refused == "claude-opus-4.8"

    @pytest.mark.asyncio
    async def test_startup_auto_keeps_the_backend_default(self):
        from kiro_crew.acp.client import DEFAULT_MODEL

        client = self._client(["gpt-5.6-terra"], DEFAULT_MODEL)
        client._resolved_model_id = "gpt-5.6-terra"
        sent = []
        client._send_request = _record(sent)

        await client._apply_startup_model()

        assert sent == []
        assert client._resolved_model_id == "gpt-5.6-terra"

    @pytest.mark.asyncio
    async def test_startup_still_applies_a_usable_model(self):
        client = self._client(["gpt-5.6-terra", "gpt-6-astra"], "gpt-6-astra")
        client._resolved_model_id = "gpt-5.6-terra"
        sent = []
        client._send_request = _record(sent)

        await client._apply_startup_model()

        assert len(sent) == 1
        assert sent[0][1]["modelId"] == "gpt-6-astra"
        assert client._model == "gpt-6-astra"
        assert client._resolved_model_id == "gpt-6-astra"
        assert client.model_pin_refused == ""

    @pytest.mark.asyncio
    async def test_startup_resolves_namespaced_pin_to_advertised_spelling(self):
        """A stale `<namespace>::` qualifier on a fully served model.

        The pin was stored when a catalog advertised the qualified spelling;
        this session advertises the bare id. The wire must send the ADVERTISED
        spelling — running the model the pin names — rather than withholding
        and silently dropping to the backend default.
        """
        client = self._client(["z-ai/glm-5.3-flash"], "openrouter::z-ai/glm-5.3-flash")
        sent = []
        client._send_request = _record(sent)

        await client._apply_startup_model()

        assert len(sent) == 1
        assert sent[0][1]["modelId"] == "z-ai/glm-5.3-flash"
        # _model records what the session actually runs (the warm-pool re-apply
        # path reads it), so it must hold the resolved spelling, not the
        # qualified pin.
        assert client._model == "z-ai/glm-5.3-flash"

    @pytest.mark.asyncio
    async def test_startup_still_withholds_namespaced_pin_absent_when_peeled(self):
        """The resolve is not a rubber stamp: absent under BOTH spellings withholds."""
        from kiro_crew.acp.client import DEFAULT_MODEL

        client = self._client(["z-ai/glm-5.3-flash"], "openrouter::no-such-model")
        sent = []
        client._send_request = _record(sent)

        await client._apply_startup_model()

        assert sent == []
        assert client._model == DEFAULT_MODEL

    @pytest.mark.asyncio
    async def test_startup_leaves_claude_backend_alone(self):
        """The claude backend advertises BARE ids while _model is prefixed.

        Comparing the two namespaces would call every legitimate model unusable,
        so that backend keeps its own session/new substitution advisory.
        """
        client = self._client(
            ["claude-opus-4-8[1m]"],
            "global.anthropic.claude-opus-4-8[1m]",
            is_claude=True,
        )
        applied = []

        async def _set_config_option(config_id, value):
            applied.append((config_id, value))

        client.set_config_option = _set_config_option

        await client._apply_startup_model()

        assert applied == [("model", "global.anthropic.claude-opus-4-8[1m]")]
        assert client._resolved_model_id == "global.anthropic.claude-opus-4-8[1m]"

    @pytest.mark.asyncio
    async def test_startup_attributes_config_option_substitution_to_served_model(self):
        client = self._client(
            ["claude-opus-4-8[1m]"],
            "global.anthropic.claude-opus-4-8[1m]",
            is_claude=True,
        )
        sent = []

        async def _send_request(method, params):
            sent.append((method, params))
            return 1

        async def _wait_for_response(req_id, timeout=0.0):
            client._last_substitution_model = "global.anthropic.claude-sonnet-4-6[1m]"
            return {}

        client._send_request = _send_request
        client._wait_for_response = _wait_for_response

        await client._apply_startup_model()

        assert sent == [
            (
                "session/set_config_option",
                {
                    "sessionId": "sess-1",
                    "configId": "model",
                    "value": "global.anthropic.claude-opus-4-8[1m]",
                },
            )
        ]
        assert client._resolved_model_id == "global.anthropic.claude-sonnet-4-6[1m]"

    @pytest.mark.asyncio
    async def test_startup_override_does_not_inherit_a_stale_substitution(self):
        """An advisory belongs to the request that emitted it.

        A substitution recorded by an earlier switch survives a process reset
        or resume. Reading it after the startup override, without clearing it
        first, attributes the session to a model this dispatch never got.
        """
        client = self._client(
            ["claude-opus-4-8[1m]"],
            "global.anthropic.claude-opus-4-8[1m]",
            is_claude=True,
        )
        client._last_substitution_model = "global.anthropic.claude-sonnet-4-6[1m]"

        async def _send_request(method, params):
            return 1

        async def _wait_for_response(req_id, timeout=0.0):
            return {}  # this dispatch carries no advisory of its own

        client._send_request = _send_request
        client._wait_for_response = _wait_for_response

        await client._apply_startup_model()

        assert client._resolved_model_id == "global.anthropic.claude-opus-4-8[1m]"
        assert client._last_substitution_model is None

    @pytest.mark.asyncio
    async def test_clean_explicit_switch_does_not_reuse_startup_substitution(self):
        client = self._client(
            ["claude-opus-4-8[1m]"],
            "global.anthropic.claude-opus-4-8[1m]",
            is_claude=True,
        )
        waits = 0

        async def _send_request(method, params):
            return 1

        async def _wait_for_response(req_id, timeout=0.0):
            nonlocal waits
            waits += 1
            if waits == 1:
                client._last_substitution_model = "global.anthropic.claude-sonnet-4-6[1m]"
            return {}

        client._send_request = _send_request
        client._wait_for_response = _wait_for_response

        await client._apply_startup_model()
        assert client._resolved_model_id == "global.anthropic.claude-sonnet-4-6[1m]"

        await client.set_model("global.anthropic.claude-opus-4-8[1m]")

        assert client._model == "global.anthropic.claude-opus-4-8[1m]"
        assert client._resolved_model_id == "global.anthropic.claude-opus-4-8[1m]"
        assert client._last_substitution_model is None

    @pytest.mark.asyncio
    async def test_explicit_switch_is_refused_not_downgraded(self):
        """An explicit pick gets an error, because silence would misreport it."""
        from kiro_crew.acp.client import AcpModelUnavailable

        client = self._client(["claude-sonnet-4.6", "claude-haiku-4.5"], "claude-sonnet-4.6")
        sent = []
        client._send_request = _record(sent)

        with pytest.raises(AcpModelUnavailable) as excinfo:
            await client.set_model("claude-opus-4.8")

        assert sent == []
        msg = str(excinfo.value)
        assert "claude-opus-4.8" in msg
        assert "not available on your account" in msg
        # The actionable part: what they CAN pick.
        assert "claude-sonnet-4.6" in msg
        assert "claude-haiku-4.5" in msg
        # The identity hint stays CONDITIONAL and read-only. This error also
        # reaches users who really are on a free tier and for whom picking an
        # advertised model is the whole fix, so it must not read as an
        # instruction to re-authenticate: `whoami` only reports which tier is
        # signed in, and no destructive step is ever named.
        assert "if you expected" in msg.lower()
        assert "kiro-cli whoami" in msg
        assert "logout" not in msg.lower()
        assert "kiro-cli login" not in msg
        # Terminal, and EXPLICITLY so -- None would send the retry layer back to
        # string-matching, which is what produced the retry rows.
        assert excinfo.value.transient is False

    @pytest.mark.asyncio
    async def test_explicit_switch_to_advertised_model_works(self):
        client = self._client(["claude-sonnet-4.6", "claude-opus-4.8"], "claude-sonnet-4.6")
        sent = []
        client._send_request = _record(sent)

        await client.set_model("claude-opus-4.8")

        assert len(sent) == 1
        assert client._model == "claude-opus-4.8"

    @pytest.mark.asyncio
    async def test_explicit_switch_attributes_config_option_substitution_to_served_model(self):
        client = self._client(
            ["claude-opus-4-8[1m]"],
            "global.anthropic.claude-opus-4-8[1m]",
            is_claude=True,
        )
        sent = []

        async def _send_request(method, params):
            sent.append((method, params))
            return 1

        async def _wait_for_response(req_id, timeout=0.0):
            client._last_substitution_model = "global.anthropic.claude-sonnet-4-6[1m]"
            return {}

        client._send_request = _send_request
        client._wait_for_response = _wait_for_response

        await client.set_model("global.anthropic.claude-opus-4-8[1m]")

        assert sent == [
            (
                "session/set_config_option",
                {
                    "sessionId": "sess-1",
                    "configId": "model",
                    "value": "global.anthropic.claude-opus-4-8[1m]",
                },
            )
        ]
        assert client._model == "global.anthropic.claude-opus-4-8[1m]"
        assert client._resolved_model_id == "global.anthropic.claude-sonnet-4-6[1m]"


def _record(sink):
    """An async _send_request stub that records calls and returns a request id."""

    async def _send_request(method, params=None):
        sink.append((method, params or {}))
        return 1

    return _send_request


class TestColdCacheModelFallback:
    """The cold-cache push twin of TestModelEntitlementPreflight.

    ``resolve_wire_model_id`` folds onto the advertised spelling only when the
    advertised cache is warm. On the first-ever session that cache is empty,
    so a prefixed ``[1m]`` id reached the wire verbatim and the adapter
    rejected it — resetting the session and dropping the user onto the default
    with no explanation (the ``Invalid value for config option model:
    global.anthropic.claude-sonnet-4-6[1m]`` report). The push helper derives
    fallback spellings from the id itself and lets the adapter judge each.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in this module calls monkeypatch.undo()
    def _cold_advertised_cache(self, monkeypatch):
        """Pin the cache these tests are ABOUT to the state their name claims.

        ``_apply_startup_model`` folds the id onto the advertised spelling through
        ``model_registry.resolve_wire_model_id`` before it pushes anything, and
        the advertised set is a module global another test on the same xdist
        worker may have warmed (``claude-sonnet-4-6`` advertised). Warm, the fold
        collapses ``global.anthropic.claude-sonnet-4-6[1m]`` to the served
        spelling and the adapter sees ONE push instead of the three fallbacks --
        a 1-in-5 failure in full runs. Cold is the premise, so pin it.
        """
        from kiro_crew import model_registry

        monkeypatch.setattr(model_registry, "_ADVERTISED_MODELS", {})

    @staticmethod
    def _claude_client(model):
        from kiro_crew.acp.client import ACP_BACKEND_CLAUDE, AcpClient

        client = AcpClient()
        client._session_id = "sess-1"
        client._model = model
        client._resolved_model_id = model
        client._acp_backend = ACP_BACKEND_CLAUDE
        return client

    @staticmethod
    def _rejecting_set_config_option(applied, accepted):
        from kiro_crew.acp.client import AcpError

        async def _set_config_option(config_id, value):
            applied.append((config_id, value))
            if value not in accepted:
                raise AcpError(f"Invalid value for config option model: {value}")

        return _set_config_option

    @pytest.mark.asyncio
    async def test_startup_falls_back_to_bare_spelling(self):
        """The reported failure: prefixed [1m] id refused, bare spelling served."""
        from kiro_crew.acp.client import DEFAULT_MODEL

        client = self._claude_client("global.anthropic.claude-sonnet-4-6[1m]")
        applied = []
        client.set_config_option = self._rejecting_set_config_option(
            applied, {"claude-sonnet-4-6[1m]"}
        )

        await client._apply_startup_model()

        assert applied == [
            ("model", "global.anthropic.claude-sonnet-4-6[1m]"),
            ("model", "claude-sonnet-4-6[1m]"),
        ]
        # Records the spelling that actually went on the wire, not the refused
        # one (the warm-pool re-apply path reads this field).
        assert client._model == "claude-sonnet-4-6[1m]"
        assert client._model != DEFAULT_MODEL

    @pytest.mark.asyncio
    async def test_startup_prefers_verbatim_when_accepted(self):
        """A warm cache folds before the wire: the original id is accepted as-is."""
        client = self._claude_client("claude-sonnet-4-6[1m]")
        applied = []
        client.set_config_option = self._rejecting_set_config_option(
            applied, {"claude-sonnet-4-6[1m]"}
        )

        await client._apply_startup_model()

        assert applied == [("model", "claude-sonnet-4-6[1m]")]
        assert client._model == "claude-sonnet-4-6[1m]"

    @pytest.mark.asyncio
    async def test_startup_all_refused_stays_on_default(self):
        """Every spelling refused = the withhold contract, not a crash."""
        from kiro_crew.acp.client import DEFAULT_MODEL

        client = self._claude_client("global.anthropic.claude-sonnet-4-6[1m]")
        applied = []
        client.set_config_option = self._rejecting_set_config_option(applied, set())

        await client._apply_startup_model()

        assert len(applied) == 3  # verbatim, prefix-stripped, fully bare
        assert client._model == DEFAULT_MODEL

    @pytest.mark.asyncio
    async def test_explicit_switch_refused_raises_unavailable(self):
        """set_model is an explicit pick: refusal is typed, never a silent lie."""
        from kiro_crew.acp.client import AcpModelUnavailable

        client = self._claude_client("claude-sonnet-4-6[1m]")
        client.set_config_option = self._rejecting_set_config_option([], set())

        with pytest.raises(AcpModelUnavailable):
            await client.set_model("global.anthropic.claude-sonnet-4-6[1m]")

    @pytest.mark.asyncio
    async def test_explicit_switch_sends_accepted_fallback(self):
        """An explicit pick that SOME spelling serves still lands on the model."""
        client = self._claude_client("global.anthropic.claude-sonnet-4-6[1m]")
        applied = []
        client.set_config_option = self._rejecting_set_config_option(
            applied, {"claude-sonnet-4-6[1m]"}
        )

        await client.set_model("global.anthropic.claude-sonnet-4-6[1m]")

        assert applied[-1] == ("model", "claude-sonnet-4-6[1m]")
        assert client._model == "claude-sonnet-4-6[1m]"
        assert client._resolved_model_id == "claude-sonnet-4-6[1m]"

    @pytest.mark.asyncio
    async def test_unknown_config_option_skips_push(self):
        """No 'model' option at all (other adapter build): no spelling retry."""
        from kiro_crew.acp.client import DEFAULT_MODEL, AcpError

        client = self._claude_client("global.anthropic.claude-sonnet-4-6[1m]")
        applied = []

        async def _unknown(config_id, value):
            applied.append((config_id, value))
            raise AcpError("Unknown config option: model")

        client.set_config_option = _unknown

        await client._apply_startup_model()

        assert applied == [("model", "global.anthropic.claude-sonnet-4-6[1m]")]
        assert client._model == DEFAULT_MODEL

    @pytest.mark.asyncio
    async def test_transport_error_propagates(self):
        """A non-value-rejection failure is not ours to swallow."""
        from kiro_crew.acp.client import AcpError

        client = self._claude_client("claude-sonnet-4-6[1m]")

        async def _transport(config_id, value):
            raise AcpError("transport closed")

        client.set_config_option = _transport

        with pytest.raises(AcpError, match="transport closed"):
            await client._apply_startup_model()

    def test_candidate_order_is_derived_from_the_id(self):
        from kiro_crew.acp.client import AcpClient

        assert AcpClient._model_config_candidates("global.anthropic.claude-sonnet-4-6[1m]") == [
            "global.anthropic.claude-sonnet-4-6[1m]",
            "claude-sonnet-4-6[1m]",
            "claude-sonnet-4-6",
        ]
        # No prefix / no window marker: single candidate, nothing derived.
        assert AcpClient._model_config_candidates("claude-sonnet-4-6") == ["claude-sonnet-4-6"]
