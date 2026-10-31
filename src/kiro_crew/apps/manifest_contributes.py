"""The ``contributes`` block: rows an app adds to surfaces the host renders.

Session controls in the composer, Command Bar commands and the one argument a command
may collect, chat side-panel tabs, and file-menu rows, plus ``Contributes``, which holds
them. Every contribution is declarative data the host interprets -- no app code crosses
into a host surface -- and the dashboard reader mirrors the caps and grammars it relies on
(``contributedCommands.ts``, ``panelTabRegistry.ts``, ``fileMenuContributions.tsx``), so
a change to a mirrored one is a change to both sides.

Served through :mod:`kiro_crew.apps.manifest`, which callers and tests import and patch;
this module never imports it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: Hard cap on session controls per app. A control here renders inside the
#: composer, on the path of every turn — an app must not be able to crowd the
#: bar out. Apps needing more configuration have a page for it.
MAX_SESSION_CONTROLS_PER_APP = 2

#: Control ids are kebab-case like app names: they appear in a React key, in
#: per-control persistence, and potentially in a URL, so the charset is bounded.
_SESSION_CONTROL_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: A status route is a path *within* the app's own backend, so it is deliberately
#: not a URL: no scheme, no host, no query. The dashboard appends the session
#: identity itself, which stops an app from pointing the poll at another origin.
_SESSION_CONTROL_STATUS_PATH_RE = re.compile(r"^[a-z0-9][a-z0-9/_-]{0,63}$")


def _normalize_status_path(raw: Any) -> str:
    """Normalize a declared ``statusPath`` without laundering a cross-origin URL.

    A single leading slash is a harmless way to write a relative route, so it is
    stripped. A ``//`` prefix is not: that is a protocol-relative URL naming
    another host, and stripping its slashes would turn ``//evilhost/x`` into
    ``evilhost/x`` — which satisfies the route allowlist and reads like an
    ordinary relative path. Such a value is returned unchanged so that
    validation refuses it and the install fails with the real reason, rather
    than silently accepting a rewritten one.

    A host containing a dot happened to be refused anyway, because ``.`` is
    outside the allowlist. A dotless one was not, which is why the guard has to
    be explicit rather than incidental.
    """
    text = str(raw)
    if text.startswith("//"):
        return text
    return text.lstrip("/")


@dataclass
class SessionControlContribution:
    """A compact control an app contributes to the session (composer) bar.

    The slot exists because per-session app configuration has nowhere else to
    live: without it an app contributes a sidebar page and nothing else, so a
    setting scoped to "this chat" has to be set on a separate page against an
    opaque session key the app cannot even discover.

    Rendered by the dashboard as a lazily-imported ESM module, exactly like a
    page, so the existing import map (react, the app SDK) and the single-React
    guarantee apply unchanged. The component is handed the active session's
    identity as props — that is the whole point of the slot.

    ``statusPath`` is optional and lets the chip carry state *before* it is
    opened. Without it a control can only report anything once its module is
    lazily imported, which is on first click — so a per-session setting looks
    unset until you go looking for it. The dashboard GETs
    ``<the app's own route base>/<statusPath>?session_key=…`` and reads
    ``{state, tooltip}``, where state is ``ok`` | ``warn`` | ``none``.

    That base depends on how the app serves its backend, because the two are
    mounted at different prefixes: an app with in-gateway hook routes answers
    under ``/api/apps/<app>/``, while an app running its own backend PROCESS is
    reverse-proxied at ``/apps/<app>/api/``. The dashboard picks the prefix from
    the manifest rather than trusting a declared one, so a control does not have
    to know which it is.
    """

    id: str = ""  # stable per-app identifier, e.g. "env-picker"
    entryPoint: str = ""  # ESM bundle path relative to ui/  # noqa: N815
    label: str = ""  # accessible name; also the tooltip
    icon: str = ""  # lucide icon name
    statusPath: str = ""  # optional backend route reporting per-session chip state  # noqa: N815

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "entryPoint": self.entryPoint}
        if self.label:
            d["label"] = self.label
        if self.icon:
            d["icon"] = self.icon
        if self.statusPath:
            d["statusPath"] = self.statusPath
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionControlContribution:
        return cls(
            id=str(data.get("id", "")),
            entryPoint=str(data.get("entryPoint", "")),  # noqa: N815
            label=str(data.get("label", "")),
            icon=str(data.get("icon", "")),
            statusPath=_normalize_status_path(data.get("statusPath", "")),  # noqa: N815
        )


# A contributed command's id keys the Command Bar row and its usage record, so the
# grammar is the same narrow kebab slug the overlay registry uses -- no dots, no
# path separators, no leading dash.
# Both patterns below are applied with `.fullmatch()`, never `.match()`. Python's `$`
# also matches immediately BEFORE a trailing newline, so `.match()` accepts
# `"approve-all\n"` and `"github.com\n"` -- while JavaScript's `$` without the `m` flag
# does not, so `contributedCommands.ts` rejects exactly those. That asymmetry is the
# drift shape this contract has already been bitten by three times: the manifest
# installs clean and the launcher then shows nothing, with no error the app author sees.
_COMMAND_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

#: A contributed command's ``agent``: a bare config stem. Byte-for-byte the pattern
#: `contributedCommands.ts` clamps against (used with ``fullmatch``, see the note
#: above), so a value the launcher would drop is refused at install instead of
#: silently running the command on the dashboard default.
_COMMAND_AGENT_RE = re.compile(r"[A-Za-z0-9._-]{1,120}")

#: Longest accepted argument host allowlist. The list is scanned per keystroke, and
#: one longer than this is a manifest bug rather than a real allowlist.
_MAX_ARGUMENT_HOSTS = 20

#: A literal hostname, optionally with a leading dot meaning "this domain or any
#: subdomain of it". Fixed and host-owned: it is never built from manifest input.
_HOST_RE = re.compile(r"^\.?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")

#: Most commands one app may contribute, mirroring `MAX_COMMANDS_PER_APP` in
#: `contributedCommands.ts`. Not a layout limit -- the group is display-capped anyway
#: -- but a bound on how much work one app can add to ranking on every keystroke.
_MAX_COMMANDS_PER_APP = 20

#: Bounds on hidden match aliases, mirroring `MAX_KEYWORDS` / `MAX_KEYWORD` in
#: `contributedCommands.ts`. The launcher's ranking walks every keyword on every
#: keystroke, so an unbounded list is paid per character typed, not once per render.
_MAX_KEYWORDS = 30
_MAX_KEYWORD = 60
#: Longest accepted row label, mirroring `MAX_TITLE` in `contributedCommands.ts`.
_MAX_TITLE = 120

#: Longest accepted prompt template. The prompt is sent to an agent as if the user
#: typed it; a template past this length is a document, not a command.
_MAX_PROMPT_TEMPLATE = 4000

#: The placeholder a prompt template uses to interpolate the collected argument.
ARGUMENT_TOKEN = "{argument}"  # noqa: S105 - a template placeholder, not a secret

# A panel-tab id keys the frontend side-panel tab registry and is joined into the
# persisted tab kind ``app:<app_name>:<id>``, so it is a narrow storage-safe slug --
# no dots, no path separators, no leading digit. Applied with ``.fullmatch()`` for the
# reason spelled out for ``_COMMAND_SLUG_RE`` above: ``.match()`` would accept a
# trailing newline that the persisted kind then carries into localStorage.
_PANEL_TAB_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

#: Most side-panel tabs one app may contribute, mirroring ``MAX_PANEL_TABS_PER_APP`` in
#: ``panelTabRegistry.ts``. The strip is a fixed-width surface shared with the built-in
#: tabs, so this bounds how much of it one app can claim -- and, like the command caps
#: above, it is enforced on BOTH sides: a cap only the manifest enforces would let the
#: read path render the overflow, and one only the reader enforces is a silent
#: truncation the app author is never told about.
_MAX_PANEL_TABS_PER_APP = 8

#: Most file-menu rows one app may contribute, mirroring `MAX_FILE_MENU_ITEMS_PER_APP`
#: in `fileMenuContributions.tsx`. Lower than the command cap because these rows are not
#: a searchable list: every one of them lands in three menus a reader opens by hand, and
#: a menu long enough to scroll is worse than a missing row. Enforced on both sides --
#: a cap only the manifest enforces truncates silently in the menu instead.
_MAX_FILE_MENU_ITEMS_PER_APP = 10

#: The file-menu surfaces a contributed row may attach to. Data rather than a branch, so
#: adding a surface is this line plus a render site, never an evaluator edit.
FILE_MENU_SURFACES = frozenset({"file-overflow", "tree-context", "folder-row"})

#: The node kinds a `when.kinds` filter may name.
_FILE_MENU_KINDS = frozenset({"file", "dir"})

#: Most editor bindings one app may bind, mirroring `MAX_EDITOR_BINDINGS_PER_APP` in
#: `editorBindingContributions.ts`. Each one is a key chord or a modifier-click the
#: reader has to remember: a dozen per app is already more than a reader will.
_MAX_EDITOR_BINDINGS_PER_APP = 12

#: The modifiers a binding trigger may name. `primary` is Cmd on macOS and Ctrl
#: elsewhere, the same rule the editor applies to its own multi-cursor click, so an
#: app's chord lands on the key its readers expect on their platform.
EDITOR_BINDING_MODIFIERS = frozenset({"primary", "shift", "alt"})

#: Keys a binding trigger may name: the function keys, letters and digits. Letters
#: and digits need at least one modifier (`validate` checks), since a bare one is
#: typing. Everything else -- Enter, Escape, arrows, Tab, Backspace -- is the
#: editor's, and a chord on them would take an editing key away from the reader.
_EDITOR_BINDING_KEY_RE = re.compile(r"^(f([1-9]|1[0-2])|[a-z0-9])$")

#: Chords the host owns whatever the file: the save key, the dashboard's quick
#: search, and the dashboard shortcuts that have the same chord on every platform
#: (the editor sees a key before the dashboard does, so a binding on one would take
#: that shortcut away while the editor has focus). The dashboard refuses the rest
#: of its shortcuts per platform, with the reader's own changes applied; its drift
#: test (`editorBindingDashboardChords.test.ts`) keeps this set equal to the shared
#: part. Every other chord is an app's to bind; two apps binding one chord for the
#: same file are resolved by the dispatch order, not refused here.
RESERVED_EDITOR_CHORDS = frozenset(
    {
        "primary+s",
        "primary+k",
        # Panel navigation, the shortcuts reference, new and close chat.
        "alt+c",
        "alt+n",
        "alt+p",
        "alt+s",
        "alt+k",
        "primary+n",
        "primary+w",
        "alt+shift+n",
        "alt+shift+w",
        # Cycling the agent, reasoning, approval mode and model; focus mode.
        "alt+shift+a",
        "alt+shift+z",
        "alt+shift+d",
        "alt+shift+c",
        "alt+shift+f",
        "alt+shift+v",
        "alt+shift+s",
        "alt+shift+x",
        "alt+shift+m",
        # Switching between the local gateway and remote crews.
        "primary+1",
        "primary+2",
        "primary+3",
        "primary+4",
        "primary+5",
        "primary+6",
    }
)

#: Entries ``permissions.editor`` may name. ``content``: a binding's answer of
#: ``{"stale": true}`` may be followed by the buffer's text, once.
EDITOR_PERMISSION_CONTENT = "content"
EDITOR_PERMISSION_ENTRIES = frozenset({EDITOR_PERMISSION_CONTENT})


def _mirrored_len(text: str) -> int:
    """Length in UTF-16 code units -- what JavaScript's ``.length`` counts.

    Every cap above is mirrored by a constant in ``contributedCommands.ts`` compared
    against ``.length``, and the two languages do not agree on what a character is:
    Python counts CODE POINTS, JavaScript counts UTF-16 units, so anything outside the
    BMP counts once here and twice there. A 100-emoji title is 100 to ``len()`` and 200
    to the launcher, which passed the manifest and was then dropped by the renderer --
    the app installed clean and the row never appeared. This file already says of the
    title cap that "a cap that only one side enforces is not a cap"; that holds for the
    UNIT as well as for the number, so the caps are measured the host's way.
    UNPAIRED surrogates are why this passes ``surrogatepass``: JSON can carry a lone
    ``\\ud800`` escape, ``json.loads`` accepts it, and a plain ``utf-16-le`` encode then
    raises ``UnicodeEncodeError`` -- so a manifest would CRASH validation instead of
    being told what is wrong with it. With the flag the count still matches the host
    exactly: a lone surrogate is one unit in both languages, an astral character two.
    """
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


@dataclass
class CommandArgument:
    """The ONE value a contributed command collects before it can act.

    Deliberately singular. Raycast-style multi-argument tokens are a real feature,
    but every argument is another thing the reader must get right before a command
    that writes somewhere fires, and one value covers the cases this contribution
    point exists for (a link, a query, an identifier). A second argument is an
    additive change to this class, not a rewrite of it.

    The argument is checkable BEFORE the command runs -- the collected text is
    spliced into an instruction handed to an agent with tools, so "anything the
    reader pasted" is not an acceptable domain.

    ``kind`` names one of a FIXED set of matchers the host implements. It is
    deliberately not a regex, and that is the whole design of this field. An earlier
    revision let the manifest ship its own ``pattern``; a regex is a small program,
    and running a third party's program against the field on every keystroke, on the
    thread that draws the launcher, is a hang the reader cannot escape -- ``^(a+)+$``
    and ``^(a|aa)+$`` are both under ten characters and both exponential, and neither
    Python nor JavaScript can interrupt a synchronous match. Fencing that off with
    syntactic checks was attempted and abandoned: the checks can only ever recognize
    shapes, so each one invites the next hostile pattern that it does not cover.

    So the manifest DESCRIBES what it wants and the host decides how to check it.
    ``url`` parses with the runtime's own URL parser (linear, no backtracking) and
    then applies ``hosts``; ``text`` accepts any non-empty value. Both run in time
    proportional to the input no matter what the manifest says. Adding a kind is a
    change to this file -- which is exactly the point: the vocabulary is ours.

    The cost is precision, and it is a real cost. A pattern could demand
    ``/pull/<n>`` specifically; ``kind="url"`` with ``hosts=["github.com"]`` accepts
    any URL on that host and leaves what the link DENOTES to the agent reading it.
    That is the right split -- the host is the wrong place to encode another
    product's URL taxonomy, and it cannot do so safely.
    """

    #: Matchers the host implements. Extending this is a deliberate host change.
    KINDS = ("url", "text")

    placeholder: str = ""
    hint: str = ""
    kind: str = "text"
    hosts: list[str] = field(default_factory=list)
    patternError: str = ""
    #: Whether the source manifest carried the retired ``pattern`` key. Kept so
    #: ``validate`` can refuse a stale-contract app loudly instead of silently
    #: dropping an unknown key and running on the loosest matcher. Not serialized --
    #: it describes the INPUT, not the contract.
    saw_pattern: bool = False
    #: Whether the source manifest carried a ``hosts`` that was not a list. Kept for the
    #: same reason as ``saw_pattern``: the coerced value is indistinguishable from a
    #: deliberate empty list, and empty means ANY host. Not serialized.
    bad_hosts: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.placeholder:
            d["placeholder"] = self.placeholder
        if self.hint:
            d["hint"] = self.hint
        if self.kind:
            d["kind"] = self.kind
        if self.hosts:
            d["hosts"] = list(self.hosts)
        if self.patternError:
            d["patternError"] = self.patternError
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CommandArgument:
        hosts_raw = data.get("hosts", [])
        return cls(
            placeholder=str(data.get("placeholder", "")),
            hint=str(data.get("hint", "")),
            kind=str(data.get("kind", "text")),
            hosts=(
                [str(h).strip().lower() for h in hosts_raw if str(h).strip()]
                if isinstance(hosts_raw, list)
                else []
            ),
            patternError=str(data.get("patternError", "")),
            saw_pattern="pattern" in data,
            # A `hosts` that is present but not a list would otherwise coerce to the
            # empty list -- which does not mean "no opinion", it means "any host". The
            # author wrote a restriction and would get none, silently, with autoSend
            # still on. Recorded here and refused in validate() rather than dropped.
            bad_hosts="hosts" in data and not isinstance(hosts_raw, list),
        )


@dataclass
class CommandContribution:
    """One command an app contributes to the host's Command Bar.

    This is the seam that lets a command row live OUTSIDE this repository: the app
    declares what the row says and what it does, and the host renders and runs it.
    It is deliberately DECLARATIVE -- a title, an optional argument, and a prompt
    template -- and carries no code. An app that could ship a function into the
    launcher would be running third-party JavaScript inside the host's own
    surface, on every keystroke, with the reader's session; declaring data the
    host interprets is the same trade the overlay registry already makes by
    resolving ``id`` against components compiled into the bundle rather than
    loading one from the app.

    ``prompt`` is the command's action: activating it opens a NEW session seeded
    with this text. ``autoSend`` asks the host to send it immediately rather than
    leaving it in the composer -- see the module spec for what the host shows the
    reader before it does.

    ``icon`` names a glyph from the host's own set. An arbitrary URL or inline SVG
    is refused: the launcher is not a place to load remote images from, and a glyph
    that must be fetched cannot render in a surface that promises to issue no
    request.
    """

    id: str = ""
    title: str = ""
    subtitle: str = ""
    icon: str = ""
    keywords: list[str] = field(default_factory=list)
    prompt: str = ""
    autoSend: bool = False
    #: Agent the seeded session is created with; empty means the dashboard default.
    #: Must name one of this app's OWN ``agents`` (checked by ``AppManifest.validate``)
    #: -- the prompt is app-authored, so it may not borrow a host agent's grants.
    agent: str = ""
    argument: CommandArgument | None = None
    #: Whether the manifest's ``argument`` was present but not an object. Same shape as
    #: ``Contributes.bad_commands``, and the one place where erasing it also DIVERGES
    #: from the host: the frontend distinguishes "no argument" from "argument declared
    #: but broken" and drops the whole row for the latter, so coercing to ``None`` here
    #: installs a manifest whose command the launcher then refuses to render. Not
    #: serialized -- it describes the INPUT.
    bad_argument: bool = False

    def erases_a_restriction(self) -> bool:
        """Whether serializing this would emit something LOOSER than the input.

        The refusal flags deliberately describe the input rather than the contract, so
        they are not serialized -- but ``list_apps()`` reads a manifest off disk and
        re-serializes it with no ``validate()`` in between. For a manifest edited after
        install that is the whole gap: ``pattern`` and a scalar ``hosts`` both vanish,
        and what reaches the dashboard is a well-formed argument on the DEFAULT matcher
        -- ``text``, any host -- so the frontend's mirror of these refusals has nothing
        left to fire on and the value goes to the agent unchecked.

        A malformed ``kind`` or a non-hostname ``hosts`` entry is NOT listed here: those
        survive serialization verbatim, so the frontend still sees and refuses them.
        """
        if self.bad_argument:
            return True
        arg = self.argument
        return arg is not None and (arg.saw_pattern or arg.bad_hosts)

    def to_dict(self) -> dict[str, Any]:
        if self.erases_a_restriction():
            # Emitting nothing costs this app its row on a surface whose install path
            # already refused it loudly. Emitting a rejection MARKER instead would put
            # the safe outcome behind the reader's dashboard understanding a new field,
            # and a dashboard that did not would read the permissive matcher -- the
            # failure this exists to prevent. So it fails closed here.
            return {}
        d: dict[str, Any] = {"id": self.id, "title": self.title, "prompt": self.prompt}
        if self.subtitle:
            d["subtitle"] = self.subtitle
        if self.icon:
            d["icon"] = self.icon
        if self.keywords:
            d["keywords"] = list(self.keywords)
        if self.autoSend:
            d["autoSend"] = True
        if self.agent:
            d["agent"] = self.agent
        if self.argument is not None:
            arg_d = self.argument.to_dict()
            if arg_d:
                d["argument"] = arg_d
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CommandContribution:
        arg_raw = data.get("argument")
        keywords_raw = data.get("keywords", [])
        agent_raw = data.get("agent")
        return cls(
            id=str(data.get("id", "")),
            title=str(data.get("title", "")),
            subtitle=str(data.get("subtitle", "")),
            icon=str(data.get("icon", "")),
            keywords=[str(k) for k in keywords_raw] if isinstance(keywords_raw, list) else [],
            prompt=str(data.get("prompt", "")),
            # Identity against the JSON boolean, NOT ``bool(...)``: every non-empty
            # string is truthy, so ``"autoSend": "false"`` would coerce to True and
            # then serialize back as ``true`` -- a manifest that reads as disabled
            # silently enabling the one capability that sends text on the reader's
            # behalf. Only the literal ``true`` turns it on.
            autoSend=data.get("autoSend") is True,
            agent=agent_raw if isinstance(agent_raw, str) else "",
            argument=CommandArgument.from_dict(arg_raw) if isinstance(arg_raw, dict) else None,
            # A present-but-not-an-object ``argument`` would otherwise coerce to "no
            # argument declared", which is a DIFFERENT command rather than an invalid
            # one. An explicit ``null`` is treated as absent, matching the host.
            bad_argument=(
                "argument" in data and arg_raw is not None and not isinstance(arg_raw, dict)
            ),
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        where = f"contributes.commands[{self.id or '?'}]"
        if not self.id:
            errors.append("contributes.commands: entry missing id")
        elif not _COMMAND_SLUG_RE.fullmatch(self.id):
            errors.append(
                f"{where}: id must be lowercase alphanumeric with dashes, got {self.id!r}"
            )
        if len(self.keywords) > _MAX_KEYWORDS:
            # The launcher's ranking walks every keyword of every row on every keystroke,
            # so this is the one declared field whose cost is paid per character typed.
            # Mirrors `MAX_KEYWORDS` in `contributedCommands.ts`, which drops the overflow
            # -- refused here so the author is told rather than silently trimmed.
            errors.append(
                f"{where}: {len(self.keywords)} keywords exceeds the limit of " f"{_MAX_KEYWORDS}"
            )
        for kw in self.keywords:
            if _mirrored_len(kw) > _MAX_KEYWORD:
                errors.append(
                    f"{where}: keyword exceeds {_MAX_KEYWORD} characters " f"({_mirrored_len(kw)})"
                )
                break
        if self.agent and not _COMMAND_AGENT_RE.fullmatch(self.agent):
            # A bare config stem: no separator (which would escape the agents dir),
            # and exactly the set `contributedCommands.ts` accepts. Whether it names
            # one of THIS app's agents is checked by `AppManifest.validate`, which is
            # where the app's own `agents` are known.
            errors.append(
                f"{where}: agent must match [A-Za-z0-9._-] and be 1-120 characters, "
                f"got {self.agent!r}"
            )
        if not self.title:
            errors.append(f"{where}: missing title")
        elif _mirrored_len(self.title) > _MAX_TITLE:
            # Mirrors `MAX_TITLE` in `contributedCommands.ts`. Missing here originally,
            # which meant an over-long title passed the manifest and was then dropped by
            # the frontend -- the command vanished from the launcher with the app author
            # having seen no error on install, the worst of both validators.
            errors.append(
                f"{where}: title exceeds {_MAX_TITLE} characters " f"({_mirrored_len(self.title)})"
            )
        if self.subtitle and _mirrored_len(self.subtitle) > _MAX_TITLE:
            # Mirrors the frontend's cap. The subtitle is SEARCHED -- `rankRootRows` runs
            # `fuzzyMatch` over it on every keystroke -- so it belongs with the title and
            # keyword bounds rather than with the untouched display fields.
            errors.append(
                f"{where}: subtitle exceeds {_MAX_TITLE} characters "
                f"({_mirrored_len(self.subtitle)})"
            )
        if not self.prompt:
            # A command with no prompt has no action. There is no other verb yet, so
            # this is a broken row rather than a differently-shaped one.
            errors.append(f"{where}: missing prompt")
        elif _mirrored_len(self.prompt) > _MAX_PROMPT_TEMPLATE:
            errors.append(
                f"{where}: prompt exceeds {_MAX_PROMPT_TEMPLATE} characters "
                f"({_mirrored_len(self.prompt)})"
            )
        interpolates = ARGUMENT_TOKEN in self.prompt
        if self.bad_argument:
            # Reported INSTEAD of the two "declares no argument" errors below, which are
            # both true of the parsed value and both misleading about the manifest: they
            # tell an author who visibly wrote an ``argument`` that they wrote none.
            errors.append(
                f"{where}: argument must be an object -- a non-object value reads as a "
                "command with no argument, which the host treats as a different command "
                "rather than a broken one and refuses to render at all"
            )
        elif self.argument is None:
            if interpolates:
                errors.append(
                    f"{where}: prompt interpolates {ARGUMENT_TOKEN} but the command "
                    "declares no argument"
                )
            if self.autoSend:
                # The host's consent mechanism for autoSend is the resolved-prompt
                # preview, and that preview lives in the ARGUMENT state. A command
                # with no argument never enters it, so autoSend there would send
                # app-authored text to a tool-enabled agent with nothing shown to the
                # reader at all. Refused rather than silently downgraded, so the app
                # author learns the rule instead of wondering why it did not fire.
                errors.append(
                    f"{where}: autoSend requires an argument -- the host shows the "
                    "resolved prompt in the argument field before sending, and a "
                    "command with no argument never reaches that step"
                )
        else:
            if not interpolates:
                # The reader is asked for a value the command then ignores -- always a
                # mistake, and a confusing one, because the command still runs.
                errors.append(
                    f"{where}: declares an argument but the prompt never uses " f"{ARGUMENT_TOKEN}"
                )
            errors.extend(self._validate_matcher(where))
        return errors

    def _validate_matcher(self, where: str) -> list[str]:
        errors: list[str] = []
        arg = self.argument
        if arg is None:
            return errors
        if arg.saw_pattern:
            # An app written against the revision of this contract that accepted its
            # own regex. Refused rather than migrated: `pattern` is an unknown key now,
            # so ignoring it would leave the argument on the default `text` matcher --
            # accepting ANY non-empty string -- while the app still declares autoSend
            # and still believes its pattern is guarding the value. Failing loudly is
            # the only outcome that does not quietly widen what reaches the agent.
            errors.append(
                f"{where}: argument.pattern is no longer accepted -- declare "
                f"argument.kind ({', '.join(CommandArgument.KINDS)}) instead, because "
                "the host implements the matcher and a manifest cannot supply one"
            )
        if arg.bad_hosts:
            errors.append(
                f"{where}: argument.hosts must be an array of hostnames -- a non-array "
                "value would erase the restriction rather than apply it, and an empty "
                "allowlist means ANY host"
            )
        if arg.kind not in CommandArgument.KINDS:
            errors.append(
                f"{where}: argument.kind must be one of "
                f"{', '.join(CommandArgument.KINDS)} (got {arg.kind!r})"
            )
        if arg.hosts and arg.kind != "url":
            errors.append(f"{where}: argument.hosts applies only to kind 'url'")
        if len(arg.hosts) > _MAX_ARGUMENT_HOSTS:
            errors.append(
                f"{where}: argument.hosts exceeds {_MAX_ARGUMENT_HOSTS} entries "
                f"({len(arg.hosts)})"
            )
        for name in ("placeholder", "hint", "patternError"):
            # The last app-supplied strings with no bound. Not searched, so they cost
            # LAYOUT rather than per-keystroke work: an outsized hint or patternError
            # pushes the resolved-prompt preview and the footer around, and that preview
            # is the consent surface for autoSend. Mirrors the frontend's cap, which
            # refuses the command outright.
            value = getattr(arg, name, "")
            if _mirrored_len(value) > _MAX_TITLE:
                errors.append(
                    f"{where}: argument.{name} exceeds {_MAX_TITLE} characters "
                    f"({_mirrored_len(value)})"
                )
        for host in arg.hosts:
            # A host is compared literally against the parsed URL's hostname, so
            # anything that is not a hostname cannot match and is a manifest bug worth
            # naming rather than silently never matching. A leading dot is allowed and
            # means "this domain or any subdomain".
            if not _HOST_RE.fullmatch(host):
                errors.append(f"{where}: argument.hosts entry is not a hostname ({host!r})")
        return errors


@dataclass
class PanelTabConfig:
    """One chat side-panel tab an app contributes.

    Declarative for the same reason a command is: core reads this and mounts
    ``entry`` through the in-process ESM app host that ``ui.pages`` already use --
    it never imports app code, and no live component crosses the app boundary. The
    frontend keys the tab on ``app:<app_name>:<id>``, which is what lets a tab
    survive a reload and disappear cleanly when the app is disabled.
    """

    id: str = ""  # stable tab id, unique within the app
    title: str = ""  # strip label
    menuLabel: str = ""  # '+' menu row label  # noqa: N815
    menuDescription: str = ""  # one-line description shown in the launcher  # noqa: N815
    icon: str = ""  # lucide icon name, resolved by the reader against lucide-react
    # ESM entry mounted as the tab body, resolved against the app's ``ui`` directory
    # -- the same base ``ui.entry`` uses, because the reader mounts both through the
    # one static route (``/apps/<name>/ui/<entry>``). So ``panel.mjs`` is the file at
    # ``<app root>/ui/panel.mjs``, and a leading ``ui/`` would resolve to ``ui/ui/``.
    entry: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.id:
            d["id"] = self.id
        if self.title:
            d["title"] = self.title
        if self.menuLabel:
            d["menuLabel"] = self.menuLabel
        if self.menuDescription:
            d["menuDescription"] = self.menuDescription
        if self.icon:
            d["icon"] = self.icon
        if self.entry:
            d["entry"] = self.entry
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PanelTabConfig:
        return cls(
            id=str(data.get("id", "")),
            title=str(data.get("title", "")),
            menuLabel=str(data.get("menuLabel", "")),  # noqa: N815
            menuDescription=str(data.get("menuDescription", "")),  # noqa: N815
            icon=str(data.get("icon", "")),
            entry=str(data.get("entry", "")),
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        where = f"contributes.panelTabs[{self.id or '?'}]"
        if not self.id:
            errors.append("contributes.panelTabs: entry missing id")
        elif not _PANEL_TAB_ID_RE.fullmatch(self.id):
            errors.append(f"{where}: id must be a storage-safe slug ({self.id!r})")
        for name, value in (
            ("title", self.title),
            ("menuLabel", self.menuLabel),
            ("entry", self.entry),
        ):
            if not value:
                errors.append(f"{where}: missing {name}")
        # `menuDescription` is capped alongside the two required labels because the
        # launcher card renders it: an uncapped one would be the single field an app
        # could use to blow out that surface's layout.
        for name, value in (
            ("title", self.title),
            ("menuLabel", self.menuLabel),
            ("menuDescription", self.menuDescription),
        ):
            if _mirrored_len(value) > _MAX_TITLE:
                got = _mirrored_len(value)
                errors.append(f"{where}: {name} exceeds {_MAX_TITLE} characters ({got})")
        # `entry` traversal is checked in `AppManifest.validate`, which is the only
        # scope that knows the app root -- see `_app_path_refusal` there.
        return errors


@dataclass
class FileMenuWhen:
    """Declarative visibility predicate for a contributed file-menu row.

    Evaluated by the host, never a live callback across the app boundary -- an app
    bundle is loaded at runtime and cannot register a function into a menu the host
    renders. An empty field is "no constraint on this axis"; every present field must
    match (AND), and the same predicate is mirrored in ``fileMenuContributions.tsx``
    because the host decides visibility on both the render and the dispatch side.
    """

    #: Lowercase, dot-stripped extensions: ``["md", "py"]``.
    extensions: list[str] = field(default_factory=list)
    #: Subset of :data:`_FILE_MENU_KINDS`.
    kinds: list[str] = field(default_factory=list)
    #: Whether ``extensions`` / ``kinds`` were present but not lists. Same reason as
    #: ``CommandArgument.bad_hosts``: coercing to ``[]`` does not mean "no opinion", it
    #: means "match everything", so an author who wrote a restriction would silently get
    #: none. Not serialized.
    bad_fields: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.extensions:
            d["extensions"] = self.extensions
        if self.kinds:
            d["kinds"] = self.kinds
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileMenuWhen:
        ext_raw = data.get("extensions", [])
        kinds_raw = data.get("kinds", [])
        return cls(
            # A leading dot is normalized off so `.md` and `md` name the same file, and
            # the case is folded here so the render side can compare literally.
            extensions=[
                str(e).lower().lstrip(".")
                for e in (ext_raw if isinstance(ext_raw, list) else [])
                if e
            ],
            kinds=[str(k) for k in (kinds_raw if isinstance(kinds_raw, list) else []) if k],
            bad_fields=("extensions" in data and not isinstance(ext_raw, list))
            or ("kinds" in data and not isinstance(kinds_raw, list)),
        )


@dataclass
class FileMenuItemConfig:
    """One row an app contributes to a file, tree, or folder menu.

    Endpoint-dispatched like :class:`CommandContribution` is prompt-dispatched: the host
    reads this declaration, renders the row itself, and POSTs the file's PATH to
    ``endpoint`` when the row is activated. It never imports app code and holds no live
    callback, which is what makes the seam reachable by an app installed at runtime
    rather than only by a build-time composition root.
    """

    id: str = ""
    #: Row label. An app-owned literal: the host has no catalog key for a row it does
    #: not know about.
    label: str = ""
    #: Icon name resolved against the host's icon set (see ``AppIcon``).
    icon: str = ""
    #: The app's own route the row POSTs to, under ``/api/apps/<app>/``. The allowlist
    #: is enforced by the host at dispatch, not by this structural check.
    endpoint: str = ""
    surfaces: list[str] = field(default_factory=list)
    when: FileMenuWhen = field(default_factory=FileMenuWhen)
    #: Whether ``surfaces`` was present but not a list -- an erased restriction rather
    #: than an absent one, so it is refused instead of coerced. Not serialized.
    bad_surfaces: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.id:
            d["id"] = self.id
        if self.label:
            d["label"] = self.label
        if self.icon:
            d["icon"] = self.icon
        if self.endpoint:
            d["endpoint"] = self.endpoint
        if self.surfaces:
            d["surfaces"] = self.surfaces
        when_d = self.when.to_dict()
        if when_d:
            d["when"] = when_d
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileMenuItemConfig:
        when_raw = data.get("when", {})
        surfaces_raw = data.get("surfaces", [])
        return cls(
            id=str(data.get("id", "")),
            label=str(data.get("label", "")),
            icon=str(data.get("icon", "")),
            endpoint=str(data.get("endpoint", "")),
            surfaces=[
                str(s) for s in (surfaces_raw if isinstance(surfaces_raw, list) else []) if s
            ],
            when=(
                FileMenuWhen.from_dict(when_raw)
                if isinstance(when_raw, dict)
                else FileMenuWhen(bad_fields=True)
            ),
            bad_surfaces="surfaces" in data and not isinstance(surfaces_raw, list),
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        where = f"contributes.fileMenuItems[{self.id or '?'}]"
        if not self.id:
            errors.append("contributes.fileMenuItems: entry missing id")
        elif not _COMMAND_SLUG_RE.fullmatch(self.id):
            # Shares the command slug's grammar deliberately: both are app-owned ids
            # mirrored by a frontend regex, and two patterns that must agree while being
            # spelled separately is the exact drift that grammar's comment already names.
            errors.append(f"{where}: id must be a lowercase kebab slug")
        if not self.label:
            errors.append(f"{where}: missing label")
        elif _mirrored_len(self.label) > _MAX_TITLE:
            errors.append(
                f"{where}: label exceeds {_MAX_TITLE} characters ({_mirrored_len(self.label)})"
            )
        if _mirrored_len(self.icon) > _MAX_TITLE:
            errors.append(
                f"{where}: icon exceeds {_MAX_TITLE} characters ({_mirrored_len(self.icon)})"
            )
        if not self.endpoint:
            errors.append(f"{where}: missing endpoint")
        if self.bad_surfaces:
            errors.append(
                f"{where}: surfaces must be an array -- a non-array value passes as empty "
                "and the row then appears in no menu, with no error its author can see"
            )
        elif not self.surfaces:
            errors.append(f"{where}: must name at least one surface")
        for surface in self.surfaces:
            if surface not in FILE_MENU_SURFACES:
                errors.append(
                    f"{where}: unknown surface {surface!r} "
                    f"(expected one of {sorted(FILE_MENU_SURFACES)})"
                )
        if self.when.bad_fields:
            errors.append(
                f"{where}: when.extensions and when.kinds must be arrays -- a non-array "
                "value passes as unfiltered, so the row would show everywhere the author "
                "meant to restrict it"
            )
        for node_kind in self.when.kinds:
            if node_kind not in _FILE_MENU_KINDS:
                errors.append(
                    f"{where}: when.kinds has unknown kind {node_kind!r} "
                    f"(expected one of {sorted(_FILE_MENU_KINDS)})"
                )
        return errors


@dataclass
class EditorBindingTrigger:
    """What fires a contributed editor binding: a key chord or a modifier-click.

    Exactly one of ``key`` and ``click`` is set. ``modifiers`` is the set of
    :data:`EDITOR_BINDING_MODIFIERS` that must be held, every other modifier up, so
    ``F12`` and ``Shift+F12`` are two triggers and a Cmd+Shift+click never fires a
    Cmd+click binding. ``canonical()`` is the spelling both sides compare by.
    """

    key: str = ""
    click: bool = False
    modifiers: list[str] = field(default_factory=list)
    #: ``modifiers`` present but not a list, or ``click`` present but not a boolean:
    #: an erased restriction, refused rather than coerced. Not serialized.
    bad_fields: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.key:
            d["key"] = self.key
        if self.click:
            d["click"] = True
        if self.modifiers:
            d["modifiers"] = self.modifiers
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EditorBindingTrigger:
        mods_raw = data.get("modifiers", [])
        click_raw = data.get("click", False)
        return cls(
            # Case-folded here so the dispatch side can compare literally.
            key=str(data.get("key", "")).lower(),
            click=click_raw is True,
            modifiers=sorted(
                {str(m).lower() for m in (mods_raw if isinstance(mods_raw, list) else []) if m}
            ),
            bad_fields=("modifiers" in data and not isinstance(mods_raw, list))
            or ("click" in data and not isinstance(click_raw, bool)),
        )

    def canonical(self) -> str:
        """``primary+shift+f12`` or ``click:primary``: the trigger as one comparable string."""
        mods = "+".join(sorted(self.modifiers))
        if self.click:
            return f"click:{mods}"
        return f"{mods}+{self.key}" if mods else self.key

    def validate(self, where: str) -> list[str]:
        errors: list[str] = []
        if self.bad_fields:
            errors.append(
                f"{where}: trigger.modifiers must be an array and trigger.click a boolean -- "
                "a value of another type passes as absent, and the binding then fires on "
                "a plainer input than its author bound"
            )
        if bool(self.key) == self.click:
            errors.append(f"{where}: trigger must name exactly one of key or click")
            return errors
        for mod in self.modifiers:
            if mod not in EDITOR_BINDING_MODIFIERS:
                errors.append(
                    f"{where}: unknown modifier {mod!r} "
                    f"(expected one of {sorted(EDITOR_BINDING_MODIFIERS)})"
                )
        if self.click:
            if not self.modifiers:
                # A plain click is how the reader places the caret.
                errors.append(f"{where}: a click trigger needs at least one modifier")
            return errors
        if not _EDITOR_BINDING_KEY_RE.fullmatch(self.key):
            errors.append(f"{where}: trigger.key {self.key!r} must be F1-F12, a letter or a digit")
        elif not self.key.startswith("f") and not self.modifiers:
            errors.append(f"{where}: a letter or digit trigger needs at least one modifier")
        if self.canonical() in RESERVED_EDITOR_CHORDS:
            errors.append(f"{where}: {self.canonical()} is the host's own chord")
        return errors


@dataclass
class EditorBindingConfig:
    """One key chord or modifier-click an app binds in the side panel's code editor.

    The host owns the listener, the caret and the response: it matches the event
    against the declared trigger and POSTs the file's PATH and the position to
    ``endpoint``; the app answers with a message to show or a location to open. It
    never imports app code and holds no live callback, the same seam as
    :class:`FileMenuItemConfig`, and by default the same rule about content: only a
    SHA-256 of the buffer crosses with the binding, so the app can tell whether its
    on-disk copy is the text the reader sees. An app granted
    ``permissions.editor: ["content"]`` may answer ``{"stale": true}`` to that hash
    and is then sent the buffer once; the grant is shown and approved on the app
    page like a route, so a reader knows which apps can read what they typed.
    """

    id: str = ""
    #: What the binding does, for the reader: a screen reader announces it, and any
    #: listing of an app's bindings shows it. App-owned literal.
    label: str = ""
    trigger: EditorBindingTrigger = field(default_factory=EditorBindingTrigger)
    endpoint: str = ""
    when: FileMenuWhen = field(default_factory=FileMenuWhen)
    #: ``trigger`` present but not an object. Not serialized.
    bad_trigger: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.id:
            d["id"] = self.id
        if self.label:
            d["label"] = self.label
        trigger = self.trigger.to_dict()
        if trigger:
            d["trigger"] = trigger
        if self.endpoint:
            d["endpoint"] = self.endpoint
        when_d = self.when.to_dict()
        if when_d:
            d["when"] = when_d
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EditorBindingConfig:
        trigger_raw = data.get("trigger", {})
        when_raw = data.get("when", {})
        return cls(
            id=str(data.get("id", "")),
            label=str(data.get("label", "")),
            trigger=(
                EditorBindingTrigger.from_dict(trigger_raw)
                if isinstance(trigger_raw, dict)
                else EditorBindingTrigger()
            ),
            endpoint=str(data.get("endpoint", "")),
            when=(
                FileMenuWhen.from_dict(when_raw)
                if isinstance(when_raw, dict)
                else FileMenuWhen(bad_fields=True)
            ),
            bad_trigger="trigger" in data and not isinstance(trigger_raw, dict),
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        where = f"contributes.editorBindings[{self.id or '?'}]"
        if not self.id:
            errors.append("contributes.editorBindings: entry missing id")
        elif not _COMMAND_SLUG_RE.fullmatch(self.id):
            errors.append(f"{where}: id must be a lowercase kebab slug")
        if not self.label:
            errors.append(f"{where}: missing label")
        elif _mirrored_len(self.label) > _MAX_TITLE:
            errors.append(
                f"{where}: label exceeds {_MAX_TITLE} characters ({_mirrored_len(self.label)})"
            )
        if not self.endpoint:
            errors.append(f"{where}: missing endpoint")
        if self.bad_trigger:
            errors.append(f"{where}: trigger must be an object")
        else:
            errors.extend(self.trigger.validate(where))
        if self.when.bad_fields:
            errors.append(
                f"{where}: when.extensions and when.kinds must be arrays -- a non-array "
                "value passes as unfiltered, so the binding would fire in every file the "
                "author meant to restrict it to"
            )
        if self.when.kinds and self.when.kinds != ["file"]:
            # The editor shows files; a binding over a directory has nowhere to fire.
            errors.append(f"{where}: when.kinds may only name 'file'")
        return errors


@dataclass
class Contributes:
    """What an app adds to host surfaces it does not own.

    Separate from ``ui`` on purpose: ``ui`` is where an app declares surfaces of
    its OWN (a page, an overlay it supplies a component for), while a contribution
    is a row inside a surface the host renders and controls. Keeping them apart is
    what lets an app with no page, no bundle and no backend -- a manifest and a
    skill -- still reach the launcher.
    """

    commands: list[CommandContribution] = field(default_factory=list)
    #: Side-panel tabs. Unlike a command, a tab does carry a bundle (``entry``), but it
    #: is still a contribution rather than ``ui``: the strip, the persistence and the
    #: mounting are the host's, and the app only says which body to put in one slot.
    panelTabs: list[PanelTabConfig] = field(default_factory=list)  # noqa: N815
    #: Rows an app adds to the file-editor overflow, workspace-tree context, and folder
    #: menus. Same contract as ``commands`` two fields up -- declared, host-rendered,
    #: dispatched to the app's own endpoint -- so it carries the same malformed-input
    #: flags rather than coercing quietly.
    fileMenuItems: list[FileMenuItemConfig] = field(default_factory=list)  # noqa: N815
    #: Key chords and modifier-clicks an app binds in the code editor. Same contract
    #: as ``fileMenuItems``: declared, host-dispatched to the app's endpoint with the
    #: file's path, never its content.
    editorBindings: list[EditorBindingConfig] = field(default_factory=list)  # noqa: N815
    #: ``editorBindings`` present but not a list, and how many entries of a well-formed
    #: array were not objects. Not serialized.
    bad_editor_bindings: bool = False
    dropped_editor_bindings: int = 0
    #: Whether the source manifest's ``commands`` was present but not a list. Same reason
    #: as ``CommandArgument.bad_hosts``: coercing to ``[]`` is indistinguishable from a
    #: deliberate empty list, so the declaration would pass validation and then vanish
    #: from ``to_dict`` -- the author sees no error and no rows. Not serialized.
    bad_commands: bool = False
    #: Whether the manifest's ``contributes`` itself was present but not an object.
    #: Outermost case of the same shape. Not serialized.
    bad_block: bool = False
    #: How many ENTRIES of a well-formed ``commands`` array were not objects. The array
    #: being a list is not enough: silently filtering out a single bad element would
    #: let an app declaring five commands with one typo install with four and no
    #: warning.
    #: Counted rather than flagged so the error can say how many vanished. Not
    #: serialized.
    dropped_commands: int = 0
    sessionControls: list[SessionControlContribution] = field(default_factory=list)  # noqa: N815
    #: Whether the manifest's ``sessionControls`` was present but not a list. Same reason
    #: as ``bad_commands``: coercing to ``[]`` reads as a deliberate empty list, so the
    #: declaration would install clean and then never render a chip. Not serialized.
    bad_session_controls: bool = False
    #: Whether the source manifest's ``panelTabs`` was present but not a list, and how
    #: many entries of a well-formed array were not objects. Same reasoning as the two
    #: ``commands`` flags above, and the failure is if anything quieter here: a tab that
    #: never parses leaves no row in the ``+`` menu, no card in the launcher and no
    #: error, which reads exactly like an app that simply has no panel. Not serialized.
    bad_panel_tabs: bool = False
    dropped_panel_tabs: int = 0
    #: ``fileMenuItems`` present but not a list. Not serialized.
    bad_file_menu_items: bool = False
    #: How many entries of a well-formed ``fileMenuItems`` array were not objects. Not
    #: serialized.
    dropped_file_menu_items: int = 0

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        # A command that would serialize looser than it was declared emits ``{}`` and is
        # dropped here, so the read path cannot hand the dashboard a restriction-free
        # copy of an argument the manifest refused.
        commands = [c.to_dict() for c in self.commands]
        kept = [c for c in commands if c]
        if kept:
            d["commands"] = kept
        if self.sessionControls:
            d["sessionControls"] = [c.to_dict() for c in self.sessionControls]
        tabs = [t.to_dict() for t in self.panelTabs]
        kept_tabs = [t for t in tabs if t]
        if kept_tabs:
            d["panelTabs"] = kept_tabs
        items = [i.to_dict() for i in self.fileMenuItems]
        kept_items = [i for i in items if i]
        if kept_items:
            d["fileMenuItems"] = kept_items
        bindings = [g.to_dict() for g in self.editorBindings]
        kept_bindings = [g for g in bindings if g]
        if kept_bindings:
            d["editorBindings"] = kept_bindings
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Contributes:
        raw = data.get("commands", [])
        entries = raw if isinstance(raw, list) else []
        # Same defensive shape-guard as commands: a present-but-non-list
        # `sessionControls` must not raise out of install validation.
        raw_controls = data.get("sessionControls", [])
        controls = (
            [
                # A non-dict ENTRY is kept as an empty placeholder rather than
                # dropped. `validate()` promises a malformed control is refused at
                # install time instead of surfacing as a broken chat, and an entry
                # discarded here is reported as nothing at all — the app installs
                # clean and the control simply never appears. The placeholder fails
                # the required-field checks, which is that promised refusal.
                (
                    SessionControlContribution.from_dict(c)
                    if isinstance(c, dict)
                    else SessionControlContribution()
                )
                for c in raw_controls
            ]
            if isinstance(raw_controls, list)
            else []
        )
        tabs_raw = data.get("panelTabs", [])
        tab_entries = tabs_raw if isinstance(tabs_raw, list) else []
        items_raw = data.get("fileMenuItems", [])
        item_entries = items_raw if isinstance(items_raw, list) else []
        bindings_raw = data.get("editorBindings", [])
        binding_entries = bindings_raw if isinstance(bindings_raw, list) else []
        return cls(
            editorBindings=[
                EditorBindingConfig.from_dict(g) for g in binding_entries if isinstance(g, dict)
            ],
            bad_editor_bindings="editorBindings" in data and not isinstance(bindings_raw, list),
            dropped_editor_bindings=sum(1 for g in binding_entries if not isinstance(g, dict)),
            commands=[CommandContribution.from_dict(c) for c in entries if isinstance(c, dict)],
            panelTabs=[PanelTabConfig.from_dict(t) for t in tab_entries if isinstance(t, dict)],
            fileMenuItems=[
                FileMenuItemConfig.from_dict(i) for i in item_entries if isinstance(i, dict)
            ],
            bad_commands="commands" in data and not isinstance(raw, list),
            dropped_commands=sum(1 for c in entries if not isinstance(c, dict)),
            sessionControls=controls,
            bad_session_controls="sessionControls" in data and not isinstance(raw_controls, list),
            bad_panel_tabs="panelTabs" in data and not isinstance(tabs_raw, list),
            dropped_panel_tabs=sum(1 for t in tab_entries if not isinstance(t, dict)),
            bad_file_menu_items="fileMenuItems" in data and not isinstance(items_raw, list),
            dropped_file_menu_items=sum(1 for i in item_entries if not isinstance(i, dict)),
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.bad_block:
            errors.append(
                "contributes must be an object -- a non-object value validates as "
                "contributing nothing and then disappears from the serialized "
                "manifest, so the app author sees neither an error nor any rows"
            )
        if self.bad_commands:
            errors.append(
                "contributes.commands must be an array -- a non-array value passes as "
                "empty and then disappears from the serialized manifest, so the app "
                "author sees neither an error nor any rows"
            )
        if self.bad_session_controls:
            errors.append(
                "contributes.sessionControls must be an array -- a non-array value "
                "passes as empty and then disappears from the serialized manifest, so "
                "the app author sees neither an error nor any chip"
            )
        if self.dropped_commands:
            errors.append(
                f"contributes.commands: {self.dropped_commands} entr"
                f"{'y' if self.dropped_commands == 1 else 'ies'} "
                "must be an object -- a non-object entry is filtered out before "
                "validation, so the app installs with the remaining rows and its "
                "author is never told one was dropped"
            )
        if len(self.commands) > _MAX_COMMANDS_PER_APP:
            # Mirrors the frontend's slice. The fourth of four bounds to be mirrored
            # and the last one missed, which is the same failure the title cap had:
            # the manifest installed clean and the launcher then dropped the overflow,
            # so a thirty-command app lost ten rows with no error its author could see.
            # A cap that only one side enforces is not a cap; it is a silent truncation.
            errors.append(
                f"contributes.commands: {len(self.commands)} commands exceeds the "
                f"limit of {_MAX_COMMANDS_PER_APP}"
            )
        seen: set[str] = set()
        for cmd in self.commands:
            errors.extend(cmd.validate())
            if cmd.id:
                if cmd.id in seen:
                    # Two rows with one id: the second silently wins the frecency
                    # record and one of them becomes unreachable by usage.
                    errors.append(f"contributes.commands: duplicate id {cmd.id!r}")
                seen.add(cmd.id)
        if self.bad_panel_tabs:
            errors.append(
                "contributes.panelTabs must be an array -- a non-array value passes as "
                "empty and then disappears from the serialized manifest, so the app "
                "author sees neither an error nor a tab"
            )
        if self.dropped_panel_tabs:
            errors.append(
                f"contributes.panelTabs: {self.dropped_panel_tabs} entr"
                f"{'y' if self.dropped_panel_tabs == 1 else 'ies'} "
                "must be an object -- a non-object entry is filtered out before "
                "validation, so the app installs with the remaining tabs and its "
                "author is never told one was dropped"
            )
        if len(self.panelTabs) > _MAX_PANEL_TABS_PER_APP:
            # Mirrors the reader's slice in `panelTabRegistry.ts`, for the reason the
            # command cap above spells out: enforced on one side only, the overflow is
            # dropped by whichever side is stricter and nobody is told.
            errors.append(
                f"contributes.panelTabs: {len(self.panelTabs)} tabs exceeds the "
                f"limit of {_MAX_PANEL_TABS_PER_APP}"
            )
        tab_ids: set[str] = set()
        for tab in self.panelTabs:
            errors.extend(tab.validate())
            if tab.id:
                if tab.id in tab_ids:
                    # Two tabs with one id collapse onto a single persisted kind, so the
                    # second is unreachable and the first answers for both.
                    errors.append(f"contributes.panelTabs: duplicate id {tab.id!r}")
                tab_ids.add(tab.id)
        if self.bad_file_menu_items:
            errors.append(
                "contributes.fileMenuItems must be an array -- a non-array value passes "
                "as empty and then disappears from the serialized manifest, so the app "
                "author sees neither an error nor any rows"
            )
        if self.dropped_file_menu_items:
            errors.append(
                f"contributes.fileMenuItems: {self.dropped_file_menu_items} entr"
                f"{'y' if self.dropped_file_menu_items == 1 else 'ies'} "
                "must be an object -- a non-object entry is filtered out before "
                "validation, so the app installs with the remaining rows and its "
                "author is never told one was dropped"
            )
        if len(self.fileMenuItems) > _MAX_FILE_MENU_ITEMS_PER_APP:
            errors.append(
                f"contributes.fileMenuItems: {len(self.fileMenuItems)} items exceeds the "
                f"limit of {_MAX_FILE_MENU_ITEMS_PER_APP}"
            )
        seen_items: set[str] = set()
        for item in self.fileMenuItems:
            errors.extend(item.validate())
            if item.id:
                if item.id in seen_items:
                    # Two rows with one id: the dispatch key is the id, so the second
                    # row's activation would target the first row's endpoint.
                    errors.append(f"contributes.fileMenuItems: duplicate id {item.id!r}")
                seen_items.add(item.id)
        if self.bad_editor_bindings:
            errors.append(
                "contributes.editorBindings must be an array -- a non-array value passes "
                "as empty and then disappears from the serialized manifest, so the app "
                "author sees neither an error nor a binding"
            )
        if self.dropped_editor_bindings:
            errors.append(
                f"contributes.editorBindings: {self.dropped_editor_bindings} entr"
                f"{'y' if self.dropped_editor_bindings == 1 else 'ies'} "
                "must be an object -- a non-object entry is filtered out before "
                "validation, so the app installs with the remaining bindings and its "
                "author is never told one was dropped"
            )
        if len(self.editorBindings) > _MAX_EDITOR_BINDINGS_PER_APP:
            errors.append(
                f"contributes.editorBindings: {len(self.editorBindings)} bindings exceeds "
                f"the limit of {_MAX_EDITOR_BINDINGS_PER_APP}"
            )
        seen_bindings: set[str] = set()
        seen_triggers: dict[str, str] = {}
        for binding in self.editorBindings:
            errors.extend(binding.validate())
            if binding.id:
                if binding.id in seen_bindings:
                    errors.append(f"contributes.editorBindings: duplicate id {binding.id!r}")
                seen_bindings.add(binding.id)
            chord = binding.trigger.canonical()
            if chord and not binding.bad_trigger:
                # Two bindings on one chord within an app: only one could fire, and
                # which one would be the array's order rather than the author's intent.
                if chord in seen_triggers and binding.id != seen_triggers[chord]:
                    errors.append(
                        f"contributes.editorBindings: {binding.id!r} and "
                        f"{seen_triggers[chord]!r} both bind {chord}"
                    )
                seen_triggers.setdefault(chord, binding.id)
        return errors
