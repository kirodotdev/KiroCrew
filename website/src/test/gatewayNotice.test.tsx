import { describe, it, expect } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import type { ReactElement } from 'react'

import type { ChatMessage } from '../types'
import { resolveGatewayNotice } from '../pages/chat/gatewayNotice'
import { defaultMessageRenderers, mergeRenderers, resolveRenderer, type MessageRenderContext } from '../app-sdk/messageRenderers'
import { createTranscriptRenderers } from '../pages/chat/transcriptRenderers'
import en from '../i18n/locales/en.json'

/**
 * Frontend half of the gateway status-row notice contract, starting with the
 * transient-5xx family; the other families are pinned further down. The gateway appends an
 * `error` row per retry attempt carrying a `meta.notice` wire token (its English
 * content is only the channel/SSE fallback); the dashboard keys localized copy on
 * the token. Pinned here: token → copy; a pending retry renders as a soft
 * NoticeCard on BOTH registries (main transcript + app-sdk) and settles to
 * "Connection restored" once an assistant row follows WITHIN the same recovery,
 * or to the past-tense "Connection interrupted" when a non-ladder error row (the
 * give-up included) closes the window first (a later user turn stops the scan
 * and leaves it pending), so a
 * failed retry never reads "restored" off an unrelated later answer and never
 * stays "retrying…" above its own red give-up; the terminal shape stays a
 * red ErrorCard with the host's Continue affordance intact; token-less history
 * rows in the pre-token wording classify the way their gateway did (a
 * "recovering…" row without `kind = transient_retry` never re-queued, so it is
 * terminal). The final block pins the token table against the Python constants,
 * the way test_recovery_marker_parity.py does for RecoveryCard's PREFIXES.
 */

const COPY = en.pages.chat.transientNotice
const GN = en.pages.chat.gatewayNotice

const msg = (role: string, over: Partial<ChatMessage> = {}): ChatMessage =>
  ({ role, content: '', cls: '', ...over }) as ChatMessage

// Live rows: the English content is the channel/SSE fallback the dashboard never
// shows; the card is keyed on `meta.notice`. The content here is deliberately
// NOT the real wire text, to prove nothing keys on it.
const withNotice = (notice: string, kind?: string) =>
  msg('error', { content: `fallback prose for ${notice}`, meta: { notice, ...(kind ? { kind } : {}) } } as Partial<ChatMessage>)
const retrying = withNotice('transient_retrying', 'transient_retry')
const resuming = withNotice('transient_resuming', 'transient_retry')
const giveUp = withNotice('transient_give_up')
const legacyRetrying = msg('error', { content: '⟳ Backend hiccup — retrying…' })
// The pre-token gateway wrote "recovering…" for both outcomes and marked only the
// re-queued one with the retry kind.
const legacyResumingQueued = msg('error', { content: '⟳ Backend hiccup — recovering…', meta: { kind: 'transient_retry' } } as Partial<ChatMessage>)
const legacyResumingTerminal = msg('error', { content: '⟳ Backend hiccup — recovering…' })
const legacyGiveUp = msg('error', { content: '⟳ Backend hiccup — please retry.' })
const unrelated = msg('error', { content: '❌ Something else entirely' })

