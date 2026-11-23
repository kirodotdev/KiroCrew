"""Single source for the Slack app manifest and its app-create deep link.

Three callers need the same bytes and MUST not disagree about them:

* ``cli_setup`` (``kirocrew manifest``) prints the manifest or the deep link,
* the dashboard's ``GET /api/slack/manifest`` serves both to Settings → Messaging
  Channels → Slack,
* ``security._kirocrew_slack_app_link_alias`` decides whether a deep link is
  ours by reading the three names out of it, re-rendering with exactly those
  names through :func:`render` and comparing.

The third one is why this module exists rather than the render being inlined at
each call site. The exfil redactor exempts our deep link only when the payload
reproduces this template exactly, so if one emitter changed its strip rule or
trailing newline while the validator kept the old one, the exemption would stop
matching, the ``[REDACTED: suspicious URL to api.slack.com]`` bug would come
back for users, and no test would notice as long as the tests rebuilt the
payload themselves. Sharing the procedure — not just the template file — is what
makes that drift impossible.

The manifest is TOGGLE-AWARE: template lines ending in ``#@<feature>`` render
only when that Slack Code switch is on (``#@a|b`` = either). The set of switches
is finite (:data:`FEATURES`), so the validator rebuilds every combination
(:func:`feature_sets`) instead of loosening its pattern, and
:func:`requirements` / :func:`diff_requirements` give the settings page the
exact scope, event and feature lines a switch adds or removes.
"""

from __future__ import annotations

import itertools
import json
import re
import unicodedata
from dataclasses import dataclass, field
from importlib.resources import files as _pkg_files
from typing import Iterable
from urllib.parse import quote

#: Placeholder for the app name (``display_information.name``).
APP_NAME_PLACEHOLDER = "__APP_NAME__"
#: Placeholder for the bot user's name, what people type after ``@``
#: (``features.bot_user.display_name``).
BOT_NAME_PLACEHOLDER = "__BOT_NAME__"

#: Placeholder for the slash command, without its ``/``. Slack resolves
#: slash-command names across a whole Enterprise Grid org, so two installs that
#: both register ``/kirocrew`` collide and Slack routes the command to the wrong
#: app. The settings page says so next to the command field, and the legacy
#: alias path names the command after the alias (:func:`slash_command`).
COMMAND_PLACEHOLDER = "{{COMMAND}}"

#: The alias the dashboard renders with when the caller names none. It keeps
#: the generic ``/kirocrew`` command: that path saves no ``slack.command``, so
#: the gateway listens for the default and a renamed command would go unanswered.
DEFAULT_ALIAS = "kirocrew"

#: What a fresh install calls its app, its bot and its slash command.
DEFAULT_NAME = "kirocrew"

#: Prefix of the app and bot name an alias rendered before names were settable
#: (``KiroCrew-<alias>``). Existing apps keep it until their owner renames them.  # brand-ok
LEGACY_NAME_PREFIX = "KiroCrew-"  # brand-ok

#: Slack's limits (App manifest reference): the app name is at most 35
#: characters and the bot user's display name at most 80.
APP_NAME_MAX = 35
BOT_NAME_MAX = 80

#: Prefix of every alias-named slash command.
SLASH_COMMAND_PREFIX = "kirocrew-"

#: A command name the bundled manifest can register, with its ``/``:
#: ``/kirocrew`` or ``/kirocrew-<alias>`` (:func:`slash_command`). Slack delivers
#: only commands this app registered, so the gateway answers every name of this
#: shape whatever ``slack.command`` says. The alias class is what
#: :func:`slash_command` emits, so a matched name is safe to echo.
MANIFEST_COMMAND_RE = re.compile(
    rf"/(?:{re.escape(DEFAULT_ALIAS)}|{re.escape(SLASH_COMMAND_PREFIX)}[a-z0-9_-]+)"
)


def is_app_command(command: str, *names: object) -> bool:
    """Whether *command* (``/name``, as Slack sends it) is this app's own.

    It is when it matches :data:`MANIFEST_COMMAND_RE`, or equals ``/<name>``
    for one of *names*: the live ``slack.command`` and the name the gateway
    shows. Checking the live config keeps a custom command answered after the
    shown name changed to another registered one.
    """
    if MANIFEST_COMMAND_RE.fullmatch(command):
        return True
    return any(isinstance(n, str) and n and command == f"/{n}" for n in names)


