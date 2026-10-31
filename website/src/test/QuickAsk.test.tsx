import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import i18next from 'i18next'
import '../i18n/all'

import BrowserLiveCard from '../pages/chat/BrowserLiveCard'
import { browserLiveStore } from '../hooks/useBrowserLive'
import type { DecodedBrowserFrame } from '../hooks/useBrowserLive.frame'
import { readRecording } from '../hooks/useBrowserLive.replay'
import { eventLabel } from '../pages/chat/BrowserLiveReplayStage'
import { askUi } from '../pages/chat/quickAsk/askUi'
import { readQuickAsk } from '../pages/chat/quickAsk/types'
import { __REMEMBER_STORAGE_KEY, recall, remember } from '../pages/chat/quickAsk/remember'

/**
 * Quick asks on the live card, driven through the store exactly as the
 * websocket drives it: per-kind bodies, fold and reopen, the answer POST's
 * shape, the error copy, and the secrets invariant (a typed value leaves only
 * in the answer POST; nothing stores or logs it).
 */

const S = 'slot:qa'
const ORIGIN = 'https://accounts.example-identity-provider.com:8443'
const t = (k: string, o?: Record<string, unknown>) => i18next.t(`pages.chat.quickAsk.${k}`, o)

const rect = { x: 100, y: 120, w: 200, h: 40 }
const asks = {
  credentials: { id: 'a1', kind: 'credentials', origin: ORIGIN, reason: 'I need to sign in to read your inbox', fields: [{ role: 'username', label: 'username', rect }, { role: 'password', input: 'password', rect: { ...rect, y: 180 } }], frame: { w: 640, h: 400 }, expires_at: 9e12, phase: 'waiting' },
  text: { id: 'a2', kind: 'text', origin: ORIGIN, reason: '', fields: [{ role: 'text', input: 'email', label: 'email', rect }], frame: { w: 640, h: 400 }, expires_at: 9e12, phase: 'waiting' },
  otp: { id: 'a3', kind: 'otp', origin: ORIGIN, reason: '', digits: 6, fields: [{ role: 'otp', input: 'otp', rect }], frame: { w: 640, h: 400 }, expires_at: 9e12, phase: 'waiting' },
  human_check: { id: 'a4', kind: 'human_check', origin: ORIGIN, reason: '', fields: [{ role: 'widget', rect }], frame: { w: 640, h: 400 }, expires_at: 9e12, phase: 'waiting' },
  confirm: { id: 'a5', kind: 'confirm', origin: ORIGIN, reason: '', summary: 'Book 2 tickets, $35 total', button: 'Place order', fields: [{ role: 'action', rect }], frame: { w: 640, h: 400 }, expires_at: 9e12, phase: 'waiting' },
} as const

const ev = (ask: unknown, over: Record<string, unknown> = {}) => ({ session_key: S, turn_id: 't1', state: 'live', url: `${ORIGIN}/login`, title: 'Sign in', browser: 'kiro_chromium', headless: true, ask, ...over })
const frame = (seq: number): DecodedBrowserFrame => ({
  header: { type: 'browser_frame', session_key: S, seq, w: 640, h: 400, mask: [] },
  jpeg: new Uint8Array([1, 2, 3]),
  receivedAt: seq,
})

type Writes = NonNullable<Parameters<typeof browserLiveStore.__setWritesForTests>[0]>
let writes: Writes & { answer: ReturnType<typeof vi.fn> }
let restore: ReturnType<typeof browserLiveStore.__setWritesForTests>

class FakeIO {
  observe() {}
  disconnect() {}
  unobserve() {}
  takeRecords() { return [] }
  root = null
  rootMargin = ''
  thresholds = []
}

function phone(on: boolean) {
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: on && q.includes('max-width'), media: q, onchange: null,
    addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, dispatchEvent: () => false,
  }))
}

