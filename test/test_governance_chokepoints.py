"""Phase 7 — per-scope chokepoints beyond the name gate.

Covers the ``filesystem.read``/``filesystem.write``/``network.egress`` scopes enforced at
the host gate via tool kind + real args, ``folders.*`` as their profile alias, the
keystone on the real edit path, the raw params a permission event carries, path
normalization, and that a chokepoint which fails OPEN is observable and one that
errors fails CLOSED. The sandbox floor, the cron, spawn, capability, channel, app and
publish chokepoints are in ``test_governance_chokepoints_scope_gates.py``.
"""

from __future__ import annotations

import os

import pytest
from governance_chokepoints_helpers import _isolate  # noqa: F401
from governance_chokepoints_helpers import _install

from kiro_crew.platform import governance_profiles as gp


# ── filesystem + egress at the host gate (tool kind + real args) ──
class TestFilesystemEgressAtGate:
    def test_filesystem_read_denied_via_reading_title(self):
        # A "Reading <path>" title is classified to filesystem.read; a policy
        # read-deny blocks it at the name gate.
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "filesystem": {"read": {"mode": "deny", "deny": ["**/.env"]}},
            }
        )
        from kiro_crew.hooks import TOOL_DENY, HookManager

        hooks = HookManager()
        result = hooks.on_tool_call("Reading /home/u/proj/.env", session_key="cli_chat")
        assert result.action == TOOL_DENY

    def test_filesystem_write_denied_via_edit_args(self):
        # A write outside the allowed write paths is denied via tool_kind=edit +
        # raw_params path (the title alone cannot carry this).
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "filesystem": {"write": {"mode": "allow", "allow": ["/home/u/workspace/**"]}},
            }
        )
        from kiro_crew.hooks import TOOL_DENY, HookManager

        hooks = HookManager()
        denied = hooks.on_tool_call(
            "code",
            session_key="cli_chat",
            tool_kind="edit",
            raw_params={"path": "/etc/passwd"},
        )
        assert denied.action == TOOL_DENY
        allowed = hooks.on_tool_call(
            "code",
            session_key="cli_chat",
            tool_kind="edit",
            raw_params={"path": "/home/u/workspace/site.py"},
        )
        assert allowed.action != TOOL_DENY

    def test_filesystem_write_denied_via_diff_only_edit(self):
        # The same confinement must bind an edit whose target is named ONLY in
        # the diff content block (event.diff_path): governance classifies the
        # params-union-diff-block target set, so a diff-only edit outside the
        # allow-list is denied rather than reaching the ceiling pathless.
        # Paths come from _fs_tree so they are platform-absolute: a POSIX
        # literal fails os.path.isabs on Windows and would trip the unanchored
        # hard-deny instead of exercising the ceiling this test is about.
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "filesystem": {
                    "write": {"mode": "allow", "allow": [self._fs_tree("workspace", "**")]}
                },
            }
        )
        from kiro_crew.hooks import TOOL_DENY, HookManager

        hooks = HookManager()
        denied = hooks.on_tool_call(
            "code",
            session_key="cli_chat",
            tool_kind="edit",
            raw_params={"command": "create", "fileText": "x"},
            diff_path=self._fs_tree("secrets", "creds.txt"),
        )
        assert denied.action == TOOL_DENY
        allowed = hooks.on_tool_call(
            "code",
            session_key="cli_chat",
            tool_kind="edit",
            raw_params={"command": "create", "fileText": "x"},
            diff_path=self._fs_tree("workspace", "site.py"),
        )
        assert allowed.action != TOOL_DENY

    def test_diff_block_route_keeps_the_network_egress_pair(self):
        # Routing a call onto the edit branch because its frame carried a diff
        # block must never DROP a pair the pre-route classification would have
        # emitted: a kindless (or fetch-kind) call carrying BOTH a url param
        # and a diff block keeps its network.egress pair, so a governed egress
        # ceiling still binds it.
        from kiro_crew.platform.governance import classify_tool_args

        pairs = classify_tool_args(
            "",
            {"url": "https://evil.com/x"},
            diff_path=self._fs_tree("workspace", "site.py"),
        )
        assert ("network.egress", "evil.com") in pairs, (
            "a diff-block call carrying a url shed its egress pair -- an "
            "egress-governed ceiling no longer binds it"
        )
        assert ("filesystem.write", self._fs_tree("workspace", "site.py")) in pairs

    def test_filesystem_write_traversal_escape_denied(self):
        # A ``..`` traversal that lexically escapes the allow-prefix must be
        # DENIED: without path normalization, fnmatch's ``*`` spans the ``..`` so
        # ``/home/u/workspace/../.bashrc`` matches ``/home/u/workspace/**`` and the
        # write is wrongly permitted (it resolves to ~/.bashrc, outside the
        # allow-list) — a containment bypass. (path-traversal finding.)
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "filesystem": {"write": {"mode": "allow", "allow": ["/home/u/workspace/**"]}},
            }
        )
        from kiro_crew.hooks import TOOL_DENY, HookManager

        hooks = HookManager()
        denied = hooks.on_tool_call(
            "code",
            session_key="cli_chat",
            tool_kind="edit",
            raw_params={"path": "/home/u/workspace/../.bashrc"},
        )
        assert denied.action == TOOL_DENY
        # A legitimate in-tree write with a redundant ``.`` segment still matches.
        allowed = hooks.on_tool_call(
            "code",
            session_key="cli_chat",
            tool_kind="edit",
            raw_params={"path": "/home/u/workspace/./src/app.py"},
        )
        assert allowed.action != TOOL_DENY

    def test_filesystem_relative_path_cannot_dodge_absolute_deny(self, monkeypatch, tmp_path):
        # An agent-supplied RELATIVE path must not bypass an absolute DENY glob by
        # failing to match: ``_norm_item`` absolutizes it against the CWD first, so
        # a relative path inside a denied tree is still blocked.
        # (before the fix the relative item stayed relative and never matched
        # ``/<cwd>/secret/**``, so the deny silently failed open.)
        monkeypatch.chdir(tmp_path)
        cwd = str(tmp_path)
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "filesystem": {"read": {"mode": "deny", "deny": [f"{cwd}/secret/**"]}},
            }
        )
        from kiro_crew.hooks import TOOL_DENY, HookManager

        hooks = HookManager()
        # Relative path that resolves into the denied subtree → DENY.
        denied = hooks.on_tool_call(
            "fs_read",
            session_key="cli_chat",
            tool_kind="read",
            raw_params={"path": "secret/key.pem"},
        )
        assert denied.action == TOOL_DENY
        # An out-of-tree relative read is unaffected (not denied by this rule).
        allowed = hooks.on_tool_call(
            "fs_read",
            session_key="cli_chat",
            tool_kind="read",
            raw_params={"path": "public/readme.md"},
        )
        assert allowed.action != TOOL_DENY

    def test_egress_denied_via_fetch_args(self):
        # A web_fetch (tool_kind=fetch) to a host outside the egress allowlist is
        # denied; the host is extracted from the URL.
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "network": {"egress": {"mode": "allow", "allow": ["*.amazonaws.com"]}},
            }
        )
        from kiro_crew.hooks import TOOL_DENY, HookManager

        hooks = HookManager()
        denied = hooks.on_tool_call(
            "web_fetch",
            session_key="cli_chat",
            tool_kind="fetch",
            raw_params={"url": "https://evil.example.com/x"},
        )
        assert denied.action == TOOL_DENY
        allowed = hooks.on_tool_call(
            "web_fetch",
            session_key="cli_chat",
            tool_kind="fetch",
            raw_params={"url": "https://s3.amazonaws.com/bucket"},
        )
        assert allowed.action != TOOL_DENY

    def test_ungoverned_args_are_noop(self):
        _install(None)
        from kiro_crew.hooks import TOOL_DENY, HookManager

        hooks = HookManager()
        r = hooks.on_tool_call(
            "code", session_key="cli_chat", tool_kind="edit", raw_params={"path": "/etc/x"}
        )
        assert r.action != TOOL_DENY

    def test_hostless_url_is_not_phantom_egress(self):
        # A fetch of a hostless URL (file://, mailto:, data:) must NOT be
        # classified as egress to a phantom host (e.g. the scheme "file") — it
        # carries no network host, so an egress allowlist must not block it.
        from kiro_crew.platform.governance import _url_host, classify_tool_args

        assert _url_host("file:///etc/passwd") == ""
        assert classify_tool_args("fetch", {"url": "file:///etc/passwd"}) == ()
        # But a real scheme-less host (with or without a port) is still recovered.
        assert _url_host("example.com/path") == "example.com"
        assert _url_host("example.com:8080/path") == "example.com"

    def test_non_network_scheme_uris_have_no_phantom_host(self):
        # mailto:/javascript:/data:/tel: use ':' without '://'. The scheme-less
        # retry must NOT mis-parse their payload as an authority — otherwise the
        # egress gate grounds its decision on a host the URL never contacts
        # (e.g. mailto:user@evil.com → phantom "evil.com").
        from kiro_crew.platform.governance import _url_host, classify_tool_args

        for u in (
            "mailto:user@example.com",
            "javascript:alert(1)",
            "data:text/html,<b>x</b>",
            "tel:+1-555-0100",
            "gopher://g.example.com/x",  # scheme present but NOT a network scheme
        ):
            assert _url_host(u) == "", u
            assert classify_tool_args("fetch", {"url": u}) == (), u
        # Real network schemes still resolve their host (incl. ws/ftp + userinfo).
        assert _url_host("https://user:pass@good.com:443/p") == "good.com"
        assert _url_host("ws://w.example.com/s") == "w.example.com"
        assert _url_host("ftp://f.example.com/x") == "f.example.com"
        # Protocol-relative //host/path is still recovered.
        assert _url_host("//cdn.example.com/a") == "cdn.example.com"

    def test_mailto_cannot_pass_egress_allowlist_via_phantom_host(self):
        # End-to-end: with an egress allowlist pinned to allowed.com, a
        # mailto:exfil@allowed.com must NOT slip through as egress to "allowed.com"
        # — it is hostless, so it is simply ungoverned-by-egress (no phantom match).
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "network": {"egress": {"mode": "allow", "allow": ["allowed.com"]}},
            }
        )
        from kiro_crew.platform.governance import classify_tool_args

        assert classify_tool_args("fetch", {"url": "mailto:exfil@allowed.com"}) == ()

    def test_empty_tool_kind_falls_back_to_param_shape(self):
        # The ACP `kind` field is spec-OPTIONAL; when the backend omits it,
        # tool_kind arrives "". A write must still be governed via the param
        # shape (path → both fs ceilings), and a shell command (carries
        # `command`) must NOT be misrouted to filesystem.
        from kiro_crew.platform.governance import classify_tool_args

        # Empty kind + path → both read+write ceilings (can't tell which).
        pairs = dict(classify_tool_args("", {"path": "/etc/passwd"}))
        assert pairs.get("filesystem.read") == "/etc/passwd"
        assert pairs.get("filesystem.write") == "/etc/passwd"
        # Empty kind + url → egress.
        assert classify_tool_args("", {"url": "https://evil.com/x"}) == (
            ("network.egress", "evil.com"),
        )
        # Empty kind + a shell command → NOT filesystem/egress (commands scope).
        assert classify_tool_args("", {"command": "rm -rf /"}) == ()

    @pytest.mark.parametrize("key", ["path", "file_path", "filePath"])
    def test_every_path_alias_is_classified(self, key):
        from kiro_crew.platform.governance import classify_tool_args

        assert classify_tool_args("edit", {key: "/srv/secret"}) == (
            ("filesystem.write", "/srv/secret"),
        )
        assert classify_tool_args("read", {key: "/srv/secret"}) == (
            ("filesystem.read", "/srv/secret"),
        )

    def test_conflicting_path_aliases_are_all_classified(self):
        """An innocent first alias must not mask a sensitive later alias."""
        from kiro_crew.platform.governance import classify_tool_args

        assert classify_tool_args(
            "edit",
            {
                "path": "/tmp/innocent",
                "file_path": {"not": "a path"},
                "filePath": "/home/user/.ssh/id_rsa",
            },
        ) == (
            ("filesystem.write", "/tmp/innocent"),
            ("filesystem.write", "/home/user/.ssh/id_rsa"),
        )

    def test_conflicting_path_alias_cannot_bypass_gate(self):
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "filesystem": {"write": {"mode": "allow", "allow": ["/tmp/**"]}},
            }
        )
        from kiro_crew.hooks import TOOL_DENY, HookManager

        decision = HookManager().on_tool_call(
            "Editing notes",
            session_key="cli_chat",
            tool_kind="edit",
            raw_params={"path": "/tmp/allowed.txt", "filePath": "/srv/outside.txt"},
        )
        assert decision.action == TOOL_DENY

    def test_empty_kind_write_still_denied_at_gate(self):
        # End-to-end: an edit with tool_kind="" (backend omitted kind) to a
        # path outside the write allowlist must still be DENIED at the gate.
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "filesystem": {"write": {"mode": "allow", "allow": ["/home/u/ws/**"]}},
            }
        )
        from kiro_crew.hooks import TOOL_DENY, HookManager

        hooks = HookManager()
        r = hooks.on_tool_call(
            "Editing config", session_key="cli_chat", tool_kind="", raw_params={"path": "/etc/x"}
        )
        assert r.action == TOOL_DENY

    # ── array-nested path extraction (governance parity with the
    #    hooks keystone; a flat top-level-only extractor misses nested paths). ──
    def test_nested_array_read_path_is_classified(self):
        # A batch-shaped tool buries its target inside an array argument. The
        # governance plane must surface it (before the fix this returned ()).
        from kiro_crew.platform.governance import classify_tool_args

        assert classify_tool_args(
            "read", {"operations": [{"mode": "Line", "path": "~/secrets/notes.txt"}]}
        ) == (("filesystem.read", "~/secrets/notes.txt"),)

    def test_nested_array_edit_path_maps_to_write(self):
        from kiro_crew.platform.governance import classify_tool_args

        assert classify_tool_args(
            "edit", {"operations": [{"mode": "Line", "path": "~/secrets/notes.txt"}]}
        ) == (("filesystem.write", "~/secrets/notes.txt"),)

    def test_deeply_nested_path_under_dict_and_list_mix(self):
        # A path buried under an arbitrary mix of dicts and lists is still found.
        from kiro_crew.platform.governance import classify_tool_args

        assert classify_tool_args(
            "read", {"a": [{"b": {"c": [{"file_path": "~/secrets/deep.key"}]}}]}
        ) == (("filesystem.read", "~/secrets/deep.key"),)

    # ``_match_path`` normalizes the queried ITEM with ``os.path.abspath`` and
    # matches the operator's PATTERN verbatim, so a POSIX-literal pair
    # (``/home/u/ws/**`` vs ``/home/u/ws/doc.md``) is not a portable fixture: on
    # Windows the item absolutizes to ``<cwd-drive>\home\u\ws\doc.md`` while the
    # pattern keeps its forward slashes, so NOTHING matches and an allow-mode
    # ceiling denies even the in-tree read. Derive both the pattern and the items
    # from ONE platform-native root instead, so the in-tree case is genuinely
    # permitted and — the part that matters for a ceiling test — the out-of-tree
    # case is denied because the ceiling BOUND, not because the two strings could
    # never have matched on this platform.
    @staticmethod
    def _fs_tree(*parts: str) -> str:
        return os.path.join(os.path.abspath(os.path.join(os.sep, "home", "u")), *parts)

    def test_nested_array_read_denied_at_gate(self):
        # END-TO-END: an operator profile confining reads to the workspace must
        # DENY a nested-array read of a path outside it. Before the fix the
        # nested path was invisible to governance and the call was PERMITTED.
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "filesystem": {"read": {"mode": "allow", "allow": [self._fs_tree("ws", "**")]}},
            }
        )
        from kiro_crew.hooks import TOOL_DENY, HookManager

        hooks = HookManager()
        denied = hooks.on_tool_call(
            "fs_read",
            session_key="cli_chat",
            tool_kind="read",
            raw_params={
                "operations": [{"mode": "Line", "path": self._fs_tree("secrets", "notes.txt")}]
            },
        )
        assert denied.action == TOOL_DENY

    def test_nested_array_read_within_ceiling_still_permitted(self):
        # A nested-array path INSIDE the allowed tree must still pass — the fix
        # tightens exactly the escaping case, not legitimate in-tree reads.
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "filesystem": {"read": {"mode": "allow", "allow": [self._fs_tree("ws", "**")]}},
            }
        )
        from kiro_crew.hooks import TOOL_DENY, HookManager

        hooks = HookManager()
        allowed = hooks.on_tool_call(
            "fs_read",
            session_key="cli_chat",
            tool_kind="read",
            raw_params={"operations": [{"mode": "Line", "path": self._fs_tree("ws", "doc.md")}]},
        )
        assert allowed.action != TOOL_DENY

    # ── truncated-scan policy on the permit-by-default plane (open-question-2) ──
    #
    # A payload padded past the bounded walk's node/path caps truncates. On this
    # permit-by-default plane a truncated scan that yielded no path would otherwise
    # fall to the ``if not pairs: permit`` branch — the "bury the path past 10_000
    # nodes to escape the ceiling" fail-open. The chosen policy (issue option (c))
    # emits the kind's filesystem scope against a never-permittable marker, so an
    # ALLOW-mode ceiling that confines the scope DENIES the unverifiable call while
    # an ungoverned host still permits it (permit-by-default preserved).
    @staticmethod
    def _truncating_read_params():
        from kiro_crew.platform.tool_paths import _TARGET_PATH_MAX_NODES

        pad = [{"k": i} for i in range(_TARGET_PATH_MAX_NODES + 50)]
        pad.append({"path": "/home/u/secrets/buried.txt"})
        return {"operations": pad}

    def test_truncated_scan_emits_never_permittable_marker(self):
        from kiro_crew.platform.governance import _TRUNCATED_SCAN_ITEM, classify_tool_args
        from kiro_crew.platform.tool_paths import target_paths

        params = self._truncating_read_params()
        assert target_paths(params).truncated is True
        pairs = classify_tool_args("read", params)
        # Must NOT be empty (would land on permit-by-default) and must carry the
        # synthetic marker so a governed ceiling can bind.
        assert pairs != ()
        assert ("filesystem.read", _TRUNCATED_SCAN_ITEM) in pairs

    def test_truncated_scan_unknown_kind_consults_both_fs_scopes(self):
        from kiro_crew.platform.governance import _TRUNCATED_SCAN_ITEM, classify_tool_args

        pairs = classify_tool_args("", self._truncating_read_params())
        assert ("filesystem.read", _TRUNCATED_SCAN_ITEM) in pairs
        assert ("filesystem.write", _TRUNCATED_SCAN_ITEM) in pairs

    def test_truncated_scan_denied_at_governance_plane_under_ceiling(self):
        # GOVERNANCE-PLANE, KEYSTONE-INDEPENDENT: this is the assertion that guards
        # the fix on revert. An ALLOW-mode read ceiling (confine to the workspace)
        # must DENY the synthetic truncation marker via resolve(). It fails if the
        # governance truncation branch is reverted (classify_tool_args then returns
        # () for a zero-path truncated scan and resolve is never asked about the
        # marker). The end-to-end HookManager test below cannot guard the fix on its
        # own because the always-on keystone hard-denies any truncated scan first.
        from kiro_crew.platform.governance import (
            _TRUNCATED_SCAN_ITEM,
            classify_tool_args,
            parse_policy,
            resolve,
        )

        allow_ceiling = parse_policy(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "filesystem": {"read": {"mode": "allow", "allow": ["/home/u/ws/**"]}},
            }
        )
        # classify emits the marker (would be () if the truncation branch reverted).
        pairs = classify_tool_args("read", self._truncating_read_params())
        assert ("filesystem.read", _TRUNCATED_SCAN_ITEM) in pairs
        # The prefix-bounded ALLOW ceiling cannot match the never-permittable marker,
        # so the truncated scan is denied — the fail-open is closed on this plane.
        assert not resolve(allow_ceiling, None, "filesystem.read", _TRUNCATED_SCAN_ITEM).permitted

    def test_truncated_scan_denied_under_governed_ceiling(self):
        # END-TO-END: with an ALLOW-mode read ceiling (confine to the workspace),
        # a truncated scan is DENIED. NOTE: at this chokepoint the always-on hooks
        # keystone hard-denies any truncated scan before governance runs, so this
        # test alone does NOT guard the governance fix — see
        # test_truncated_scan_denied_at_governance_plane_under_ceiling for the
        # keystone-independent, revert-sensitive guard.
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "filesystem": {"read": {"mode": "allow", "allow": ["/home/u/ws/**"]}},
            }
        )
        from kiro_crew.hooks import TOOL_DENY, HookManager

        hooks = HookManager()
        denied = hooks.on_tool_call(
            "fs_read",
            session_key="cli_chat",
            tool_kind="read",
            raw_params=self._truncating_read_params(),
        )
        assert denied.action == TOOL_DENY

    def test_truncated_scan_permitted_when_ungoverned(self):
        # The permit-by-default property must be asserted ON THE GOVERNANCE PLANE
        # IN ISOLATION, not through HookManager.on_tool_call: the always-on
        # sensitive-path keystone in hooks HARD-DENIES *any* truncated scan
        # (real_paths.truncated) BEFORE governance is consulted, so on_tool_call
        # returns deny regardless of policy and cannot observe the governance
        # plane's decision. Here we drive classify_tool_args + resolve directly:
        # the truncation emits the marker, and an UNGOVERNED ceiling (None) permits
        # it — permit-by-default is preserved for hosts with no policy.
        from kiro_crew.platform.governance import (
            _TRUNCATED_SCAN_ITEM,
            classify_tool_args,
            resolve,
        )

        pairs = classify_tool_args("read", self._truncating_read_params())
        assert ("filesystem.read", _TRUNCATED_SCAN_ITEM) in pairs
        # Ungoverned scope (ceiling=None, profile=None): the marker is permitted,
        # so a standalone host with no policy is not over-blocked.
        assert resolve(None, None, "filesystem.read", _TRUNCATED_SCAN_ITEM).permitted

    def test_flat_and_fetch_cases_unchanged_regression(self):
        # Regression: the flat top-level path and the fetch/url derivation must be
        # byte-for-byte unchanged by the depth-aware rewrite.
        from kiro_crew.platform.governance import classify_tool_args

        assert classify_tool_args("read", {"path": "~/secrets/notes.txt"}) == (
            ("filesystem.read", "~/secrets/notes.txt"),
        )
        assert classify_tool_args("edit", {"path": "/etc/passwd"}) == (
            ("filesystem.write", "/etc/passwd"),
        )
        assert classify_tool_args("fetch", {"url": "https://evil.example.com/x"}) == (
            ("network.egress", "evil.example.com"),
        )


