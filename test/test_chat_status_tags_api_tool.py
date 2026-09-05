"""Tests for the ``chat_status_tags_api`` MCP tool's authorization story.

The tool is the ONLY credentialed path from the Chat Status Tags reconcile cron
to the gateway's chat API (the ``ops_mission_control_api`` precedent: the MCP
server process holds the internal secret; the agent never sees a credential,
and the ``kirocrew token`` mint it would otherwise reach for is refused by the
shipped ``credential-exfil-kirocrew-token`` deny floor). Three planes must stay
mutually consistent, and each has a failure mode this module pins:

- the **(method, path) allowlist** in ``validation.py`` — widening it is an
  authorization change and must look like one in review;
- the **schema** rejecting off-surface calls (and, decisively, ``POST /api/chat``
  — the send-message route) before any HTTP happens;
- the **gateway** admitting the resolved ``/api/chat`` family for
  internal-secret callers, so an allowlisted call does not 403 at runtime.

The handler is exercised too: the slot key is interpolated server-side and the
response is redacted, because a slot's detail carries untrusted message text.
"""

import unittest
from unittest import mock

from kiro_crew import mcp_core
from kiro_crew.dashboard.server import _MIXED_INTERNAL_API_PATHS
from kiro_crew.dashboard.token_auth import internal_path_matches
from kiro_crew.mcp_tools import apps
from kiro_crew.validation import (
    CHAT_STATUS_TAGS_ALLOWED_CALLS,
    CHAT_STATUS_TAGS_API_SCHEMA,
    ValidationError,
    validate_tool_args,
)

# The literal base the handler prepends to a base-relative allowlist path.
_BASE = "/api/chat"


class TestAllowlist(unittest.TestCase):
    def test_the_agent_surface_is_exactly_the_four_calls(self):
        """Exactly what the reconcile cron needs, nothing more.

        A new entry here is an authorization change; this test is the review
        speed bump that makes it look like one. The message-DETAIL route
        (``GET /slots/{slot}``) is deliberately NOT here: an app token is
        confined to slots it owns, so the reconciler could never read a user
        chat's messages through it — it reads PR URLs from each slot's
        ``source_links`` on the list instead.
        """
        self.assertEqual(
            CHAT_STATUS_TAGS_ALLOWED_CALLS,
            frozenset(
                {
                    ("GET", "/slots"),
                    ("GET", "/tags"),
                    ("POST", "/tags"),
                    ("PUT", "/slots/{slot}/tags"),
                }
            ),
        )

    def test_message_detail_route_is_not_on_the_surface(self):
        """The reconciler must not be able to read a chat's raw messages.

        ``GET /slots/{slot}`` is closed to app tokens by
        ``_deny_cross_app_slot_access`` for every chat the app does not own,
        which is every user chat. Admitting it into the allowlist would only
        produce ``slot_not_found`` at runtime AND widen the app's declared
        reach to message content it can never actually read — so it is out.
        """
        self.assertNotIn(("GET", "/slots/{slot}"), CHAT_STATUS_TAGS_ALLOWED_CALLS)

    def test_send_message_route_is_not_on_the_surface(self):
        """The reconciler must never be able to send a chat turn.

        ``POST /api/chat`` (base-relative: the empty tail / ``/``) is the
        message-injection route. It is absent by both spellings.
        """
        paths = {p for _, p in CHAT_STATUS_TAGS_ALLOWED_CALLS}
        self.assertNotIn("", paths)
        self.assertNotIn("/", paths)
        # And the pair form, however written, is not admitted.
        self.assertNotIn(("POST", ""), CHAT_STATUS_TAGS_ALLOWED_CALLS)
        self.assertNotIn(("POST", "/"), CHAT_STATUS_TAGS_ALLOWED_CALLS)
        self.assertNotIn(("POST", "/slots"), CHAT_STATUS_TAGS_ALLOWED_CALLS)

    def test_no_write_beyond_a_slots_tag_list(self):
        """The only write paths are tag-create and one slot's tag list."""
        writes = {(m, p) for m, p in CHAT_STATUS_TAGS_ALLOWED_CALLS if m != "GET"}
        self.assertEqual(writes, {("POST", "/tags"), ("PUT", "/slots/{slot}/tags")})