describe('resolveGatewayNotice', () => {
  it('maps the pending shapes to localized NoticeCard copy in the warn tone', () => {
    expect(resolveGatewayNotice(retrying, [retrying], 0)).toEqual({ text: COPY.retrying, card: 'notice', tone: 'warn' })
    expect(resolveGatewayNotice(resuming, [resuming], 0)).toEqual({ text: COPY.resuming, card: 'notice', tone: 'warn' })
  })

  it('keeps the terminal shape on the red ErrorCard, localized', () => {
    expect(resolveGatewayNotice(giveUp, [giveUp], 0)).toEqual({ text: COPY.give_up, card: 'error' })
  })

  it('classifies token-less history rows the way their gateway did', () => {
    // Persisted rows are re-read on every reload; no migration rewrites them.
    expect(resolveGatewayNotice(legacyRetrying, [legacyRetrying], 0)?.text).toBe(COPY.retrying)
    expect(resolveGatewayNotice(legacyResumingQueued, [legacyResumingQueued], 0)).toEqual({ text: COPY.resuming, card: 'notice', tone: 'warn' })
    // No retry kind = nothing was re-queued: terminal, never a pending notice.
    expect(resolveGatewayNotice(legacyResumingTerminal, [legacyResumingTerminal], 0)).toEqual({ text: COPY.give_up, card: 'error' })
    expect(resolveGatewayNotice(legacyGiveUp, [legacyGiveUp], 0)).toEqual({ text: COPY.give_up, card: 'error' })
  })

  it('settles a pending notice to "restored" once an assistant row follows it', () => {
    const list = [retrying, msg('assistant', { content: 'the answer' })]
    expect(resolveGatewayNotice(retrying, list, 0)).toEqual({ text: COPY.restored, card: 'notice', tone: 'info' })
    // A live stream counts too — the model is answering again.
    const live = [resuming, msg('streaming', { content: 'partial…' })]
    expect(resolveGatewayNotice(resuming, live, 0)?.text).toBe(COPY.restored)
  })

  const INTERRUPTED = { text: COPY.interrupted, card: 'notice', tone: 'info' }

  it('stays pending (present tense, warn) while nothing has closed the window', () => {
    // Only a tool row so far: the ladder may still be driving.
    const list = [retrying, msg('tool', { content: 'tool output' })]
    expect(resolveGatewayNotice(retrying, list, 0)).toEqual({ text: COPY.retrying, card: 'notice', tone: 'warn' })
    // An assistant row ABOVE it proves nothing: only later rows settle a recovery.
    const above = [msg('assistant', { content: 'earlier' }), retrying]
    expect(resolveGatewayNotice(retrying, above, 1)?.text).toBe(COPY.retrying)
  })

  it('settles to the past-tense "interrupted" when the ladder gives up', () => {
    // [retrying, giveUp]: the pending row above the red give-up does not read
    // as a retry still in progress — and never as "restored".
    expect(resolveGatewayNotice(retrying, [retrying, giveUp], 0)).toEqual(INTERRUPTED)
    expect(resolveGatewayNotice(resuming, [resuming, giveUp], 0)).toEqual(INTERRUPTED)
    // Every pending link of the same ladder settles together.
    const chain = [retrying, retrying, resuming, giveUp]
    for (const i of [0, 1, 2]) expect(resolveGatewayNotice(chain[i], chain, i)).toEqual(INTERRUPTED)
    // The give-up row itself stays the red ErrorCard.
    expect(resolveGatewayNotice(giveUp, chain, 3)).toEqual({ text: COPY.give_up, card: 'error' })
  })

  it('settles to "interrupted" when any other error closes the window', () => {
    // [retrying, ❌]: the recovery failed some other way.
    const list = [retrying, msg('error', { content: '❌ Internal error' })]
    expect(resolveGatewayNotice(retrying, list, 0)).toEqual(INTERRUPTED)
  })

  it('scans past another re-queued recovery row (a dead process) to the outcome it reaches', () => {
    // The replay lost its process: the gateway appends a token-less
    // `kind: transient_retry` row and re-queues again. That row is still
    // recovery, so it neither settles the notice nor hides the later outcome.
    const processLost = msg('error', { content: '⟳ Connection lost — retrying…', meta: { kind: 'transient_retry' } } as Partial<ChatMessage>)
    const PENDING = { text: COPY.retrying, card: 'notice', tone: 'warn' }
    expect(resolveGatewayNotice(retrying, [retrying, processLost], 0)).toEqual(PENDING)
    expect(resolveGatewayNotice(retrying, [retrying, processLost, msg('assistant', { content: 'ok' })], 0)?.text).toBe(COPY.restored)
    expect(resolveGatewayNotice(retrying, [retrying, processLost, giveUp], 0)).toEqual(INTERRUPTED)
  })

  it('leaves an ABANDONED recovery pending: a user row proves neither outcome', () => {
    // [retrying, user, assistant]: a message typed during the backoff is written
    // at send time while the pre-token replay is re-queued at the head and still
    // runs, so the user row does not prove the retry ended ("interrupted" would
    // be false), and the answer below may belong to the new turn ("restored"
    // would be unproven). The row keeps its present-tense pending copy.
    const PENDING = { text: COPY.retrying, card: 'notice', tone: 'warn' }
    const abandoned = [retrying, msg('user', { content: 'hello?' }), msg('assistant', { content: 'hi' })]
    expect(resolveGatewayNotice(retrying, abandoned, 0)).toEqual(PENDING)
    expect(resolveGatewayNotice(retrying, [retrying, msg('user', { content: 'still there?' })], 0)).toEqual(PENDING)
    expect(resolveGatewayNotice(resuming, [resuming, msg('user', { content: 'x' })], 0)?.text).toBe(COPY.resuming)
  })

  it('scopes the scan to THIS recovery: a failed retry never reads "restored" off a later turn', () => {
    // Recovery failed (❌), then the person asked something else and got an answer.
    // The pending row sits directly above its own red error and reads interrupted.
    const failedThenUnrelated = [
      retrying,
      msg('error', { content: '❌ Internal error' }),
      msg('user', { content: 'never mind, something else' }),
      msg('assistant', { content: 'answer to the unrelated turn' }),
    ]
    expect(resolveGatewayNotice(retrying, failedThenUnrelated, 0)).toEqual(INTERRUPTED)
    // The terminal give-up row closes the window the same way.
    const gaveUpThenUnrelated = [resuming, giveUp, msg('user', { content: 'again' }), msg('assistant', { content: 'ok' })]
    expect(resolveGatewayNotice(resuming, gaveUpThenUnrelated, 0)).toEqual(INTERRUPTED)
  })

  it('keeps one copy set for the pending pair: two states, two strings that name their object', () => {
    // `retrying` (nothing streamed, the whole turn re-sent) and `resuming` (a
    // partial answer is on screen and is being finished) are distinct gateway
    // states, so they keep distinct copy, and the resuming copy names the answer
    // it finishes. Pinned as a set so the pair cannot drift back into one idea
    // worded twice.
    expect(COPY.retrying).toBe('Connection unstable — retrying…')
    expect(COPY.resuming).toBe('Connection unstable — finishing this answer…')
    expect(COPY.resuming).not.toBe(COPY.retrying)
    // Both settle to the same two past-tense outcomes.
    expect(COPY.restored).toBe('Connection restored')
    expect(COPY.interrupted).toBe('Connection interrupted')
    // And the frontend draws each shape with its own string.
    expect(resolveGatewayNotice(retrying, [retrying], 0)?.text).toBe(COPY.retrying)
    expect(resolveGatewayNotice(resuming, [resuming], 0)?.text).toBe(COPY.resuming)
  })

  it('settles every link of one ladder together when the answer finally lands', () => {
    // Two 5xx attempts in a row append two pending rows before the answer; both
    // belong to the same recovery, so neither is stuck on "retrying…".
    const chain = [retrying, retrying, resuming, msg('assistant', { content: 'finally' })]
    expect(resolveGatewayNotice(retrying, chain, 0)?.text).toBe(COPY.restored)
    expect(resolveGatewayNotice(retrying, chain, 1)?.text).toBe(COPY.restored)
    expect(resolveGatewayNotice(resuming, chain, 2)?.text).toBe(COPY.restored)
    // Transparent rows (tool output) in between do not close the window.
    const withTool = [retrying, msg('tool', { content: 'tool output' }), msg('assistant', { content: 'done' })]
    expect(resolveGatewayNotice(retrying, withTool, 0)?.text).toBe(COPY.restored)
  })

  it('never settles the terminal shape', () => {
    const list = [giveUp, msg('assistant', { content: 'a later, unrelated turn' })]
    expect(resolveGatewayNotice(giveUp, list, 0)?.text).toBe(COPY.give_up)
  })

  it('leaves every other error row alone (verbatim prose is the contract)', () => {
    expect(resolveGatewayNotice(unrelated, [unrelated], 0)).toBeNull()
    // The token only means something on an `error` row.
    expect(resolveGatewayNotice(msg('notice', { meta: { notice: 'transient_retrying' } } as Partial<ChatMessage>), [], 0)).toBeNull()
    // An unknown token is not ours either.
    expect(resolveGatewayNotice(msg('error', { content: 'x', meta: { notice: 'something_else' } } as Partial<ChatMessage>), [], 0)).toBeNull()
  })
})