#: Slack's limit on a slash command, leading ``/`` included.
SLASH_COMMAND_MAX = 32

#: Longest accepted alias (the legacy ``?alias=`` / ``--alias`` path). With the
#: ``KiroCrew-`` prefix it still fits Slack's 35-character app name.  # brand-ok
ALIAS_MAX = 26

#: Accepted alias shape, anchored. Kept in sync with ``ALIAS_MAX``.
ALIAS_RE = re.compile(rf"\A[A-Za-z0-9_-]{{1,{ALIAS_MAX}}}\Z")

#: A slash-command name Slack accepts (no leading ``/``), as ``slack.command``
#: stores it.
_COMMAND_RE = re.compile(rf"\A[a-z0-9_-]{{1,{SLASH_COMMAND_MAX - 1}}}\Z")

APP_CREATE_HOST = "api.slack.com"
APP_CREATE_PATH = "/apps"
_APP_CREATE_BASE = f"https://{APP_CREATE_HOST}{APP_CREATE_PATH}"


def valid_alias(alias: str) -> bool:
    """True when *alias* is a shape both emitters will substitute."""
    return ALIAS_RE.match(alias) is not None


def valid_command(command: str) -> bool:
    """True when *command* (no leading ``/``) is a slash command Slack accepts:
    lowercase letters, digits, ``-`` and ``_``, 31 characters at most."""
    return _COMMAND_RE.match(command) is not None


#: Unicode categories refused in a name: controls, format characters (zero-width
#: and bidi overrides, which make one name look like another), surrogates,
#: private-use and unassigned code points, and line or paragraph separators.
_REFUSED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


def _valid_name(name: str, limit: int) -> bool:
    return (
        0 < len(name) <= limit
        and name == name.strip()
        and not any(unicodedata.category(ch) in _REFUSED_CATEGORIES for ch in name)
    )


def valid_app_name(name: str) -> bool:
    """True for an app name Slack accepts: 1-35 characters, no edge spaces,
    no control or invisible characters."""
    return _valid_name(name, APP_NAME_MAX)


def valid_bot_name(name: str) -> bool:
    """True for a bot display name Slack accepts: 1-80 characters, no edge
    spaces, no control or invisible characters."""
    return _valid_name(name, BOT_NAME_MAX)


@dataclass(frozen=True)
class AppNames:
    """What the manifest names: the app, its bot user (``@``) and its command (``/``).

    ``command`` carries no leading ``/``, as ``slack.command`` stores it.
    """

    app_name: str = DEFAULT_NAME
    bot_name: str = DEFAULT_NAME
    command: str = DEFAULT_NAME

    def error(self) -> str | None:
        """Why Slack would refuse these names, or None when it accepts them all."""
        if not valid_app_name(self.app_name):
            return f"app_name must be 1-{APP_NAME_MAX} characters, with no control characters"
        if not valid_bot_name(self.bot_name):
            return f"bot_name must be 1-{BOT_NAME_MAX} characters, with no control characters"
        if not valid_command(self.command):
            return f"command must be 1-{SLASH_COMMAND_MAX - 1} lowercase letters, digits, - or _"
        return None


#: Fresh-install names: ``kirocrew``, ``@kirocrew`` and ``/kirocrew``.
DEFAULT_NAMES = AppNames()


def legacy_names(alias: str) -> AppNames:
    """The names an *alias* rendered before they were settable.

    ``LEGACY_NAME_PREFIX + alias`` for the app and the bot, :func:`slash_command` for the
    command: what every app created from an alias is called, so rendering an
    existing app from its alias does not rename it.
    """
    legacy = LEGACY_NAME_PREFIX + alias
    return AppNames(app_name=legacy, bot_name=legacy, command=slash_command(alias))