class TestFoldersAliasesFilesystem:
    """A profile's folders.read/folders.write must narrow the policy's
    filesystem.read/filesystem.write ceiling (same path scope, different name —
    provider profile App. A.3). They are normalized to filesystem.* at parse time."""

    def test_profile_folders_write_narrows_filesystem_write(self):
        from kiro_crew.platform.governance import parse_profile, resolve

        prof = parse_profile(
            {
                "name": "p",
                "bind": {"type": "surface", "id": "dashboard"},
                "folders": {"write": {"mode": "allow", "allow": ["/home/u/ws/**"]}},
            }
        )
        # The folders.write key normalizes to filesystem.write (the gate's query).
        assert "filesystem.write" in prof.controls
        assert "folders.write" not in prof.controls
        assert not resolve(None, prof, "filesystem.write", "/etc/x").permitted
        assert resolve(None, prof, "filesystem.write", "/home/u/ws/site.py").permitted

    def test_folders_and_filesystem_both_present_intersect(self):
        # If a file authors BOTH folders.write and filesystem.write, they compose
        # (intersect) rather than one silently overwriting the other.
        from kiro_crew.platform.governance import parse_policy, resolve

        pol = parse_policy(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "filesystem": {"write": {"mode": "allow", "allow": ["/a/**", "/b/**"]}},
                "folders": {"write": {"mode": "allow", "allow": ["/a/**"]}},
            }
        )
        # Intersection: /a permitted by both; /b permitted by filesystem only → denied.
        assert resolve(pol, None, "filesystem.write", "/a/x").permitted
        assert not resolve(pol, None, "filesystem.write", "/b/x").permitted


