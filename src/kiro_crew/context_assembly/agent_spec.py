"""What the agent's spec and prompt files contribute to a prompt.

The ``[AGENT SYSTEM PROMPT]`` contract: which file a session's contract comes from
(:func:`_resolve_agent_prompt`, :func:`_load_agent_prompt`, :func:`_read_prompt_file`),
the bot name and the template tokens it carries (:func:`_resolve_prompt_templates`),
and the per-session readings those tokens resolve to -- the delegation cap, the
Computer Use gate and the browse gate, each read once per session
(:func:`_session_reading`). Beside it,
the other reads of an agent's spec: its ``includeCrewContext`` flag
(:func:`_read_include_crew_context`) and the Claude Code steering load
(:func:`_load_steering_resources`) with its admission gate
(:func:`steering_target_admissible`).

``_prompt_path``, ``_shipped_prompt``, ``kiro_agents_dir`` and ``has_dashboard_surface``
are read through :mod:`kiro_crew.context` at call time, because callers rebind them
there. Template code reads ``ContextBuilder`` attributes through the class, and the
methods read builder state through ``self``, so a class or instance patch applies.

Builder state read and written: ``_bot_name``; ``_cap_figures`` and
``_cap_figures_lock``; ``_computer_use_gates`` and ``_computer_use_gates_lock``;
``_browser_gates`` and ``_browser_gates_lock``; the class constants
``_CAP_FIGURE_SESSIONS``, ``_MAX_SUBAGENTS_TOKEN`` and ``_COMPUTER_USE_TOKEN``.

New agent-spec reads and prompt templates belong here.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew.context_assembly import sections as _sections

if TYPE_CHECKING:
    from kiro_crew.context import _ReadingT

logger = logging.getLogger("kiro_crew.context")

_WIDGET_TOKEN = "{{WIDGET_BLOCK}}"

#: Built-in agents whose spec prompt asks for widgets (the conductor's
#: introduction card and task board) and so get the per-surface widget pointer
#: in their ``[AGENT SYSTEM PROMPT]`` block. ``kirocrew-ledger-conductor`` is the
#: conductor's deprecated alias with the same body. ``kirocrew-worker`` is absent
#: on purpose: it reports to the work ledger, not to a person.
WIDGET_BLOCK_AGENTS = frozenset({"kirocrew-conductor", "kirocrew-ledger-conductor"})


def steering_target_admissible(resolved: Path, base: Path | None = None) -> bool:
    """Admission gate for a steering document's RESOLVED path.

    The session loader (:func:`_load_steering_resources`) admits a glob hit —
    symlinks included, since ``Path.resolve()`` follows them — when the target
    stays under the trust base, is a regular file, and is not a sensitive
    location. *base* defaults to ``$HOME``, the loader's own anchor; the
    dashboard's steering listing admits a leaf symlink through this same
    predicate with the source's LINK trust base (``$HOME`` for ``user``, the
    steering root itself for ``workspace``), so a repository-committed link
    can never read outside the root it ships in, and the ``user`` case cannot
    disagree with what the loader injects.
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner

    base_resolved = str((base or Path.home()).resolve()) + os.sep
    return (
        str(resolved).startswith(base_resolved)
        and resolved.is_file()
        and not ctx.is_sensitive_path(str(resolved))
    )