class TestSchema(unittest.TestCase):
    def _validate(self, **kwargs):
        return validate_tool_args(kwargs, CHAT_STATUS_TAGS_API_SCHEMA)

    def test_a_valid_get_list_passes(self):
        cleaned = self._validate(method="GET", path="/slots")
        self.assertEqual(cleaned["method"], "GET")
        self.assertEqual(cleaned["path"], "/slots")

    def test_a_valid_parameterised_call_passes(self):
        cleaned = self._validate(
            method="PUT",
            path="/slots/{slot}/tags",
            slot_key="chat-5",
            body_json='{"tags": ["Tag0ABC"]}',
        )
        self.assertEqual(cleaned["slot_key"], "chat-5")

    def test_message_detail_path_is_no_longer_on_the_surface(self):
        """``GET /slots/{slot}`` was retired from the allowlist; the schema
        rejects it now, so a prompt that still asked for it fails fast rather
        than reaching the gateway and 404ing."""
        with self.assertRaises(ValidationError):
            self._validate(method="GET", path="/slots/{slot}", slot_key="chat-5")

    def test_a_valid_tag_create_passes(self):
        cleaned = self._validate(
            method="POST",
            path="/tags",
            body_json='{"name": "review", "color": "blue", "status": true}',
        )
        self.assertEqual(cleaned["path"], "/tags")

    def test_a_valid_put_tags_passes(self):
        cleaned = self._validate(
            method="PUT",
            path="/slots/{slot}/tags",
            slot_key="chat-5",
            body_json='{"tags": ["Tag0ABC"]}',
        )
        self.assertEqual(cleaned["path"], "/slots/{slot}/tags")

    def test_method_path_pair_is_checked_not_just_membership(self):
        """Both halves are individually legal; the PAIR is not.

        ``/slots`` is a real path and ``PUT`` is a real method — a validator
        checking the two enums independently would pass these.
        """
        with self.assertRaises(ValidationError):
            self._validate(method="PUT", path="/slots")
        with self.assertRaises(ValidationError):
            self._validate(method="POST", path="/slots/{slot}", slot_key="chat-5")

    def test_post_api_chat_send_message_is_rejected(self):
        """The send-message route must never validate, by any spelling."""
        for path in ("", "/", "/send", "/api/chat"):
            with self.assertRaises(ValidationError):
                self._validate(method="POST", path=path)

    def test_off_surface_paths_are_rejected(self):
        for method, path in (
            ("DELETE", "/tags/{id}"),
            ("POST", "/slots"),
            ("POST", "/slots/{slot}/stop"),
            ("GET", "/folders"),
        ):
            with self.assertRaises(ValidationError):
                self._validate(method=method, path=path)

    def test_full_paths_are_rejected(self):
        """Paths are base-relative; a full ``/api/chat/...`` means confusion."""
        with self.assertRaises(ValidationError):
            self._validate(method="GET", path="/api/chat/slots")

    def test_parameterised_path_requires_a_slot_key(self):
        with self.assertRaises(ValidationError):
            self._validate(method="PUT", path="/slots/{slot}/tags")

    def test_non_parameterised_path_rejects_a_slot_key(self):
        with self.assertRaises(ValidationError):
            self._validate(method="GET", path="/slots", slot_key="chat-5")

    def test_slot_key_shape_is_enforced(self):
        """A key can never carry a path separator or query metacharacter.

        Each of these would let an interpolated key traverse out of its
        segment or rewrite the path it rides on.
        """
        for bad in (
            "chat/../tags",
            "a/b",
            "chat 5",
            "a?b",
            "a#frag",
            "a%2f",
            "",
        ):
            with self.assertRaises(ValidationError):
                self._validate(
                    method="PUT",
                    path="/slots/{slot}/tags",
                    slot_key=bad,
                    body_json='{"tags": []}',
                )

    def test_slot_key_accepts_real_key_shapes(self):
        for good in ("chat-5", "app:chat-status-tags", "chat_5", "a.b-c:d"):
            cleaned = self._validate(
                method="PUT",
                path="/slots/{slot}/tags",
                slot_key=good,
                body_json='{"tags": []}',
            )
            self.assertEqual(cleaned["slot_key"], good)

    def test_query_cannot_smuggle_a_path(self):
        for bad in ("../slots", "a=1?b=2", "x=1#frag", "a=/etc/passwd"):
            with self.assertRaises(ValidationError):
                self._validate(method="GET", path="/slots", query=bad)

    def test_query_is_get_only(self):
        with self.assertRaises(ValidationError):
            self._validate(method="POST", path="/tags", query="a=1")

    def test_body_is_not_accepted_on_get(self):
        with self.assertRaises(ValidationError):
            self._validate(method="GET", path="/slots", body_json='{"a": 1}')