class TestKeystoneOnRealPath:
    """The always-on is_sensitive_path keystone must check the REAL edit path,
    not only the display title — an 'Editing <file>' title hides the path."""

    def test_edit_to_trust_root_blocked_even_with_innocuous_title(self):
        _install(None)  # ungoverned: ONLY the always-on keystone is in play
        from kiro_crew.hooks import TOOL_DENY, HookManager

        hooks = HookManager()
        # A generic title that does not contain the path; the real path is the
        # governance trust-root file the agent must never rewrite.
        r = hooks.on_tool_call(
            "code",
            session_key="cli_chat",
            tool_kind="edit",
            raw_params={"path": "~/.kirocrew/security_policy.json"},
        )
        assert r.action == TOOL_DENY
        assert "sensitive path" in r.reason.lower()

    def test_edit_to_ssh_key_blocked_via_real_path(self):
        _install(None)
        from kiro_crew.hooks import TOOL_DENY, HookManager

        hooks = HookManager()
        r = hooks.on_tool_call(
            "Editing key",
            session_key="cli_chat",
            tool_kind="edit",
            raw_params={"path": "~/.ssh/id_rsa"},
        )
        assert r.action == TOOL_DENY

    def test_benign_edit_path_not_blocked(self):
        _install(None)
        from kiro_crew.hooks import TOOL_DENY, HookManager

        hooks = HookManager()
        r = hooks.on_tool_call(
            "code",
            session_key="cli_chat",
            tool_kind="edit",
            raw_params={"path": "/tmp/scratch.txt"},
        )
        assert r.action != TOOL_DENY