/** Identity `row`/`wrapper` so a render returns the card element itself. */
const ctx = (over: Partial<MessageRenderContext> = {}): MessageRenderContext => ({
  index: 0,
  messages: [],
  running: false,
  key: 'k0',
  onFileOpen: () => {},
  hideCardOwnedOAuth: false,
  autoDeniedIds: new Set<string>(),
  wrapper: (children) => children,
  row: (children) => children,
  ...over,
})

const mainRegistry = mergeRenderers(createTranscriptRenderers({ slot: 's1', continuable: true, interrupted: true, onContinue: () => undefined }))

function renderWith(registry: typeof defaultMessageRenderers, m: ChatMessage, over?: Partial<MessageRenderContext>) {
  const entry = resolveRenderer(m, registry)
  expect(entry?.id).toBe(m.role)
  return render(<>{entry!.render(m, ctx({ messages: [m], ...over }))}</>)
}

describe.each([
  ['main transcript registry', mainRegistry],
  ['app-sdk default registry', defaultMessageRenderers],
])('%s', (_name, registry) => {
  it('draws a pending retry as a warn NoticeCard, not a red error card', () => {
    renderWith(registry, retrying)
    const card = screen.getByTestId('notice-card')
    expect(card).toHaveAttribute('data-tone', 'warn')
    expect(card).toHaveTextContent(COPY.retrying)
    expect(screen.queryByTestId('error-card')).toBeNull()
    // The raw gateway prose never reaches the screen.
    expect(screen.queryByText(/fallback prose/)).toBeNull()
    cleanup()
  })

  it('draws a settled retry as an info NoticeCard reading "restored"', () => {
    const list = [retrying, msg('assistant', { content: 'done' })]
    renderWith(registry, retrying, { index: 0, messages: list })
    const card = screen.getByTestId('notice-card')
    expect(card).toHaveAttribute('data-tone', 'info')
    expect(card).toHaveTextContent(COPY.restored)
    cleanup()
  })

  it('draws a given-up retry as an info NoticeCard reading "interrupted" above the red give-up', () => {
    const list = [retrying, giveUp]
    renderWith(registry, retrying, { index: 0, messages: list })
    const card = screen.getByTestId('notice-card')
    expect(card).toHaveAttribute('data-tone', 'info')
    expect(card).toHaveTextContent(COPY.interrupted)
    expect(card).not.toHaveTextContent(COPY.retrying)
    cleanup()
  })

  it('keeps the terminal shape on the ErrorCard with localized text', () => {
    renderWith(registry, giveUp)
    expect(screen.getByTestId('error-card')).toHaveTextContent(COPY.give_up)
    expect(screen.queryByTestId('notice-card')).toBeNull()
    cleanup()
  })

  it('renders a legacy "Backend hiccup" history row through the same card', () => {
    renderWith(registry, legacyRetrying)
    expect(screen.getByTestId('notice-card')).toHaveTextContent(COPY.retrying)
    expect(screen.queryByText(/hiccup/)).toBeNull()
    cleanup()
  })

  it('still renders an unrelated error verbatim', () => {
    renderWith(registry, unrelated)
    expect(screen.getByTestId('error-card')).toHaveTextContent('❌ Something else entirely')
    cleanup()
  })
})

