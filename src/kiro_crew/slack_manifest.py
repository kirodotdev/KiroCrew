"""Single source for the Slack app manifest and its app-create deep link.

Three callers need the same bytes and MUST not disagree about them:

* ``cli_setup`` (``kirocrew manifest``) prints the manifest or the deep link,
* the dashboard's ``GET /api/slack/manifest`` serves both to Settings → Slack,
* ``security._kirocrew_slack_app_link_alias`` decides whether a deep link is
  ours by rebuilding the expected payload and comparing.

The third one is why this module exists rather than the render being inlined at
each call site. The exfil redactor exempts our deep link only when the payload
reproduces this template exactly, so if one emitter changed its strip rule or
trailing newline while the validator kept the old one, the exemption would stop
matching, the ``[REDACTED: suspicious URL to api.slack.com]`` bug would come
back for users, and no test would notice as long as the tests rebuilt the
payload themselves. Sharing the procedure — not just the template file — is what
makes that drift impossible.

The manifest is TOGGLE-AWARE: template lines ending in ``#@<feature>`` render
only when that Slack Code switch is on. The set of switches
is finite (:data:`FEATURES`), so the validator rebuilds every combination
(:func:`feature_sets`) instead of loosening its pattern, and
:func:`requirements` / :func:`diff_requirements` give the settings page the
exact scope, event and feature lines a switch adds or removes.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from importlib.resources import files as _pkg_files
from typing import Iterable
from urllib.parse import quote

#: Placeholder substituted with the operator's alias, in two places.
ALIAS_PLACEHOLDER = "{{ALIAS}}"

#: Placeholder substituted with ``slash_command(alias)``. Slack resolves
#: slash-command names across a whole Enterprise Grid org, so two installs that
#: both register ``/kirocrew`` collide and Slack routes the command to the wrong
#: app. Naming the command after the alias keeps each install's command its own.
COMMAND_PLACEHOLDER = "{{COMMAND}}"

#: The alias the dashboard renders with when the caller names none. It keeps
#: the generic ``/kirocrew`` command: that path saves no ``slack.command``, so
#: the gateway listens for the default and a renamed command would go unanswered.
DEFAULT_ALIAS = "kirocrew"

#: Prefix of every alias-named slash command.
SLASH_COMMAND_PREFIX = "kirocrew-"

#: Slack's limit on a slash command, leading ``/`` included.
SLASH_COMMAND_MAX = 32

#: Longest accepted alias. Both emitters enforce this, and the validator's
#: derived pattern is bounded by it. It is deliberately tight: the alias is the
#: ONLY caller-controlled span inside an exempted deep link, so its width is the
#: width of the channel the exemption opens. 32 also matches the Slack app-name
#: budget the manifest spends it on.
ALIAS_MAX = 32

#: Accepted alias shape, anchored. Kept in sync with ``ALIAS_MAX``.
ALIAS_RE = re.compile(rf"\A[A-Za-z0-9_-]{{1,{ALIAS_MAX}}}\Z")

#: A slash-command name Slack accepts (no leading ``/``), as ``slack.command``
#: stores it.
_COMMAND_RE = re.compile(rf"\A[a-z0-9_-]{{1,{SLASH_COMMAND_MAX - 1}}}\Z")

#: Alias body for embedding in a larger pattern (unanchored, no group).
ALIAS_PATTERN = rf"[A-Za-z0-9_-]{{1,{ALIAS_MAX}}}"

APP_CREATE_HOST = "api.slack.com"
APP_CREATE_PATH = "/apps"
_APP_CREATE_BASE = f"https://{APP_CREATE_HOST}{APP_CREATE_PATH}"


def valid_alias(alias: str) -> bool:
    """True when *alias* is a shape both emitters will substitute."""
    return ALIAS_RE.match(alias) is not None


def slash_command(alias: str) -> str:
    """The slash command (no leading ``/``) the manifest registers for *alias*.

    Lowercased, because Slack accepts only lowercase command names, and cut to
    fit ``SLASH_COMMAND_MAX`` so every alias ``valid_alias`` admits still
    renders a command Slack accepts. This is also the ``slack.command`` value the
    gateway must be configured with for that app.
    """
    if alias == DEFAULT_ALIAS:
        return DEFAULT_ALIAS
    room = SLASH_COMMAND_MAX - 1 - len(SLASH_COMMAND_PREFIX)
    return SLASH_COMMAND_PREFIX + alias.lower()[:room]


#: Slack Code switch: the app is a Slack agent (``features.agent_view``): threads
#: run as agent sessions with a live status, a title and Slack's native Stop.
AGENT_SESSIONS = "agent_sessions"
#: The Slack Code switch REGISTRY: switch name -> the bot scope it cannot run
#: without. The name is also the switch's ``slack.<name>`` config key and the
#: template's ``#@<name>`` gate. Registering a switch is one entry here, its
#: gated lines in ``slack-manifest.yaml`` and its config field: the render, every
#: combination the exfil validator rebuilds (:func:`feature_sets`), the
#: requirement diffs, the drift probe, the capability record and the settings
#: page's update panel all follow from this mapping.
FEATURE_SCOPES: dict[str, str] = {
    AGENT_SESSIONS: "assistant:write",
}
#: Every registered switch, in a fixed (display) order.
FEATURES: tuple[str, ...] = tuple(FEATURE_SCOPES)

#: A gated template line: ``<content>  #@flag`` at line end.
_GATE_RE = re.compile(r"\s+#@(?P<flag>[a-z_]+)\s*$")


def normalize_features(features: Iterable[str] | None) -> frozenset[str]:
    """*features* as a frozenset of known switch names; unknown names dropped."""
    return frozenset(f for f in (features or ()) if f in FEATURES)


def feature_sets() -> tuple[frozenset[str], ...]:
    """Every combination of :data:`FEATURES` (the empty set first)."""
    return tuple(
        frozenset(combo)
        for size in range(len(FEATURES) + 1)
        for combo in itertools.combinations(FEATURES, size)
    )


def raw_template() -> str:
    """The packaged template verbatim: comments, gates and placeholders intact."""
    return str(_pkg_files("kiro_crew").joinpath("slack-manifest.yaml").read_text("utf-8"))


def template(features: Iterable[str] | None = None) -> str:
    """The template for *features*: gated lines kept (gate removed) or dropped.

    A line gated on a switch that is off is removed whole, comment or not; one
    whose switch is on keeps its content with the ``#@...`` gate cut off, so no
    rendered manifest ever carries a gate. ``##`` lines are notes about the
    template itself and are never rendered.
    """
    enabled = normalize_features(features)
    out: list[str] = []
    for line in raw_template().splitlines():
        if line.lstrip().startswith("##"):
            continue  # template-only notes: never part of a rendered manifest
        gate = _GATE_RE.search(line)
        if gate is None:
            out.append(line)
        elif gate["flag"] in enabled:
            out.append(line[: gate.start()])
    return "\n".join(out) + "\n"


def stripped_template(features: Iterable[str] | None = None) -> str:
    """The template for *features* with comment lines dropped, placeholders intact.

    This is the one definition of "comment-stripped": the deep-link payload and
    the validator's expected-payload patterns are both built from it, so neither
    can drift from the other.
    """
    lines = [ln for ln in template(features).splitlines() if not ln.lstrip().startswith("#")]
    return "\n".join(lines).strip() + "\n"


def render(
    alias: str,
    *,
    strip_comments: bool = False,
    features: Iterable[str] | None = None,
    command: str | None = None,
) -> str:
    """The manifest with *alias* substituted, for the switches in *features*.

    ``strip_comments=False`` keeps the guidance comments, which is what a human
    reads or pastes into Slack. ``True`` yields exactly the deep-link payload.
    No *features* renders the manifest with every Slack Code switch off.
    *command* overrides the slash command (default ``slash_command(alias)``):
    the manifest an EXISTING app is updated from must keep the command that app
    already registers, which the gateway answers as ``slack.command``. A
    command of the wrong shape is ignored. The deep link never takes one: its
    payload must stay one the exfil validator can rebuild from the alias.
    """
    text = stripped_template(features) if strip_comments else template(features)
    slash = command if command and _COMMAND_RE.match(command) else slash_command(alias)
    return text.replace(COMMAND_PLACEHOLDER, slash).replace(ALIAS_PLACEHOLDER, alias)


def alias_for_command(command: str) -> str:
    """The alias whose manifest registers *command*, else :data:`DEFAULT_ALIAS`.

    Inverts :func:`slash_command` for ``kirocrew-<alias>`` commands. The alias is
    lowercase by then (Slack command names are), so an app created from a
    mixed-case alias gets the lowercase spelling back; the slash command, which
    is what routing depends on, is the same either way.
    """
    if command.startswith(SLASH_COMMAND_PREFIX):
        candidate = command[len(SLASH_COMMAND_PREFIX) :]
        if valid_alias(candidate):
            return candidate
    return DEFAULT_ALIAS


def deep_link(alias: str, features: Iterable[str] | None = None) -> str:
    """Slack's new-app deep link carrying the rendered manifest.

    ``quote`` (not ``quote_plus``): a literal ``+`` would decode back as a space
    under form-encoding, and ``%20`` is unambiguous everywhere Slack parses this.
    """
    encoded = quote(render(alias, strip_comments=True, features=features), safe="")
    return f"{_APP_CREATE_BASE}?new_app=1&manifest_yaml={encoded}"


def app_manifest_url(app_id: str) -> str | None:
    """The App Manifest page of an existing Slack app, or None for a bad id.

    Slack app ids are ``A`` plus upper-case alphanumerics; anything else is
    refused so a value read off Slack cannot steer the link elsewhere.
    """
    if not re.fullmatch(r"A[A-Z0-9]{6,20}", app_id or ""):
        return None
    return f"{_APP_CREATE_BASE}/{app_id}/app-manifest"


@dataclass(frozen=True)
class ManifestRequirements:
    """What one rendered manifest asks Slack for, by kind."""

    bot_scopes: frozenset[str] = field(default_factory=frozenset)
    bot_events: frozenset[str] = field(default_factory=frozenset)
    #: ``features.*`` blocks the manifest declares (``agent_view``…).
    features: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class ManifestChange:
    """The difference between two manifests: what a switch flip adds or removes."""

    added_scopes: tuple[str, ...] = ()
    removed_scopes: tuple[str, ...] = ()
    added_events: tuple[str, ...] = ()
    removed_events: tuple[str, ...] = ()
    added_features: tuple[str, ...] = ()
    removed_features: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        """True when the two manifests ask Slack for the same things."""
        return not any(
            (
                self.added_scopes,
                self.removed_scopes,
                self.added_events,
                self.removed_events,
                self.added_features,
                self.removed_features,
            )
        )

    def as_dict(self) -> dict[str, list[str]]:
        """JSON-ready form, every list sorted."""
        return {
            "added_scopes": list(self.added_scopes),
            "removed_scopes": list(self.removed_scopes),
            "added_events": list(self.added_events),
            "removed_events": list(self.removed_events),
            "added_features": list(self.added_features),
            "removed_features": list(self.removed_features),
        }


def requirements(features: Iterable[str] | None = None) -> ManifestRequirements:
    """Parse the manifest for *features* into its scope, event and feature sets."""
    import yaml  # type: ignore[import-untyped]

    parsed = yaml.safe_load(render(DEFAULT_ALIAS, features=features)) or {}
    oauth = (parsed.get("oauth_config") or {}).get("scopes") or {}
    events = (parsed.get("settings") or {}).get("event_subscriptions") or {}
    return ManifestRequirements(
        bot_scopes=frozenset(oauth.get("bot") or ()),
        bot_events=frozenset(events.get("bot_events") or ()),
        features=frozenset((parsed.get("features") or {}).keys()),
    )


def diff_requirements(old: ManifestRequirements, new: ManifestRequirements) -> ManifestChange:
    """What moving an app from manifest *old* to manifest *new* changes."""
    return ManifestChange(
        added_scopes=tuple(sorted(new.bot_scopes - old.bot_scopes)),
        removed_scopes=tuple(sorted(old.bot_scopes - new.bot_scopes)),
        added_events=tuple(sorted(new.bot_events - old.bot_events)),
        removed_events=tuple(sorted(old.bot_events - new.bot_events)),
        added_features=tuple(sorted(new.features - old.features)),
        removed_features=tuple(sorted(old.features - new.features)),
    )