def _load_steering_resources() -> str:
    """Load steering files from the agent config's resources array.

    kiro-cli injects these automatically for its sessions; the dashboard
    must do it explicitly so that dashboard chat sessions also benefit
    from project-specific steering conventions.
    Only loads ``file://`` resources matching ``*.md``.
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner

    try:
        cfg_path = ctx.kiro_agents_dir() / "kirocrew.json"
        if not cfg_path.exists():
            return ""
        # The agents dir is user-writable and shared with other tools, so the
        # spec goes through the hardened agent-spec reader. ``safe_read_file``
        # screens the resolved target but reads it with an unbounded
        # ``fh.read()`` -- the size cap guards ``safe_read_file_bytes``, the
        # other helper -- and emits no SEL event, so it would read an oversized
        # spec whole here and audit no refusal. Every outcome the blanket
        # ``except`` below would absorb (PermissionError on a sensitive target,
        # AttributeError on non-object JSON) arrives as ``None`` and returns
        # the same empty string, without the read.
        from kiro_crew.agent_discovery import _read_agent_spec

        cfg = _read_agent_spec(
            cfg_path,
            operation="steering_resources",
            source="unknown",
        )
        if cfg is None:
            return ""
        resources = cfg.get("resources", [])
        parts: list[str] = []
        for res in resources:
            if not isinstance(res, str) or not res.startswith("file://"):
                continue
            raw_pattern = res.removeprefix("file://")
            base = Path.home()
            for p in sorted(base.glob(raw_pattern)):
                if p.suffix == ".md" and steering_target_admissible(p.resolve()):
                    try:
                        parts.append(ctx.safe_read_file(str(p)))
                    except PermissionError:
                        pass
        if parts:
            logger.debug(
                "loaded %d steering bytes from %d files", sum(len(p) for p in parts), len(parts)
            )
        return "\n".join(parts) if parts else ""
    except Exception as exc:
        logger.debug("steering load failed: %s", type(exc).__name__)
        return ""


def _read_include_crew_context(agent: str) -> bool:
    """Read ``includeCrewContext`` from *agent*'s materialized JSON. True on any miss.

    Reuses ``_load_agent_prompt``'s sensitive-path-gated scan: skip ``._`` macOS
    sidecars, ``resolve(strict=True)``, refuse a sensitive resolved target, tolerate
    ``ValueError``/``OSError``, and match on the declared ``name`` (or the filename
    stem). Returns ``True`` unless the matched spec carries an explicit boolean
    ``false`` — an absent flag, a non-boolean value, a missing/unreadable spec, or a
    directory error all default to injecting, reproducing the pre-opt-out behavior.
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner

    try:
        candidates = ctx.iter_agent_spec_files(ctx.kiro_agents_dir(), ordered=False)
    except OSError:
        return True
    for f in candidates:
        if f.name.startswith("._"):
            continue
        try:
            resolved = f.resolve(strict=True)
        except OSError:
            continue
        if ctx.is_sensitive_path(str(resolved)):
            continue
        try:
            # Read through the guarded reader (not resolved.read_text): it
            # re-resolves, refuses a sensitive target, and opens O_NOFOLLOW —
            # closing the TOCTOU where the final path component is swapped to a
            # symlink into ~/.aws etc. AFTER the is_sensitive_path check above.
            data = ctx.parse_agent_spec_text(ctx.safe_read_file(str(f)), f)
            if not isinstance(data, dict):
                continue
            if data.get("name") == agent or f.stem == agent:
                val = data.get("includeCrewContext", True)
                # Honor only an explicit boolean; anything else defaults to inject.
                return val if isinstance(val, bool) else True
        except (OSError, ValueError):
            continue
    return True


