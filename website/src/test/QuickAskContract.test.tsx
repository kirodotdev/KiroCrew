import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import '../i18n/all'

import BrowserLiveCard from '../pages/chat/BrowserLiveCard'
import { browserLiveStore } from '../hooks/useBrowserLive'
import { readRecording } from '../hooks/useBrowserLive.replay'
import { askUi } from '../pages/chat/quickAsk/askUi'
import { readQuickAsk } from '../pages/chat/quickAsk/types'
import contract from './fixtures/quickAskContract.json'

/**
 * The card's half of the quick-ask wire contract. The gateway's half
 * (test/test_browser_quick_ask_contract.py) proves it publishes these asks
 * exactly and takes these answer bodies; this proves the card reads every
 * ask whole, posts exactly these bodies, and reads the replay person event.
 */

type Kind = keyof typeof contract.asks
const S = contract.session_key
const wire = (kind: Kind) => contract.asks[kind].wire
const body = (name: string) => contract.answers.find(a => a.name === name)!.body

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

beforeEach(() => {
  browserLiveStore.__resetForTests()
  askUi.__resetForTests()
  localStorage.clear()
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
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: false, media: q, onchange: null,
    addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, dispatchEvent: () => false,
  }))
})
afterEach(() => {
  cleanup()
  browserLiveStore.__setWritesForTests(restore)
  browserLiveStore.__resetForTests()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function show(ask: unknown) {
  const o = (ask as { origin: string }).origin
  act(() => browserLiveStore.applyEvent({ session_key: S, turn_id: 't1', state: 'live', url: `${o}/page`, title: 'Page', browser: 'kiro_chromium', headless: true, ask }))
  return render(<BrowserLiveCard sessionKey={S} anchorKey="call:1" anchorTs={null} running goTo={() => {}} />)
}
const type = (id: string, v: string) => { fireEvent.input(screen.getByTestId(id), { target: { value: v } }) }
const posted = (kind: Kind, name: string) =>
  waitFor(() => expect(writes.answer).toHaveBeenLastCalledWith({ session_key: S, ask_id: wire(kind).id, ...body(name) }))

describe('quick ask contract: the gateway ask, read whole', () => {
  it.each(Object.keys(contract.asks) as Kind[])('%s keeps every field the gateway sends', kind => {
    expect(readQuickAsk(wire(kind))).toEqual(wire(kind))
  })
})

describe('quick ask contract: the answer bodies the gateway takes', () => {
  it('credentials with both boxes', async () => {
    show(wire('credentials'))
    type('quick-ask-username', 'octocat')
    type('quick-ask-password', 'hunter2-secret')
    fireEvent.click(screen.getByTestId('quick-ask-primary'))
    await posted('credentials', 'credentials_both')
  })

  it('credentials with the optional username left empty', async () => {
    show(wire('credentials'))
    type('quick-ask-password', 'hunter2-secret')
    fireEvent.click(screen.getByTestId('quick-ask-primary'))
    await posted('credentials', 'credentials_password_only')
  })

  it('text fill and decline', async () => {
    show(wire('text'))
    type('quick-ask-text', 'me@example.com')
    fireEvent.click(screen.getByTestId('quick-ask-primary'))
    await posted('text', 'text_fill')
    cleanup()
    browserLiveStore.__resetForTests()
    askUi.__resetForTests()
    show(wire('text'))
    fireEvent.click(screen.getByTestId('quick-ask-decline'))
    await posted('text', 'text_decline')
  })

  it('otp fill', async () => {
    show(wire('otp'))
    type('quick-ask-otp', '123456')
    fireEvent.click(screen.getByTestId('quick-ask-primary'))
    await posted('otp', 'otp_fill')
  })

  it('human_check take_over, then done from the taken_over bar', async () => {
    show({ ...wire('human_check'), phase: 'waiting' })
    fireEvent.click(screen.getByTestId('quick-ask-primary'))
    await posted('human_check', 'human_check_take_over')
    act(() => browserLiveStore.applyEvent({ session_key: S, turn_id: 't1', state: 'live', url: `${wire('human_check').origin}/page`, title: 'Page', browser: 'kiro_chromium', headless: true, ask: wire('human_check') }))
    fireEvent.click(screen.getByTestId('quick-ask-done'))
    await posted('human_check', 'human_check_done')
  })

  it('confirm and its cancel', async () => {
    show(wire('confirm'))
    fireEvent.click(screen.getByTestId('quick-ask-primary'))
    await posted('confirm', 'confirm_confirm')
    cleanup()
    browserLiveStore.__resetForTests()
    askUi.__resetForTests()
    show(wire('confirm'))
    fireEvent.click(screen.getByTestId('quick-ask-decline'))
    await posted('confirm', 'confirm_cancel')
  })
})

describe('quick ask contract: the replay person event', () => {
  it('is read as a person step with its four words', () => {
    const { event, kind, action, outcome } = contract.person_event
    const rec = readRecording({ turn_id: 't1', events: [event] }, S)!
    expect(rec.events[0].op).toBe('person')
    expect(rec.events[0].person).toEqual({ kind, action, outcome, origin: event.url })
  })
})
