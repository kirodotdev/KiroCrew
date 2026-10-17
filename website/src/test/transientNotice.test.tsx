import { describe, it, expect } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import type { ReactElement } from 'react'

import type { ChatMessage } from '../types'
import { resolveTransientNotice } from '../pages/chat/transientNotice'
import { defaultMessageRenderers, mergeRenderers, resolveRenderer, type MessageRenderContext } from '../app-sdk/messageRenderers'
import { createTranscriptRenderers } from '../pages/chat/transcriptRenderers'
import en from '../i18n/locales/en.json'

/**
 * Frontend half of the transient-5xx notice contract. The gateway appends an
 * `error` row per retry attempt carrying a `meta.notice` wire token (its English
 * content is only the channel/SSE fallback); the dashboard keys localized copy on
 * the token. Pinned here: token → copy; a pending retry renders as a soft
 * NoticeCard on BOTH registries (main transcript + app-sdk) and settles to
 * "Connection restored" once an assistant row follows WITHIN the same recovery
 * (a later user turn or a non-ladder error row closes the window, so a failed
 * retry never reads "restored" off an unrelated later answer); the terminal shape stays a
 * red ErrorCard with the host's Continue affordance intact; token-less history
 * rows in the pre-token wording classify the way their gateway did (a
 * "recovering…" row without `kind = transient_retry` never re-queued, so it is
 * terminal). The final block pins the token table against the Python constants,
 * the way test_recovery_marker_parity.py does for RecoveryCard's PREFIXES.
 */

const COPY = en.pages.chat.transientNotice

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

