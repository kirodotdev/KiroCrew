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
// Every terminal turn error reaches the feed as role="error", so the role is the whole
// contract and the text is deliberately irrelevant to the derivation.
const errorRow = (content = '❌ Request session/new timed out after 90s'): ChatMessage =>
  ({ role: 'error', content, cls: 'msg msg-err' })
const queuedRow = (content: string, queueId = 'q1'): ChatMessage =>
  ({ role: 'queued', content, cls: 'msg msg-queued', meta: { queueId } })
// An auto-retry notice: role `error` like any failure, but carrying the kind the
// backend stamps when it has already queued the recovery that re-runs the turn.
const retryNoticeLive = (content = '⟳ Backend hiccup — retrying…'): ChatMessage =>
  ({ role: 'error', content, cls: 'msg msg-err', kind: 'transient_retry', meta: { kind: 'transient_retry' } })
const retryNoticeReload = (content = '⟳ Backend hiccup — retrying…'): ChatMessage =>
  ({ role: 'error', content, cls: 'msg msg-err', meta: { kind: 'transient_retry' } })
// Both forms are load-bearing (see `isStopEvent`): the live path sets `kind`, a rehydrated
// transcript carries only `meta.kind` unpacked from `cls`.
const stopEventLive = (content = '⏹️ Stopped by user'): ChatMessage =>
  ({ role: 'system', content, cls: 'msg msg-sys', kind: 'stop_event', meta: { kind: 'stop_event' } })
