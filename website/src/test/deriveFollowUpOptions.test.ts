import { describe, it, expect } from 'vitest'
import type { ChatMessage } from '../types'
import { deriveFollowUpOptions, parseOptions } from '../app-sdk/protocol'

const user = (content: string): ChatMessage => ({ role: 'user', content, cls: 'msg msg-u' })
const assistant = (content: string): ChatMessage => ({ role: 'assistant', content, cls: 'msg msg-a' })
// Live websocket path tags the notice with a top-level `kind`.
const compactionLive = (content = '✅ Conversation compacted: summary'): ChatMessage =>
  ({ role: 'assistant', content, cls: 'msg msg-a', kind: 'compaction', meta: { kind: 'compaction' } })
// History-reload path only carries `meta.kind` (append persists meta, not a top-level kind).
const compactionReload = (content = '✅ Conversation compacted: summary'): ChatMessage =>
  ({ role: 'assistant', content, cls: 'msg msg-a', meta: { kind: 'compaction' } })
// A dashboard note: POST /api/chat/slots/{slot}/note writes role="inject", cls="reconcile-note".
const note = (content: string): ChatMessage => ({ role: 'inject', content, cls: 'reconcile-note' })
// The SAME note after a restart: history persists `cls` only for role="system", so a
// rehydrated note carries no class and `meta.noteSession` is the surviving provenance.
const rehydratedNote = (content: string): ChatMessage =>
  ({ role: 'inject', content, cls: '', meta: { noteSession: 'chat-1844-1787619403' } })

const OPTIONS_MSG = 'Pick one [OPTIONS: Alpha | Beta | Gamma]'