def install_names(
    app_name: str = "", bot_name: str = "", command: str = "", *, existing: bool
) -> AppNames:
    """The names this install's manifest uses, from its saved config.

    A saved ``slack.app_name`` / ``slack.bot_name`` wins. An unsaved one is
    ``kirocrew`` only on a fresh install: one with the default command and no
    Slack app yet (*existing* false: no bot token). Anything else keeps the
    ``LEGACY_NAME_PREFIX + alias`` name it has always rendered, the alias read back from
    the saved command, so updating an existing app from this manifest never
    renames its bot or breaks its ``@`` mentions.
    """
    saved = command or DEFAULT_NAME
    alias = alias_for_command(saved)
    if existing or saved != DEFAULT_NAME:
        fallback = LEGACY_NAME_PREFIX + alias
    else:
        fallback = DEFAULT_NAME
    # A command saved before Slack's rule was checked (uppercase, too long)
    # renders the alias command, as the manifest always did for such a value.
    rendered = saved if valid_command(saved) else slash_command(alias)
    return AppNames(app_name=app_name or fallback, bot_name=bot_name or fallback, command=rendered)


#: A name YAML reads back as the same string when written bare. Anything else
#: (a space, ``:``, ``#``, a quote, non-ASCII, a leading digit) is written as a
#: double-quoted JSON string, which YAML reads as the same text.
_PLAIN_SCALAR_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9_.-]*\Z")
#: Bare words YAML 1.1 reads as a boolean or null rather than a string.
_YAML_WORDS = frozenset({"y", "n", "yes", "no", "true", "false", "on", "off", "null"})


def yaml_scalar(value: str) -> str:
    """*value* as the YAML scalar the manifest writes it as."""
    if _PLAIN_SCALAR_RE.match(value) and value.lower() not in _YAML_WORDS:
        return value
    return json.dumps(value, ensure_ascii=False)


def scalar_value(scalar: str) -> str | None:
    """The string a :func:`yaml_scalar` output stands for, or None for any other text."""
    if not scalar.startswith('"'):
        return scalar
    try:
        value = json.loads(scalar)
    except ValueError:
        return None
    return value if isinstance(value, str) else None


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
#: Slack Code switch: the app may open code channels (``features.code_channels``).
CODE_CHANNELS = "code_channels"
#: The Slack Code switch REGISTRY, for the switches the gateway runs: switch name
#: -> the bot scope it cannot run without. The name is also the switch's
#: ``slack.<name>`` config key and the template's ``#@<name>`` gate. Registering a
#: switch is one entry here, its gated lines in ``slack-manifest.yaml`` and its
#: config field: the render, every combination the exfil validator rebuilds
#: (:func:`feature_sets`), the requirement diffs, the drift probe, the capability
#: record and the settings page's update panel all follow from this mapping.
FEATURE_SCOPES: dict[str, str] = {
    AGENT_SESSIONS: "assistant:write",
    CODE_CHANNELS: "code_channels:manage",
}
#: Slack Code switch: the same Slack app also serves Slack's MCP server
#: (``settings.is_mcp_enabled``, PKCE, the localhost redirect and read-only user
#: scopes), for each person's own Slack sign-in on the Connections card. It only
#: changes what the manifest asks Slack for: the gateway runs nothing for it, so
#: it has no bot scope and is deliberately NOT in :data:`FEATURE_SCOPES`, which
#: the capability probe, the latches and the runtime gate read (a bot token
#: cannot read an app's user scopes or settings anyway).
SLACK_MCP = "slack_mcp"
#: The switches that only change the manifest: a toggle and gated template
#: lines, no bot scope, nothing the gateway runs. Disjoint from
#: :data:`FEATURE_SCOPES`; code that loops :data:`FEATURES` and needs a switch's
#: bot scope skips these (or loops ``FEATURE_SCOPES`` instead).
MANIFEST_ONLY_FEATURES: tuple[str, ...] = (SLACK_MCP,)
#: Every registered switch, in a fixed (display) order.
FEATURES: tuple[str, ...] = (*FEATURE_SCOPES, *MANIFEST_ONLY_FEATURES)
#: Bot scopes the manifest asks for that neither the gateway nor any switch needs to
#: run: an install without one is not out of date (the drift check skips it). Without
#: ``users:read.email`` only Share Slack access's add-by-email is refused, and its
#: refusal names the scope.
OPTIONAL_BOT_SCOPES: frozenset[str] = frozenset({"users:read.email"})