class TestPermissionEventCarriesRawParams:
    """The EVENT_PERMISSION_REQUEST the
    gate actually runs on must carry raw_tool_params, or filesystem.write /
    network.egress enforcement is a no-op in production."""

    def test_permission_event_recovers_cached_params(self):
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, JsonRpcMessage

        client = AcpClient.__new__(AcpClient)  # avoid spawning a real process
        client._tool_call_inputs = {}
        client._tool_call_params = {}
        client._tool_call_is_shell = {}
        client._tool_call_mcp_server = {}
        client._tool_call_tool_name = {}
        client._permission_options = {}
        # Deliberately omit the newer redaction-provenance cache: minimal and
        # legacy constructors must remain compatible without weakening the
        # permission event's authorization provenance.
        assert not hasattr(client, "_tool_call_input_redacted")
        # Simulate the ToolCall notification caching structured params...
        client._tool_call_params["tc-1"] = {"path": "/etc/passwd", "command": None}
        # ...then the request_permission message referencing the same toolCallId.
        msg = JsonRpcMessage(
            id="req-1",
            params={
                "toolCall": {
                    "toolCallId": "tc-1",
                    "title": "Editing /etc/passwd",
                    "kind": "edit",
                },
                "options": [],
            },
        )
        evt = client._build_permission_event(msg)
        assert evt.kind == EVENT_PERMISSION_REQUEST
        assert evt.raw_tool_params == {"path": "/etc/passwd", "command": None}
        assert evt.tool_kind == "edit"
        assert evt.tool_input_redacted is False  # no cached input to distrust