describe('the terminal shape keeps the single-chat Continue affordance', () => {
  it('passes onContinue through for the newest error when the turn was interrupted', () => {
    const entry = resolveRenderer(giveUp, mainRegistry)
    const el = entry!.render(giveUp, ctx({ messages: [giveUp] })) as ReactElement
    expect(el.props.onContinue).toBeTypeOf('function')
    expect(el.props.content).toBe(COPY.give_up)
  })
})

describe('the other gateway status-row families', () => {
  const errorRow = (notice: string, args?: Record<string, unknown>) =>
    msg('error', { content: `fallback prose for ${notice}`, meta: { notice, ...(args ? { notice_args: args } : {}) } } as Partial<ChatMessage>)
  const noticeRow = (notice: string, args?: Record<string, unknown>) =>
    msg('notice', { content: `fallback prose for ${notice}`, meta: { kind: 'empty_turn_notice', notice, ...(args ? { notice_args: args } : {}) } } as Partial<ChatMessage>)

  it('keeps a recovery row on the red ErrorCard with localized copy and its Resume wording', () => {
    expect(resolveGatewayNotice(errorRow('session_busy_give_up'), [], 0)).toEqual({
      card: 'error',
      text: GN.session_busy_give_up,
      resumeText: en.pages.chat.errorCard.retry_session_busy,
    })
    expect(resolveGatewayNotice(errorRow('session_stuck'), [], 0)).toEqual({ card: 'error', text: GN.session_stuck.replace('{{detail}}', '') })
  })

  it('interpolates the exit code a connection-lost row carries', () => {
    const row = errorRow('connection_lost_give_up', { exit_code: -9 })
    expect(resolveGatewayNotice(row, [row], 0)).toMatchObject({ card: 'error', text: 'Connection lost (exit -9) — please retry.' })
  })

  it('draws an empty-response or model-fallback row as a NoticeCard in its tone', () => {
    expect(resolveGatewayNotice(noticeRow('empty_turn_give_up'), [], 0)).toEqual({ card: 'notice', text: GN.empty_turn_give_up, tone: 'info' })
    expect(resolveGatewayNotice(noticeRow('empty_turn_continuing', { n: 2, max: 3 }), [], 0)?.text).toBe(
      'The model returned nothing — auto-continuing (recovery 2 of 3).',
    )
    expect(resolveGatewayNotice(noticeRow('empty_turn_continuing'), [], 0)?.text).toBe(GN.empty_turn_continuing)
    expect(resolveGatewayNotice(noticeRow('model_access_fallback', { rejected: 'big', model: 'small' }), [], 0)).toEqual({
      card: 'notice',
      text: 'Your account cannot use model big — running on small instead.',
      tone: 'warn',
    })
  })

  it('classifies pre-token history rows of every family by their wording', () => {
    const legacy = (role: string, content: string) => msg(role, { content })
    expect(resolveGatewayNotice(legacy('error', '⟳ Turn stalled — please retry.'), [], 0)?.text).toBe(GN.turn_stalled_give_up)
    expect(resolveGatewayNotice(legacy('error', '⟳ Connection lost (exit 1) — retrying...'), [], 0)?.text).toBe(
      'Connection lost (exit 1) — retrying…',
    )
    expect(resolveGatewayNotice(legacy('notice', 'ℹ The model returned nothing this turn. Just send your message again to continue.'), [], 0)?.text).toBe(
      GN.empty_turn_give_up,
    )
    expect(resolveGatewayNotice(legacy('notice', "⚠ Your account cannot use model 'a' — running on 'b' instead."), [], 0)?.text).toBe(
      'Your account cannot use model a — running on b instead.',
    )
    // A wording only means something on its own role.
    expect(resolveGatewayNotice(legacy('notice', '⟳ Turn stalled — please retry.'), [], 0)).toBeNull()
    // Any other notice keeps its own text.
    expect(resolveGatewayNotice(legacy('notice', 'ℹ Something else'), [], 0)).toBeNull()
  })

  it.each([
    ['main transcript registry', mainRegistry],
    ['app-sdk default registry', defaultMessageRenderers],
  ])('%s draws a gateway notice row with localized copy, never the fallback prose', (_name, registry) => {
    renderWith(registry, noticeRow('empty_turn_after_work'))
    expect(screen.getByTestId('notice-card')).toHaveTextContent(GN.empty_turn_after_work)
    expect(screen.queryByText(/fallback prose/)).toBeNull()
    cleanup()
  })
})

