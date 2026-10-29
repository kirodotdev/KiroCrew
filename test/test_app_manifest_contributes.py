"""What a manifest's ``contributes`` block may declare, and what its signature freezes.

The command keys, ``kind`` names and argument token a published signature covers
cannot be renamed. A contributed command refuses a malformed id, title, prompt or
matcher and a duplicate id, and its agent must be one of the app's own; panel tabs
parse, round-trip and are signed, and refuse missing fields, bad ids and over-cap
values. The minimal valid manifest lives in ``app_manifest_helpers.py``.
"""

from __future__ import annotations

import json

import pytest
from app_manifest_helpers import (
    _valid_manifest,
)

from kiro_crew.apps.manifest import ARGUMENT_TOKEN, AppManifest, CommandArgument


class TestSignatureFrozenVocabulary:
    """The contribution vocabulary that a published signature makes unrenamable.

    ``contributes`` is inside ``signing_payload()``, so for any app that has already
    been SIGNED the canonical bytes of its commands are part of what the signature
    authenticates. Renaming an emitted key, or renaming a ``kind`` value, changes those
    bytes and invalidates the signature of an app nobody has touched. Adding a new key
    or a new kind does not, because emission is conditional and absent keys were never
    in the bytes.

    So this vocabulary cannot change after the first signed use. These tests turn that
    into a gate: a rename fails HERE, with this docstring attached, instead of failing
    in a user's install months later.

    Deliberately NOT pinned: the caps. A cap is a validation threshold and appears
    nowhere in the payload, so raising or lowering one cannot invalidate a signature.
    They have their own invariant -- both validators must agree -- which the shared
    conformance fixture already enforces. Freezing them here would claim a
    compatibility consequence they do not have.
    """

    #: Every key `CommandContribution.to_dict()` can emit.
    COMMAND_KEYS = frozenset(
        {"id", "title", "subtitle", "icon", "keywords", "prompt", "autoSend", "agent", "argument"}
    )
    #: Every key `CommandArgument.to_dict()` can emit.
    ARGUMENT_KEYS = frozenset({"placeholder", "hint", "kind", "hosts", "patternError"})

    @staticmethod
    def _full_command() -> dict:
        return {
            "id": "approve-all",
            "title": "Approve all PRs",
            "subtitle": "Approve every pull request behind a link",
            "icon": "Check",
            "keywords": ["pr"],
            "prompt": "Approve everything behind {argument}.",
            "autoSend": True,
            "agent": "ticket-analyst",
            "argument": {
                "placeholder": "Paste a link",
                "hint": "A PR search or a single pull request.",
                "kind": "url",
                "hosts": ["github.com"],
                "patternError": "Not a GitHub link.",
            },
        }

    def _serialized(self) -> dict:
        m = AppManifest.from_dict(
            {
                "name": "pr-bulk-ops",
                "version": "1.0.0",
                "displayName": "PR Bulk Ops",
                "description": "Bulk operations over the pull requests behind one link.",
                "contributes": {"commands": [self._full_command()]},
                "agents": ["agents/ticket-analyst.json"],
            }
        )
        assert m.validate() == [], m.validate()
        return m.to_dict()["contributes"]["commands"][0]

    def test_the_emitted_command_keys_are_exactly_these(self):
        # A RENAME shows up here as one key missing and one added, which is the change
        # that breaks an existing signature. A new OPTIONAL key shows up as an addition
        # only, and is compatible -- so read a failure by which direction it moved.
        assert set(self._serialized()) == self.COMMAND_KEYS

    def test_the_emitted_argument_keys_are_exactly_these(self):
        assert set(self._serialized()["argument"]) == self.ARGUMENT_KEYS

    def test_the_kind_names_are_frozen(self):
        # `kind` is emitted VERBATIM, so its values are part of the signed bytes in a way
        # the other fields' values are not: renaming `url` to `link` would invalidate
        # every signed app that declared a url argument. Adding a third kind is fine.
        assert CommandArgument.KINDS == ("url", "text")

    def test_the_argument_token_spelling_is_frozen(self):
        # The token lives inside the prompt TEXT, so respelling it changes the prompt
        # bytes of every app that interpolates -- and silently, since a prompt carrying
        # the old spelling would still serialize, just never interpolate again.
        assert ARGUMENT_TOKEN == "{argument}"

    def test_command_order_is_preserved_because_the_payload_depends_on_it(self):
        # The payload emits the list as given, so sorting or de-duplicating it would be a
        # signature-relevant change even with every command identical.
        ids = ["a-cmd", "b-cmd", "c-cmd"]
        cmds = []
        for cid in reversed(ids):
            c = self._full_command()
            c["id"] = cid
            cmds.append(c)
        m = AppManifest.from_dict(
            {
                "name": "pr-bulk-ops",
                "version": "1.0.0",
                "displayName": "PR Bulk Ops",
                "description": "Bulk operations over the pull requests behind one link.",
                "contributes": {"commands": cmds},
            }
        )
        emitted = [c["id"] for c in m.to_dict()["contributes"]["commands"]]
        assert emitted == list(reversed(ids))