describe('deriveFollowUpOptions', () => {
  it('returns options from the last assistant turn', () => {
    const { followUpOptions } = deriveFollowUpOptions([user('go'), assistant(OPTIONS_MSG)], false)
    expect(followUpOptions).toEqual(['Alpha', 'Beta', 'Gamma'])
  })

  it('returns no options while streaming', () => {
    expect(deriveFollowUpOptions([user('go'), assistant(OPTIONS_MSG)], true).followUpOptions).toEqual([])
  })

  it('clears options once the user has replied', () => {
    const msgs = [user('go'), assistant(OPTIONS_MSG), user('Alpha')]
    expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual([])
  })

  // Regression: an auto-compaction notice is appended as an assistant-role
  // message AFTER the options-bearing turn. It must not shadow those options.
  it('keeps options when a compaction notice (live kind) follows the options turn', () => {
    const msgs = [user('go'), assistant(OPTIONS_MSG), compactionLive()]
    expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual(['Alpha', 'Beta', 'Gamma'])
  })

  it('keeps options when a compaction notice (reloaded meta.kind) follows the options turn', () => {
    const msgs = [user('go'), assistant(OPTIONS_MSG), compactionReload()]
    expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual(['Alpha', 'Beta', 'Gamma'])
  })

  it('keeps options when a session-reload notice follows the options turn', () => {
    // Same contract as the compaction notice: any system notice kind is
    // scaffolding, never the assistant's last word (isSystemNoticeKind).
    const notice: ChatMessage = {
      role: 'assistant', content: 'Session reloaded: …', cls: 'msg msg-a',
      kind: 'session_reload', meta: { kind: 'session_reload' },
    }
    const msgs = [user('go'), assistant(OPTIONS_MSG), notice]
    expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual(['Alpha', 'Beta', 'Gamma'])
  })

  it('skips multiple stacked compaction notices', () => {
    const msgs = [user('go'), assistant(OPTIONS_MSG), compactionLive(), compactionReload()]
    expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual(['Alpha', 'Beta', 'Gamma'])
  })

  it('still stops at a user message that follows a compaction notice', () => {
    // user reply after compaction → previous turn is over, no options
    const msgs = [user('go'), assistant(OPTIONS_MSG), compactionLive(), user('next')]
    expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual([])
  })

  it('returns no options when there is no assistant turn', () => {
    expect(deriveFollowUpOptions([user('hi')], false).followUpOptions).toEqual([])
  })

  // Regression: Quick Send while the slot is busy appends a 'queued' bubble
  // instead of an optimistic 'user' bubble. Options must still vanish.
  it('clears options when a queued message follows the options turn', () => {
    const queued: ChatMessage = { role: 'queued', content: 'Alpha', cls: 'msg msg-queued', meta: { queueId: 'q1' } }
    const msgs = [user('go'), assistant(OPTIONS_MSG), queued]
    expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual([])
  })

  it('clears options when a queued message follows a compaction notice after options', () => {
    const queued: ChatMessage = { role: 'queued', content: 'Beta', cls: 'msg msg-queued', meta: { queueId: 'q2' } }
    const msgs = [user('go'), assistant(OPTIONS_MSG), compactionLive(), queued]
    expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual([])
  })

  // An `ask_question` card and the pills would otherwise offer the same choices
  // at once, in the same band above the composer. Only the card can answer the
  // blocked tool call, so the pills yield to it.
  it('returns no options while a question card is pending', () => {
    const msgs = [user('go'), assistant(OPTIONS_MSG)]
    expect(deriveFollowUpOptions(msgs, false, true).followUpOptions).toEqual([])
  })

  it('restores options once the pending question resolves', () => {
    const msgs = [user('go'), assistant(OPTIONS_MSG)]
    expect(deriveFollowUpOptions(msgs, false, false).followUpOptions).toEqual(['Alpha', 'Beta', 'Gamma'])
  })

  // Surfaces that never mount a card omit the argument; suppressing there would
  // leave them with no way to answer.
  it('offers options when the pending flag is omitted', () => {
    const msgs = [user('go'), assistant(OPTIONS_MSG)]
    expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual(['Alpha', 'Beta', 'Gamma'])
  })

  it('suppresses the plan flag along with the options while a card is pending', () => {
    const plan = assistant('📋 Plan for: ship it\nStage 1: build\n[OPTIONS: Approve | Revise]')
    const derived = deriveFollowUpOptions([user('go'), plan], false, true)
    expect(derived.followUpOptions).toEqual([])
    expect(derived.followUpIsPlan).toBe(false)
  })

  // A note is written as role="inject", which used to match no branch and fall through.
  // Carrying options is what lets a zero-token cron offer an action without an LLM turn.
  describe('note-carried options', () => {
    it('returns options from a note that carries a marker', () => {
      const msgs = [user('go'), assistant('done'), note('Triage complete [OPTIONS: Fix | Skip]')]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual(['Fix', 'Skip'])
    })

    // A note row needs an identity for the same reason an assistant row does: the
    // plan-dispatch latch and the bar's render key are gated on followUpSourceKey, and a
    // null key would read as "no options on offer" while chips were on screen. Note rows
    // reached this derivation only after the note-carried-options feature landed, so
    // nothing pinned their key until now.
    it('gives a note-carried options row a row identity, not null', () => {
      const msgs = [user('go'), note('Triage complete [OPTIONS: Fix | Skip]')]
      expect(deriveFollowUpOptions(msgs, false).followUpSourceKey).toBe('idx:1')
    })

    it('keys two byte-identical notes apart so the later one is not mistaken for the earlier', () => {
      const body = 'Triage complete [OPTIONS: Fix | Skip]'
      const first = deriveFollowUpOptions([user('go'), note(body)], false).followUpSourceKey
      const second = deriveFollowUpOptions(
        [user('go'), note(body), assistant('working'), note(body)],
        false,
      ).followUpSourceKey
      expect(second).not.toBe(first)
    })

    // THE load-bearing case: had the inject branch returned instead of continuing, an
    // option-less cron note would silently hide the buttons of the real turn it follows.
    it('keeps the ASSISTANT options when an option-less note follows the options turn', () => {
      const msgs = [user('go'), assistant(OPTIONS_MSG), note('FYI: 3 CRs triaged, nothing to do')]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual(['Alpha', 'Beta', 'Gamma'])
    })

    it('still clears options when a user message follows the note', () => {
      const msgs = [user('go'), assistant('done'), note('Pick [OPTIONS: Fix | Skip]'), user('Fix')]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual([])
    })

    it('honours single-select [OPTION:] on a note', () => {
      const parsed = parseOptions('Approve? [OPTION: Yes]')
      expect(parsed.multi).toBe(false)
      expect(deriveFollowUpOptions([user('go'), note('Approve? [OPTION: Yes]')], false).followUpOptions)
        .toEqual(['Yes'])
    })

    it('strips the marker from the note text a transcript renders', () => {
      // Pure-function half only. The RENDERER's strip is asserted on the real render
      // path in AppSdkMessageRenderersCov80 ("strips the OPTIONS marker from ... note bubble").
      const { text } = parseOptions('Triage complete [OPTIONS: Fix | Skip]')
      expect(text).toBe('Triage complete')
      expect(text).not.toContain('[OPTIONS:')
    })

    // The gate is the note wire contract (cls), not the bare `inject` role: 5 other
    // producers emit `inject` and must not gain pills from their own text.
    it('offers pills for a note but NOT for a marker-bearing non-note inject row', () => {
      const marker = 'Pick [OPTIONS: Fix | Skip]'
      const asNote: ChatMessage = { role: 'inject', content: marker, cls: 'reconcile-note' }
      const asCron: ChatMessage = { role: 'inject', content: marker, cls: '' }
      expect(deriveFollowUpOptions([user('go'), asNote], false).followUpOptions).toEqual(['Fix', 'Skip'])
      expect(deriveFollowUpOptions([user('go'), asCron], false).followUpOptions).toEqual([])
    })

    it('keeps a non-note inject row transparent rather than blocking', () => {
      // A cron/replay injection must neither claim the pills nor hide the assistant's.
      const asCron: ChatMessage = { role: 'inject', content: 'Pick [OPTIONS: Nope]', cls: 'msg msg-u' }
      const msgs = [user('go'), assistant(OPTIONS_MSG), asCron]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual(['Alpha', 'Beta', 'Gamma'])
    })

    it('requires the note class as a whole class, not a substring', () => {
      const nearMiss: ChatMessage = { role: 'inject', content: 'Pick [OPTIONS: Fix]', cls: 'reconcile-note-draft' }
      expect(deriveFollowUpOptions([user('go'), nearMiss], false).followUpOptions).toEqual([])
    })

    // A `cls`-only gate is true on the live frame and false on the same row after a
    // reload, so a saved note lost its buttons and leaked its marker on restart.
    it('returns options from a note that was persisted and rehydrated without its class', () => {
      const msgs = [user('go'), assistant('done'), rehydratedNote('Triage complete [OPTIONS: Fix | Skip]')]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual(['Fix', 'Skip'])
    })

    it('offers no pills for a rehydrated inject row that carries no note provenance', () => {
      const bare: ChatMessage = { role: 'inject', content: 'Pick [OPTIONS: Fix | Skip]', cls: '' }
      expect(deriveFollowUpOptions([user('go'), bare], false).followUpOptions).toEqual([])
    })

    it('keeps a provenance-less inject row transparent rather than blocking', () => {
      const bare: ChatMessage = { role: 'inject', content: 'Pick [OPTIONS: Nope]', cls: '' }
      const msgs = [user('go'), assistant(OPTIONS_MSG), bare]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual(['Alpha', 'Beta', 'Gamma'])
    })

    // The notice guard runs BEFORE the new inject branch, so a notice-kinded row is
    // skipped whatever its role -- the new branch must not intercept it.
    it('lets the system-notice guard skip an inject row tagged with a notice kind', () => {
      const noticeInject: ChatMessage = {
        role: 'inject', content: 'stale [OPTIONS: Nope]', cls: 'reconcile-note',
        kind: 'compaction', meta: { kind: 'compaction' },
      }
      const msgs = [user('go'), assistant(OPTIONS_MSG), noticeInject]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual(['Alpha', 'Beta', 'Gamma'])
    })

    // Was asserted as `true` before GPT 5.6 flagged it on a05bb7e38: `followUpIsPlan` is read
    // ONLY to dispatch /plan-action, so a plan-shaped note could cancel a live plan.
    it('never claims the plan flag for a note, even when the note text is plan-shaped', () => {
      const planNote = note('📋 Plan for: ship it\nStage 1: build\n[OPTIONS: Approve | Revise]')
      const derived = deriveFollowUpOptions([user('go'), planNote], false)
      expect(derived.followUpOptions).toEqual(['Approve', 'Revise'])
      expect(derived.followUpIsPlan).toBe(false)
    })

    // The SAME plan text on a real assistant turn must still dispatch, or the fix would have
    // broken the orchestrator instead of scoping the flag to its actual provenance.
    it('still claims the plan flag for the same text on an assistant turn', () => {
      const planText = '📋 Plan for: ship it\nStage 1: build\n[OPTIONS: Approve | Revise]'
      expect(deriveFollowUpOptions([user('go'), note(planText)], false).followUpIsPlan).toBe(false)
      expect(deriveFollowUpOptions([user('go'), assistant(planText)], false).followUpIsPlan).toBe(true)
    })

    // The transcript guard dispatches a plan action on
    // `followUpIsPlan && mode === 'orchestrator' && slot`; only the first term a note controls.
    it('a plan-shaped note carrying Cancel does not dispatch a plan action', () => {
      // Mirrors that guard with its real inputs.
      const dispatchesPlanAction = (m: ChatMessage, mode: string, slot: string | null) =>
        deriveFollowUpOptions([user('go'), m], false).followUpIsPlan && mode === 'orchestrator' && !!slot
      const planText = '📋 Plan for: ship it\nStage 1: build\n[OPTIONS: Go | Cancel]'
      expect(deriveFollowUpOptions([user('go'), note(planText)], false).followUpOptions)
        .toEqual(['Go', 'Cancel'])
      expect(dispatchesPlanAction(note(planText), 'orchestrator', 'slot-1')).toBe(false)
      // Negative control: the real orchestrator plan still dispatches, so the assertion above
      // measures provenance rather than a guard that refuses everything.
      expect(dispatchesPlanAction(assistant(planText), 'orchestrator', 'slot-1')).toBe(true)
    })
  })

  // The plan-dispatch latch is acknowledgement-gated on followUpSourceKey, so a row that
  // re-keys WITHOUT actually changing frees the latch while the same chips are still on
  // screen — and a stale second click then queues an unintended extra Go. The store keys
  // virtual rows by `clientTs ?? ts` and deliberately carries `clientTs` onto the reloaded
  // server copy (chatSlice.ts), so this derivation must follow the same order rather than
  // invent a conflicting one.
  describe('row identity stability across hydration', () => {
    const withMeta = (content: string, meta: Record<string, unknown>): ChatMessage =>
      ({ role: 'assistant', content, cls: 'msg msg-a', meta })

    it('keeps one identity when a reconnect refresh ADDS a server mid to a client-stamped row', () => {
      const before = deriveFollowUpOptions(
        [user('go'), withMeta(OPTIONS_MSG, { clientTs: 'born-7' })],
        false,
      ).followUpSourceKey
      // The SAME row after the refresh: clientTs survives, mid and ts arrive.
      const after = deriveFollowUpOptions(
        [
          user('go'),
          {
            ...withMeta(OPTIONS_MSG, { clientTs: 'born-7', mid: 'srv-42' }),
            ts: '2026-08-27T08:00:00Z',
          },
        ],
        false,
      ).followUpSourceKey
      expect(after).toBe(before)
      expect(after).toBe('born-7')
    })

    it('falls back to mid, then ts, for a row that never carried a client stamp', () => {
      expect(
        deriveFollowUpOptions([user('go'), withMeta(OPTIONS_MSG, { mid: 'srv-42' })], false)
          .followUpSourceKey,
      ).toBe('srv-42')
      expect(
        deriveFollowUpOptions(
          [user('go'), { ...withMeta(OPTIONS_MSG, {}), ts: '2026-08-27T08:00:00Z' }],
          false,
        ).followUpSourceKey,
      ).toBe('2026-08-27T08:00:00Z')
    })

    it('still keys two DIFFERENT client-stamped rows apart', () => {
      const a = deriveFollowUpOptions(
        [user('go'), withMeta(OPTIONS_MSG, { clientTs: 'born-7' })],
        false,
      ).followUpSourceKey
      const b = deriveFollowUpOptions(
        [user('go'), withMeta(OPTIONS_MSG, { clientTs: 'born-8' })],
        false,
      ).followUpSourceKey
      expect(a).not.toBe(b)
    })
  })
})