const stopEventReload = (content = '⏹️ Stopped by user'): ChatMessage =>
  ({ role: 'system', content, cls: 'msg msg-sys', meta: { kind: 'stop_event' } })

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

  // A pill click appends the optimistic `user` bubble BEFORE the network call, and a failed
  // turn never removes it — so the unconditional stop hid the pills for good.
  describe('options survive a failed turn', () => {
    it('keeps options when the turn the user started failed', () => {
      const msgs = [user('go'), assistant(OPTIONS_MSG), user('Alpha'), errorRow()]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual(['Alpha', 'Beta', 'Gamma'])
    })

    // Was `keeps the plan flag too` until validation found the double-advance: the pill's click
    // was dispatched, and a dispatch that fails ambiguously may already have committed.
    it('drops the plan flag, because a failed plan turn may already have advanced', () => {
      const plan = assistant('📋 Plan for: ship it\nStage 1: build\n[OPTIONS: Approve | Revise]')
      const derived = deriveFollowUpOptions([user('go'), plan, user('Approve'), errorRow()], false)
      expect(derived.followUpOptions).toEqual([])
      expect(derived.followUpIsPlan).toBe(false)
    })

    // Asserted the opposite until validation found the double-run: a `queued` row's QUEUE
    // ENTRY survives the error, so re-offering the pill runs the choice twice, not once.
    it('clears options when a QUEUED send failed', () => {
      const msgs = [user('go'), assistant(OPTIONS_MSG), queuedRow('Beta'), errorRow()]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual([])
    })

    // The exact reported path: Quick Send while busy, an AUTH error, then sign-in. The queue
    // entry is still pending, so offering the pill would be the second of two executions.
    it('offers nothing after a queued send hit an auth-error boundary', () => {
      const msgs = [
        user('go'), assistant(OPTIONS_MSG),
        queuedRow('Beta', 'q1'), errorRow('🔒 Authentication required — please sign in'),
      ]
      const derived = deriveFollowUpOptions(msgs, false)
      // No pill to click, so the pending queue entry remains the ONLY execution of 'Beta'.
      expect(derived.followUpOptions).toEqual([])
      expect(derived.followUpSourceKey).toBe(null)
    })

    // A `user` row carries no queue entry, so crossing it re-offers a choice that never ran.
    it('still keeps options when a USER send failed, which strands no queue entry', () => {
      const msgs = [user('go'), assistant(OPTIONS_MSG), user('Beta'), errorRow()]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual(['Alpha', 'Beta', 'Gamma'])
    })

    // A deliberate Stop ENDS the turn rather than interrupting it — the same reading
    // `selectTurnInterrupted` already applies — so the error licence must not cross it.
    it('offers nothing when the user pressed Stop after the failure', () => {
      const msgs = [user('go'), assistant(OPTIONS_MSG), user('Alpha'), errorRow(), stopEventLive()]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual([])
    })

    // The rehydrated form carries only `meta.kind`; a guard reading just `kind` would
    // restore the pills after a reload and re-open a choice the user cancelled.
    it('offers nothing for a rehydrated Stop row after the failure', () => {
      const msgs = [user('go'), assistant(OPTIONS_MSG), user('Alpha'), errorRow(), stopEventReload()]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual([])
    })

    it('offers nothing when Stop precedes the error row in the feed', () => {
      const msgs = [user('go'), assistant(OPTIONS_MSG), user('Alpha'), stopEventLive(), errorRow()]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual([])
    })

    // Pins a behaviour CHANGE with no failure involved: a stopped injected or Continue turn
    // has no `user` row, and the stop card was transparent to the scan before this PR.
    it('hides options for a stopped turn with no user row', () => {
      expect(deriveFollowUpOptions([user('go'), assistant(OPTIONS_MSG), stopEventLive()], false).followUpOptions).toEqual([])
      expect(deriveFollowUpOptions([user('go'), assistant(OPTIONS_MSG), stopEventReload()], false).followUpOptions).toEqual([])
    })

    it('keeps options when a failed turn carries no Stop row at all', () => {
      const msgs = [user('go'), assistant(OPTIONS_MSG), user('Alpha'), errorRow()]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual(['Alpha', 'Beta', 'Gamma'])
    })

    // The backend queues the recovery that re-runs the turn BEFORE the user can click, so
    // re-offering the pill here executes the same choice twice.
    it('offers nothing while an automatic retry is pending', () => {
      const msgs = [user('go'), assistant(OPTIONS_MSG), user('Alpha'), retryNoticeLive()]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual([])
    })

    // The rehydrated row carries only `meta.kind`; a guard reading just `kind` would re-offer
    // the pill after a reload, which is when the double-execution actually bites.
    it('offers nothing for a rehydrated retry notice', () => {
      const msgs = [user('go'), assistant(OPTIONS_MSG), user('Alpha'), retryNoticeReload()]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual([])
    })

    // The retry ran and then failed for good, so the question is open again and nothing is
    // pending that could re-run the choice.
    it('restores options when a retry notice is followed by a terminal error', () => {
      const msgs = [user('go'), assistant(OPTIONS_MSG), user('Alpha'), retryNoticeLive(), errorRow()]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual(['Alpha', 'Beta', 'Gamma'])
    })

    it('offers nothing for a plan row reached across a pending retry', () => {
      const plan = assistant('📋 Plan for: ship it\nStage 1: build\n[OPTIONS: Go | Go All | Cancel]')
      expect(deriveFollowUpOptions([user('go'), plan, user('Go'), retryNoticeLive()], false).followUpOptions).toEqual([])
    })

    it('keeps options across two stacked failed attempts', () => {
      // Re-arming per error is what makes this work: each error licenses exactly one crossing.
      const msgs = [
        user('go'), assistant(OPTIONS_MSG),
        user('Alpha'), errorRow(),
        user('Alpha'), errorRow('❌ Connection error'),
      ]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual(['Alpha', 'Beta', 'Gamma'])
    })

    // The queued row now STOPS the scan, so the two `user` failures above it are still
    // crossed but the older options row behind the queued row is deliberately out of reach.
    it('stops at a queued row even with later failed user attempts', () => {
      const msgs = [
        user('go'), assistant(OPTIONS_MSG),
        queuedRow('Alpha', 'q1'), errorRow(),
        user('Alpha'), errorRow('❌ Send failed'),
        user('Alpha'), errorRow('⏱️ timed out'),
      ]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual([])
    })

    it('keeps options across three stacked failed USER attempts', () => {
      const msgs = [
        user('go'), assistant(OPTIONS_MSG),
        user('Alpha'), errorRow(),
        user('Alpha'), errorRow('❌ Send failed'),
        user('Alpha'), errorRow('⏱️ timed out'),
      ]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual(['Alpha', 'Beta', 'Gamma'])
    })

    // The text is not consulted at all, so a cause the derivation has never heard of is
    // covered for free. This is what "every error" rests on.
    it.each([
      '⏱️ Request session/new timed out after 90s',
      '❌ Connection error',
      '⟳ Session busy — please retry.',
      'Session stuck — please start a new chat.',
      '',
      'some cause invented after this test was written',
    ])('keeps options whatever the error text says (%j)', text => {
      const msgs = [user('go'), assistant(OPTIONS_MSG), user('Alpha'), { ...errorRow(), content: text }]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual(['Alpha', 'Beta', 'Gamma'])
    })

    it('still clears options when the turn succeeded with an option-less reply', () => {
      const msgs = [user('go'), assistant(OPTIONS_MSG), user('Alpha'), assistant('Done, no options here')]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual([])
    })

    // THE load-bearing control: an error arms exactly ONE crossing, so a turn that completed
    // between the old options and the failure still stops the scan.
    it('does not reach back past a turn that succeeded before the failure', () => {
      const msgs = [
        user('go'), assistant(OPTIONS_MSG),
        user('Alpha'), assistant('Done, no options here'),
        user('next'), errorRow(),
      ]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual([])
    })

    // #2409 must not regress: a queued send with no error after it still hides the pills.
    it('still clears options for a queued send that has not failed', () => {
      const msgs = [user('go'), assistant(OPTIONS_MSG), queuedRow('Alpha')]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual([])
    })

    it('offers the NEWER options when the retried turn succeeded with its own marker', () => {
      const msgs = [
        user('go'), assistant(OPTIONS_MSG),
        user('Alpha'), errorRow(),
        user('Alpha'), assistant('Retried fine [OPTIONS: Delta | Epsilon]'),
      ]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual(['Delta', 'Epsilon'])
    })

    it('still suppresses options while streaming, even after a failed turn', () => {
      const msgs = [user('go'), assistant(OPTIONS_MSG), user('Alpha'), errorRow()]
      expect(deriveFollowUpOptions(msgs, true).followUpOptions).toEqual([])
    })

    it('still suppresses options while a question card is pending, even after a failed turn', () => {
      const msgs = [user('go'), assistant(OPTIONS_MSG), user('Alpha'), errorRow()]
      expect(deriveFollowUpOptions(msgs, false, true).followUpOptions).toEqual([])
    })

    it('crosses a failed turn whose feed also carries a compaction notice', () => {
      const msgs = [user('go'), assistant(OPTIONS_MSG), user('Alpha'), compactionLive(), errorRow()]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual(['Alpha', 'Beta', 'Gamma'])
    })

    it('crosses a failed turn whose feed also carries an option-less note', () => {
      const msgs = [user('go'), assistant(OPTIONS_MSG), user('Alpha'), note('FYI: nothing to do'), errorRow()]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual(['Alpha', 'Beta', 'Gamma'])
    })

    it('lets a note claim the pills over an older assistant turn after a failure', () => {
      const msgs = [user('go'), assistant(OPTIONS_MSG), user('Alpha'), errorRow(), note('Retry? [OPTIONS: Yes | No]')]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual(['Yes', 'No'])
    })

    // An error with no user/queued row after it was ALREADY transparent (it matched no
    // branch and fell through). Pinned so the explicit `error` branch cannot change it.
    it('leaves a bare error row transparent to the scan', () => {
      const msgs = [user('go'), assistant(OPTIONS_MSG), errorRow()]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual(['Alpha', 'Beta', 'Gamma'])
    })

    it('returns no options when a failed turn has no options-bearing turn behind it', () => {
      const msgs = [user('go'), assistant('no marker here'), user('again'), errorRow()]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual([])
    })

    it('returns no options when the feed is nothing but a failed send', () => {
      expect(deriveFollowUpOptions([user('go'), errorRow()], false).followUpOptions).toEqual([])
    })
  })

  // A turn that streamed text before dying flushes it as a REAL assistant row ahead of the
  // error row, and that option-less row shadowed the question just as the `user` row did.
  describe('options survive a failed turn that emitted partial text', () => {
    const partial = (content = 'Working on it, here is what I found so far…'): ChatMessage =>
      assistant(content)

    it('keeps options when the failed turn flushed partial text first', () => {
      const msgs = [user('go'), assistant(OPTIONS_MSG), user('Alpha'), partial(), errorRow()]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual(['Alpha', 'Beta', 'Gamma'])
    })

    // Same double-run reason as the non-partial queued case: the queue entry outlives the
    // error, so the partial flush does not change whether the pill may be re-offered.
    it('clears options when a queued send failed after emitting partial text', () => {
      const msgs = [user('go'), assistant(OPTIONS_MSG), queuedRow('Beta'), partial(), errorRow()]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual([])
    })

    it('keeps options across several partial-then-error segments in one failed turn', () => {
      // The recovery path appends [partial] [notice] repeatedly before the terminal error.
      const msgs = [
        user('go'), assistant(OPTIONS_MSG), user('Alpha'),
        partial('first chunk'), errorRow('⟳ Backend hiccup — recovering…'),
        partial('second chunk'), errorRow('❌ gave up'),
      ]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual(['Alpha', 'Beta', 'Gamma'])
    })

    it('prefers a partial that itself carries a marker over the older turn', () => {
      const msgs = [
        user('go'), assistant(OPTIONS_MSG), user('Alpha'),
        assistant('Partway there [OPTIONS: Retry | Abandon]'), errorRow(),
      ]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual(['Retry', 'Abandon'])
    })

    it('still clears options when the partial turn ultimately succeeded', () => {
      // No error at all: the option-less reply is the final word and closes the question.
      const msgs = [user('go'), assistant(OPTIONS_MSG), user('Alpha'), partial(), assistant('All done')]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual([])
    })

    it('does not reach past a completed turn to a failure two turns back', () => {
      const msgs = [
        user('go'), assistant(OPTIONS_MSG),
        user('Alpha'), assistant('All done'),
        user('next'), partial(), errorRow(),
      ]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual([])
    })

    // The accepted trade-off, pinned so it stays a decision on record: nothing marks an
    // assistant row partial rather than complete, so a busy-slot refused send reads the same.
    it('re-offers earlier choices when an error follows a completed option-less reply', () => {
      const msgs = [user('go'), assistant(OPTIONS_MSG), user('Alpha'), assistant('All done'), errorRow()]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual(['Alpha', 'Beta', 'Gamma'])
    })

    // Demotion was tried first and was not enough: Quick Send sends a pill in one click
    // whatever `followUpIsPlan` says, so the ambiguous plan case is offered nothing at all.
    it('offers nothing for a re-offered PLAN row after a completed reply', () => {
      const plan = assistant('📋 Plan for: ship it\nStage 1: build\n[OPTIONS: Go | Go All | Cancel]')
      const msgs = [user('go'), plan, user('Go'), assistant('Stage 1 complete.'), errorRow()]
      const derived = deriveFollowUpOptions(msgs, false)
      expect(derived.followUpOptions).toEqual([])
      expect(derived.followUpIsPlan).toBe(false)
      expect(derived.followUpSourceKey).toBe(null)
    })

    // Was `keeps the plan flag when only a failed user row was crossed`: that rested on the plan
    // not having advanced, but the go-latch is per page load, so a reload re-exposes the Go.
    it('offers nothing for a PLAN row reached by crossing a failed user turn', () => {
      const plan = assistant('📋 Plan for: ship it\nStage 1: build\n[OPTIONS: Go | Go All | Cancel]')
      const derived = deriveFollowUpOptions([user('go'), plan, user('Go'), errorRow()], false)
      expect(derived.followUpOptions).toEqual([])
      expect(derived.followUpIsPlan).toBe(false)
      expect(derived.followUpSourceKey).toBe(null)
    })

    it('offers nothing for a PLAN row across two stacked failed attempts', () => {
      const plan = assistant('📋 Plan for: ship it\nStage 1: build\n[OPTIONS: Go | Go All | Cancel]')
      const msgs = [user('go'), plan, user('Go'), errorRow(), user('Go'), errorRow()]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual([])
    })

    // Scoping control: suppression is PLAN-only, so a blanket regression fails here.
    it('still restores NON-plan options across the same failed user turn', () => {
      const msgs = [user('go'), assistant(OPTIONS_MSG), user('Alpha'), errorRow()]
      expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual(['Alpha', 'Beta', 'Gamma'])
    })

    // Control: plan chips are not blanket-suppressed. With no failed turn crossed, one-tap
    // approval is still correct and `followUpIsPlan` must survive.
    it('keeps a PLAN row offered when no failed turn was crossed', () => {
      const plan = assistant('📋 Plan for: ship it\nStage 1: build\n[OPTIONS: Go | Go All | Cancel]')
      const derived = deriveFollowUpOptions([user('go'), plan], false)
      expect(derived.followUpOptions).toEqual(['Go', 'Go All', 'Cancel'])
      expect(derived.followUpIsPlan).toBe(true)
    })

    // Control for the tests above: without it, they still pass if the
    // derivation stops keying on the error row at all.
    it('does not re-offer a PLAN row when the completed reply was not followed by an error', () => {
      const plan = assistant('📋 Plan for: ship it\nStage 1: build\n[OPTIONS: Go | Go All | Cancel]')
      const msgs = [user('go'), plan, user('Go'), assistant('Stage 1 complete.')]
      const derived = deriveFollowUpOptions(msgs, false)
      expect(derived.followUpOptions).toEqual([])
      expect(derived.followUpIsPlan).toBe(false)
    })

    it('still suppresses a crossed partial while streaming or a card is pending', () => {
      const msgs = [user('go'), assistant(OPTIONS_MSG), user('Alpha'), partial(), errorRow()]
      expect(deriveFollowUpOptions(msgs, true).followUpOptions).toEqual([])
      expect(deriveFollowUpOptions(msgs, false, true).followUpOptions).toEqual([])
    })
  })
})
