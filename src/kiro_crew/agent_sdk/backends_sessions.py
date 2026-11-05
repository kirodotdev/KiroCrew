"""Which process serves a harness's sessions, and whether a session is shared, evicted,
restored or forked.

The shared runtime and the direct client path, subagent session sharing, teardown
that evicts, starts answered one at a time, and the restore verbs: harness-owned
records, a load result without modes, ``session/resume``, and a fork point.

One of the owners of the harness vocabulary in :mod:`kiro_crew.agent_sdk.backends`,
which re-exports every name defined here and is the import path callers use. Like that
module it is a leaf: nothing here imports ``kiro_crew.acp``, ``kiro_crew.config`` or
``kiro_crew.platform`` at module scope. ``scripts/check_harness_parity.py`` lists this file
in ``VOCABULARY_PATHS``, which is what lets its ``ACP_BACKEND*`` names be defined here.
"""

from __future__ import annotations

from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_COPILOT,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKEND_STRANDS,
)

# Backends whose single process can host N concurrent ACP sessions (AcpRuntime
# demux) AND whose shared subagent session can be CONTINUED after the conversation
# that spawned it ends. claude-agent-acp runs through AcpClient (one process per
# session) and is not a member.
#
# What survives is the PERSISTED THREAD, never a resident session. Teardown closes:
# a subagent session left alive on the shared process after its parent ends is a
# memory leak, and on codex an expensive one -- each resident session carries the MCP
# fleet ``codex`` starts from its own config, measured at roughly 44 processes and
# 2751 MB for one session. So a member's teardown disposes the in-memory session, and
# ``spawn_continue`` re-reaches the conversation by loading the record the host kept.
# Membership therefore asks one question: after this backend's teardown verb, can a
# ``session/load`` still restore the thread?
#
# kiro-cli answers yes with a transcript Crew holds under ``<kiro home>/sessions/cli``
# that ``_kiro.dev/session/terminate`` leaves on disk.
#
# codex-acp answers yes with a thread ``codex`` persists under ``CODEX_HOME``, and
# the answer is MEASURED rather than argued -- measured on codex-acp 1.11.0 against codex
# 0.154.0, one real adapter (the load half re-measured on 2.2.2, below):
#
#   * ``session/close`` (a request) evicts: the sessionId stops answering. That is
#     the same fact ``ACP_BACKENDS_SESSION_EVICTION`` records, and it is unchanged.
#   * ``session/load`` on that closed id SUCCEEDS, replays the conversation, and the
#     session then answers a question about the first turn.
#   * the same load succeeds from a RESTARTED adapter process over the same
#     ``CODEX_HOME`` -- the shape a continuation actually takes, since the runtime
#     that served the subagent is usually gone by then. Token accounting confirms
#     the context came from the thread rather than the prompt: the recall turn spent
#     330 input tokens against 9786 cached-read.
#   * ``session/delete`` archives the thread and a load then refuses, so release has
#     a verb that genuinely disposes.
#
# ``ACP_BACKENDS_HARNESS_OWNED_SESSIONS`` is what carries that restore: codex
# resolves a load from the sessionId alone, with no Crew-side transcript to check.
# What is measured where, because the halves have different reach.
# ``test_real_codex_acp_session_close_evicts`` carries the ``real_adapter`` marker, so
# the contract lane runs it on a pinned adapter and an adapter bump that stops
# ``session/close`` evicting goes red there. The RESTORE half is in the lane too, on
# 2.2.2: ``test_real_codex_acp_closed_session_loads_again`` runs real turns against a
# loopback model, closes, and loads in the same and in a restarted adapter, and the
# next turn's model request carries the first turn. ``session/delete`` and the recall
# ANSWER stay with the opt-in ``test_real_codex_acp_load_after_close_restores``.
#
# KAS answers NO, and that is the whole of its exclusion: its teardown maps to
# ``_kiro/session/delete``, which REMOVES the persisted record, so there is nothing
# for a load to restore and a shared subagent would strand ``spawn_continue`` on
# ``conversation_gone``. A different gap from anything codex had, owned by whoever
# gives KAS a non-destroying teardown; until then its subagents get dedicated
# sessions, which is working behaviour rather than a degraded one.
#
# opencode is not a member: one binary serves one session over its own stdio pipe,
# so there is no second session to share.
# pi is not a member: Crew spawns one ``pi-acp`` process per session over its own
# stdio pipe, so there is no second session to share.
# goose is not a member: it is not served by ``AcpRuntime`` (see
# ``ACP_BACKENDS_ACP_RUNTIME``), so Crew opens one goose process per session and
# there is no second session to share.
#
# deepseek is not a member, and here the limit is CREW's driver rather than the
# harness. The harness multiplexes: one connection carries several independent
# sessions, ``session/close`` disposes only the addressed one, and a closed session
# stays listable and resumable. What it is not served by is ``AcpRuntime``, the only
# demux Crew has -- see ``ACP_BACKENDS_ACP_RUNTIME`` -- so Crew opens one process per
# session and there is no shared session to persist. A harness capability Crew cannot
# reach is recorded here rather than claimed.
ACP_BACKENDS_SESSION_SHARING = frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_CODEX})

