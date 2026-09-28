/**
 * The live chat-slot wire under /api/chat: slot list, detail, create and
 * delete, turn control and the message queue, side conversations,
 * fork/variant/regenerate/rewind, intent summaries, Slack and channel-mirror
 * links, the send wire, and the composer autocomplete read.
 */

import type { ChatSlot } from '../../types'
import type { SessionSummary } from '../../types/sessionSummary'
import { getStoredConsent } from '../../utils/themeConsent'
import { chatSlotDetailPath } from '../chatSlotPaths'
import { resolveDefaultMemoryMode } from '../queryClient'
import { TAB_ID } from '../tabId'
import type { ClientTransport } from './transport'

/**
 * Resolve the theme-consent token to transmit for an installed pack's chat.
 *
 * Two-tier consent, wire side: the client does not compute a trust boolean —
 * it just transmits the RAW stored grant (the sha256 the user granted for the
 * persona content they saw). The backend does the content-binding check: it
 * injects the persona only if this token equals sha256 of the persona.md it
 * reads, so a re-install that swaps persona.md (new sha) does not match the
 * stale grant and the never-consented persona is never injected.
 *
 * Installed/custom packs are keyed `custom-<slug>` in colorTheme (useTheme), so
 * slice(7) drops the `custom-` prefix to recover the slug. Returns null (field
 * omitted from the body) when there's nothing transmittable: no colorTheme, a
 * built-in theme, no stored grant, or a legacy `'1'`/`''` token (which must
 * re-prompt, never activate).
 */
function themeConsentSha(colorTheme?: string): string | null {
  if (!colorTheme || !colorTheme.startsWith('custom-')) return null
  const stored = getStoredConsent(colorTheme.slice('custom-'.length))
  if (stored === null || stored === '' || stored === '1') return null
  return stored
}