class TestGatewayAdmission(unittest.TestCase):
    """The gateway must admit the resolved chat-API family for secret callers."""

    def test_every_allowlisted_call_is_admitted(self):
        """A call the schema allows but the gateway 403s is a broken tool.

        The granting entry is the ``/api/chat`` prefix already in
        ``_MIXED_INTERNAL_API_PATHS``; a parameterised template resolves to a
        concrete key before it reaches the wire.
        """
        for _method, path in CHAT_STATUS_TAGS_ALLOWED_CALLS:
            full = _BASE + path.replace("{slot}", "chat-5")
            self.assertTrue(
                internal_path_matches(full, _MIXED_INTERNAL_API_PATHS),
                f"{full} is allowlisted for the tool but not admitted by the gateway",
            )


class TestHandler(unittest.TestCase):
    """Exercise the handler: allowlist gate, interpolation, redaction, cap."""

    def test_off_surface_pair_is_refused_before_any_http(self):
        with (
            mock.patch.object(mcp_core, "_get") as g,
            mock.patch.object(mcp_core, "_post") as p,
            mock.patch.object(mcp_core, "_put") as u,
        ):
            out = apps.chat_status_tags_api("chat_status_tags_api", {"method": "POST", "path": ""})
        self.assertTrue(out.startswith("Error:"))
        g.assert_not_called()
        p.assert_not_called()
        u.assert_not_called()

    def test_get_slots_hits_the_prefixed_base(self):
        with mock.patch.object(mcp_core, "_get", return_value={"slots": []}) as g:
            apps.chat_status_tags_api("chat_status_tags_api", {"method": "GET", "path": "/slots"})
        g.assert_called_once_with("/api/chat/slots")

    def test_slot_key_is_interpolated_server_side(self):
        with mock.patch.object(mcp_core, "_put", return_value={"ok": True}) as u:
            apps.chat_status_tags_api(
                "chat_status_tags_api",
                {
                    "method": "PUT",
                    "path": "/slots/{slot}/tags",
                    "slot_key": "chat-5",
                    "body_json": '{"tags": ["Tag0ABC"]}',
                },
            )
        u.assert_called_once_with("/api/chat/slots/chat-5/tags", {"tags": ["Tag0ABC"]})

    def test_put_tags_interpolates_and_sends_body(self):
        with mock.patch.object(mcp_core, "_put", return_value={"ok": True}) as u:
            apps.chat_status_tags_api(
                "chat_status_tags_api",
                {
                    "method": "PUT",
                    "path": "/slots/{slot}/tags",
                    "slot_key": "chat-5",
                    "body_json": '{"tags": ["Tag0ABC"]}',
                },
            )
        u.assert_called_once_with("/api/chat/slots/chat-5/tags", {"tags": ["Tag0ABC"]})

    def test_response_is_redacted(self):
        """A credential quoted into a slot's serialized text is scrubbed out.

        The slot list carries title/source-link text derived from untrusted
        LLM/external message content; the handler redacts on the way out so it
        cannot flow into this agent's context.
        """
        secret = "AKIA" + "A" * 16  # AWS access key ID shape, reliably redacted
        leaky = {"slots": [{"key": "chat-5", "title": "creds " + secret}]}
        with mock.patch.object(mcp_core, "_get", return_value=leaky):
            out = apps.chat_status_tags_api(
                "chat_status_tags_api",
                {"method": "GET", "path": "/slots"},
            )
        self.assertNotIn(secret, out)

    def test_invalid_body_json_is_reported_not_raised(self):
        with mock.patch.object(mcp_core, "_post") as p:
            out = apps.chat_status_tags_api(
                "chat_status_tags_api",
                {"method": "POST", "path": "/tags", "body_json": "{not json"},
            )
        self.assertTrue(out.startswith("Error:"))
        p.assert_not_called()

    def test_oversize_response_is_capped(self):
        big = {"slots": [{"key": "chat-5", "title": "x" * 200_000}]}
        with mock.patch.object(mcp_core, "_get", return_value=big):
            out = apps.chat_status_tags_api(
                "chat_status_tags_api",
                {"method": "GET", "path": "/slots"},
            )
        self.assertIn("truncated", out)
        self.assertLessEqual(len(out), 60_500)


class TestRegistration(unittest.TestCase):
    def test_tool_is_declared_and_dispatched(self):
        names = {s["name"] for s in apps.schemas()}
        self.assertIn("chat_status_tags_api", names)
        self.assertIn("chat_status_tags_api", apps.HANDLERS)

    def test_schema_is_registered_for_the_guarded_validation_step(self):
        from kiro_crew.validation import MCP_CORE_SCHEMAS

        self.assertIs(MCP_CORE_SCHEMAS["chat_status_tags_api"], CHAT_STATUS_TAGS_API_SCHEMA)


if __name__ == "__main__":
    unittest.main()