#: A gated template line: ``<content>  #@flag`` or ``#@flag1|flag2`` at line end;
#: ``#@!flag`` gates on the switch being OFF (a line the switch replaces).
_GATE_RE = re.compile(r"\s+#@(?P<off>!)?(?P<flags>[a-z_]+(?:\|[a-z_]+)*)\s*$")


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
    rendered manifest ever carries a gate. A ``#@!`` gate is the reverse: kept
    only while its switch is off. ``##`` lines are notes about the template
    itself and are never rendered.
    """
    enabled = normalize_features(features)
    out: list[str] = []
    for line in raw_template().splitlines():
        if line.lstrip().startswith("##"):
            continue  # template-only notes: never part of a rendered manifest
        gate = _GATE_RE.search(line)
        if gate is None:
            out.append(line)
        elif bool(enabled.intersection(gate["flags"].split("|"))) != bool(gate["off"]):
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
    names: AppNames | str,
    *,
    strip_comments: bool = False,
    features: Iterable[str] | None = None,
) -> str:
    """The manifest naming *names*, for the switches in *features*.

    A plain string is a legacy alias (:func:`legacy_names`). Names Slack would
    refuse raise ``ValueError``: callers check :meth:`AppNames.error` first.
    ``strip_comments=False`` keeps the guidance comments, which is what a human
    reads or pastes into Slack. ``True`` yields exactly the deep-link payload,
    which the exfil validator rebuilds through this same function.
    No *features* renders the manifest with every Slack Code switch off.
    """
    resolved = legacy_names(names) if isinstance(names, str) else names
    problem = resolved.error()
    if problem:
        raise ValueError(problem)
    text = stripped_template(features) if strip_comments else template(features)
    # One pass, so a name that contains another placeholder's text stays literal.
    values = {
        APP_NAME_PLACEHOLDER: yaml_scalar(resolved.app_name),
        BOT_NAME_PLACEHOLDER: yaml_scalar(resolved.bot_name),
        COMMAND_PLACEHOLDER: resolved.command,
    }
    return _PLACEHOLDER_RE.sub(lambda m: values[m.group(0)], text)


_PLACEHOLDER_RE = re.compile(
    "|".join(
        re.escape(p) for p in (APP_NAME_PLACEHOLDER, BOT_NAME_PLACEHOLDER, COMMAND_PLACEHOLDER)
    )
)


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


def deep_link(names: AppNames | str, features: Iterable[str] | None = None) -> str:
    """Slack's new-app deep link carrying the rendered manifest.

    ``quote`` (not ``quote_plus``): a literal ``+`` would decode back as a space
    under form-encoding, and ``%20`` is unambiguous everywhere Slack parses this.
    """
    encoded = quote(render(names, strip_comments=True, features=features), safe="")
    return f"{_APP_CREATE_BASE}?new_app=1&manifest_yaml={encoded}"


_APP_ID_RE = re.compile(r"A[A-Z0-9]{6,20}")
_TEAM_ID_RE = re.compile(r"T[A-Z0-9]{6,20}")

#: Slack's documented redirect into an app's DM ("Deep linking into Slack").
APP_REDIRECT_URL = "https://slack.com/app_redirect"


def app_manifest_url(app_id: str) -> str | None:
    """The App Manifest page of an existing Slack app, or None for a bad id.

    Slack app ids are ``A`` plus upper-case alphanumerics; anything else is
    refused so a value read off Slack cannot steer the link elsewhere.
    """
    if not _APP_ID_RE.fullmatch(app_id or ""):
        return None
    return f"{_APP_CREATE_BASE}/{app_id}/app-manifest"


def app_basic_info_url(app_id: str) -> str | None:
    """The Basic Information page (where App-Level Tokens are generated), or None.

    Same id check and URL shape as :func:`app_manifest_url`.
    """
    if not _APP_ID_RE.fullmatch(app_id or ""):
        return None
    return f"{_APP_CREATE_BASE}/{app_id}/general"


def app_dm_url(app_id: str, team_id: str = "") -> str | None:
    """A link that opens the app's DM in Slack, or None for a bad app id.

    A workspace id (``T…``) is added when valid. An org-level install has none
    (its team is an ``E…`` org id), so the link carries only the app and Slack
    opens it in the user's default workspace.
    """
    if not _APP_ID_RE.fullmatch(app_id or ""):
        return None
    url = f"{APP_REDIRECT_URL}?app={app_id}"
    if _TEAM_ID_RE.fullmatch(team_id or ""):
        url += f"&team={team_id}"
    return url


@dataclass(frozen=True)
class ManifestRequirements:
    """What one rendered manifest asks Slack for, by kind."""

    bot_scopes: frozenset[str] = field(default_factory=frozenset)
    bot_events: frozenset[str] = field(default_factory=frozenset)
    #: ``features.*`` blocks the manifest declares (``code_channels``…).
    features: frozenset[str] = field(default_factory=frozenset)
    #: ``oauth_config.scopes.user``: what a person's own sign-in may ask for.
    user_scopes: frozenset[str] = field(default_factory=frozenset)
    #: App settings outside scopes and events, one ``<path>: <value>`` line per
    #: value (``settings.is_mcp_enabled: true``,
    #: ``oauth_config.redirect_urls: http://…``).
    settings: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class ManifestChange:
    """The difference between two manifests: what a switch flip adds or removes."""

    added_scopes: tuple[str, ...] = ()
    removed_scopes: tuple[str, ...] = ()
    added_events: tuple[str, ...] = ()
    removed_events: tuple[str, ...] = ()
    added_features: tuple[str, ...] = ()
    removed_features: tuple[str, ...] = ()
    added_user_scopes: tuple[str, ...] = ()
    removed_user_scopes: tuple[str, ...] = ()
    added_settings: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        """True when the two manifests ask Slack for the same things."""
        return not any(self.as_dict().values())

    def as_dict(self) -> dict[str, list[str]]:
        """JSON-ready form, every list sorted."""
        return {
            "added_scopes": list(self.added_scopes),
            "removed_scopes": list(self.removed_scopes),
            "added_events": list(self.added_events),
            "removed_events": list(self.removed_events),
            "added_features": list(self.added_features),
            "removed_features": list(self.removed_features),
            "added_user_scopes": list(self.added_user_scopes),
            "removed_user_scopes": list(self.removed_user_scopes),
            "added_settings": list(self.added_settings),
        }


def _setting_lines(prefix: str, value: object, skip: frozenset[str]) -> Iterable[str]:
    """``<path>: <value>`` for every scalar under *value*; list items one line each."""
    if isinstance(value, dict):
        for key, sub in value.items():
            path = f"{prefix}.{key}"
            if path not in skip:
                yield from _setting_lines(path, sub, skip)
    elif isinstance(value, list):
        for item in value:
            yield from _setting_lines(prefix, item, skip)
    elif isinstance(value, bool):
        yield f"{prefix}: {'true' if value else 'false'}"
    else:
        yield f"{prefix}: {value!s}"


#: Covered by their own kinds (scopes, events), so not repeated as settings.
_NOT_SETTINGS = frozenset({"oauth_config.scopes", "settings.event_subscriptions"})


def requirements(features: Iterable[str] | None = None) -> ManifestRequirements:
    """Parse the manifest for *features* into its scope, event, feature and setting sets."""
    import yaml  # type: ignore[import-untyped]

    parsed = yaml.safe_load(render(DEFAULT_NAMES, features=features)) or {}
    oauth = (parsed.get("oauth_config") or {}).get("scopes") or {}
    events = (parsed.get("settings") or {}).get("event_subscriptions") or {}
    settings = frozenset(
        line
        for section in ("oauth_config", "settings")
        for line in _setting_lines(section, parsed.get(section) or {}, _NOT_SETTINGS)
    )
    return ManifestRequirements(
        bot_scopes=frozenset(oauth.get("bot") or ()),
        bot_events=frozenset(events.get("bot_events") or ()),
        features=frozenset((parsed.get("features") or {}).keys()),
        user_scopes=frozenset(oauth.get("user") or ()),
        settings=settings,
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
        added_user_scopes=tuple(sorted(new.user_scopes - old.user_scopes)),
        removed_user_scopes=tuple(sorted(old.user_scopes - new.user_scopes)),
        added_settings=tuple(sorted(new.settings - old.settings)),
    )