export function createChatEndpoints({ post, put, del, patch, j, sessionKeyHeader: _sk, sendResponseAuthRecovery }: ClientTransport) {
  const summaries = {
    /** Intent summary for the chat summary panel.
     *
     *  Read-only: it never triggers generation. Summaries are produced at turn end
     *  by a background pass, so opening the panel cannot spend tokens and repeated
     *  opening cannot become a refresh loop. Returns `enabled: false` (not an
     *  error) when the feature is off, so the panel can explain itself. */
    sessionSummary: (slot: string) =>
      fetch('/api/chat/slots/' + encodeURIComponent(slot) + '/summary').then(j) as Promise<SessionSummary>,
    /** Summarize this session NOW, on the person's explicit request.
     *
     *  Same path as the GET, different verb: reading a summary must stay free of
     *  side effects, so spending tokens is a separate verb rather than a flag on
     *  the read. Rejects with the body's `code` (`summary_in_flight`,
     *  `too_few_turns`, `summary_unavailable`, `summary_disabled`) so the panel can
     *  say which rather than showing one generic failure. */
    generateSessionSummary: (slot: string) =>
      fetch('/api/chat/slots/' + encodeURIComponent(slot) + '/summary', {
        method: 'POST',
      }).then(j) as Promise<SessionSummary>,
  }

  const slotList = {
    chatSlots: () => fetch('/api/chat/slots').then(j),
  }

  const slots = {
    /** Every pull request / issue link a session carries — the unbudgeted read
     *  behind the sidebar's expandable "+N" overflow chip. The slots payload caps
     *  chips per kind, so the links behind that chip are not on the client until
     *  this is called. */
    chatSlotSourceLinks: (slot: string): Promise<{ links: NonNullable<ChatSlot['source_links']>; total: number }> =>
      fetch('/api/chat/slots/' + encodeURIComponent(slot) + '/source-links').then(j),
    chatSlotDetail: (slot: string, limit?: number, before?: number, signal?: AbortSignal) => {
      const p = new URLSearchParams()
      if (limit) p.set('limit', String(limit))
      if (before !== undefined) p.set('before', String(before))
      return fetch(chatSlotDetailPath(slot) + '?' + p, { signal }).then(j)
    },
    /** Create a chat slot. `instance_id` binds the new session to a connected crew
     *  for EXECUTION: it lives in this machine's list and history, and its turns run
     *  over there. The backend opens the peer's slot first, so a peer that is
     *  disconnected or on a different version fails the create rather than yielding
     *  a session that cannot send.
     *
     *  `adopt_remote_slot` switches that same `instance_id` branch from MINT to
     *  ADOPT: instead of the backend minting a fresh peer session to bind, it binds
     *  the EXISTING one named here — the `key` of a row from
     *  `GET /api/instances/{id}/chat-slots`. The new local slot is still fresh, so
     *  the `remote_already_bound` guard does not fire, and the peer's transcript is
     *  backfilled server-side. Requires `instance_id`; without it the backend
     *  answers `400 adopt_needs_instance`. */
    createChatSlot: async (name?: string, agent?: string, model?: string, mode?: string, memory_mode?: string, title?: string, artifact?: string, folder_id?: string, instance_id?: string, adopt_remote_slot?: string, agent_kind?: 'member' | 'template') => {
      // ADOPT deliberately resolves NO default memory mode. The adopted slot carries
      // the PEER session's own `memory_mode` — that mode is the privacy boundary and
      // the session it belongs to already chose it — so sending this machine's
      // default would either be ignored or, worse, silently turn an incognito peer
      // session into a persistent local transcript. An explicit `memory_mode`
      // argument still wins, because a caller that names one means it.
      const resolvedMemoryMode = memory_mode ?? (adopt_remote_slot
        ? undefined
        : await resolveDefaultMemoryMode(
          () => fetch('/api/dashboard/config').then(j),
        ))
      return post('/api/chat/slots', {
        ...(name ? { name } : {}),
        ...(agent ? { agent } : {}),
        // Only beside an agent: a namespace without a name selects nothing.
        ...(agent && agent_kind ? { agent_kind } : {}),
        ...(model ? { model } : {}),
        ...(mode ? { mode } : {}),
        ...(resolvedMemoryMode ? { memory_mode: resolvedMemoryMode } : {}),
        ...(title ? { title } : {}),
        ...(artifact ? { artifact } : {}),
        ...(folder_id ? { folder_id } : {}),
        ...(instance_id ? { instance_id } : {}),
        ...(adopt_remote_slot ? { adopt_remote_slot } : {}),
      }).then(j) as Promise<ChatSlot>
    },
    /** Inject silent background context into a slot — consumed on the next user
     * message. Used by the artifact companion chat to name the bound artifact so
     * the user's first message needs no slug boilerplate. */
    chatSlotContext: (slot: string, content: string, opts?: { source?: string; ephemeral?: boolean; maxAge?: number }) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/context', { content, ...(opts?.source ? { source: opts.source } : {}), ...(opts?.ephemeral !== undefined ? { ephemeral: opts.ephemeral } : {}), ...(opts?.maxAge !== undefined ? { maxAge: opts.maxAge } : {}) }).then(j),
    deleteChatSlot: (slot: string) => del('/api/chat/slots/' + encodeURIComponent(slot)).then(j),
    cleanupSessions: (maxInactiveDays: number, activeSlot?: string, dryRun?: boolean) => post('/api/chat/slots/cleanup', { max_inactive_days: maxInactiveDays, active_slot: activeSlot || '', dry_run: !!dryRun }).then(j) as Promise<{ ok: boolean; archived: number; keys: string[]; failed: string[]; dry_run?: boolean; count?: number; active_is_stale?: boolean }>,
    stopChatSlot: (slot: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/stop').then(j),
    stopChatSlotForce: (slot: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/stop?force=true').then(j),
    cancelQueuedMessage: (slot: string, queueId: string) => del('/api/chat/slots/' + encodeURIComponent(slot) + '/queue/' + encodeURIComponent(queueId)).then(j),
    editQueuedMessage: (slot: string, queueId: string, content: string) => patch('/api/chat/slots/' + encodeURIComponent(slot) + '/queue/' + encodeURIComponent(queueId), { content }).then(j),
    reorderQueuedMessages: (slot: string, order: string[]) => put('/api/chat/slots/' + encodeURIComponent(slot) + '/queue/order', { order }).then(j),
    interruptSlot: (slot: string, queueId?: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/interrupt', queueId ? { queue_id: queueId } : {}).then(j),
    /** Ask the sleeping `wait` tool to return early. Cooperative, not a stop:
     *  the turn continues with a normal tool result. `waitId` must name the sleep
     *  currently in flight — the backend answers 409 for a stale one, which is how
     *  a click on a leftover countdown is rejected rather than ending a later wait. */
    endWait: (slot: string, waitId: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/end-wait', { wait_id: waitId }).then(j),
    approveChatSlot: (slot: string, action: string, extra?: Record<string, string>) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/approve', { action, ...extra }).then(j),
    planAction: (slot: string, action: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/plan-action', { action }).then(j),
    resumeChatSlot: (key: string, title?: string) => post('/api/chat/slots/' + encodeURIComponent(key) + '/resume', { name: key, key, title: title || key }).then(j),
    forkChatSlot: (slot: string, atIndex?: number, prompt?: string, mode?: string, direction?: string, messageId?: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/fork', { ...(atIndex !== undefined ? { at_message_index: atIndex } : {}), ...(messageId ? { at_message_id: messageId } : {}), ...(prompt ? { prompt } : {}), ...(mode ? { mode } : {}), ...(direction ? { direction } : {}) }).then(j),
    sideOpen: (slot: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/side/open', {}).then(j) as Promise<{ ok: boolean; open: boolean; messages: number; last_run_id: string; created_at: string }>,
    sideTurn: (slot: string, question: string, opts?: { steer?: boolean }) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/side/turn', { question, ...(opts?.steer ? { steer: true } : {}) }).then(j) as Promise<{ ok: boolean; run_id?: string; messages?: number; steered?: boolean; pending?: boolean; queued?: boolean; demoted?: boolean; queue_id?: string; still_queued?: boolean; depth?: number; steer_id?: string }>,
    sideQueueCancel: (slot: string, queueId: string) => del('/api/chat/slots/' + encodeURIComponent(slot) + '/side/queue/' + encodeURIComponent(queueId), { client: TAB_ID }).then(j) as Promise<{ ok: boolean; content: string; depth: number }>,
    sideQueueEdit: (slot: string, queueId: string, content: string) => patch('/api/chat/slots/' + encodeURIComponent(slot) + '/side/queue/' + encodeURIComponent(queueId), { content }).then(j) as Promise<{ ok: boolean; depth: number }>,
    sideClose: (slot: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/side/close', {}).then(j) as Promise<{ ok: boolean; was_open: boolean }>,
    chatMode: (mode: string, slot?: string) => post('/api/chat/mode', { mode, slot: slot || '' }).then(j),
    generateTitle: (slot: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/generate-title').then(j),
    resolveNavLinks: (links: { url: string; context: string }[]) => post('/api/chat/nav/resolve-links', { links }).then(j) as Promise<{ summaries: string[] }>,
    renameSlot: (slot: string, title: string) => patch('/api/chat/slots/' + encodeURIComponent(slot) + '/title', { title }).then(j),
    regenerateSlot: (slot: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/regenerate').then(j),
    /** Pick an interrupted turn back up. NOT `/resume` — that path opens a history session into a tab. */
    continueSlot: (slot: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/continue').then(j),
    switchVariant: (slot: string, index: number) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/switch-variant', { index }).then(j),
    editResend: (slot: string, ts: string, content: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/edit-resend', { ts, content }).then(j),
    rewind: (slot: string, ts: string, content: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/rewind', { ts, content }).then(j),
    slackLink: (slot: string, channel?: string, threadTs?: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/slack-link', (channel || threadTs) ? { ...(channel ? { channel } : {}), ...(threadTs ? { thread_ts: threadTs } : {}) } : undefined).then(j),
    unlinkSlack: (slot: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/slack-unlink').then(j),
    // Sets whether turns reach the linked Slack thread. One call for both
    // directions: a session born in its thread has no binding to re-establish, so
    // reconnecting cannot go through slack-link.
    pauseSlack: (slot: string, paused: boolean) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/slack-pause', { paused }).then(j),
    /** `origin` names WHICH non-Slack delivery to act on: the conversation the
     *  session was born in, or its explicit mirror binding. A session can hold
     *  both, and they mute independently, so the row has to say which it is. */
    pauseMirror: (slot: string, paused: boolean, origin = false) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/mirror-pause', { paused, origin }).then(j),
    channelTargets: () => fetch('/api/chat/channel-targets').then(j),
    linkMirror: (slot: string, channelType: string, targetId: string) => post(
      '/api/chat/slots/' + encodeURIComponent(slot) + '/mirror-link',
      { channel_type: channelType, target_id: targetId },
    ).then(j),
    remindMirror: (slot: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/mirror-link').then(j),
    // Severs the binding a link row names — the session's mirror OR its Slack
    // thread: `expected` is the row's `{channel_type, binding}` and the server
    // routes a `slack` binding to the Slack teardown itself, so the menu carries
    // no channel-to-endpoint assumption. Without `expected`, an unconditional
    // clear of the mirror for callers that hold no row.
    unlinkMirror: (slot: string, expected?: { channel_type: string; binding: string }) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/mirror-unlink', expected).then(j),
    slackChannels: () => fetch('/api/slack/channels').then(j),
  }

  const send = {
    sendChat: (message: string, slot?: string, colorTheme?: string, signal?: AbortSignal, meta?: Record<string, unknown>, steer?: boolean | 'auto') => {
      // theme_consent_sha is the WIRE TOKEN (two-tier consent). The client just
      // TRANSMITS the raw stored grant (see themeConsentSha) — the server verifies
      // content-binding, injecting the persona only when this token equals sha256
      // of the persona.md it reads. Omitted for a built-in theme, no grant, or a
      // legacy '1'/'' token (must re-prompt). The legacy `theme_consent` boolean
      // is intentionally NOT sent: gating is content-bound server-side.
      //
      // Browse mode is no longer sent per message: it is default-on server-side
      // whenever Browser Mode is enabled in Settings (a durable capability),
      // gated there rather than per turn.
      //
      // `steer` carries the user's "act on this now" intent. Mid-turn it injects
      // into the RUNNING turn instead of queueing (the backend falls back to the
      // queue if steer is unavailable, so the text is never dropped, and answers
      // `{ok, steered}`); on an idle slot there is no running turn to inject into
      // and the flag's only effect server-side is to skip the hold that parks a
      // user message behind still-running sub-agents. One wire for both: this is
      // the fetch seam under the chat-core `sendTurn`, which every steer now
      // rides (there is no separate steer helper).
      //
      // `'auto'` is the composer's third busy mode: the same intent to act NOW,
      // with the choice between injecting and queueing handed to the gateway for
      // this one message (`decisions/points/message_steer.py`). Sent as the literal
      // string beside the boolean the two manual modes send, so a gateway that does
      // not know the word reads a truthy flag and steers — which is exactly the
      // fallback the decision itself has.
      //
      // The response is handed back RAW (the chat-core transport reads the
      // receipt itself; a 4xx/5xx must resolve, not throw like `j`), but it still
      // runs the same auth recovery every `j`-parsed call has -- see
      // `sendResponseAuthRecovery` -- instead of surfacing an expired or
      // stale-owner session as a bare "refused" send. The steer helper this
      // replaced went through `j` and had both; the transport must not lose them.
      const themeConsent = themeConsentSha(colorTheme)
      return fetch('/api/chat?ws=1', { method: 'POST', headers: { 'Content-Type': 'application/json', ..._sk }, body: JSON.stringify({ message, slot, ...(colorTheme ? { color_theme: colorTheme } : {}), ...(themeConsent ? { theme_consent_sha: themeConsent } : {}), ...(meta ? { meta } : {}), ...(steer ? { steer: steer === 'auto' ? 'auto' : true } : {}) }), signal }).then(sendResponseAuthRecovery)
    },
  }

  const composerAutocomplete = {
    // Autocomplete
    autocomplete: (q: string): Promise<{suggestions: string[]}> => fetch('/api/autocomplete?q=' + encodeURIComponent(q)).then(j),
  }

  return { summaries, slotList, slots, send, composerAutocomplete }
}
