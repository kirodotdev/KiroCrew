"""Tests for app-contributed session controls (composer session-panel slot).

A session control renders inside the composer, on the path of every turn, so
these tests weight the *refusal* paths: a malformed control must be rejected at
install time rather than discovered as a broken chat.
"""

from __future__ import annotations

import pytest

from kiro_crew.apps.manifest import (
    MAX_SESSION_CONTROLS_PER_APP,
    AppManifest,
    _normalize_status_path,
)

BASE = {
    "name": "my-app",
    "version": "1.0.0",
    "displayName": "My App",
    "description": "d",
}


def _mf(controls: list[dict], **contributes) -> AppManifest:
    return AppManifest.from_dict(
        {**BASE, "contributes": {"sessionControls": controls, **contributes}}
    )


class TestParsing:
    def test_parses_a_control(self):
        m = _mf(
            [{"id": "env-picker", "entryPoint": "dist/c.mjs", "label": "Scope", "icon": "Share2"}]
        )
        assert len(m.contributes.sessionControls) == 1
        c = m.contributes.sessionControls[0]
        assert (c.id, c.entryPoint, c.label, c.icon) == (
            "env-picker",
            "dist/c.mjs",
            "Scope",
            "Share2",
        )

    def test_non_dict_entries_are_retained_as_placeholders_not_dropped(self):
        """A non-dict entry must survive parsing so ``validate()`` can refuse it.

        Parsing stays non-fatal — a hand-edited manifest must not raise — but the
        entry is kept as an empty placeholder rather than discarded. Dropping it
        made the manifest install clean with the control silently absent, which
        contradicts the promise that a malformed control is refused at install
        time instead of being discovered later as a broken chat.
        """
        m = AppManifest.from_dict(
            {
                **BASE,
                "contributes": {"sessionControls": ["nope", 7, {"id": "a", "entryPoint": "c.mjs"}]},
            }
        )
        assert [c.id for c in m.contributes.sessionControls] == ["", "", "a"]

    def test_a_non_dict_entry_is_reported_by_validate(self):
        """The placeholder is only worth keeping if it actually surfaces."""
        m = AppManifest.from_dict(
            {
                **BASE,
                "contributes": {"sessionControls": ["nope", {"id": "a", "entryPoint": "c.mjs"}]},
            }
        )
        errors = m.validate()
        assert any("missing required field: id" in e for e in errors)

    def test_a_non_list_sessioncontrols_is_an_empty_list(self):
        """A hand-edited manifest carrying a non-list must not raise on parse."""
        m = AppManifest.from_dict({**BASE, "contributes": {"sessionControls": None}})
        assert m.contributes.sessionControls == []

    def test_a_non_list_sessioncontrols_is_refused_not_silently_empty(self):
        """Coercing to `[]` reads as a deliberate empty list.

        Mirrors `contributes.commands`: without this the app installs clean and
        no chip ever appears, so the author sees neither an error nor a control.
        """
        m = AppManifest.from_dict({**BASE, "contributes": {"sessionControls": "nope"}})
        assert any("contributes.sessionControls must be an array" in e for e in m.validate())

    def test_absent_sessioncontrols_is_an_empty_list(self):
        """An app declaring none must be byte-for-byte unaffected."""
        m = AppManifest.from_dict({**BASE, "ui": {"entry": "dist/p.mjs"}})
        assert m.contributes.sessionControls == []
        # `contributes` must not appear at all, rather than as an empty object:
        # an app with no contribution has to serialize exactly as it did before
        # the block existed, or every unrelated manifest's signature changes.
        assert "contributes" not in m.to_dict()

    def test_round_trip_preserves_controls(self):
        m = _mf([{"id": "a", "entryPoint": "c.mjs", "label": "L"}])
        again = AppManifest.from_dict(m.to_dict())
        assert [c.to_dict() for c in again.contributes.sessionControls] == [
            {"id": "a", "entryPoint": "c.mjs", "label": "L"}
        ]