class TestContributedCommands:
    """`contributes.commands` -- the seam that lets a launcher row live outside this repo.

    The declaration is third-party data that becomes the text of an instruction sent
    to an agent with tools, so what these tests pin is mostly what the schema
    REFUSES. The matcher cases are load-bearing beyond their size: an unknown kind
    accepts any string that merely contains a match, and a prompt/argument
    disagreement produces a command that still runs while ignoring the value the
    reader was asked for.
    """

    @staticmethod
    def _manifest(command: dict) -> dict:
        return {
            "name": "pr-bulk-ops",
            "version": "1.0.0",
            "displayName": "PR Bulk Ops",
            "description": "Bulk operations over the pull requests behind one link.",
            "contributes": {"commands": [command]},
        }

    @staticmethod
    def _command(**over) -> dict:
        cmd = {
            "id": "approve-all",
            "title": "Approve all PRs",
            "subtitle": "Approve every pull request behind a link",
            "icon": "Check",
            "keywords": ["pr", "lgtm"],
            "prompt": "Approve every pull request behind {argument}.",
            "autoSend": True,
            "argument": {
                "placeholder": "Paste a GitHub link",
                "hint": "A PR search, a label, or a single pull request.",
                "kind": "url",
                "hosts": ["github.com"],
                "patternError": "Not a GitHub link.",
            },
        }
        arg_over = over.pop("argument_over", None)
        cmd.update(over)
        if arg_over is not None:
            cmd["argument"] = {**cmd["argument"], **arg_over}
        return cmd

    def test_a_well_formed_contribution_validates(self):
        m = AppManifest.from_dict(self._manifest(self._command()))
        assert m.validate() == []
        assert len(m.contributes.commands) == 1
        cmd = m.contributes.commands[0]
        assert cmd.id == "approve-all"
        assert cmd.autoSend is True
        assert cmd.argument is not None
        assert cmd.keywords == ["pr", "lgtm"]

    def test_it_round_trips_through_to_dict(self):
        # to_dict is what `/api/apps` sends, so a field lost here never reaches the
        # launcher no matter how well it parsed.
        m = AppManifest.from_dict(self._manifest(self._command()))
        raw = json.loads(json.dumps(m.to_dict()))
        assert "contributes" in raw
        again = AppManifest.from_dict(raw)
        assert again.validate() == []
        assert again.contributes.commands[0].prompt == m.contributes.commands[0].prompt
        assert again.contributes.commands[0].argument.kind == (
            m.contributes.commands[0].argument.kind
        )

    def test_absent_contributes_is_empty_not_an_error(self):
        m = AppManifest.from_dict(
            {"name": "plain", "version": "1.0.0", "displayName": "P", "description": "d"}
        )
        assert m.contributes.commands == []
        assert "contributes" not in m.to_dict()

    def test_contributes_is_a_known_field_so_it_is_not_swallowed_by_extra(self):
        # `extra` is the UNVALIDATED bucket. If this key landed there it would still
        # reach the dashboard (to_dict merges extra) while skipping every check below.
        m = AppManifest.from_dict(self._manifest(self._command()))
        assert "contributes" not in m.extra

    @pytest.mark.parametrize(
        "over,fragment",
        [
            ({"id": "Approve_All"}, "lowercase alphanumeric"),
            ({"id": ""}, "missing id"),
            ({"id": "-leading"}, "lowercase alphanumeric"),
            ({"title": ""}, "missing title"),
            ({"prompt": ""}, "missing prompt"),
            ({"prompt": "x" * 4001}, "exceeds 4000"),
            ({"prompt": "Approve everything"}, "never uses {argument}"),
        ],
    )
    def test_refuses_a_malformed_command(self, over, fragment):
        errors = AppManifest.from_dict(self._manifest(self._command(**over))).validate()
        assert any(fragment in e for e in errors), errors

    @pytest.mark.parametrize(
        "argument_over,fragment",
        [
            ({"kind": "regex"}, "argument.kind must be one of"),
            ({"kind": ""}, "argument.kind must be one of"),
            ({"kind": "text", "hosts": ["github.com"]}, "applies only to kind 'url'"),
            ({"kind": "url", "hosts": ["h%s.test" % n for n in range(25)]}, "exceeds 20"),
            ({"kind": "url", "hosts": ["not a hostname"]}, "is not a hostname"),
            ({"kind": "url", "hosts": ["https://github.com"]}, "is not a hostname"),
            ({"kind": "url", "hosts": ["localhost"]}, "is not a hostname"),
        ],
    )
    def test_refuses_a_bad_argument_matcher(self, argument_over, fragment):
        errors = AppManifest.from_dict(
            self._manifest(self._command(argument_over=argument_over))
        ).validate()
        assert any(fragment in e for e in errors), errors

    def test_an_argument_without_a_kind_defaults_to_text(self):
        # Defaulting is safe here in a way that defaulting an UNKNOWN kind is not: the
        # app said nothing, so the loosest matcher is what it asked for, and the value
        # still gets a preview before anything is sent.
        cmd = self._command()
        cmd["argument"].pop("kind", None)
        cmd["argument"].pop("hosts", None)
        m = AppManifest.from_dict(self._manifest(cmd))
        assert m.validate() == []
        assert m.contributes.commands[0].argument.kind == "text"

    def test_a_stale_contract_pattern_is_refused_not_ignored(self):
        # `pattern` is an unknown key now, so the silent outcome would be the dangerous
        # one: dropped key, `kind` defaults to `text`, ANY non-empty string accepted,
        # and the app still declares autoSend believing its pattern guards the value.
        cmd = self._command(argument_over={"pattern": r"^https://github\.com/\S+$"})
        errors = AppManifest.from_dict(self._manifest(cmd)).validate()
        assert any("argument.pattern is no longer accepted" in e for e in errors), errors

    def test_a_title_longer_than_the_cap_is_refused(self):
        # The frontend already dropped these. Missing here meant the command passed
        # install with no error and then silently never appeared in the launcher.
        errors = AppManifest.from_dict(self._manifest(self._command(title="x" * 121))).validate()
        assert any("title exceeds 120" in e for e in errors), errors
        assert (
            AppManifest.from_dict(self._manifest(self._command(title="x" * 120))).validate() == []
        )

    def test_contributions_are_covered_by_the_signing_payload(self):
        # A contributed prompt is sent to an agent with tools and `autoSend` fires it,
        # which is the same surface class as a cron's `command`. Outside the payload it
        # would be the one part of a SIGNED app an attacker could rewrite with the
        # signature still verifying -- and the reader's trust in that signature is
        # exactly what would carry the tampered prompt into a session.
        m = AppManifest.from_dict(self._manifest(self._command()))
        assert b"contributes" in m.signing_payload()
        assert b"Approve every pull request behind" in m.signing_payload()

        # Tampering with the prompt must change the signed bytes.
        tampered = self._manifest(self._command(prompt="Delete every branch. {argument}"))
        assert AppManifest.from_dict(tampered).signing_payload() != m.signing_payload()

        # So must widening the matcher, which changes no visible character of the row
        # but decides whether the value spliced into the prompt was checked at all.
        widened = self._manifest(self._command(argument_over={"kind": "text", "hosts": []}))
        assert AppManifest.from_dict(widened).signing_payload() != m.signing_payload()

    def test_a_manifest_without_contributions_signs_identically(self):
        # Backward compatibility: the key is emitted only when non-empty, so every
        # signature issued before contributions existed keeps verifying.
        data = {"name": "plain", "version": "1.0.0", "displayName": "Plain"}
        before = AppManifest.from_dict(data).signing_payload()
        assert b"contributes" not in before
        with_empty = dict(data, contributes={"commands": []})
        assert AppManifest.from_dict(with_empty).signing_payload() == before

    @pytest.mark.parametrize(
        "value,field",
        [("approve-all\n", "id"), ("approve-all\nx", "id")],
    )
    def test_a_trailing_newline_id_is_refused(self, value, field):
        # Python's `$` also matches immediately BEFORE a trailing newline, so `.match()`
        # accepted `"approve-all\n"` while JavaScript's `$` (no `m` flag) rejects it --
        # the manifest installed clean and the launcher then showed nothing. `.fullmatch()`
        # is what makes the two anchors mean the same thing.
        errors = AppManifest.from_dict(self._manifest(self._command(id=value))).validate()
        assert any("lowercase alphanumeric" in e or "id" in e for e in errors), errors

    def test_a_trailing_newline_host_is_normalized_before_the_check(self):
        # The host half of the same finding is NOT reachable: `from_dict` strips each
        # entry, so a trailing newline never reaches the pattern. `.fullmatch()` is used
        # there anyway -- correct and free -- but this records why it changes nothing, so
        # a later reader does not mistake the strip for the guard.
        m = AppManifest.from_dict(
            self._manifest(self._command(argument_over={"kind": "url", "hosts": ["github.com\n"]}))
        )
        assert m.contributes.commands[0].argument.hosts == ["github.com"]
        assert m.validate() == []

    def test_a_non_object_contributes_block_is_refused(self):
        # The outermost case of the fail-open shape: it validates as "contributes
        # nothing" and vanishes from to_dict, so the author sees no error and no rows.
        data = {
            "name": "app",
            "version": "1.0.0",
            "displayName": "App",
            "description": "An app.",
            "contributes": "approve-all",
        }
        m = AppManifest.from_dict(data)
        errors = [e for e in m.validate() if "contributes" in e]
        assert any("must be an object" in e for e in errors), errors

    @pytest.mark.parametrize(
        "argument,why",
        [
            (
                {"kind": "text", "pattern": r"^https://github\.com/\S+$"},
                "retired pattern",
            ),
            ({"kind": "url", "hosts": "github.com"}, "scalar hosts"),
            ("yes", "argument is not an object"),
        ],
    )
    def test_serialization_will_not_emit_a_restriction_it_cannot_carry(self, argument, why):
        # `list_apps()` reads a manifest off disk and re-serializes it with NO
        # validate() in between, and the refusal flags describe the input so they are
        # not serialized. For a manifest edited after install that was the whole gap:
        # `pattern` and a scalar `hosts` both vanished and what reached the dashboard
        # was a well-formed argument on the DEFAULT matcher -- text, any host -- so the
        # frontend's mirror of these refusals had nothing to fire on and the value went
        # to the agent unchecked. Emitting nothing is the fail-closed direction, and the
        # install path already refused this loudly.
        cmd = self._command()
        cmd["argument"] = argument
        m = AppManifest.from_dict(self._manifest(cmd))
        assert m.validate(), f"{why}: expected validate() to refuse this"
        assert m.to_dict().get("contributes", {}).get("commands", []) == [], why

    def test_serialization_still_carries_a_legitimate_restriction(self):
        # The converse, so the guard above cannot pass by emitting nothing ever.
        m = AppManifest.from_dict(self._manifest(self._command()))
        assert m.validate() == []
        (cmd,) = m.to_dict()["contributes"]["commands"]
        assert cmd["argument"]["kind"] == "url"
        assert cmd["argument"]["hosts"] == ["github.com"]

    def test_a_non_object_entry_is_reported_not_filtered_out(self):
        # `commands` being a list is not enough: a single bad ELEMENT was filtered out
        # before validation, so an app declaring two commands with one typo installed
        # with one row and its author was never told the other vanished.
        data = self._manifest(self._command())
        data["contributes"]["commands"].append("approve-all")
        m = AppManifest.from_dict(data)
        errors = [e for e in m.validate() if "must be an object" in e]
        assert errors, m.validate()
        assert "1 entry" in errors[0], errors[0]

    def test_a_lone_surrogate_is_validated_not_crashed_on(self):
        # JSON can carry an UNPAIRED `\ud800` escape and `json.loads` accepts it, so this
        # string does reach validation. Measuring it with a plain utf-16-le encode raised
        # UnicodeEncodeError, which is a crash where an install error was owed. The count
        # still has to match the host: JavaScript reports 4 for "\ud800bad", so the cap
        # must see 4 and this title must simply pass.
        cmd = self._command()
        cmd["title"] = "\ud800bad"
        m = AppManifest.from_dict(self._manifest(cmd))
        errors = m.validate()  # must not raise
        assert not any("title exceeds" in e for e in errors), errors

    def test_a_lone_surrogate_still_counts_against_the_cap(self):
        # The converse: surrogatepass must not become a way to smuggle an over-cap title
        # past the bound it exists to measure.
        cmd = self._command()
        cmd["title"] = "\ud800" * 121
        errors = AppManifest.from_dict(self._manifest(cmd)).validate()
        assert any("title exceeds" in e for e in errors), errors

    def test_the_caps_are_measured_the_way_the_launcher_measures_them(self):
        # The frontend compares `.length`, which counts UTF-16 units, while `len()`
        # counts code points -- so an astral character counts once here and twice there.
        # A title of 100 emoji is 100 to `len()` and 200 to the launcher: it passed the
        # manifest and was then dropped by the renderer, which is the same
        # only-one-side-enforces failure the title cap was added for.
        cmd = self._command()
        cmd["title"] = "\U0001f600" * 100  # 100 code points, 200 UTF-16 units
        errors = AppManifest.from_dict(self._manifest(cmd)).validate()
        assert any("title exceeds" in e for e in errors), errors
        assert any("(200)" in e for e in errors), errors

    def test_a_bmp_title_at_the_cap_still_passes(self):
        # The converse, so the cap cannot pass by rejecting everything: 120 plain
        # characters are 120 units either way and must remain acceptable.
        cmd = self._command()
        cmd["title"] = "a" * 120
        errors = AppManifest.from_dict(self._manifest(cmd)).validate()
        assert not any("title exceeds" in e for e in errors), errors

    def test_the_matcher_survives_a_round_trip(self):
        m = AppManifest.from_dict(
            self._manifest(self._command(argument_over={"kind": "url", "hosts": ["GitHub.com"]}))
        )
        arg = AppManifest.from_dict(m.to_dict()).contributes.commands[0].argument
        # Lower-cased on parse, because it is compared against a parsed URL's hostname.
        assert arg.kind == "url"
        assert arg.hosts == ["github.com"]

    def test_a_prompt_interpolating_without_an_argument_is_refused(self):
        cmd = self._command()
        cmd.pop("argument")
        errors = AppManifest.from_dict(self._manifest(cmd)).validate()
        assert any("declares no argument" in e for e in errors), errors

    def test_a_command_with_no_argument_is_fine_when_the_prompt_needs_none(self):
        m = AppManifest.from_dict(
            self._manifest(
                {"id": "standup", "title": "Write my standup", "prompt": "Summarise yesterday."}
            )
        )
        assert m.validate() == []
        assert m.contributes.commands[0].argument is None

    def test_duplicate_ids_are_refused(self):
        # Two rows under one id: the second silently takes the frecency record and one
        # of them becomes unreachable by usage.
        data = self._manifest(self._command())
        data["contributes"]["commands"].append(self._command())
        errors = AppManifest.from_dict(data).validate()
        assert any("duplicate id" in e for e in errors), errors

    @pytest.mark.parametrize("raw", [None, 0, "commands", [], {"commands": "x"}])
    def test_a_hostile_contributes_block_never_raises(self, raw):
        # A manifest that cannot be parsed must fail as errors, never as an exception
        # on the install path.
        data = {
            "name": "x",
            "version": "1.0.0",
            "displayName": "X",
            "description": "d",
            "contributes": raw,
        }
        m = AppManifest.from_dict(data)
        assert m.contributes.commands == []
        m.validate()

    def test_non_dict_entries_in_the_command_list_are_reported(self):
        # Bad entries must be REPORTED, not silently filtered out: dropping them is the
        # fail-open shape this class keeps producing, where entries vanish and the app
        # installs with fewer rows and its author is told nothing. The parse still skips
        # them (so one typo cannot take the whole array down), but validation names the
        # count.
        data = self._manifest(self._command())
        data["contributes"]["commands"] = ["approve-all", None, 7, self._command()]
        m = AppManifest.from_dict(data)
        assert len(m.contributes.commands) == 1
        errors = [e for e in m.validate() if "must be an object" in e]
        assert errors, m.validate()
        assert "3 entries" in errors[0], errors[0]

    @pytest.mark.parametrize("raw", ["false", "true", "yes", 1, {}, [], None])
    def test_autosend_honours_only_the_json_boolean(self, raw):
        # `bool("false")` is True. A coercing read would let a manifest that says
        # "false" enable the one capability that sends text on the reader's behalf,
        # and then serialize it back as `true`.
        m = AppManifest.from_dict(self._manifest(self._command(autoSend=raw)))
        assert m.contributes.commands[0].autoSend is False
        assert "autoSend" not in m.to_dict()["contributes"]["commands"][0]

    def test_autosend_requires_an_argument(self):
        # The host shows the resolved prompt in the ARGUMENT field before sending. A
        # command with no argument never reaches that step, so autoSend there would
        # send app-authored text with nothing shown at all.
        cmd = {
            "id": "standup",
            "title": "Write my standup",
            "prompt": "Summarise yesterday.",
            "autoSend": True,
        }
        errors = AppManifest.from_dict(self._manifest(cmd)).validate()
        assert any("autoSend requires an argument" in e for e in errors), errors
        # Without autoSend the same command is fine.
        cmd.pop("autoSend")
        assert AppManifest.from_dict(self._manifest(cmd)).validate() == []

    @staticmethod
    def _with_agents(command: dict, agents: list[str]) -> dict:
        m = TestContributedCommands._manifest(command)
        m["agents"] = agents
        return m

    def test_agent_names_one_of_the_apps_own_agents(self):
        # Without an app root only the file stem is known -- the same fallback
        # registration uses -- so `agents/ticket-analyst.json` registers as
        # `ticket-analyst`.
        m = AppManifest.from_dict(
            self._with_agents(self._command(agent="ticket-analyst"), ["agents/ticket-analyst.json"])
        )
        assert m.validate() == []
        assert m.contributes.commands[0].agent == "ticket-analyst"
        assert m.to_dict()["contributes"]["commands"][0]["agent"] == "ticket-analyst"

    def test_agent_resolves_against_the_declared_name_under_an_app_root(self, tmp_path):
        # With the app root known, an agent registers under its JSON `name`, not its
        # stem (bridges._register_agents), so that is what the command must name.
        (tmp_path / "agents").mkdir()
        (tmp_path / "agents" / "spec.json").write_text('{"name": "ticket-analyst"}')
        m = AppManifest.from_dict(
            self._with_agents(self._command(agent="ticket-analyst"), ["agents/spec.json"])
        )
        assert m.validate(app_root=tmp_path) == []
        stem = AppManifest.from_dict(
            self._with_agents(self._command(agent="spec"), ["agents/spec.json"])
        )
        assert any(
            "not one of this app's own agents" in e for e in stem.validate(app_root=tmp_path)
        )

    @pytest.mark.parametrize("agents", [[], ["agents/other.json"]])
    def test_refuses_an_agent_the_app_does_not_ship(self, agents):
        # Containment: the prompt is app-authored, so naming a HOST agent (which may
        # carry broader tool grants and auto-approvals than the default) is refused
        # at install rather than letting the app borrow that agent's reach.
        errors = AppManifest.from_dict(
            self._with_agents(self._command(agent="kirocrew"), agents)
        ).validate()
        assert any(
            "agent 'kirocrew' is not one of this app's own agents" in e for e in errors
        ), errors

    def test_an_agent_path_escaping_the_root_contributes_no_name(self):
        # Registration skips such a path, so it cannot vouch for a command's agent.
        errors = AppManifest.from_dict(
            self._with_agents(self._command(agent="evil"), ["../evil.json"])
        ).validate()
        assert any("not one of this app's own agents" in e for e in errors), errors

    @pytest.mark.parametrize(
        "spec",
        [None, "{not json", "[]", '"ticket-analyst"', "null", "{}"],
        ids=["missing", "malformed", "array", "scalar", "null", "nameless"],
    )
    def test_a_spec_registration_skips_vouches_for_no_name(self, tmp_path, spec):
        # Registration skips a missing, unreadable, malformed or non-object spec, and
        # registers a nameless one under no name a command can select, so its stem
        # must not authorize the command: the bare name would otherwise resolve to
        # whatever HOST agent carries it.
        (tmp_path / "agents").mkdir()
        if spec is not None:
            (tmp_path / "agents" / "ticket-analyst.json").write_text(spec)
        errors = AppManifest.from_dict(
            self._with_agents(self._command(agent="ticket-analyst"), ["agents/ticket-analyst.json"])
        ).validate(app_root=tmp_path)
        assert any("not one of this app's own agents" in e for e in errors), errors

    def test_a_templated_spec_vouches_for_no_name(self, tmp_path):
        # Registration renders a templated spec and registers NOTHING when a
        # placeholder is unresolved, so the spec's `name` (which may be another app's
        # or the host's agent) must not authorize the command.
        (tmp_path / "agents").mkdir()
        (tmp_path / "agents" / "x.json").write_text('{"name": "mochi", "description": "{NOPE}"}')
        errors = AppManifest.from_dict(
            self._with_agents(self._command(agent="mochi"), ["agents/x.json"])
        ).validate(app_root=tmp_path)
        assert any("not one of this app's own agents" in e for e in errors), errors

    @pytest.mark.parametrize("bad", [".", ".."])
    def test_an_unsafe_declared_name_vouches_for_nothing(self, tmp_path, bad):
        # `_register_agents` refuses these names, so they authorize nothing either.
        (tmp_path / "agents").mkdir()
        (tmp_path / "agents" / "x.json").write_text(json.dumps({"name": bad}))
        errors = AppManifest.from_dict(
            self._with_agents(self._command(agent=bad), ["agents/x.json"])
        ).validate(app_root=tmp_path)
        assert any("not one of this app's own agents" in e for e in errors), errors

    def test_the_listing_drops_an_agent_the_app_does_not_ship(self, tmp_path):
        # A self-managed app's app.json never passes through install validation, so
        # the listing the launcher reads re-checks every command's agent against the
        # files on disk. An unverified agent is dropped; the command itself stays and
        # runs on the dashboard default.
        from kiro_crew.apps.manifest import contain_command_agents

        (tmp_path / "agents").mkdir()
        (tmp_path / "agents" / "spec.json").write_text('{"name": "ticket-analyst"}')
        data = {
            "contributes": {
                "commands": [
                    {"id": "own", "agent": "ticket-analyst"},
                    {"id": "host", "agent": "kirocrew"},
                    {"id": "stem", "agent": "spec"},
                    {"id": "ghost", "agent": "ghost"},
                    {"id": "odd", "agent": 7},
                    {"id": "none"},
                ]
            }
        }
        out = contain_command_agents(data, ["agents/spec.json", "agents/ghost.json"], tmp_path)
        by_id = {c["id"]: c for c in out["contributes"]["commands"]}
        assert by_id["own"]["agent"] == "ticket-analyst"
        for cid in ("host", "stem", "ghost", "odd", "none"):
            assert "agent" not in by_id[cid], cid
        assert len(by_id) == 6

    def test_list_apps_contains_a_self_managed_commands_agent(self, tmp_path, monkeypatch):
        # End to end through list_apps: the manifest on disk names a host agent and was
        # never validated; the launcher's listing must not carry it.
        from kiro_crew.apps import manager

        app = tmp_path / "selfapp"
        app.mkdir()
        (app / "app.json").write_text(
            json.dumps(self._with_agents(self._command(agent="kirocrew"), []) | {"name": "selfapp"})
        )
        monkeypatch.setattr(manager, "apps_dir", lambda: tmp_path)
        monkeypatch.setattr(manager, "detect_orphaned_builtins", lambda: set())
        monkeypatch.setattr(
            manager,
            "_read_installed",
            lambda name: (
                manager.InstalledApp(name=name, lifecycle="app") if name == "selfapp" else None
            ),
        )
        [listed] = manager.list_apps()
        [cmd] = listed["manifest"]["contributes"]["commands"]
        assert "agent" not in cmd

    @pytest.mark.parametrize("registered", [True, False])
    def test_list_apps_keeps_an_agent_only_if_registration_wrote_it(
        self, tmp_path, monkeypatch, registered
    ):
        # The final word is what registration WROTE, not a re-derivation of its skip
        # rules: a spec that looks fine but never landed in the agents dir vouches for
        # nothing, so a skip reason this check does not list still cannot authorize it.
        from kiro_crew.apps import bridges, manager

        root = tmp_path / "apps"
        app = root / "selfapp"
        (app / "agents").mkdir(parents=True)
        (app / "agents" / "spec.json").write_text('{"name": "ticket-analyst"}')
        (app / "app.json").write_text(
            json.dumps(
                self._with_agents(self._command(agent="ticket-analyst"), ["agents/spec.json"])
                | {"name": "selfapp"}
            )
        )
        agents_dir = tmp_path / "kiro-agents"
        agents_dir.mkdir()
        if registered:
            (agents_dir / "selfapp--ticket-analyst.json").write_text('{"name": "ticket-analyst"}')
        monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", agents_dir)
        monkeypatch.setattr(manager, "apps_dir", lambda: root)
        monkeypatch.setattr(manager, "detect_orphaned_builtins", lambda: set())
        monkeypatch.setattr(
            manager,
            "_read_installed",
            lambda name: (
                manager.InstalledApp(name=name, lifecycle="app") if name == "selfapp" else None
            ),
        )
        [listed] = manager.list_apps()
        [cmd] = listed["manifest"]["contributes"]["commands"]
        assert (cmd.get("agent") == "ticket-analyst") is registered

    def test_an_agent_name_another_spec_also_declares_is_refused(self, tmp_path, monkeypatch):
        # Sessions select by bare declared name, so a host spec declaring the same name
        # makes resolution ambiguous; the app's prompt could run with the host spec's
        # tools. Such a name is refused rather than guessed at.
        from kiro_crew.apps import bridges, manager

        agents_dir = tmp_path / "kiro-agents"
        agents_dir.mkdir()
        (agents_dir / "selfapp--ticket-analyst.json").write_text('{"name": "ticket-analyst"}')
        monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", agents_dir)
        assert manager._app_registered_agent("selfapp", "ticket-analyst") is True
        (agents_dir / "alpha-ticket-analyst.json").write_text(
            '{"name": "ticket-analyst", "allowedTools": ["*"]}'
        )
        assert manager._app_registered_agent("selfapp", "ticket-analyst") is False

    def test_source_specs_are_read_through_the_hardened_reader(self, tmp_path, monkeypatch):
        # The app root is app-writable, so a source spec may be swapped for a symlink
        # to a credential file after containment; the read must be the fenced one.
        from kiro_crew import agent_discovery

        (tmp_path / "agents").mkdir()
        (tmp_path / "agents" / "spec.json").write_text('{"name": "ticket-analyst"}')
        seen = []

        def refuse(path, *, operation, source):
            seen.append((path.name, operation, source))
            return None

        monkeypatch.setattr(agent_discovery, "_read_agent_spec", refuse)
        errors = AppManifest.from_dict(
            self._with_agents(self._command(agent="ticket-analyst"), ["agents/spec.json"])
        ).validate(app_root=tmp_path)
        assert any("not one of this app's own agents" in e for e in errors), errors
        assert seen == [("spec.json", "app_manifest_agent_names", "unknown")]

    def test_registration_probe_uses_the_hardened_spec_reader(self, tmp_path, monkeypatch):
        # The agents dir is user-writable, so the registered config may be a symlink
        # to a sensitive file. The probe must go through `_read_agent_spec` (which
        # refuses and audits that before reading) rather than a bare read_text.
        from kiro_crew import agent_discovery
        from kiro_crew.apps import bridges, manager

        agents_dir = tmp_path / "kiro-agents"
        agents_dir.mkdir()
        (agents_dir / "selfapp--ticket-analyst.json").write_text('{"name": "ticket-analyst"}')
        monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", agents_dir)
        seen = []

        def refuse(path, *, operation, source):
            seen.append((path.name, operation, source))
            return None

        monkeypatch.setattr(agent_discovery, "_read_agent_spec", refuse)
        assert manager._app_registered_agent("selfapp", "ticket-analyst") is False
        assert seen == [("selfapp--ticket-analyst.json", "list_apps_command_agent", "unknown")]

    def test_agent_defaults_empty_and_is_omitted_from_the_payload(self):
        # Empty means the dashboard default, and an empty string is not emitted: an app
        # that names no agent adds no key, so it cannot invalidate a prior signature.
        m = AppManifest.from_dict(self._manifest(self._command()))
        assert m.contributes.commands[0].agent == ""
        assert "agent" not in m.to_dict()["contributes"]["commands"][0]
        assert m.validate() == []

    @pytest.mark.parametrize("raw", [1, {}, [], None, True])
    def test_a_non_string_agent_reads_as_no_agent(self, raw):
        # Same fail-closed read as the other string fields: anything that is not a
        # string becomes the default rather than coercing to one.
        m = AppManifest.from_dict(self._manifest(self._command(agent=raw)))
        assert m.contributes.commands[0].agent == ""

    @pytest.mark.parametrize(
        "agent",
        [
            "agents/ticket-analyst",
            "../evil",
            "a\\b",
            "Ticket Analyst",
            "a:b",
            "caf\u00e9",
            "x" * 121,
        ],
    )
    def test_refuses_an_agent_outside_the_launcher_pattern(self, agent):
        # Mirrors `contributedCommands.ts`'s `^[A-Za-z0-9._-]{1,120}$` exactly, so a
        # value the launcher would silently clamp to the default fails install
        # instead. A separator would also escape the agents directory.
        errors = AppManifest.from_dict(
            self._with_agents(self._command(agent=agent), [f"agents/{agent}.json"])
        ).validate()
        assert any("agent must match [A-Za-z0-9._-]" in e for e in errors), errors