# Backends served by AcpRuntime + AcpSessionHandle — the kiro-agent family
# (kiro-cli and KAS) whose single process hosts N sessions via demux.
# claude-agent-acp runs one AcpClient per session and is NOT a
# member. Membership drives the shared runtime start path and the kiro-family
# spawn conventions: members read the cli.json effort/tool-search overlay and
# receive effort at spawn, whereas claude applies it via a live push after the
# session is ready. Stated as opt-in membership (harness-parity H5/H6) so the
# four sites that mean "kiro or kas" say so positively rather than as
# ``not is_claude_backend`` — an inference that silently captures every harness
# added later. This is a SUPERSET of ACP_BACKENDS_SESSION_SHARING: running on
# AcpRuntime is necessary for session sharing but not sufficient (KAS runs here
# yet is excluded from sharing until keep-aware teardown lands).
# opencode is not a member: it is spawned per session and reads none of the
# kiro-family cli.json overlay, so it takes the AcpClient path.
# pi is not a member for the same reason: one ``pi-acp`` process per session, no
# cli.json overlay, the AcpClient path.
#
# deepseek is not a member, and it is the first harness excluded here whose own
# protocol would support membership: one of its connections carries several
# independent sessions. ``AcpRuntime`` is not a general multiplexer, though -- it
# carries the kiro-family spawn argv, the cli.json effort and Tool Search overlay,
# and the ``_kiro/*`` auth and delete verbs -- so serving this harness from it means
# a demux that is not kiro-shaped, which is its own work. It takes the AcpClient
# path, one process per session, until that exists.
#
# ``ACP_BACKEND_CODEX`` IS a member, and it is the first one that is not
# kiro-shaped. It earns membership on the two facts a shared process needs, both
# captured off a live ``codex-acp`` 1.11.0 against a real ``codex`` build:
#
# * one adapter process serves N sessions and keeps them apart. Two
#   ``session/new`` calls on one connection return distinct ids, and across a
#   prompt on one of them every ``session/update`` notification carries that
#   session's id -- none leaks to the other.
# * the adapter itself does not grow with sessions: 103 MB at zero sessions and
#   85 MB at eight. The growth is in ``codex app-server``, and it is gentle
#   (+14 MB per session on a minimal config, +37 MB with a host MCP registry).
#
# These two facts are not re-measured on the Real Adapter Contract lane's pin
# (``test/real_adapters/package.json``).
#
# What it does NOT bring is the kiro-family spawn convention: it reads no
# cli.json overlay and takes effort through ``session/set_config_option``. That
# is why membership here is a statement about the TRANSPORT and nothing else --
# every kiro-family convention is its own set, and codex is absent from each.
ACP_BACKENDS_ACP_RUNTIME = frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS, ACP_BACKEND_CODEX})

# Backends ``AcpClient._spawn`` can construct a session for directly: kiro (the
# resolve-and-exec branch a backend with no process adapter falls through to)
# plus the five with an entry in ``kiro_crew.acp.harness._PROCESS_ADAPTERS``.
# A POSITIVE allowlist, not "every backend except kas/codex": a future backend
# added to ``BASELINE_SELECTABLE_BACKENDS`` with no arm here must fail closed
# (report not-spawnable) rather than silently fall to the kiro-cli branch under
# its own identity the way kas and codex do today -- the exact bug this fact
# exists to let a caller avoid.
ACP_BACKENDS_ACP_CLIENT_SPAWNABLE = frozenset(
    {
        ACP_BACKEND_KIRO,
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_OPENCODE,
        ACP_BACKEND_GOOSE,
        ACP_BACKEND_PI,
        ACP_BACKEND_DEEPSEEK,
        ACP_BACKEND_COPILOT,
        ACP_BACKEND_STRANDS,
    }
)