describe('resolveTransientNotice', () => {
  it('maps the pending shapes to localized NoticeCard copy in the warn tone', () => {
    expect(resolveTransientNotice(retrying, [retrying], 0)).toEqual({ text: COPY.retrying, card: 'notice', tone: 'warn' })
    expect(resolveTransientNotice(resuming, [resuming], 0)).toEqual({ text: COPY.resuming, card: 'notice', tone: 'warn' })
  })

  it('keeps the terminal shape on the red ErrorCard, localized', () => {
    expect(resolveTransientNotice(giveUp, [giveUp], 0)).toEqual({ text: COPY.give_up, card: 'error' })
  })

  it('classifies token-less history rows the way their gateway did', () => {
    // Persisted rows are re-read on every reload; no migration rewrites them.
    expect(resolveTransientNotice(legacyRetrying, [legacyRetrying], 0)?.text).toBe(COPY.retrying)
    expect(resolveTransientNotice(legacyResumingQueued, [legacyResumingQueued], 0)).toEqual({ text: COPY.resuming, card: 'notice', tone: 'warn' })
    // No retry kind = nothing was re-queued: terminal, never a pending notice.
    expect(resolveTransientNotice(legacyResumingTerminal, [legacyResumingTerminal], 0)).toEqual({ text: COPY.give_up, card: 'error' })
    expect(resolveTransientNotice(legacyGiveUp, [legacyGiveUp], 0)).toEqual({ text: COPY.give_up, card: 'error' })
  })

  it('settles a pending notice to "restored" once an assistant row follows it', () => {
    const list = [retrying, msg('assistant', { content: 'the answer' })]
    expect(resolveTransientNotice(retrying, list, 0)).toEqual({ text: COPY.restored, card: 'notice', tone: 'info' })
    // A live stream counts too — the model is answering again.
    const live = [resuming, msg('streaming', { content: 'partial…' })]
    expect(resolveTransientNotice(resuming, live, 0)?.text).toBe(COPY.restored)
  })

  it('does NOT settle when only another error follows — the ladder may have given up', () => {
    const list = [retrying, msg('error', { content: '❌ Internal error' })]
    expect(resolveTransientNotice(retrying, list, 0)?.text).toBe(COPY.retrying)
    // Nor from an assistant row ABOVE it: only later rows prove a recovery.
    const above = [msg('assistant', { content: 'earlier' }), retrying]
    expect(resolveTransientNotice(retrying, above, 1)?.text).toBe(COPY.retrying)
  })

  it('scopes the scan to THIS recovery: a failed retry never reads "restored" off a later turn', () => {
    // Recovery failed (❌), then the person asked something else and got an answer.
    // The pending row sits directly above its own red error and must stay pending.
    const failedThenUnrelated = [
      retrying,
      msg('error', { content: '❌ Internal error' }),
      msg('user', { content: 'never mind, something else' }),
      msg('assistant', { content: 'answer to the unrelated turn' }),
    ]
    expect(resolveTransientNotice(retrying, failedThenUnrelated, 0)).toEqual({ text: COPY.retrying, card: 'notice', tone: 'warn' })
    // The terminal give-up row closes the window the same way.
    const gaveUpThenUnrelated = [resuming, giveUp, msg('user', { content: 'again' }), msg('assistant', { content: 'ok' })]
    expect(resolveTransientNotice(resuming, gaveUpThenUnrelated, 0)?.text).toBe(COPY.resuming)
    // A new user turn with no error in between closes it too: the recovery was
    // simply abandoned, and the answer below belongs to the new turn.
    const abandoned = [retrying, msg('user', { content: 'hello?' }), msg('assistant', { content: 'hi' })]
    expect(resolveTransientNotice(retrying, abandoned, 0)?.text).toBe(COPY.retrying)
  })

  it('settles every link of one ladder together when the answer finally lands', () => {
    // Two 5xx attempts in a row append two pending rows before the answer; both
    // belong to the same recovery, so neither is stuck on "retrying…".
    const chain = [retrying, retrying, resuming, msg('assistant', { content: 'finally' })]
    expect(resolveTransientNotice(retrying, chain, 0)?.text).toBe(COPY.restored)
    expect(resolveTransientNotice(retrying, chain, 1)?.text).toBe(COPY.restored)
    expect(resolveTransientNotice(resuming, chain, 2)?.text).toBe(COPY.restored)
    // Transparent rows (tool output) in between do not close the window.
    const withTool = [retrying, msg('tool', { content: 'tool output' }), msg('assistant', { content: 'done' })]
    expect(resolveTransientNotice(retrying, withTool, 0)?.text).toBe(COPY.restored)
  })

  it('never settles the terminal shape', () => {
    const list = [giveUp, msg('assistant', { content: 'a later, unrelated turn' })]
    expect(resolveTransientNotice(giveUp, list, 0)?.text).toBe(COPY.give_up)
  })

  it('leaves every other error row alone (verbatim prose is the contract)', () => {
    expect(resolveTransientNotice(unrelated, [unrelated], 0)).toBeNull()
    // The token only means something on an `error` row.
    expect(resolveTransientNotice(msg('notice', { meta: { notice: 'transient_retrying' } } as Partial<ChatMessage>), [], 0)).toBeNull()
    // An unknown token is not ours either.
    expect(resolveTransientNotice(msg('error', { content: 'x', meta: { notice: 'something_else' } } as Partial<ChatMessage>), [], 0)).toBeNull()
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
  expect(entry?.id).toBe('error')
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

describe('wire parity with the gateway constants', () => {
  // `TRANSIENT_NOTICE_<NAME> = "<token>"` at column 0 in chat_utils.py (the META_KEY
  // entry names the meta field, not a token); every token must be known to the
  // frontend or its row falls back to raw English on every locale.
  const py = readFileSync(join(__dirname, '../../../src/kiro_crew/dashboard/chat_utils.py'), 'utf8')
  const backend = [...py.matchAll(/^TRANSIENT_NOTICE_(?!META_KEY)[A-Z_]+\s*=\s*"([^"]+)"/gm)].map((m) => m[1])
  const metaKey = /^TRANSIENT_NOTICE_META_KEY\s*=\s*"([^"]+)"/m.exec(py)?.[1]

  it('finds the three backend tokens and the meta field name the frontend reads', () => {
    expect(backend).toHaveLength(3)
    expect(metaKey).toBe('notice')
  })

  it.each(backend)('the frontend resolves token %s', (token) => {
    expect(resolveTransientNotice(msg('error', { content: 'x', meta: { notice: token } } as Partial<ChatMessage>), [], 0)).not.toBeNull()
  })
})