class TestGovernanceDegradedIsObservable:
    """A chokepoint that FAILS OPEN must not be silent."""

    def test_governance_permits_degrade_emits_warning_and_sel(self, monkeypatch, caplog):
        # Force an unexpected error inside resolve_active_scope so governance_permits
        # hits its except-branch and degrades to permit.
        _install({"version": 1, "boot": {"fail_closed": True}})

        def _boom(*a, **k):
            raise RuntimeError("simulated resolve regression")

        monkeypatch.setattr(gp, "resolve_active_scope", _boom)

        emitted: list = []
        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(
            sel_mod.sel(),
            "log_governance_degraded",
            lambda **kw: emitted.append(kw),
        )

        import logging

        with caplog.at_level(logging.WARNING):
            decision = gp.governance_permits("commands", "rm -rf /", session_key="cron:j:r")

        # Degrades to permit (so a latent regression cannot wedge the surface) ...
        assert decision.permitted is True
        # ... but the fail-open is now OBSERVABLE: a WARNING log + a SEL record.
        assert any("FAILED OPEN" in r.message for r in caplog.records)
        assert emitted, "governance_degraded SEL must be emitted on the degrade path"
        assert emitted[0]["chokepoint"] == "governance_permits"
        assert emitted[0]["scope"] == "commands"

    def test_stdio_chokepoint_degrade_is_sel_only_no_warning(self, monkeypatch, caplog):
        # The stdio MCP path passes log_warning=False (stderr would corrupt the
        # JSON-RPC stream) but STILL writes the file-backed SEL.
        emitted: list = []
        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(
            sel_mod.sel(), "log_governance_degraded", lambda **kw: emitted.append(kw)
        )
        import logging

        with caplog.at_level(logging.WARNING):
            gp.audit_governance_degraded(
                "send_message", session_key="slack:c", scope="channels", log_warning=False
            )
        assert emitted and emitted[0]["chokepoint"] == "send_message"
        assert not any("FAILED OPEN" in r.message for r in caplog.records)

    def test_sel_emit_failure_escalates_to_warning_even_when_silent(self, monkeypatch, caplog):
        # If the SEL write ITSELF fails AND log_warning=False (stdio path), the
        # fail-open would otherwise be completely invisible at prod log level.
        # The SEL-emit failure must escalate to WARNING regardless.
        import kiro_crew.sel as sel_mod

        def _boom(**kw):
            raise OSError("disk full")

        monkeypatch.setattr(sel_mod.sel(), "log_governance_degraded", _boom)
        import logging

        with caplog.at_level(logging.WARNING):
            gp.audit_governance_degraded(
                "learn_add", session_key="", scope="capabilities.memory_writes", log_warning=False
            )
        # The helper itself must not raise out of the (caller's) except-branch ...
        # ... and the audit-failure is now observable at WARNING.
        assert any("SEL emit FAILED" in r.message for r in caplog.records)

    def test_late_import_failure_does_not_propagate_from_chokepoint(self, monkeypatch):
        # Every chokepoint late-imports audit_governance_degraded inside its
        # except-branch. If that import fails (rename/partial install/cycle), it
        # must NOT raise out and convert the soft fail-open into a hard fail that
        # wedges the tool call. Simulate by making the symbol raise on access.
        _install({"version": 1, "boot": {"fail_closed": True}})

        def _boom(*a, **k):
            raise RuntimeError("forced gate regression")

        # Force the gate body to raise so the except-branch runs ...
        monkeypatch.setattr(gp, "resolve_active_scope", _boom)
        # ... and make the degrade-audit helper raise (stands in for an ImportError
        # of the late `from ... import audit_governance_degraded`).
        monkeypatch.setattr(gp, "audit_governance_degraded", _boom)

        from kiro_crew.hooks import HookManager

        hooks = HookManager()
        # Must return a decision (degrade to no-opinion), NOT raise.
        result = hooks.on_tool_call(
            "code", session_key="cli_chat", tool_kind="read", raw_params={"path": "/tmp/x"}
        )
        assert result is not None  # the call completed; no exception escaped

    def test_governance_permits_log_warning_false_suppresses_inner_warning(
        self, monkeypatch, caplog
    ):
        # A stdio MCP caller passes log_warning=False INTO governance_permits.  The
        # common degrade (a resolution error) is caught INSIDE governance_permits
        # and never re-raises, so the caller's own outer except cannot suppress it
        # — the flag must be honored at the inner emit point (follow-up).
        _install({"version": 1, "boot": {"fail_closed": True}})

        def _boom(*a, **k):
            raise RuntimeError("simulated resolve regression")

        monkeypatch.setattr(gp, "resolve_active_scope", _boom)

        emitted: list = []
        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(
            sel_mod.sel(), "log_governance_degraded", lambda **kw: emitted.append(kw)
        )
        import logging

        with caplog.at_level(logging.WARNING):
            decision = gp.governance_permits(
                "capabilities.messaging", "", session_key="slack:c", log_warning=False
            )

        # Still degrades to permit and still writes the durable SEL ...
        assert decision.permitted is True
        assert emitted and emitted[0]["chokepoint"] == "governance_permits"
        # ... but NO stderr WARNING (it would corrupt the stdio JSON-RPC stream).
        assert not any("FAILED OPEN" in r.message for r in caplog.records)

    def test_governance_floor_ordinal_log_warning_false_suppresses_inner_warning(
        self, monkeypatch, caplog
    ):
        # Same inner-suppression contract for the sandbox ordinal floor chokepoint.
        _install({"version": 1, "boot": {"fail_closed": True}})

        def _boom(*a, **k):
            raise RuntimeError("simulated resolve regression")

        monkeypatch.setattr(gp, "resolve_active_scope", _boom)

        emitted: list = []
        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(
            sel_mod.sel(), "log_governance_degraded", lambda **kw: emitted.append(kw)
        )
        import logging

        with caplog.at_level(logging.WARNING):
            floor = gp.governance_floor_ordinal(
                "sandbox.min_level", session_key="cron:j:r", log_warning=False
            )

        assert floor is None
        assert emitted and emitted[0]["chokepoint"] == "governance_floor_ordinal"
        assert not any("FAILED OPEN" in r.message for r in caplog.records)

    def test_degraded_sel_record_carries_app_and_unknown_source(self, monkeypatch, tmp_path):
        # The per-app fail-open must be attributable: the persisted SEL record
        # carries the ``app`` slug (so an investigator knows WHICH app's narrowing
        # was bypassed), and an empty session_key classifies source="unknown"
        # rather than being mis-tagged "slack". (follow-up #6/#8.)
        import json

        from kiro_crew.sel import SecurityEventLog

        SecurityEventLog._instance = None
        SecurityEventLog._initialized = False
        sel_dir = tmp_path / "sel"
        sel_obj = SecurityEventLog(base_dir=sel_dir, sync=True)
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: sel_obj)
        try:
            gp.audit_governance_degraded(
                "learn_add",
                scope="capabilities.memory_writes",
                app="file-explorer",
                log_warning=False,
            )

            sel_file = sel_dir / "security_events.jsonl"
            records = [
                json.loads(line)
                for line in sel_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            degraded = [r for r in records if r.get("event_type") == "governance_degraded"]
            assert degraded, "a governance_degraded SEL record must be persisted"
            rec = degraded[-1]
            assert rec["metadata"]["app"] == "file-explorer"
            assert rec["source"] == "unknown"  # empty session_key, NOT "slack"
        finally:
            SecurityEventLog._instance = None
            SecurityEventLog._initialized = False


class TestMatchPathNormalization:
    """`_match_path` normalizes the ITEM only — never the operator's pattern.

    Normalizing the pattern with ``os.path.normpath`` corrupts globs whose ``..``
    sits next to a wildcard (``/a/**/../b`` → ``/a/b``, dropping the ``**``),
    widening an allow / shrinking a deny. (follow-up.)
    """

    def test_traversal_item_does_not_satisfy_allow_prefix(self):
        from kiro_crew.platform.governance import _match_path

        assert not _match_path("/home/u/ws/../.bashrc", "/home/u/ws/**")
        # In-tree . / .. that stays inside still matches.
        assert _match_path("/home/u/ws/./src/app.py", "/home/u/ws/**")
        assert _match_path("/home/u/ws/a/../b/c.py", "/home/u/ws/**")

    def test_wildcard_adjacent_pattern_is_not_collapsed(self):
        import fnmatch

        from kiro_crew.platform.governance import _match_path

        # The pattern is matched verbatim: ``_match_path`` agrees with a raw
        # ``fnmatchcase`` on the un-collapsed glob (an absolute item needs no
        # normalization, isolating the pattern-handling).  If the pattern were
        # normpath'd to ``/srv/app/shared/**`` these two would diverge.
        item = "/srv/app/teamA/shared/data.txt"
        pat = "/srv/app/**/../shared/**"
        assert _match_path(item, pat) == fnmatch.fnmatchcase(item, pat)


# ── chokepoints fail CLOSED on governance error ──
class TestChokepointsFailClosed:
    def test_vet_spawn_governance_denies_on_error(self, monkeypatch):
        """A governance evaluation error must DENY the spawn (return a reason)."""
        from kiro_crew import subagent

        def _boom(*a, **k):
            raise RuntimeError("governance module broken")

        monkeypatch.setattr(gp, "governance_permits", _boom)
        reason = subagent._vet_spawn_governance("dashboard:ui", "researcher")
        assert reason is not None  # denial (None would mean allow)
        assert "fail-closed" in reason

    def test_vet_spawn_governance_reraises_composition_error(self, monkeypatch):
        """PlatformCompositionError still propagates (hard fail-closed CPP)."""
        from kiro_crew import subagent
        from kiro_crew.platform.context import PlatformCompositionError

        def _compose_fail(*a, **k):
            raise PlatformCompositionError("companion missing")

        monkeypatch.setattr(gp, "governance_permits", _compose_fail)
        with pytest.raises(PlatformCompositionError):
            subagent._vet_spawn_governance("dashboard:ui", "researcher")

    def test_enterprise_posture_denies_on_error(self, monkeypatch):
        """A governance evaluation error must DENY the workspace (return False)."""
        from kiro_crew.slack import enterprise

        def _boom(*a, **k):
            raise RuntimeError("governance module broken")

        monkeypatch.setattr(gp, "governance_permits", _boom)
        assert enterprise._governance_posture_permits_workspace("E_ATTACKER", "T_ATTACKER") is False

    def test_enterprise_posture_reraises_composition_error(self, monkeypatch):
        from kiro_crew.platform.context import PlatformCompositionError
        from kiro_crew.slack import enterprise

        def _compose_fail(*a, **k):
            raise PlatformCompositionError("companion missing")

        monkeypatch.setattr(gp, "governance_permits", _compose_fail)
        with pytest.raises(PlatformCompositionError):
            enterprise._governance_posture_permits_workspace("E1", "T1")