# Backends whose teardown verb actually EVICTS the session from the adapter's own
# session map, freeing what it held.
#
# Running on a shared process (``ACP_BACKENDS_ACP_RUNTIME``) is not that claim. A harness can
# multiplex perfectly and still have no verb that disposes one session, and the
# difference only shows on a process that outlives many sessions: every
# non-evicting teardown leaves its session addressable and its context resident,
# so the adapter grows without bound at whatever rate sessions are created.
#
# kiro-cli and KAS are members: their teardown verbs (``_kiro.dev/session/terminate``
# and ``_kiro/session/delete``) remove the session from the process.
#
# codex is a member on captured evidence. Crew's teardown verb for it is the
# standard ``session/close``, sent as a request; measured live against codex-acp
# 1.11.0, the adapter answers it with ``{}`` and afterwards the same sessionId
# stops answering ``session/set_config_option`` -- the session is gone from the
# process, while the Codex thread's own record survives (evict, not delete). The
# contrast on the same wire is ``session/cancel``: after it the same oracle keeps
# answering and a further ``session/prompt``'s ``cachedReadTokens`` shows the
# context resident, so ``cancel`` interrupts a turn and is not a teardown, and a
# harness sending it as one is correctly excluded here. The harness docstring on
# ``CodexHarness.teardown`` carries the full measurement, and a gated live test
# repeats it on every install that has the adapter.
#
# Read by every path that creates and destroys sessions on a shared process, and
# that is why membership is one fact rather than one gate per caller:
# :func:`kiro_crew.session._bg_runtime_backends` (background handles -- title
# generation, suggestions, folders, nav -- each taking an ephemeral sessionId many
# times per conversation), ``AcpSessionProvider.new_conversation`` (warm pooled
# reuse, resetting at the rate a workflow's steps run), and the entitlement probe
# in ``AcpRuntime`` (a throwaway session per unavailable-model pick). A harness
# whose teardown does not evict leaks one session on every one of those paths, at
# rates the operator never controls; a harness in this set frees it on each.
#
# An opt-in set rather than a subtraction at any call site, for the reason every
# capability set is opt-in (harness-parity H6/H7): a harness added later and
# spelled as "not <some host>" would inherit an eviction guarantee it has never
# demonstrated, and the operator who never opted into it is the one who finds the
# adapter growing. Membership is earned by a measured teardown, not by default.
#
# Each member cites a live capture whose replay snapshot shows the same id live
# before the teardown and 'not found' after it, at the releases kiro-cli 2.35.1
# installs: kiro-cli 2.35.1 ``test/fixtures/acp_frames/kiro/session-evict-live.jsonl``
# (set_mode, then ``_kiro.dev/session/terminate``, then set_mode -> -32603 Session
# not found); KAS 0.66.70 ``test/fixtures/acp_frames/kas/session-evict-live.jsonl``
# (set_mode, then ``_kiro/session/delete`` -> success, then set_mode -> -32000 not
# found); codex-acp 2.3.0 ``test/fixtures/acp_frames/codex/session-evict-live.jsonl``
# (set_config_option, then ``session/close`` -> {}, then set_config_option -> Session
# not found). The real-adapter lane's live test is the anti-drift guard; these
# captures are the replayed record.
ACP_BACKENDS_SESSION_EVICTION = frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS, ACP_BACKEND_CODEX})