describe('the recovery, cancelled-replay, end-of-turn and admission rows', () => {
  const row = (role: string, notice: string, args?: Record<string, unknown>) =>
    msg(role, { content: `fallback prose for ${notice}`, meta: { notice, ...(args ? { notice_args: args } : {}) } } as Partial<ChatMessage>)

  it('keeps a reconnect, rebuild or admission row on the red ErrorCard, localized', () => {
    expect(resolveGatewayNotice(row('error', 'session_reconnect_give_up'), [], 0)).toEqual({ card: 'error', text: GN.session_reconnect_give_up })
    expect(resolveGatewayNotice(row('error', 'compaction_retrying'), [], 0)).toEqual({ card: 'error', text: GN.compaction_retrying })
    expect(resolveGatewayNotice(row('error', 'crew_name_undispatchable'), [], 0)).toEqual({ card: 'error', text: GN.crew_name_undispatchable })
  })

  it('draws a cancelled replay, end-of-turn or queue row as an info NoticeCard', () => {
    for (const token of ['auto_continue_cancelled_superseded', 'filter_retry_cancelled_superseded', 'deferred_followup', 'memory_preparing'] as const) {
      expect(resolveGatewayNotice(row('notice', token), [], 0)).toEqual({ card: 'notice', text: GN[token], tone: 'info' })
    }
  })

  it('interpolates the values a row carries', () => {
    expect(resolveGatewayNotice(row('notice', 'capacity_retrying', { delay_secs: 4, attempt: 2 }), [], 0)?.text).toBe(
      'The last tool call was refused because the gateway was at capacity — retrying it in 4s (attempt 2).',
    )
    expect(resolveGatewayNotice(row('notice', 'agent_switch_blocked_pinned', { agent: 'a', member: 'm' }), [], 0)?.text).toBe(
      'Switching to a was blocked — this thread is pinned to m. The next turn restarts on the pinned crew.',
    )
    expect(resolveGatewayNotice(row('notice', 'model_throttled_fallback', { primary: 'p', fallback: 'f' }), [], 0)).toEqual({
      card: 'notice',
      text: 'p is throttled — running on f until p recovers.',
      tone: 'warn',
    })
  })

  it('classifies pre-token history rows by their wording', () => {
    const legacy = (role: string, content: string) => msg(role, { content })
    expect(resolveGatewayNotice(legacy('notice', 'ℹ Auto-continue cancelled — your message takes over.'), [], 0)?.text).toBe(
      GN.auto_continue_cancelled_superseded,
    )
    expect(resolveGatewayNotice(legacy('notice', 'ℹ Content-filter retry cancelled — the turn was stopped.'), [], 0)?.text).toBe(
      GN.filter_retry_cancelled_stopped,
    )
    expect(resolveGatewayNotice(legacy('error', '⟳ Compaction failed — retrying…'), [], 0)?.text).toBe(GN.compaction_retrying)
    expect(
      resolveGatewayNotice(legacy('notice', '⟳ The last tool call was refused for lack of gateway capacity — retrying it in 9s (attempt 3).'), [], 0)?.text,
    ).toBe('The last tool call was refused because the gateway was at capacity — retrying it in 9s (attempt 3).')
    expect(
      resolveGatewayNotice(legacy('notice', '📌 Agent switch to x was blocked — this chat is in Crew Mode, which stays on its conductor. The next turn restarts on the conductor.'), [], 0)?.text,
    ).toBe('Switching to x was blocked — this chat is in Crew Mode, which stays on its conductor. The next turn restarts on the conductor.')
    // A notice wording on an error row is not ours.
    expect(resolveGatewayNotice(legacy('error', 'ℹ Auto-continue cancelled — your message takes over.'), [], 0)).toBeNull()
  })
})

