import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import i18next from 'i18next'
import '../i18n/all'

import BrowserLiveCard from '../pages/chat/BrowserLiveCard'
import { browserLiveStore } from '../hooks/useBrowserLive'
import { askUi } from '../pages/chat/quickAsk/askUi'
import { SHEET_PIN_MIN_PX } from '../pages/chat/quickAsk/QuickAskSheet'
import { recall, remember } from '../pages/chat/quickAsk/remember'
import { readQuickAsk } from '../pages/chat/quickAsk/types'
import { PREVIEW_SECRETS_VAULT, setPreviewFlag } from '../utils/previewFlags'

/**
 * The sign-in sheet's Secrets vault part (preview `PREVIEW_SECRETS_VAULT`):
 * matching logins offered by name and filled only on a click, which sends the
 * NAME (the gateway fills the values); with no match an off-by-default "Save
 * to vault" box; a save that did not land said by the card after the sheet is
 * gone; and with the preview off, the sheet exactly as before.
 */

const S = 'slot:qav'
const ORIGIN = 'https://github.com'
const t = (k: string, o?: Record<string, unknown>) => i18next.t(`pages.chat.quickAsk.${k}`, o)
const rect = { x: 100, y: 120, w: 200, h: 40 }
const creds = (vault: unknown) => ({
  id: 'v1', kind: 'credentials', origin: ORIGIN, reason: 'Sign in', vault,
  fields: [{ role: 'username', rect }, { role: 'password', input: 'password', rect: { ...rect, y: 180 } }],
  frame: { w: 640, h: 400 }, expires_at: 9e12, phase: 'waiting',
})
const ev = (ask: unknown) => ({ session_key: S, turn_id: 't1', state: 'live', url: `${ORIGIN}/login`, title: 'Sign in', browser: 'kiro_chromium', headless: true, ask })

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
  setPreviewFlag(PREVIEW_SECRETS_VAULT, true)
})
afterEach(() => {
  cleanup()
  setPreviewFlag(PREVIEW_SECRETS_VAULT, false)
  browserLiveStore.__setWritesForTests(restore)
  browserLiveStore.__resetForTests()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function show(ask: unknown) {
  act(() => browserLiveStore.applyEvent(ev(ask)))
  return render(<BrowserLiveCard sessionKey={S} anchorKey="call:1" anchorTs={null} running goTo={() => {}} />)
}

const lg = (name: string, username = '') => ({ name, username })
const labels = () => screen.getAllByTestId('quick-ask-vault-login').map(b => b.textContent)

describe('quick ask: logins from the vault', () => {
  it('reads only short names and usernames on a credentials ask', () => {
    expect(readQuickAsk(creds({ items: [lg('GitHub', 'alice')] }))?.vault).toEqual({ items: [lg('GitHub', 'alice')] })
    expect(readQuickAsk(creds({ items: ['GitHub'] }))?.vault).toBeUndefined()
    expect(readQuickAsk(creds({ items: [lg('x'.repeat(121))] }))?.vault).toBeUndefined()
    expect(readQuickAsk(creds({ items: [lg('GitHub', 'u'.repeat(121))] }))?.vault).toBeUndefined()
    expect(readQuickAsk({ ...creds({ items: [lg('GitHub')] }), kind: 'text', fields: [{ role: 'text', rect }] })?.vault).toBeUndefined()
  })

  it('labels each login by its username, and a click sends its entry name only', async () => {
    show(creds({ items: [lg('GitHub', 'alice'), lg('GitHub 2', 'bob')] }))
    expect(labels()).toEqual(['alice', 'bob'])
    fireEvent.click(screen.getAllByTestId('quick-ask-vault-login')[1])
    await waitFor(() => expect(writes.answer).toHaveBeenCalledTimes(1))
    expect(writes.answer).toHaveBeenCalledWith({ session_key: S, ask_id: 'v1', action: 'fill', vault_item: 'GitHub 2' })
  })

  it('a login with no username is labelled by its entry name', () => {
    show(creds({ items: [lg('GitHub')] }))
    expect(labels()).toEqual(['GitHub'])
  })

  it('a username two logins share also shows each entry name, on those two only', () => {
    show(creds({ items: [lg('GitHub', 'alice'), lg('GitHub work', 'alice'), lg('GitLab', 'bob')] }))
    expect(labels()).toEqual(['aliceGitHub', 'aliceGitHub work', 'bob'])
  })

  it.each([
    [0, 0, false, false],
    [1, 1, false, true],
    [3, 3, false, true],
    [5, 3, true, true],
  ])('%i matches: %i login buttons, More %s, Other account %s', (count, shown, moreRow, otherRow) => {
    show(creds({ items: Array.from({ length: count }, (_, i) => lg(`Site ${i}`, `user${i}`)) }))
    expect(screen.queryAllByTestId('quick-ask-vault-login')).toHaveLength(shown)
    expect(!!screen.queryByTestId('quick-ask-vault-more-row')).toBe(moreRow)
    expect(!!screen.queryByTestId('quick-ask-other-account')).toBe(otherRow)
    // With a match the typed fields wait behind Other account; with none they show.
    expect(!!screen.queryByTestId('quick-ask-password')).toBe(count === 0)
    if (moreRow) {
      fireEvent.click(screen.getByTestId('quick-ask-vault-more-row'))
      expect(screen.getAllByTestId('quick-ask-vault-login')).toHaveLength(count)
    }
  })

  it('Other account opens the fields and an unticked Save to Secrets store, and can save a second account', async () => {
    show(creds({ items: [lg('GitHub', 'alice')] }))
    expect(screen.queryByTestId('quick-ask-primary')).toBeNull()
    fireEvent.click(screen.getByTestId('quick-ask-other-account'))
    const box = screen.getByTestId('quick-ask-save-to-vault') as HTMLInputElement
    expect(box.checked).toBe(false)
    fireEvent.click(box)
    fireEvent.input(screen.getByTestId('quick-ask-username'), { target: { value: 'bob' } })
    fireEvent.input(screen.getByTestId('quick-ask-password'), { target: { value: 'pw-2' } })
    fireEvent.click(screen.getByTestId('quick-ask-primary'))
    await waitFor(() => expect(writes.answer).toHaveBeenCalledTimes(1))
    expect(writes.answer).toHaveBeenCalledWith({ session_key: S, ask_id: 'v1', action: 'fill', values: { username: 'bob', password: 'pw-2' }, save: true })
  })

  it('counts logins past the gateway cap once all are shown', () => {
    show(creds({ items: Array.from({ length: 4 }, (_, i) => lg(`Site ${i}`, `user${i}`)), more: 2 }))
    expect(screen.queryByTestId('quick-ask-vault-more')).toBeNull()
    fireEvent.click(screen.getByTestId('quick-ask-vault-more-row'))
    expect(screen.getByTestId('quick-ask-vault-more')).toHaveTextContent(t('vault_more', { count: 2 }))
  })

  it('with no match, Save to Secrets store is off until ticked, then rides on the typed answer', async () => {
    show(creds({ items: [] }))
    expect(screen.queryByTestId('quick-ask-vault-login')).toBeNull()
    const box = screen.getByTestId('quick-ask-save-to-vault') as HTMLInputElement
    expect(box.checked).toBe(false)
    fireEvent.click(box)
    fireEvent.input(screen.getByTestId('quick-ask-username'), { target: { value: 'octocat' } })
    fireEvent.input(screen.getByTestId('quick-ask-password'), { target: { value: 'pw-1' } })
    fireEvent.click(screen.getByTestId('quick-ask-primary'))
    await waitFor(() => expect(writes.answer).toHaveBeenCalledTimes(1))
    expect(writes.answer).toHaveBeenCalledWith({ session_key: S, ask_id: 'v1', action: 'fill', values: { username: 'octocat', password: 'pw-1' }, save: true })
  })

  it('a save that did not land is said on the card once the ask is gone, until dismissed', async () => {
    writes.answer.mockResolvedValueOnce({ ok: true, outcome: 'filled', saved: false })
    show(creds({ items: [] }))
    fireEvent.click(screen.getByTestId('quick-ask-save-to-vault'))
    fireEvent.input(screen.getByTestId('quick-ask-password'), { target: { value: 'pw-1' } })
    fireEvent.click(screen.getByTestId('quick-ask-primary'))
    await waitFor(() => expect(writes.answer).toHaveBeenCalledTimes(1))
    act(() => browserLiveStore.applyEvent(ev(null)))
    const notice = await screen.findByTestId('quick-ask-vault-unsaved')
    expect(notice).toHaveTextContent(t('error_vault_not_saved'))
    // The turn ends and the card folds to its finished row: the notice stays.
    act(() => browserLiveStore.applyEvent({ ...ev(null), state: 'done' }))
    expect(screen.getByTestId('quick-ask-vault-unsaved')).toHaveTextContent(t('error_vault_not_saved'))
    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    await waitFor(() => expect(screen.queryByTestId('quick-ask-vault-unsaved')).toBeNull())
  })

  it('a save that did not land is said even when the turn ended before the answer returned', async () => {
    let finish: (v: { ok: true; outcome: string; saved: false }) => void = () => {}
    writes.answer.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    show(creds({ items: [] }))
    fireEvent.click(screen.getByTestId('quick-ask-save-to-vault'))
    fireEvent.input(screen.getByTestId('quick-ask-password'), { target: { value: 'pw-1' } })
    fireEvent.click(screen.getByTestId('quick-ask-primary'))
    await waitFor(() => expect(writes.answer).toHaveBeenCalledTimes(1))
    act(() => browserLiveStore.applyEvent({ ...ev(null), state: 'done' }))
    await act(async () => { finish({ ok: true, outcome: 'filled', saved: false }) })
    expect(await screen.findByTestId('quick-ask-vault-unsaved')).toHaveTextContent(t('error_vault_not_saved'))
  })

  it('a login the gateway cannot use says so in plain words, and the sheet stays', async () => {
    writes.answer.mockResolvedValueOnce({ ok: false, error: 'vault_unusable' })
    show(creds({ items: [lg('GitHub', 'alice')] }))
    fireEvent.click(screen.getByTestId('quick-ask-vault-login'))
    expect(await screen.findByTestId('quick-ask-error')).toHaveTextContent(t('error_vault_unusable'))
    expect(screen.getByTestId('quick-ask-sheet')).toBeInTheDocument()
  })

  it('keeps the answer row pinned while typing, with no match or with Other account open', () => {
    // jsdom has no layout: clientHeight 0 would read as a short sheet.
    const ch = vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(SHEET_PIN_MIN_PX + 80)
    show(creds({ items: [] }))
    expect(screen.getByTestId('quick-ask-actions')).toHaveClass('sticky', 'bottom-0')
    ch.mockRestore()
  })

  it('shows the more-below fade on the answer row only while the sheet hides content', () => {
    // jsdom has no layout: give the sheet a scroll box taller than its window.
    const sh = vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(600)
    const ch = vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(200)
    show(creds({ items: [] }))
    // A short sheet's row scrolls away, so the fade sits on the sheet's bottom edge.
    expect(screen.getByTestId('quick-ask-more-below')).toBeInTheDocument()
    expect(screen.getByTestId('quick-ask-actions')).not.toContainElement(screen.getByTestId('quick-ask-more-below'))
    sh.mockReturnValue(200)
    fireEvent.scroll(screen.getByTestId('quick-ask-sheet'))
    expect(screen.queryByTestId('quick-ask-more-below')).toBeNull()
    ch.mockRestore()
    sh.mockRestore()
  })

  it.each([
    [SHEET_PIN_MIN_PX + 80, true],
    [SHEET_PIN_MIN_PX - 80, false],
  ])('a %ipx sheet pins its answer row while typing: %s', (height, pinned) => {
    const ch = vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(height)
    const sh = vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(height + 300)
    show(creds({ items: [] }))
    const row = screen.getByTestId('quick-ask-actions')
    expect(row.hasAttribute('data-pinned')).toBe(pinned)
    expect(row.classList.contains('sticky')).toBe(pinned)
    // One fade either way: on the pinned row, else on the sheet's bottom edge.
    expect(screen.getAllByTestId('quick-ask-more-below')).toHaveLength(1)
    expect(row.contains(screen.getByTestId('quick-ask-more-below'))).toBe(pinned)
    sh.mockRestore()
    ch.mockRestore()
  })

  it('does not pin a Decline-only row beside the login buttons', () => {
    const ch = vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(SHEET_PIN_MIN_PX + 80)
    show(creds({ items: [lg('GitHub', 'alice')] }))
    expect(screen.getByTestId('quick-ask-actions').hasAttribute('data-pinned')).toBe(false)
    fireEvent.click(screen.getByTestId('quick-ask-other-account'))
    expect(screen.getByTestId('quick-ask-actions').hasAttribute('data-pinned')).toBe(true)
    ch.mockRestore()
  })

  it('puts More and Other account on one row', () => {
    show(creds({ items: Array.from({ length: 5 }, (_, i) => lg(`Site ${i}`, `user${i}`)) }))
    expect(screen.getByTestId('quick-ask-vault-more-row').parentElement).toBe(screen.getByTestId('quick-ask-other-account').parentElement)
  })

  it('1: offers one save box: Save to Secrets store, never Remember beside it', () => {
    show(creds({ items: [] }))
    expect(screen.getByTestId('quick-ask-save-to-vault')).toBeInTheDocument()
    expect(screen.queryByTestId('quick-ask-remember')).toBeNull()
  })

  it('says where Save to Secrets store keeps the login, beside the box', () => {
    show(creds({ items: [] }))
    const box = screen.getByTestId('quick-ask-save-to-vault').closest('label')?.parentElement
    expect(box?.querySelector(`[aria-label="${i18next.t('components.infoTip.more_information')}"]`)).not.toBeNull()
    expect(t('save_to_vault_tip')).toMatch(/Secrets/)
  })

  it('2: keeps a remembered username: chip on no match, untouched by a fill', async () => {
    remember(ORIGIN, 'username', 'kept-user')
    show(creds({ items: [] }))
    expect(screen.getByTestId('quick-ask-use-saved')).toHaveTextContent(t('use_saved', { value: 'kept-user' }))
    fireEvent.input(screen.getByTestId('quick-ask-username'), { target: { value: 'other-user' } })
    fireEvent.input(screen.getByTestId('quick-ask-password'), { target: { value: 'pw-1' } })
    fireEvent.click(screen.getByTestId('quick-ask-primary'))
    await waitFor(() => expect(writes.answer).toHaveBeenCalledTimes(1))
    expect(recall(ORIGIN, 'username')).toBe('kept-user')
  })

  it('3: a vault match shows its rows, not the remembered chip, even with Other account open', () => {
    remember(ORIGIN, 'username', 'kept-user')
    show(creds({ items: [lg('GitHub', 'alice')] }))
    expect(screen.getByTestId('quick-ask-vault-login')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('quick-ask-other-account'))
    expect(screen.queryByTestId('quick-ask-use-saved')).toBeNull()
    expect(screen.queryByTestId('quick-ask-remember')).toBeNull()
    expect(recall(ORIGIN, 'username')).toBe('kept-user')
  })

  it('4: Remember is untouched with the preview off and on a text ask', () => {
    setPreviewFlag(PREVIEW_SECRETS_VAULT, false)
    const { unmount } = show(creds({ items: [] }))
    expect(screen.getByTestId('quick-ask-remember')).toBeInTheDocument()
    unmount()
    act(() => browserLiveStore.applyEvent(ev(null)))
    setPreviewFlag(PREVIEW_SECRETS_VAULT, true)
    show({ ...creds(undefined), id: 'v2', kind: 'text', fields: [{ role: 'text', input: 'email', label: 'email', rect }] })
    expect(screen.getByTestId('quick-ask-remember')).toBeInTheDocument()
    expect(screen.queryByTestId('quick-ask-save-to-vault')).toBeNull()
  })

  it('with the preview off the sheet is as before', () => {
    setPreviewFlag(PREVIEW_SECRETS_VAULT, false)
    show(creds({ items: [lg('GitHub', 'alice')] }))
    expect(screen.queryByTestId('quick-ask-vault-login')).toBeNull()
    expect(screen.queryByTestId('quick-ask-other-account')).toBeNull()
    expect(screen.queryByTestId('quick-ask-save-to-vault')).toBeNull()
    expect(screen.getByTestId('quick-ask-password')).toBeInTheDocument()
  })
})