# Backends whose process answers ``session/new``, ``session/load`` and
# ``session/set_mode`` one at a time, so a session start sent while one of those is
# unanswered spends its budget waiting for it. Membership decides only whether a
# timed-out start's error names the requests it was sent behind
# (``AcpRuntime._one_at_a_time_ahead``); nothing is sent differently.
#
# kiro-cli is a member on measurement: on 2.26.1, four ``session/new`` sent at once
# on one process, each injecting a server that never answers ``initialize``,
# returned at 31 / 61 / 91 / 121 s, and a ``session/new`` sent during a slow
# ``set_mode`` returned only when the ``set_mode`` did. Its ACP handlers await those
# requests inside the connection's dispatch loop.
#
# KAS and codex are NOT members: neither has been measured, so a start that
# overlaps another on them is not claimed to have waited for it.
ACP_BACKENDS_SERIAL_SESSION_STARTS = frozenset({ACP_BACKEND_KIRO})

# Backends that keep their OWN session records and resolve a resume from the
# ``sessionId`` alone. For a member there is no Crew-side transcript to check
# before ``session/load`` and no ``_kiro.dev/session_file`` to send with it; a
# non-member is the kiro family, whose transcript Crew holds under
# ``<kiro home>/sessions/cli`` and pre-checks so a missing file falls back to a
# fresh session rather than a failed load. A SET rather than a chain of identity
# tests on the resume path: that path is shared with kiro-cli, and harness-parity
# H13 keeps it free of conditionals added in service of an adapter -- a harness
# added later is one member here, not one more ``elif`` there.
#
# codex's restore is asserted in the Real Adapter Contract lane; see
# ``ACP_BACKENDS_SESSION_SHARING``.
#
# pi is a member: pi-acp advertises ``loadSession`` and resolves a ``session/load``
# from the id alone through its own session map, replaying the conversation as
# ``user_message_chunk`` / ``agent_message_chunk`` updates before answering
# (``test/fixtures/acp_frames/pi/session-load-live.jsonl``).
#
# copilot is a member: Copilot CLI 1.0.100 advertises ``loadSession`` and, in a
# SECOND process, resolved a ``session/load`` from the id alone, replaying the
# conversation as ``user_message_chunk`` / ``tool_call`` updates before answering
# with ``configOptions`` and ``modes``; the next prompt answered out of the restored
# history (``test/fixtures/acp_frames/copilot/session-load-live.jsonl``). The
# restored session came back with ``allow_all`` as it was LEFT -- ``on`` after a
# ``/allow-all on`` -- which is why the routing write runs after a load too. It is
# NOT in ``ACP_BACKENDS_LOAD_WITHOUT_MODES``: that same load answer carries ``modes``.
ACP_BACKENDS_HARNESS_OWNED_SESSIONS = frozenset(
    {
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_CODEX,
        ACP_BACKEND_OPENCODE,
        ACP_BACKEND_PI,
        ACP_BACKEND_GOOSE,
        ACP_BACKEND_DEEPSEEK,
        ACP_BACKEND_COPILOT,
        # strands persists each session under the directory Crew names (its gate
        # module's ``session`` option) and restores it on ``session/load`` by id:
        # ``test/fixtures/acp_frames/strands/session-load-live.jsonl``.
        ACP_BACKEND_STRANDS,
    }
)

# Backends whose SUCCESSFUL ``session/load`` result carries no ``modes`` block.
# The kiro family and claude return one, and its absence on those harnesses is how
# a load that did not really take is told apart from one that did. OpenCode's
# successful ``session/load`` result carries ``configOptions`` and no ``modes`` --
# OBSERVED, in ``test/fixtures/acp_frames/opencode/session-load-live.jsonl``, where a
# second process loaded a session the first had created and the harness replayed the
# conversation before answering. For a member a response that is not an error IS the
# successful load. Without membership a reopened session would load, fail the
# ``modes`` check, fall through to ``session/new`` and discard the conversation the
# harness had just restored.
# pi is NOT a member: its successful ``session/load`` result carries a ``modes``
# block (pi-acp maps its thinking levels onto ACP modes), observed in the same
# capture, so the ordinary gate applies.
#
# deepseek is a member on its schema rather than on a capture of a populated block:
# the response to the verb it DOES serve declares ``modes`` optional, and the harness
# rejects modes across its whole surface, so it can never return one. Membership is
# what keeps a restored conversation from being discarded by a check for a block this
# harness has nothing to put in.
#
# strands is a member on a capture: its project-mode ``session/load`` answers ``{}``
# after replaying the restored history
# (``test/fixtures/acp_frames/strands/session-load-live.jsonl``).
ACP_BACKENDS_LOAD_WITHOUT_MODES = frozenset(
    {ACP_BACKEND_OPENCODE, ACP_BACKEND_DEEPSEEK, ACP_BACKEND_STRANDS}
)