def _read_prompt_file(pp: Path) -> str:
    """Read the resolved agent prompt; a bad user override degrades to the shipped one.

    ``_prompt_path`` returns the user's ``~/.kiro/crew/prompt.md`` ahead of the
    shipped prompt whenever it exists, and that file is hand-authored: Windows
    PowerShell 5.1 redirection writes it as UTF-16 with a BOM, so a strict UTF-8
    read raises ``UnicodeDecodeError`` -- a ``ValueError``, not an ``OSError``.
    Both provider branches of ``_resolve_agent_prompt`` read through here, and
    so does ``_load_agent_prompt`` for a spec carrying the managed contract (the
    same file), so one guard covers both failure families (``OSError``:
    missing/unreadable; ``UnicodeDecodeError``: misencoded) and both degrade the
    same way: one WARNING naming the file and the cause, then the shipped prompt,
    so the turn still answers. When the file that failed IS the shipped prompt
    there is nothing left to fall to, and "" is the "no contract" answer. A
    custom agent's OWN ``file://`` prompt keeps that loader's bounded reader.

    The read goes through ``safe_read_file``: the override lives at a path an
    in-sandbox agent may write, so a symlink planted there pointing at a
    credential file is refused (``PermissionError``, an ``OSError``) and degrades
    like any other unreadable override instead of entering the model context.
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner

    try:
        return ctx.safe_read_file(str(pp))
    except (OSError, UnicodeDecodeError) as exc:
        fallback = ctx._shipped_prompt()
        if fallback == pp:
            logger.warning("Shipped prompt %s could not be read (%s); no agent prompt", pp, exc)
            return ""
        logger.warning(
            "Ignoring prompt override %s (%s); using the shipped prompt %s", pp, exc, fallback
        )
        try:
            return ctx.safe_read_file(str(fallback))
        except (OSError, UnicodeDecodeError) as exc2:
            logger.warning(
                "Shipped prompt %s could not be read (%s); no agent prompt", fallback, exc2
            )
            return ""


def _native_prompt_snapshot(context_provider: Any, agent: str | None) -> str | None:
    """The persona the live harness loaded for *agent* at activation, or ``None``.

    Read off the provider's ``native_context_documents`` under
    ``template://<agent>#prompt``, which the runtime records when the session is
    activated (from the KAS wire definition, or on kiro-cli from the spec it
    loads at spawn and ``session/set_mode``). The runtime writes that key only
    on a harness that loads a spec itself, so a present value is the whole
    report that the harness delivers the prompt; no separate provider flag is
    asked. Both the session-start build and the post-compaction reinjection
    read it live, so a compaction later in the session still compares the block
    against what was loaded, not against the file as it reads now
    (kirodotdev/KiroCrew#13305).
    """
    if context_provider is None or not agent:
        return None
    from kiro_crew.member_essential_context import native_prompt_document_key

    value = context_provider.native_context_documents.get(native_prompt_document_key(agent))
    return value if isinstance(value, str) else None


def _substitute_bot_name(self, prompt: str) -> str:
    """Replace {bot_name} placeholder in prompt text.

    The name is read from the config watcher's snapshot at every
    substitution -- a plain attribute read, safe on the worker threads
    ``build_message`` runs on -- so an ``agent.bot_name`` write from any
    writer names the bot on the next turn. The loader has already
    sanitized the snapshot's value. The constructor's name is the fallback:
    it is the operator's boot-time value or the provider default, and is
    what an embedder with no watcher (the CLI, tests) gets.
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner

    snap = ctx.live.snapshot()
    live_name = getattr(getattr(snap, "agent", None), "bot_name", "") if snap is not None else ""
    return prompt.replace("{bot_name}", live_name or self._bot_name)


def _live_cap_figure() -> str:
    """The concurrent sub-agent cap in force, as a prompt spells it.

    ``agent.max_subagents`` (or ``agent.subagent_auto_max`` when it is 0) is
    a ceiling the adaptive controller may have cut after admitted work kept
    failing, so the figure is the cap IN FORCE -- a registry read
    (``resource_status.adaptive_exec_cap``) this gateway-process path can
    afford. When no controller runs here (the CLI, tests) the configured
    ceiling is used and labelled as one. ``resolve_max_subagents`` never
    answers 0, so a figure is always defined; "several" is left only for a
    config that cannot be read. The live reading moves with the
    controller, which is why a session holds one: see
    :meth:`_session_cap_figure`.
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner

    cap = ctx.resource_status.adaptive_exec_cap()
    if cap > 0:
        return str(cap)
    # Lazy import: kiro_crew.subagent imports this module, so a
    # top-level import would cycle.
    try:
        from kiro_crew.subagent import (  # circular import: subagent -> context
            resolve_max_subagents,
        )

        ceiling = resolve_max_subagents(ctx.KiroCrewConfig.load())
    except Exception:
        ceiling = 0
    return f"{ceiling} (configured ceiling)" if ceiling > 0 else "several"


def _cap_memo_key(session_key: str) -> str:
    """The memo's key for a session: a fixed-size digest of its key.

    ``_CAP_FIGURE_SESSIONS`` caps how MANY readings are held, and a count
    caps memory only when each thing held is itself bounded. A session key
    reaches ``build_message`` from the caller at whatever length it likes, and
    an entry leaves the memo only by eviction, never when its session closes,
    so an oversized key would stay retained. Digesting it makes every
    retained key the same size and the cap govern bytes as well as entries.

    ``surrogatepass`` on the encode so a key carrying a surrogate — a skill
    directory name the filesystem layer round-tripped through ``os.fsdecode``
    because its bytes were not valid UTF-8 — hashes to a stable digest
    instead of raising ``UnicodeEncodeError`` and aborting the turn. The
    digest only has to be stable per input, not reversible, so passing the
    surrogate bytes straight through is correct.
    """
    return hashlib.sha256(session_key.encode("utf-8", "surrogatepass")).hexdigest()


def _session_cap_figure(self, session_key: str, *, refresh: bool) -> str:
    """One session's reading of the delegation cap, taken once.

    The contract block is rendered twice for a session -- at session start,
    and again when compaction restores it -- while the reading underneath
    the figure moves with host load. Reading it per assembly therefore hands
    a compacted session a contract that differs from the one it was given,
    in a number it never chose. A session start takes the reading and every
    later rendering for that session reuses it, so the figure still tracks
    the host from session to session while one session's contract holds
    still. A session whose start this process did not serve has no reading
    and takes a live one.

    The memo holds ``_CAP_FIGURE_SESSIONS`` readings, so on a gateway that
    starts more sessions than that the reuse is not unconditional: an evicted
    session's restoring render finds no reading and takes a live one, which
    is the drift this method otherwise removes. Eviction therefore takes the
    least recently USED entry rather than the oldest one -- a hit moves its
    key to the end, so a session that keeps rendering its contract stops
    being the next one dropped. That orders the victims sensibly; it does not
    make the reuse a guarantee, and nothing distinguishes an evicted session
    from one this process never started.

    One builder serves every session in the gateway and is reached from a
    thread executor, so a sibling thread can evict this key between a
    lookup and a second read of it -- eviction takes the oldest entry, and
    the restoring render of the oldest session is exactly the caller that
    would read it back. The figure is therefore returned as a local, and the
    lookup, eviction and insertion are held under one lock.
    """
    return self._session_reading(
        self._cap_figures,
        self._cap_figures_lock,
        session_key,
        self._live_cap_figure,
        refresh=refresh,
    )


def _session_reading(
    self,
    memo: dict[str, _ReadingT],
    lock: threading.Lock,
    session_key: str,
    live: Callable[[], _ReadingT],
    *,
    refresh: bool,
) -> _ReadingT:
    """One session's reading of a host-derived prompt token, from *memo*.

    The transaction :meth:`_session_cap_figure` describes, for any token whose
    value a session must hold still: a session start (``refresh``) takes a
    *live* reading, every later render reuses it, and the lookup, eviction and
    insertion run under *lock* with the reading handed back as a local.
    """
    key = self._cap_memo_key(session_key)
    with lock:
        if not refresh:
            cached = memo.get(key)
            if cached is not None:
                memo[key] = memo.pop(key)
                return cached
        reading = live()
        if key not in memo and len(memo) >= self._CAP_FIGURE_SESSIONS:
            memo.pop(next(iter(memo)), None)
        memo[key] = reading
        return reading


def _live_computer_use_gate() -> bool:
    """Whether the agent spec mounts ``kirocrew-computer`` on this host now.

    The spec gate itself (``agent._computer_use_spec_gate``), not a second
    reading of the keystone, so the section a prompt carries and the server the
    spec emits answer from one predicate. It fails closed, and so does this: a
    failure to reach it yields the short pointer, never a section describing
    tools the session may not have.
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner

    try:
        # Through the module object, so a substituted gate is the one called.
        return bool(ctx._agent_module._computer_use_spec_gate())
    except Exception:
        logger.debug("computer-use spec gate unreadable; prompt gets the pointer", exc_info=True)
        return False


def _session_computer_use_gate(self, session_key: str, *, refresh: bool) -> bool:
    """One session's reading of the Computer Use spec gate, per backend.

    The block has to match the tools the session's CURRENT backend mounted,
    and a backend loads its MCP servers from the spec when it spawns. So the
    reading is taken when one spawns -- at session start, and again when a
    resume (``session/load``) starts a new backend for the same session -- and
    a compaction's re-render reuses it. Between those points the keystone can
    still move: the dashboard's switch rebuilds the spec and resets every
    session, which ends in a resume, while any other writer leaves the running
    backend as it was. A live reading at compaction would follow the second
    kind of move to tools the session does not have.
    """
    return self._session_reading(
        self._computer_use_gates,
        self._computer_use_gates_lock,
        session_key,
        self._live_computer_use_gate,
        refresh=refresh,
    )


def _live_browser_gate() -> bool:
    """Whether browsing is set up on this host now, as the ``browser`` tool reads it.

    ``browser_cli.install.available`` is the predicate the tool's own call-time
    check wraps: while it is False the tool refuses every op. Unlike that check
    this fails OPEN. An unreadable state keeps the whole section, because a
    section kept by mistake costs tokens while one dropped by mistake leaves a
    working browser without its approval and loopback rules.
    """
    try:
        # Function-local: an owner takes no Kiro Crew import at module scope, so
        # loading the context does not load the browser install plane.
        from kiro_crew.browser_cli import install

        return bool(install.available())
    except Exception:
        logger.debug("browse gate unreadable; prompt keeps the Browser section", exc_info=True)
        return True


def _resolve_prompt_templates(
    prompt: str,
    session_key: str,
    cap_figure: str = "",
    *,
    computer_use: bool | None = None,
    browser: bool | None = None,
) -> str:
    """Resolve conditional template blocks in prompt text.

    Dashboard sessions get a short widget pointer; Slack/CLI get it stripped.
    The ``{{MAX_SUBAGENTS}}`` token is replaced with the delegation capacity
    the model can actually fan out to: ``cap_figure`` when the caller holds
    that session's reading, otherwise a live one. Resolved for every
    transport (not just dashboard), before the widget-block branch.
    ``{{COMPUTER_USE_BLOCK}}`` is the full Computer Use section only when the
    agent spec mounts ``kirocrew-computer`` (``computer_use`` when the caller
    holds that session's reading, otherwise a live one), and a short pointer
    to the setting when it does not. The section marked by ``{{BROWSER_BEGIN}}``
    and ``{{BROWSER_END}}`` stays while browsing is set up on this host
    (``browser``, or a live reading), and becomes a short pointer while it is not.
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner

    if ctx.ContextBuilder._MAX_SUBAGENTS_TOKEN in prompt:
        prompt = prompt.replace(
            ctx.ContextBuilder._MAX_SUBAGENTS_TOKEN,
            cap_figure or ctx.ContextBuilder._live_cap_figure(),
        )

    if ctx.ContextBuilder._COMPUTER_USE_TOKEN in prompt:
        mounted = (
            ctx.ContextBuilder._live_computer_use_gate() if computer_use is None else computer_use
        )
        prompt = prompt.replace(
            ctx.ContextBuilder._COMPUTER_USE_TOKEN, _sections.computer_use_block(mounted)
        )

    if _sections.BROWSER_BEGIN in prompt or _sections.BROWSER_END in prompt:
        available = _live_browser_gate() if browser is None else browser
        prompt = _sections.browser_section(prompt, available)

    cfg = ctx.KiroCrewConfig.load()

    # A copied agent spec may still carry the retired
    # ``{{VERBOSITY_BLOCK}}`` token (it lived in every shipped prompt until
    # the block moved into session context). Strip it so the literal
    # never reaches the model; the preferences themselves arrive through
    # ``_build_response_preferences_section``.
    prompt = prompt.replace("{{VERBOSITY_BLOCK}}", "")

    # Widgets and artifacts need a chat window to render in, which is a
    # property of where the session is DISPLAYED, not where it started: a
    # Slack-born conversation with its dashboard tab open can render both.
    if not ctx.has_dashboard_surface(session_key or ""):
        return prompt.replace("{{WIDGET_BLOCK}}", "")

    density = getattr(cfg.dashboard, "widget_density", "more")
    return prompt.replace("{{WIDGET_BLOCK}}", _sections.widget_block(density))


def _load_agent_prompt(
    agent: str,
    project: str | None = None,
    *,
    owner_template: str = "",
    spec_path: Path | None = None,
    native_copy_out: list[bool] | None = None,
) -> str:
    """Read the resolved execution prompt, excluding an owner source in essentials.

    *spec_path* is the file a pinned session runs from (``skill_scope_pin``), read
    by its path instead of resolving *agent* by name project-first.

    *native_copy_out*, when given, receives one bool: whether the returned text
    is exactly the copy a harness that loads the agent's spec delivers itself.
    The shape that qualifies is ``native_spec_prompt_shape``'s (a user-level
    spec's inline ``prompt`` or absolute, non-``~`` ``file://`` that no sandbox
    mask may cover), the same rule
    the activation snapshot in ``acp/runtime_mode_activation.py`` reads by, so the two cannot
    drift apart. The managed contract replaces the stub or pointer the harness
    reads; a relative ``file://`` resolves against the spec's own directory,
    while the activation snapshot reads a path only as the spec spells it; and a project-level
    spec is not the one a harness reading only the user level runs. So none
    of those can be shown identical (kirodotdev/KiroCrew#13305).
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner
    from kiro_crew.agent_discovery import _read_agent_spec
    from kiro_crew.member_essential_context import (
        native_spec_prompt_shape,
        resolve_relative_prompt_path,
        resolve_template_path,
    )

    native_copy = False
    try:
        path = spec_path or resolve_template_path(agent, project)
        if path is None:
            return ""
        data = _read_agent_spec(path, operation="agent_prompt", source="context")
        if data is None:
            return ""
        prompt = data.get("prompt") or ""
        if not isinstance(prompt, str):
            return ""
        # The managed contract resolves to the contract file for EVERY spec
        # carrying it, owner template or not: a fork or template copy
        # inherits _NATIVE_PROMPT_STUB verbatim, and returned literally the
        # stub text would be that agent's whole persona. An owner template's
        # own prompt is delivered via essentials, so it is omitted here.
        # The contract file is the same one the default branch reads, so it
        # takes the same reader: a bad user override degrades to the shipped
        # prompt with a WARNING instead of silently erasing the contract.
        if ctx.is_managed_prompt(prompt):
            return _read_prompt_file(ctx._prompt_path())
        if agent == owner_template:
            return ""
        # The one layer every harness that loads specs natively reads, judged
        # by the shared shape rule. Guarded on its own: a failure here may
        # only cost the dedup, never the prompt.
        native_copy = native_spec_prompt_shape(prompt, path)
        if prompt.startswith("file://"):
            source = Path(prompt[7:]).expanduser()
            if not source.is_absolute():
                resolved_source = resolve_relative_prompt_path(source, path, project)
                if resolved_source is None:
                    return ""
                source, root = resolved_source
                prompt_bytes = ctx.safe_read_file_bytes_nolink(str(source), within_root=str(root))
                if prompt_bytes is None:
                    logger.debug("Skipping relative agent prompt rejected at read time")
                    return ""
                return prompt_bytes.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
            return ctx.safe_read_file(str(source))
        return prompt
    except (OSError, ValueError, ctx.FileTooLargeError):
        native_copy = False
        return ""
    finally:
        if native_copy_out is not None:
            native_copy_out.append(native_copy)


def _resolve_agent_prompt(
    self,
    agent: str | None,
    *,
    project: str | None,
    mode: str,
    session_key: str | None,
    is_cc: bool,
    private_owner: bool,
    session_start: bool,
    native_prompt_snapshot: str | None = None,
) -> str:
    """Return the agent contract for the ``[AGENT SYSTEM PROMPT]`` block, or "".

    Session start and post-compaction reinjection both call this, so the
    contract a compacted session gets back is the one it started with. Only
    a session start takes a fresh reading of the delegation cap; the call
    that restores the block reuses the session's own.

    *native_prompt_snapshot* is the copy of a custom spec's own ``prompt``
    the live harness actually loaded when the agent was activated
    (``native_context_documents``, see ``_native_prompt_snapshot``). The
    runtime records it only on a harness that loads a spec itself (kiro-cli,
    KAS), so its presence is the whole report that the harness delivers the
    prompt. The block is then withheld only when it would
    repeat that copy word for word: the loaded text has the harness's own shape
    (``_load_agent_prompt``), equals the snapshot, and resolving Crew's
    placeholders leaves it unchanged. A spec edited after activation differs
    from the snapshot and keeps the block with the current text; no snapshot
    keeps it too. Anything not shown identical keeps the block, since a wrong
    guess drops the persona outright (kirodotdev/KiroCrew#13305).

    A ``WIDGET_BLOCK_AGENTS`` prompt also carries the widget pointer, which the
    harness does not hold: when the rest would repeat the snapshot, the block
    keeps that pointer alone, as a crewmate's does.
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner
    from kiro_crew.agent_files import is_kirocrew_default_agent

    is_custom = bool(agent) and not is_kirocrew_default_agent(agent)
    native_copy: list[bool] = []
    agent_prompt: str
    if is_cc and not is_custom:
        # CC gets the same Kiro Crew persona prompt as kiro — including
        # the Output Format rules (diff blocks, image embeds, OPTIONS)
        # which are dashboard UI contracts, not kiro-specific. Only the
        # kiro-cli *branding* references are rewritten to claude code.
        # A custom agent keeps its own prompt on every provider.
        try:
            pp = ctx._prompt_path()
            agent_prompt = _read_prompt_file(pp)
            agent_prompt = agent_prompt.replace("kiro-cli", "claude code")
            agent_prompt = re.sub(r"\bKiro\b", "Claude", agent_prompt)
            agent_prompt = re.sub(r"\bkiro\b", "claude", agent_prompt)
            agent_prompt = agent_prompt.strip()
        except Exception:
            agent_prompt = ""
    elif is_custom:
        agent_prompt = self._load_agent_prompt(
            agent or "",
            project,
            owner_template=(agent or "") if private_owner else "",
            spec_path=ctx.pinned_session_skill_spec(session_key),
            native_copy_out=native_copy,
        )
    else:
        pp = ctx._prompt_path()
        logger.debug("Prompt selection: mode=%r → %s", mode, pp)
        agent_prompt = _read_prompt_file(pp)
    # The spec's own text, which a native harness may already hold; the widget
    # pointer below is Crew's alone.
    spec_prompt = agent_prompt
    # A built-in spec whose own prompt asks for widgets gets the widget pointer
    # here rather than as a token in its spec: kiro-cli loads that spec prompt
    # natively and a crewmate's essentials deliver it verbatim, and neither path
    # resolves a token. This block is the one copy that is resolved per surface,
    # and the only one a private crewmate gets, so it may hold just the pointer.
    if agent in WIDGET_BLOCK_AGENTS and _WIDGET_TOKEN not in agent_prompt:
        agent_prompt = f"{agent_prompt}\n\n{_WIDGET_TOKEN}" if agent_prompt else _WIDGET_TOKEN
    if not agent_prompt:
        return ""
    # Any host-derived token in this prompt must be snapshotted per session:
    # the contract block is asserted byte-identical across a session's
    # renders, so a token resolved live on each render cannot hold it.
    cap_figure = (
        self._session_cap_figure(session_key or "", refresh=session_start)
        if self._MAX_SUBAGENTS_TOKEN in agent_prompt
        else ""
    )
    computer_use = (
        self._session_computer_use_gate(session_key or "", refresh=session_start)
        if self._COMPUTER_USE_TOKEN in agent_prompt
        else None
    )
    # The ``browser`` tool is mounted whatever this reads and re-checks at each
    # call, so a session start is the only reading the section needs.
    browser = (
        self._session_reading(
            self._browser_gates,
            self._browser_gates_lock,
            session_key or "",
            _live_browser_gate,
            refresh=session_start,
        )
        if _sections.BROWSER_BEGIN in agent_prompt
        else None
    )
    loaded = agent_prompt
    agent_prompt = self._resolve_prompt_templates(
        agent_prompt, session_key or "", cap_figure, computer_use=computer_use, browser=browser
    )
    # Off the dashboard the pointer resolves to nothing, which leaves a prompt
    # that held only the pointer empty: no block rather than a blank one.
    if not agent_prompt.strip():
        return ""
    agent_prompt = self._substitute_bot_name(agent_prompt)
    if (
        native_copy == [True]
        and native_prompt_snapshot is not None
        and spec_prompt == native_prompt_snapshot
    ):
        if agent_prompt == loaded:
            return ""
        # The widget pointer appended above is the one part the harness lacks.
        # When resolving left the spec's own text unchanged, the block keeps
        # just the pointer, as a crewmate's does.
        if loaded == f"{spec_prompt}\n\n{_WIDGET_TOKEN}" and agent_prompt.startswith(spec_prompt):
            pointer = agent_prompt[len(spec_prompt) :]
            return pointer.lstrip("\n") if pointer.strip() else ""
    return agent_prompt