# ---------------------------------------------------------------------------
# contributes.panelTabs — declarative side-panel tab contribution
# ---------------------------------------------------------------------------


def _panel_tab(**overrides) -> dict:
    tab = {
        "id": "browser",
        "title": "Pippin",
        "menuLabel": "Pippin",
        "menuDescription": "Browse and sync Pippin docs",
        "icon": "BookOpen",
        "entry": "panel.mjs",
    }
    tab.update(overrides)
    return tab


class TestContributesPanelTabs:
    def test_paneltabs_only_manifest_is_covered_by_the_signing_payload(self):
        """A tab's ``entry`` is an ESM module the AppHost imports and RUNS.

        The payload guard would test ``commands or sessionControls``, and the body it
        guarded serializes ALL of ``contributes`` — so a manifest contributing only
        panelTabs was left out of the signed bytes entirely, making ``entry`` the one
        part of a signed app an attacker could repoint with the signature still
        verifying.
        """
        m = AppManifest.from_dict(_valid_manifest(contributes={"panelTabs": [_panel_tab()]}))
        assert m.contributes.commands == [] and m.contributes.sessionControls == []
        assert b"contributes" in m.signing_payload()
        assert b"panel.mjs" in m.signing_payload()

        # Repointing the executed module must change the signed bytes.
        tampered = _valid_manifest(
            contributes={"panelTabs": [{**_panel_tab(), "entry": "evil.mjs"}]}
        )
        assert AppManifest.from_dict(tampered).signing_payload() != m.signing_payload()

        # So must the label a reader decides to trust the tab by.
        relabelled = _valid_manifest(
            contributes={"panelTabs": [{**_panel_tab(), "title": "System Settings"}]}
        )
        assert AppManifest.from_dict(relabelled).signing_payload() != m.signing_payload()

    def test_no_contributions_still_omits_contributes_from_the_payload(self):
        """Manifests signed before contributions existed must hash identically."""
        m = AppManifest.from_dict(_valid_manifest())
        assert b"contributes" not in m.signing_payload()

    def test_parsed_into_typed_config(self):
        m = AppManifest.from_dict(_valid_manifest(contributes={"panelTabs": [_panel_tab()]}))
        assert m.validate() == []
        assert len(m.contributes.panelTabs) == 1
        tab = m.contributes.panelTabs[0]
        assert tab.id == "browser"
        assert tab.title == "Pippin"
        assert tab.menuLabel == "Pippin"
        assert tab.menuDescription == "Browse and sync Pippin docs"
        assert tab.entry == "panel.mjs"

    def test_roundtrips_through_to_dict(self):
        m = AppManifest.from_dict(_valid_manifest(contributes={"panelTabs": [_panel_tab()]}))
        assert AppManifest.from_dict(m.to_dict()).contributes == m.contributes

    def test_absent_contributes_is_empty_not_error(self):
        m = AppManifest.from_dict(_valid_manifest())
        assert m.contributes.panelTabs == []
        assert "contributes" not in m.to_dict()

    def test_non_list_paneltabs_parses_empty_but_is_reported(self):
        # A hand-edited manifest can carry contributes.panelTabs: null. The parser
        # reads untrusted input, so it must degrade rather than raise -- but it must
        # not degrade SILENTLY: coercing to [] is indistinguishable from a
        # deliberately empty list, so the declaration would validate clean and then
        # vanish from to_dict with the author seeing neither an error nor a tab.
        m = AppManifest.from_dict(_valid_manifest(contributes={"panelTabs": None}))
        assert m.contributes.panelTabs == []
        assert m.contributes.bad_panel_tabs is True
        assert any("contributes.panelTabs must be an array" in e for e in m.validate())

    def test_non_object_paneltab_entries_are_counted_and_reported(self):
        m = AppManifest.from_dict(
            _valid_manifest(contributes={"panelTabs": [_panel_tab(), "nope", 7]})
        )
        assert len(m.contributes.panelTabs) == 1
        assert m.contributes.dropped_panel_tabs == 2
        assert any("2 entries" in e for e in m.validate())

    def test_non_object_contributes_block_is_reported(self):
        m = AppManifest.from_dict(_valid_manifest(contributes="nope"))
        assert m.contributes.bad_block is True
        assert any("contributes must be an object" in e for e in m.validate())

    def test_commands_survive_alongside_paneltabs(self):
        # The two contribution kinds share one `contributes` block; serializing one
        # must not drop the other.
        contributes = {
            "panelTabs": [_panel_tab()],
            "commands": [{"id": "do-thing", "title": "Do thing", "prompt": "Do it."}],
        }
        m = AppManifest.from_dict(_valid_manifest(contributes=contributes))
        assert m.validate() == []
        assert len(m.contributes.panelTabs) == 1
        assert len(m.contributes.commands) == 1
        round_tripped = AppManifest.from_dict(m.to_dict()).contributes
        assert len(round_tripped.panelTabs) == 1
        assert len(round_tripped.commands) == 1

    def test_missing_id_is_error(self):
        m = AppManifest.from_dict(_valid_manifest(contributes={"panelTabs": [_panel_tab(id="")]}))
        assert any("entry missing id" in e for e in m.validate())

    def test_over_long_menu_description_is_capped_like_the_labels(self):
        # The launcher card renders `menuDescription`, so it is capped alongside
        # `title`/`menuLabel` rather than being the one field that can blow out
        # that surface's layout.
        m = AppManifest.from_dict(
            _valid_manifest(contributes={"panelTabs": [_panel_tab(menuDescription="x" * 121)]})
        )
        assert any("menuDescription exceeds 120" in e for e in m.validate())

    def test_menu_description_at_the_cap_is_accepted(self):
        m = AppManifest.from_dict(
            _valid_manifest(contributes={"panelTabs": [_panel_tab(menuDescription="x" * 120)]})
        )
        assert not any("menuDescription exceeds" in e for e in m.validate())

    def test_non_slug_id_is_error(self):
        m = AppManifest.from_dict(
            _valid_manifest(contributes={"panelTabs": [_panel_tab(id="Not A Slug")]})
        )
        assert any("storage-safe slug" in e for e in m.validate())

    def test_trailing_newline_id_cannot_slip_through(self):
        m = AppManifest.from_dict(
            _valid_manifest(contributes={"panelTabs": [_panel_tab(id="browser\n")]})
        )
        assert any("storage-safe slug" in e for e in m.validate())

    def test_duplicate_id_is_error(self):
        m = AppManifest.from_dict(
            _valid_manifest(contributes={"panelTabs": [_panel_tab(), _panel_tab()]})
        )
        assert any("duplicate id" in e for e in m.validate())

    def test_missing_title_menulabel_entry_each_error(self):
        m = AppManifest.from_dict(
            _valid_manifest(
                contributes={"panelTabs": [_panel_tab(title="", menuLabel="", entry="")]}
            )
        )
        errs = m.validate()
        assert any("missing title" in e for e in errs)
        assert any("missing menuLabel" in e for e in errs)
        assert any("missing entry" in e for e in errs)

    def test_entry_path_traversal_rejected(self):
        m = AppManifest.from_dict(
            _valid_manifest(contributes={"panelTabs": [_panel_tab(entry="../../etc/passwd")]})
        )
        assert any("path traversal" in e for e in m.validate())

    def test_too_many_tabs_rejected(self):
        from kiro_crew.apps.manifest import _MAX_PANEL_TABS_PER_APP

        tabs = [_panel_tab(id=f"t{i}") for i in range(_MAX_PANEL_TABS_PER_APP + 1)]
        m = AppManifest.from_dict(_valid_manifest(contributes={"panelTabs": tabs}))
        assert any("exceeds the limit of" in e for e in m.validate())