# Backends that restore a session with ``session/resume`` instead of
# ``session/load``, and advertise it under ``sessionCapabilities.resume`` instead of
# the ``loadSession`` flag.
#
# Both are STANDARD ACP v1. The schema says so of the verb this set names: it
# "resumes an existing session without returning previous messages (unlike
# ``session/load``)", and is "useful for agents that can resume sessions but don't
# implement full session loading". So a member is not a harness with a quirk to
# accommodate -- it is a harness serving the optional method the specification
# provides for exactly this case.
#
# A SET plus a method constant rather than a driver method, because the two calls
# have the same contract: ``ResumeSessionRequest`` carries the same fields as
# ``LoadSessionRequest`` (session id, cwd, MCP servers, additional directories,
# ``_meta``), and ``ResumeSessionResponse`` carries the same fields as
# ``LoadSessionResponse`` (optional modes, optional config options, ``_meta``). Only
# two reads differ -- which capability advertises the verb, and which verb is sent --
# so a per-harness method would be a function whose one difference is a string, and
# harness-parity H13 keeps the shared restore path free of a conditional added in
# service of one adapter.
#
# Membership decides BOTH reads together on purpose. A harness that advertises the
# capability under one key and serves the other verb does not exist, and splitting
# them into two sets would invite an entry in one and not the other -- which reads as
# "cannot restore" and silently starts every reopened session fresh.
#
# The REJECTED alternative, named so it is not proposed as a simplification: sniffing
# the handshake instead of declaring membership -- read ``sessionCapabilities.resume``,
# and if it is there send ``session/resume``. It looks like it removes this set, and it
# removes the DECISION instead. A harness may advertise a capability it serves badly,
# or advertise both and mean one; sniffing hands the choice to whatever the agent said
# on the day, with no record of what Crew verified and no place to write down why. The
# set is the record, and adding a harness to it is a deliberate edit with a capture
# behind it (harness-parity H6, the same reason every capability here is opt-in).
ACP_BACKENDS_RESUME_WITHOUT_LOAD = frozenset({ACP_BACKEND_DEEPSEEK})

# Harnesses whose ``session/fork`` copies a stored session only up to a given
# assistant message, which is what lets a rewind keep the native history it
# retains. Advertising ``sessionCapabilities.fork`` is not enough: a fork that
# ignores the fork point copies the whole session, including the suffix the
# rewind discards. claude-agent-acp reads the point from
# ``_meta.jetbrains.air.fork.messageId`` (from 0.71.0; see
# ``CLAUDE_ACP_FORK_POINT_MIN_VERSION``).
ACP_BACKENDS_FORK_AT_MESSAGE = frozenset({ACP_BACKEND_CLAUDE})

# Harnesses that can dispose ONE session's backend inside a process that stays up,
# after which every ``session/prompt`` on that id answers the same error. The
# process is no use to that session any more, but its saved session is: a fresh
# process ``session/load``s it. For a member, :mod:`kiro_crew.acp.disposed_session`
# names the error text the harness writes when it disposes a backend, and
# ``AcpClient`` answers it: a cancelled turn ends as cancelled, any other turn
# surfaces the error once, and the next ``ensure_ready`` replaces the process and
# loads the same id before the next prompt goes out. A request for approval that
# arrives after Crew sent ``session/cancel`` is answered ``cancelled`` at once,
# because the harness holds its turn open on that answer and the cancel cannot
# finish while it waits.
#
# strands is a member: the Strands CLI kills its Python worker when a cancel does
# not stop it within 3s, then answers that prompt, and every later one on the same
# id, with ``-32603`` and ``data.details`` "The Python agent did not stop after
# cancellation. Reload it to continue." A second process loaded the same id, replayed
# the conversation and answered the next prompt
# (``test/fixtures/acp_frames/strands/cancel-dispose-live.jsonl``, then
# ``test/fixtures/acp_frames/strands/cancel-dispose-reload-live.jsonl``).
ACP_BACKENDS_RELOAD_DISPOSED_SESSION = frozenset({ACP_BACKEND_STRANDS})