beforeEach(() => {
  browserLiveStore.__resetForTests()
  askUi.__resetForTests()
  localStorage.clear()
  sessionStorage.clear()
  writes = {
    ack: vi.fn(async () => ({ ok: true })),
    visibility: vi.fn(async () => ({ ok: true })),
    claim: vi.fn(async () => ({ ok: true })),
    input: vi.fn(async () => ({ ok: true, sent: 1 })),
    answer: vi.fn(async () => ({ ok: true, outcome: 'filled' })),
  }
  restore = browserLiveStore.__setWritesForTests(writes)
  vi.stubGlobal('IntersectionObserver', FakeIO)
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
  phone(false)
})
afterEach(() => {
  // Unmount first: a store reset under a mounted card is an update outside act.
  cleanup()
  browserLiveStore.__setWritesForTests(restore)
  browserLiveStore.__resetForTests()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const card = () => <BrowserLiveCard sessionKey={S} anchorKey="call:1" anchorTs={null} running goTo={() => {}} />
function show(ask: unknown) {
  act(() => browserLiveStore.applyEvent(ev(ask)))
  return render(card())
}
const type = (el: HTMLElement, v: string) => { fireEvent.input(el, { target: { value: v } }) }

describe('quick ask: per kind', () => {
  it('credentials: optional username, password, our title, origin, secret hint, Decline', () => {
    show(asks.credentials)
    const sheet = screen.getByTestId('quick-ask-sheet')
    expect(sheet).toHaveAttribute('role', 'dialog')
    expect(screen.getByRole('heading')).toHaveTextContent(t('title_credentials', { host: 'accounts.example-identity-provider.com:8443' }))
    expect(screen.getByTestId('quick-ask-username')).toHaveAttribute('autocomplete', 'off')
    expect(screen.getByTestId('quick-ask-password')).toHaveAttribute('type', 'password')
    expect(screen.getByTestId('quick-ask-password')).toHaveAttribute('autocomplete', 'new-password')
    expect(screen.getByTestId('quick-ask-secret-hint')).toHaveTextContent(t('secret_hint'))
    expect(screen.getByTestId('quick-ask-decline')).toBeInTheDocument()
    expect(screen.getByTestId('quick-ask-target')).toBeInTheDocument()
  })

  it('text: one field whose type and keyboard follow its label', () => {
    show(asks.text)
    const input = screen.getByTestId('quick-ask-text')
    expect(input).toHaveAttribute('type', 'email')
    expect(input).toHaveAttribute('inputmode', 'email')
    expect(input).toHaveAttribute('autocomplete', 'email')
    expect(screen.queryByTestId('quick-ask-password')).toBeNull()
  })

  it('otp: N digits, numeric keyboard, one-time-code, only-this-site line', () => {
    show({ ...asks.otp, digits: 8 })
    const input = screen.getByTestId('quick-ask-otp')
    expect(input).toHaveAttribute('inputmode', 'numeric')
    expect(input).toHaveAttribute('autocomplete', 'one-time-code')
    type(input, '1234 5678 9')
    expect((input as HTMLInputElement).value).toBe('12345678')
    expect(screen.getByRole('heading')).toHaveTextContent(t('title_otp', { n: 8 }))
    expect(screen.getByTestId('quick-ask-secret-hint')).toHaveTextContent(t('otp_only_this_site', { origin: ORIGIN }))
  })

  it('human_check: no inputs, only Take over this box and Decline', () => {
    show(asks.human_check)
    const sheet = screen.getByTestId('quick-ask-sheet')
    expect(sheet.querySelectorAll('input')).toHaveLength(0)
    expect(screen.getByTestId('quick-ask-primary')).toHaveTextContent(t('take_over_box'))
    expect(sheet).toHaveTextContent(t('human_check_explainer'))
  })

  it('confirm: summary, the DOM button text, Confirm / Cancel', async () => {
    show(asks.confirm)
    expect(screen.getByTestId('quick-ask-confirm')).toHaveTextContent('Book 2 tickets, $35 total')
    // The summary is the agent's words, labelled as such; the button text is the page's.
    expect(screen.getByTestId('quick-ask-summary')).toHaveTextContent(t('reason_line', { text: 'Book 2 tickets, $35 total' }))
    expect(screen.getByTestId('quick-ask-confirm-button')).toHaveTextContent(t('confirm_then', { button: 'Place order' }))
    expect(screen.getByTestId('quick-ask-decline')).toHaveTextContent(t('cancel'))
    fireEvent.click(screen.getByTestId('quick-ask-primary'))
    await waitFor(() => expect(writes.answer).toHaveBeenCalledWith({ session_key: S, ask_id: 'a5', action: 'confirm' }))
  })
})

describe('quick ask: layout and trust lines', () => {
  it('shows the whole origin, wrapped, never truncated', () => {
    show(asks.credentials)
    const o = screen.getByTestId('quick-ask-origin-text')
    expect(o.textContent).toBe(ORIGIN)
    expect(o.className).toContain('break-all')
    expect(o.className).not.toContain('truncate')
  })

  it('clamps the agent reason to two lines, as plain text with our prefix', () => {
    show({ ...asks.credentials, reason: '<b>hi</b> '.repeat(80) })
    const r = screen.getByTestId('quick-ask-reason')
    expect(r.className).toContain('line-clamp-2')
    expect(r.querySelector('b')).toBeNull()
    expect(r.textContent?.startsWith(t('reason_line', { text: '' }).trim())).toBe(true)
  })

  it('phone: a bottom sheet on document.body, outside the card', () => {
    phone(true)
    show(asks.credentials)
    const bottom = screen.getByTestId('quick-ask-bottom')
    expect(bottom.parentElement).toBe(document.body)
    expect(screen.getByTestId('browser-live-card').contains(bottom)).toBe(false)
  })
})

describe('quick ask: fold and reopen', () => {
  it('Esc folds to the hint (nothing sent); the hint reopens a fresh sheet', () => {
    show(asks.credentials)
    type(screen.getByTestId('quick-ask-password'), 'hunter2')
    fireEvent.keyDown(screen.getByTestId('quick-ask-sheet'), { key: 'Escape' })
    expect(screen.queryByTestId('quick-ask-sheet')).toBeNull()
    // A sign-in folds to its own card: "Secure sign-in / Sign in to <host>", Enter.
    expect(screen.getByTestId('quick-ask-inline')).toHaveTextContent(t('secure_sign_in'))
    expect(screen.getByTestId('quick-ask-inline')).toHaveAccessibleName(`${t('waiting_hint')}: ${t('title_credentials', { host: 'accounts.example-identity-provider.com:8443' })}`)
    expect(writes.answer).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('quick-ask-inline'))
    // Folding dropped what was typed: a secret never waits in a hidden box.
    expect((screen.getByTestId('quick-ask-password') as HTMLInputElement).value).toBe('')
  })

  it('focus moves into the sheet, but not out of a box the person is typing in', () => {
    show(asks.credentials)
    expect(document.activeElement).toBe(screen.getByTestId('quick-ask-username'))
    cleanup()
    browserLiveStore.__resetForTests()
    askUi.__resetForTests()
    const composer = document.createElement('textarea')
    document.body.appendChild(composer)
    composer.focus()
    show(asks.credentials)
    expect(document.activeElement).toBe(composer)
    composer.remove()
  })

  it('Not now does the same as Esc', () => {
    show(asks.otp)
    fireEvent.click(screen.getByTestId('quick-ask-not-now'))
    expect(screen.getByTestId('quick-ask-inline')).toBeInTheDocument()
  })

  it('a replaced ask while the sheet is open starts empty', () => {
    show(asks.credentials)
    type(screen.getByTestId('quick-ask-password'), 'hunter2')
    act(() => browserLiveStore.applyEvent(ev({ ...asks.credentials, id: 'a1b' })))
    expect((screen.getByTestId('quick-ask-password') as HTMLInputElement).value).toBe('')
  })

  it('unmounting mid-typing empties the inputs', () => {
    const { unmount } = show(asks.credentials)
    const pw = screen.getByTestId('quick-ask-password') as HTMLInputElement
    type(pw, 'hunter2')
    unmount()
    expect(pw.value).toBe('')
  })

  it('an ended turn drops the ask', () => {
    show(asks.credentials)
    act(() => browserLiveStore.applyEvent(ev(undefined, { state: 'done' })))
    expect(screen.queryByTestId('quick-ask-sheet')).toBeNull()
  })
})

describe('quick ask: answer and secrets', () => {
  it('fill posts exactly one answer with the values, empties the boxes, and stores no secret', async () => {
    const log = vi.spyOn(console, 'log')
    const warn = vi.spyOn(console, 'warn')
    const err = vi.spyOn(console, 'error')
    show(asks.credentials)
    type(screen.getByTestId('quick-ask-username'), 'octocat')
    type(screen.getByTestId('quick-ask-password'), 'hunter2-secret')
    fireEvent.click(screen.getByTestId('quick-ask-remember'))
    const pw = screen.getByTestId('quick-ask-password') as HTMLInputElement
    fireEvent.click(screen.getByTestId('quick-ask-primary'))
    expect(pw.value).toBe('')
    await waitFor(() => expect(writes.answer).toHaveBeenCalledTimes(1))
    expect(writes.answer).toHaveBeenCalledWith({ session_key: S, ask_id: 'a1', action: 'fill', values: { username: 'octocat', password: 'hunter2-secret' } })
    await waitFor(() => expect(screen.queryByTestId('quick-ask-sheet')).toBeNull())
    // The username was remembered for this exact origin; the password nowhere.
    expect(recall(ORIGIN, 'username')).toBe('octocat')
    const stored = JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage })
    expect(stored).not.toContain('hunter2-secret')
    expect(window.location.href).not.toContain('hunter2-secret')
    for (const spy of [log, warn, err]) for (const call of spy.mock.calls) expect(JSON.stringify(call)).not.toContain('hunter2-secret')
    // Nor in the store's record of the turn.
    expect(JSON.stringify(browserLiveStore.current(S), (k, v) => (k === 'ring' ? undefined : v))).not.toContain('hunter2-secret')
  })

  it('typing in the sheet reaches neither the takeover proxy nor the page', async () => {
    show(asks.credentials)
    act(() => browserLiveStore.applyFrame(frame(1)))
    const proxy = screen.getByTestId('browser-live-type-proxy') as HTMLTextAreaElement
    const before = proxy.value
    const pw = screen.getByTestId('quick-ask-password') as HTMLInputElement
    pw.focus()
    for (const key of ['h', 'u', 'n', 't', 'Backspace', 'Enter']) {
      fireEvent.keyDown(pw, { key, code: key.length === 1 ? `Key${key.toUpperCase()}` : key })
      fireEvent.keyUp(pw, { key, code: key.length === 1 ? `Key${key.toUpperCase()}` : key })
    }
    pw.dispatchEvent(new InputEvent('beforeinput', { inputType: 'insertText', data: 'hunter2-secret', bubbles: true, cancelable: true }))
    type(pw, 'hunter2-secret')
    expect(document.activeElement).toBe(pw)
    expect(document.activeElement).not.toBe(proxy)
    expect(proxy.value).toBe(before)
    expect(writes.input).not.toHaveBeenCalled()
    expect(writes.answer).not.toHaveBeenCalled()
  })

  it('an otp is posted digits-only and never stored', async () => {
    show(asks.otp)
    type(screen.getByTestId('quick-ask-otp'), '123 456')
    fireEvent.click(screen.getByTestId('quick-ask-primary'))
    await waitFor(() => expect(writes.answer).toHaveBeenCalledWith({ session_key: S, ask_id: 'a3', action: 'fill', values: { otp: '123456' } }))
    expect(localStorage.getItem(__REMEMBER_STORAGE_KEY)).toBeNull()
  })

  it('typed values are capped at the gateway limit, so a paste cannot earn too_large', () => {
    show(asks.credentials)
    expect(screen.getByTestId('quick-ask-password')).toHaveAttribute('maxlength', '1024')
  })

  it('Decline posts decline with no values', async () => {
    show(asks.text)
    fireEvent.click(screen.getByTestId('quick-ask-decline'))
    await waitFor(() => expect(writes.answer).toHaveBeenCalledWith({ session_key: S, ask_id: 'a2', action: 'decline' }))
  })

  it('a saved value is offered as a chip and never filled by itself', () => {
    remember(ORIGIN, 'username', 'octocat')
    remember('https://other.example', 'username', 'someone-else')
    show(asks.credentials)
    const user = screen.getByTestId('quick-ask-username') as HTMLInputElement
    expect(user.value).toBe('')
    const chip = screen.getByTestId('quick-ask-use-saved')
    expect(chip).toHaveTextContent(t('use_saved', { value: 'octocat' }))
    fireEvent.click(chip)
    expect(user.value).toBe('octocat')
    expect(screen.queryByTestId('quick-ask-use-saved')).toBeNull()
  })

  it.each([
    [{ ok: false, error: 'origin_changed' }, 'error_page_changed'],
    [{ ok: false, error: 'stale' }, 'error_page_changed'],
    [{ ok: false, error: 'not_owner' }, 'error_send_failed'],
  ])('a refused answer %j says so plainly and keeps the sheet', async (answer, key) => {
    writes.answer.mockResolvedValueOnce(answer)
    show(asks.otp)
    type(screen.getByTestId('quick-ask-otp'), '123456')
    fireEvent.click(screen.getByTestId('quick-ask-primary'))
    expect(await screen.findByTestId('quick-ask-error')).toHaveTextContent(t(key))
    expect(screen.getByTestId('quick-ask-sheet')).toBeInTheDocument()
  })

  it('a policy that turned browsing off is named, and nothing is filled', async () => {
    writes.answer.mockRejectedValueOnce(Object.assign(new Error('403'), { body: '{"ok":false,"error":"policy_denied","code":"policy_denied"}' }))
    show(asks.text)
    type(screen.getByTestId('quick-ask-text'), 'me@example.com')
    fireEvent.click(screen.getByTestId('quick-ask-primary'))
    expect(await screen.findByTestId('quick-ask-error')).toHaveTextContent(
      i18next.t('pages.settings.browserPanel.policy_off'),
    )
  })

  it('an HTTP refusal is read from its body', async () => {
    writes.answer.mockRejectedValueOnce(Object.assign(new Error('409'), { body: '{"error":"origin_changed"}' }))
    show(asks.text)
    type(screen.getByTestId('quick-ask-text'), 'me@example.com')
    fireEvent.click(screen.getByTestId('quick-ask-primary'))
    expect(await screen.findByTestId('quick-ask-error')).toHaveTextContent(t('error_page_changed'))
  })
})