class TestValidation:
    def test_a_well_formed_control_validates_clean(self):
        assert _mf([{"id": "env-picker", "entryPoint": "dist/c.mjs"}]).validate() == []

    def test_a_trailing_newline_is_refused_on_both_patterns(self):
        """Regression: `$` also matches BEFORE a trailing newline.

        These two patterns were checked with ``re.match``, so ``"a\\n"`` and
        ``"session-status\\n"`` validated clean and the statusPath then reached
        the URL the dashboard builds. The module docstring calls this trap out —
        both must use ``fullmatch``.
        """
        assert any(
            "must be kebab-case" in e
            for e in _mf([{"id": "a\n", "entryPoint": "c.mjs"}]).validate()
        )
        assert any(
            "statusPath must be a relative" in e
            for e in _mf(
                [{"id": "a", "entryPoint": "c.mjs", "statusPath": "session-status\n"}]
            ).validate()
        )

    def test_id_is_required(self):
        assert any(
            "missing required field: id" in e for e in _mf([{"entryPoint": "c.mjs"}]).validate()
        )

    def test_entrypoint_is_required(self):
        errs = _mf([{"id": "a"}]).validate()
        assert any("missing required field: entryPoint" in e for e in errs)

    @pytest.mark.parametrize(
        "bad", ["Bad_Id", "has space", "Trailing-", "-leading", "UPPER", "a--b"]
    )
    def test_id_must_be_kebab_case(self, bad):
        assert any("kebab-case" in e for e in _mf([{"id": bad, "entryPoint": "c.mjs"}]).validate())

    def test_duplicate_ids_are_rejected(self):
        """Duplicates would make two controls indistinguishable to React's key."""
        errs = _mf(
            [{"id": "a", "entryPoint": "x.mjs"}, {"id": "a", "entryPoint": "y.mjs"}]
        ).validate()
        assert any("duplicated" in e for e in errs)

    def test_entrypoint_path_traversal_is_rejected(self):
        errs = _mf([{"id": "a", "entryPoint": "../../etc/passwd"}]).validate()
        assert any("path traversal" in e for e in errs)

    def test_per_app_cap_is_enforced(self):
        over = [
            {"id": f"c{i}", "entryPoint": f"{i}.mjs"}
            for i in range(MAX_SESSION_CONTROLS_PER_APP + 1)
        ]
        assert any("at most" in e for e in _mf(over).validate())

    def test_exactly_at_the_cap_is_allowed(self):
        at = [
            {"id": f"c{i}", "entryPoint": f"{i}.mjs"} for i in range(MAX_SESSION_CONTROLS_PER_APP)
        ]
        assert _mf(at).validate() == []

    def test_pages_still_validate_independently(self):
        """The new block must not disturb the existing page checks."""
        m = AppManifest.from_dict(
            {
                **BASE,
                "ui": {"pages": [{"route": "/apps/x"}]},
                "contributes": {"sessionControls": []},
            }
        )
        assert any("label" in e for e in m.validate())


class TestSessionControlStatusPath:
    """``statusPath`` lets a chip carry state before its module is imported.

    Without it a control can only report anything on first click, so a
    per-session setting looks unset until the user goes looking for it.
    """

    def _manifest(self, **ctl):
        return AppManifest.from_dict(
            {
                "name": "demo",
                "version": "1.0.0",
                "displayName": "Demo",
                "description": "d",
                "contributes": {
                    "sessionControls": [{"id": "c", "entryPoint": "dist/c.mjs", **ctl}]
                },
            }
        )

    def test_absent_by_default_and_omitted_from_output(self):
        m = self._manifest()
        assert m.contributes.sessionControls[0].statusPath == ""
        assert "statusPath" not in m.to_dict()["contributes"]["sessionControls"][0]

    def test_round_trips(self):
        m = self._manifest(statusPath="session-status")
        assert m.to_dict()["contributes"]["sessionControls"][0]["statusPath"] == "session-status"
        again = AppManifest.from_dict(m.to_dict())
        assert again.contributes.sessionControls[0].statusPath == "session-status"

    def test_a_leading_slash_is_normalized_away(self):
        """The dashboard joins it to the app's route base, so one form is stored."""
        assert self._manifest(statusPath="/session-status").contributes.sessionControls[
            0
        ].statusPath == ("session-status")

    def test_accepts_nested_relative_routes(self):
        assert self._manifest(statusPath="status/session_1-a").validate(None) == []

    @pytest.mark.parametrize(
        "bad",
        [
            "https://evil.example/x",  # another origin
            "//evil.example/x",  # protocol-relative
            # A DOTLESS protocol-relative host. The case above is refused by
            # the charset (the `.`), not by any host check, so it passes
            # whether or not the cross-origin guard exists. This one is the
            # guard's actual test: normalization must not strip the `//` into
            # a plausible relative route.
            "//evilhost/x",  # protocol-relative, dotless host
            "../../etc/passwd",  # traversal
            "status?session=1",  # caller owns the query
            "Status",  # charset is bounded
            "x" * 70,  # length is bounded
        ],
    )
    def test_refuses_anything_that_is_not_a_relative_backend_route(self, bad):
        errs = [e for e in self._manifest(statusPath=bad).validate(None) if "statusPath" in e]
        assert errs, f"expected {bad!r} to be refused"

    def test_normalization_does_not_launder_a_protocol_relative_host(self):
        """`//host/x` must survive normalization intact so validation can refuse it.

        Stripping its slashes would produce `host/x`, which satisfies the route
        allowlist and reads like an ordinary relative path — accepting a
        cross-origin declaration by rewriting it.
        """
        assert _normalize_status_path("//evilhost/x") == "//evilhost/x"
        # A single leading slash is a harmless way to write a relative route.
        assert _normalize_status_path("/session-status") == "session-status"
        assert _normalize_status_path("session-status") == "session-status"

    def test_the_refusal_names_the_control_and_the_value(self):
        errs = [e for e in self._manifest(statusPath="Nope").validate(None) if "statusPath" in e]
        assert "c" in errs[0] and "Nope" in errs[0]

    def test_a_bad_status_path_does_not_mask_other_errors(self):
        m = AppManifest.from_dict(
            {
                "name": "demo",
                "version": "1.0.0",
                "displayName": "Demo",
                "description": "d",
                "contributes": {"sessionControls": [{"id": "BAD ID", "statusPath": "Nope"}]},
            }
        )
        errs = m.validate(None)
        assert any("kebab-case" in e for e in errs)
        assert any("entryPoint" in e for e in errs)
        assert any("statusPath" in e for e in errs)