describe('wire parity with the gateway constants', () => {
  // `TRANSIENT_NOTICE_<NAME>` in chat_utils.py and `GATEWAY_NOTICE_<NAME>` in
  // gateway_notice.py, at column 0 (the META_KEY / ARGS_KEY entries name meta
  // fields, not tokens); every token must be known to the frontend on exactly
  // one row role, or its row falls back to raw English on every locale.
  const read = (file: string) => readFileSync(join(__dirname, '../../../src/kiro_crew/dashboard', file), 'utf8')
  const sources = [read('chat_utils.py'), read('gateway_notice.py')]
  const backend = sources.flatMap((py) =>
    [...py.matchAll(/^(?:TRANSIENT|GATEWAY)_NOTICE_(?!META_KEY|ARGS_KEY)[A-Z_]+\s*=\s*"([^"]+)"/gm)].map((m) => m[1]),
  )
  const metaKeys = sources.flatMap((py) => [...py.matchAll(/^(?:TRANSIENT|GATEWAY)_NOTICE_META_KEY\s*=\s*"([^"]+)"/gm)].map((m) => m[1]))

  it('finds every backend token and the one meta field name the frontend reads', () => {
    expect(backend).toHaveLength(61)
    expect(new Set(metaKeys)).toEqual(new Set(['notice']))
  })

  it.each(backend)('the frontend resolves token %s on exactly one role', (token) => {
    const resolvedOn = ['error', 'notice'].filter(
      (role) => resolveGatewayNotice(msg(role, { content: 'x', meta: { notice: token } } as Partial<ChatMessage>), [], 0) !== null,
    )
    expect(resolvedOn).toHaveLength(1)
  })
})