describe('quick ask: human check takeover', () => {
  it('take_over posts, then the taken_over phase zooms the frame and offers Done', async () => {
    show(asks.human_check)
    act(() => browserLiveStore.applyFrame(frame(1)))
    fireEvent.click(screen.getByTestId('quick-ask-primary'))
    await waitFor(() => expect(writes.answer).toHaveBeenCalledWith({ session_key: S, ask_id: 'a4', action: 'take_over' }))
    act(() => browserLiveStore.applyEvent(ev({ ...asks.human_check, phase: 'taken_over' })))
    expect(screen.queryByTestId('quick-ask-sheet')).toBeNull()
    expect(screen.getByTestId('quick-ask-zoom').style.transform).toContain('scale(')
    expect(screen.getByTestId('browser-live-canvas')).toHaveAttribute('data-interactive', 'true')
    fireEvent.click(screen.getByTestId('quick-ask-done'))
    await waitFor(() => expect(writes.answer).toHaveBeenLastCalledWith({ session_key: S, ask_id: 'a4', action: 'done' }))
  })
})

describe('quick ask: inside an iframe', () => {
  const TOP = 'https://secure9.store.apple.com'
  const IDMSA = 'https://idmsa.apple.com'

  it('names the frame origin and the page it sits in, both in full', () => {
    show({ ...asks.credentials, origin: IDMSA, top_origin: TOP })
    expect(screen.getByTestId('quick-ask-origin-text')).toHaveTextContent(t('origin_inside', { origin: IDMSA, top: TOP }))
    expect(screen.getByRole('heading')).toHaveTextContent(t('title_credentials', { host: 'idmsa.apple.com' }))
    expect(screen.getByTestId('quick-ask-password')).toBeInTheDocument()
  })

  it('readQuickAsk keeps a real top origin and drops a fake or equal one', () => {
    expect(readQuickAsk({ ...asks.credentials, origin: IDMSA, top_origin: TOP })?.top_origin).toBe(TOP)
    expect(readQuickAsk({ ...asks.credentials, origin: IDMSA, top_origin: 'javascript:x' })?.top_origin).toBeUndefined()
    expect(readQuickAsk({ ...asks.credentials, top_origin: ORIGIN })?.top_origin).toBeUndefined()
  })

  it('credentials with no field bound: the sign-in sheet asks both up front and posts them once', async () => {
    const signIn = { ...asks.credentials, id: 'a7', fields: [{ role: 'region', rect }], steps: { username: 'required' } }
    show(signIn)
    const sheet = screen.getByTestId('quick-ask-sheet')
    // The origin is a read-only field of its own; nothing offers to save anything.
    expect(screen.getByTestId('quick-ask-origin-text')).toHaveValue(ORIGIN)
    expect(screen.getByTestId('quick-ask-origin-text')).toHaveAttribute('readonly')
    expect(sheet.querySelector('[type=checkbox]')).toBeNull()
    expect(sheet).toHaveTextContent(t('sign_in_not_saved', { host: 'accounts.example-identity-provider.com:8443' }))
    const user = screen.getByTestId('quick-ask-username') as HTMLInputElement
    const pass = screen.getByTestId('quick-ask-password') as HTMLInputElement
    expect(screen.getByLabelText(t('label_email_or_phone'))).toBe(user)
    // A required username left empty: nothing is sent.
    fireEvent.input(pass, { target: { value: 'pw-canary-1' } })
    fireEvent.click(screen.getByTestId('quick-ask-primary'))
    expect(writes.answer).not.toHaveBeenCalled()
    fireEvent.input(user, { target: { value: 'person@example.org' } })
    writes.answer.mockRejectedValueOnce(Object.assign(new Error('409'), { body: '{"error":"no_field"}' }))
    fireEvent.click(screen.getByTestId('quick-ask-primary'))
    await waitFor(() => expect(writes.answer).toHaveBeenCalledWith({ session_key: S, ask_id: 'a7', action: 'fill', values: { username: 'person@example.org', password: 'pw-canary-1' } }))
    expect(user.value).toBe('')
    expect(pass.value).toBe('')
    await waitFor(() => expect(screen.getByTestId('quick-ask-error')).toHaveTextContent(t('error_no_field')))
    // The way out for what Kiro cannot fill.
    fireEvent.click(screen.getByTestId('quick-ask-take-over'))
    await waitFor(() => expect(writes.answer).toHaveBeenLastCalledWith({ session_key: S, ask_id: 'a7', action: 'take_over' }))
  })

  it('a password-only sign-in screen asks no username; the folded card reads like a sign-in', () => {
    const signIn = { ...asks.credentials, id: 'a8', fields: [{ role: 'region', rect }], steps: { username: 'none' } }
    show(signIn)
    expect(screen.queryByTestId('quick-ask-username')).toBeNull()
    expect(screen.getByTestId('quick-ask-password')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('quick-ask-not-now'))
    const card = screen.getByTestId('quick-ask-inline')
    expect(card).toHaveTextContent(t('secure_sign_in'))
    expect(card).toHaveTextContent(t('title_credentials', { host: 'accounts.example-identity-provider.com:8443' }))
    expect(card).toHaveTextContent(t('enter'))
    expect(readQuickAsk({ ...signIn, steps: { username: 'maybe' } })?.steps).toBeUndefined()
    expect(readQuickAsk({ ...asks.text, steps: { username: 'none' } })?.steps).toBeUndefined()
  })

  it('credentials with no field bound: take over still hands the page over, Done hands back', async () => {
    const signIn = { ...asks.credentials, id: 'a6', fields: [{ role: 'region', rect }] }
    show(signIn)
    act(() => browserLiveStore.applyFrame(frame(1)))
    fireEvent.click(screen.getByTestId('quick-ask-take-over'))
    await waitFor(() => expect(writes.answer).toHaveBeenCalledWith({ session_key: S, ask_id: 'a6', action: 'take_over' }))
    act(() => browserLiveStore.applyEvent(ev({ ...signIn, phase: 'taken_over' })))
    expect(screen.getByTestId('quick-ask-takeover')).toHaveTextContent(t('sign_in_takeover_hint'))
    expect(screen.getByTestId('quick-ask-zoom').style.transform).toContain('scale(')
    expect(screen.getByTestId('browser-live-canvas')).toHaveAttribute('data-interactive', 'true')
    fireEvent.click(screen.getByTestId('quick-ask-done'))
    await waitFor(() => expect(writes.answer).toHaveBeenLastCalledWith({ session_key: S, ask_id: 'a6', action: 'done' }))
  })
})

describe('quick ask: surfaces and parsing', () => {
  it('one surface owns the sheet: the big view outranks the card, an explicit open wins', () => {
    askUi.register('card', 'x', 1)
    askUi.register('stage', 'x', 2)
    expect(askUi.owner('x')).toBe('stage')
    askUi.open('card', 'x')
    expect(askUi.owner('x')).toBe('card')
    askUi.unregister('card')
    expect(askUi.owner('x')).toBe('stage')
  })

  it('readQuickAsk refuses an ask with no real origin or unknown kind', () => {
    expect(readQuickAsk({ ...asks.credentials, origin: 'javascript:alert(1)' })).toBeNull()
    expect(readQuickAsk({ ...asks.credentials, origin: 'https://a.com/path' })).toBeNull()
    expect(readQuickAsk({ ...asks.credentials, kind: 'signin' })).toBeNull()
    expect(readQuickAsk(asks.credentials)?.fields).toHaveLength(2)
  })

  it('a replay person event keeps only its four words', () => {
    // The gateway's shape: op 'other', actor 'person', the four words under `ask`.
    const rec = readRecording({ turn_id: 't1', events: [
      { ts: 5, op: 'other', label: 'typed hunter2', args: { value: 'hunter2' }, point: null, path: null, url: ORIGIN, status: 'ok', actor: 'person', ask: { kind: 'credentials', action: 'fill', outcome: 'filled', origin: ORIGIN } },
      { ts: 6, op: 'other', label: 'not a person', args: {}, actor: 'agent', ask: { kind: 'otp' } },
    ] }, S)!
    const e = rec.events[0]
    expect(e.op).toBe('person')
    expect(e.args).toEqual({})
    expect(e.person).toEqual({ kind: 'credentials', action: 'fill', outcome: 'filled', origin: ORIGIN })
    expect(eventLabel(e)).toBe(t('replay_label', { kind: t('replay_kind_credentials'), action: t('replay_action_fill') }))
    expect(eventLabel(e)).not.toContain('hunter2')
    expect(e.actor).toBe('user')
    expect(rec.events[1].op).toBe('other')
    expect(rec.events[1].person).toBeUndefined()
  })
})
