/**
 * The App SDK's `BrowserView`, `useBrowserView` and `useBrowserCoords`.
 *
 * Driven through `browserLiveStore` as the websocket drives it, with the store's
 * writes stubbed. Covers: the preview off shows "not enabled" and writes nothing;
 * a slot that is not this app's (another app's stamp, or a longer app name that
 * extends this one) shows "denied"; the app's own slot shows the live view, and
 * Take over and Hand back post the same claim and release the chat card does;
 * marks are drawn over the picture; the coordinate mapping round-trips.
 *
 * A hand-back note, from the person's words to where they land (each path has a
 * test that goes red without its fix):
 *   - an agent op takes it: no chat send (BrowserLiveLoopHandback.test.tsx).
 *   - sent and confirmed: once, by the store, as the text and the slot only, from
 *     the chat card (BrowserLiveLoopHandback), an app page, with the chat page's
 *     intake mounted beside it, and with a crewmate pane open
 *     (ChatPane.crewSteerLandsNow.test.tsx); no draft is left.
 *   - refused: kept, "not sent"; into the chat page's composer when it shows the
 *     slot, else the stored drafts (`keepInComposer`).
 *   - answer too late, or the connection dropped: kept, "not confirmed", never
 *     sent again by itself.
 *   - storage full: the notice carries the note itself.
 *   - the view hidden (host surface inactive): it reports itself off screen, so
 *     no takeover is kept alive.
 *   - the tab closed before Show the live page: the refusal shows as an error.
 *   - the tab closed during the takeover and the note then failed: the notice
 *     still shows on the done face.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useRef, type ReactNode } from 'react'
import { Provider } from 'react-redux'
import '../i18n/all'
import { i18nT } from '../i18n/t'
import { BrowserView } from '../app-sdk/index'
import { coordsFor } from '../app-sdk/browserView'
import { useAutoSendIntake } from '../pages/chat/page/launchIntake'
import { AppIdentityProvider } from '../app-sdk/identity'
import { AppScopedApiProvider } from '../app-sdk/scopedApi'
import { browserLiveStore } from '../hooks/useBrowserLive'
import type { DecodedBrowserFrame } from '../hooks/useBrowserLive.frame'
import { PREVIEW_APP_BROWSER_SDK, cachePreviewFlag } from '../utils/previewFlags'
import type { RootState } from '../store'
import { createTestStore } from './helpers'
import { api } from '../api/client'
import { registerMainComposer } from '../utils/composerRestore'
import { __resetForTests as resetDrafts, loadDrafts } from '../utils/chatDrafts'

const OWN = 'foo-1'
const FOREIGN = 'foo-bar-1'
const SLOTS = [
  { key: OWN, title: OWN, messages: 1, running: true, app: 'foo' },
  // Spelled under `foo-` too, but stamped by the app `foo-bar`.
  { key: FOREIGN, title: FOREIGN, messages: 1, running: true, app: 'foo-bar' },
  { key: 'foo-2', title: 'foo-2', messages: 1, running: true, app: '' },
]

const ev = (key: string, over: Record<string, unknown> = {}) => ({
  session_key: key, turn_id: 't1', state: 'live', url: 'https://example.com/', title: 'Example', browser: 'kiro_chromium', headless: true, ...over,
})
const frame = (key: string): DecodedBrowserFrame => ({
  header: { type: 'browser_frame', session_key: key, seq: 1, w: 640, h: 400, mask: [] },
  jpeg: new Uint8Array([1, 2, 3]),
  receivedAt: 1,
})

type Writes = NonNullable<Parameters<typeof browserLiveStore.__setWritesForTests>[0]>
let writes: Required<Pick<Writes, 'ack' | 'visibility' | 'claim' | 'release' | 'input' | 'chat'>>
let restore: ReturnType<typeof browserLiveStore.__setWritesForTests>

beforeEach(() => {
  browserLiveStore.__resetForTests()
  writes = {
    ack: vi.fn(async () => ({ ok: true })),
    visibility: vi.fn(async () => ({ ok: true })),
    claim: vi.fn(async () => ({ ok: true })),
    release: vi.fn(async () => ({ ok: true })),
    input: vi.fn(async () => ({ ok: true, sent: 1 })),
    chat: vi.fn(async () => ({ status: 'dispatched' })),
  }
  restore = browserLiveStore.__setWritesForTests(writes)
  cachePreviewFlag(PREVIEW_APP_BROWSER_SDK, true)
  resetDrafts()
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
})
afterEach(() => {
  // Unmount first: turning the preview off fires its change event into a mounted view.
  cleanup()
  browserLiveStore.__setWritesForTests(restore)
  browserLiveStore.__resetForTests()
  cachePreviewFlag(PREVIEW_APP_BROWSER_SDK, false)
  vi.restoreAllMocks()
})

/** The app `foo`'s page, with the owner-approved browser verbs the host publishes. */
function mount(slot: string, node?: ReactNode, verbs: string[] | null = ['view'], active = true) {
  const defaults = createTestStore().getState()
  const store = createTestStore({ dashboard: { ...defaults.dashboard, slots: SLOTS } as unknown as RootState['dashboard'] })
  return render(
    <Provider store={store}>
      <AppIdentityProvider appId="foo" origin="external">
        <AppScopedApiProvider allowedApiPaths={[]} navigateFn={() => {}} approvedBrowserVerbs={verbs ?? undefined} active={active}>
          {node ?? <BrowserView slot={slot} aria-label="The page" />}
        </AppScopedApiProvider>
      </AppIdentityProvider>
    </Provider>,
  )
}

const view = () => screen.getByTestId('app-browser-view')

describe('BrowserView access', () => {
  it('shows "not enabled" and writes nothing while the preview is off', () => {
    cachePreviewFlag(PREVIEW_APP_BROWSER_SDK, false)
    act(() => { browserLiveStore.applyEvent(ev(OWN)) })
    mount(OWN)
    expect(view().dataset.state).toBe('off')
    expect(view().textContent).toBe(i18nT('appSdk.browserView.not_enabled'))
    expect(writes.visibility).not.toHaveBeenCalled()
  })

  it.each([
    ['declares no browser verbs', null],
    ['holds read but not view', ['read']],
  ])('shows not granted when the owner has not approved view: the app %s', (_why, verbs) => {
    act(() => { browserLiveStore.applyEvent(ev(OWN)) })
    mount(OWN, undefined, verbs)
    expect(view().dataset.state).toBe('not_granted')
    expect(view().textContent).toBe(i18nT('appSdk.browserView.not_granted'))
    expect(writes.visibility).not.toHaveBeenCalled()
  })

  it.each([
    ['another app stamped it, though the key is under this prefix', FOREIGN],
    ['no app stamped it', 'foo-2'],
    ['no such slot', 'foo-9'],
  ])('shows denied when %s', (_why, slot) => {
    act(() => { browserLiveStore.applyEvent(ev(slot)) })
    mount(slot)
    expect(view().dataset.state).toBe('denied')
    expect(view().textContent).toBe(i18nT('appSdk.browserView.denied'))
    expect(screen.queryByTestId('app-browser-view-take-over')).toBeNull()
    expect(writes.visibility).not.toHaveBeenCalled()
  })

})

describe('BrowserView on the app\'s own slot', () => {
  it('shows a placeholder before the slot has browsed', () => {
    mount(OWN)
    expect(view().dataset.state).toBe('idle')
  })

  it('shows the live view, takes over and hands back through the card\'s writes', async () => {
    const onTakeover = vi.fn()
    const onHandBack = vi.fn()
    const onNavigate = vi.fn()
    act(() => { browserLiveStore.applyEvent(ev(OWN)); browserLiveStore.applyFrame(frame(OWN)) })
    mount(OWN, <BrowserView slot={OWN} aria-label="The page" onTakeover={onTakeover} onHandBack={onHandBack} onNavigate={onNavigate} />)
    expect(view().dataset.state).toBe('live')
    expect(screen.getByLabelText('The page', { selector: 'canvas' })).toBeTruthy()
    expect(writes.visibility).toHaveBeenCalledWith(OWN, true)
    expect(onNavigate).toHaveBeenCalledWith({ url: 'https://example.com/', title: 'Example' })

    await act(async () => { fireEvent.click(screen.getByTestId('app-browser-view-take-over')) })
    expect(writes.claim).toHaveBeenCalledWith(OWN)
    expect(view().dataset.state).toBe('in_control')
    expect(onTakeover).toHaveBeenCalledTimes(1)

    await act(async () => { fireEvent.click(screen.getByTestId('app-browser-view-hand-back')) })
    expect(writes.release).toHaveBeenCalledWith(OWN)
    expect(view().dataset.state).toBe('live')
    expect(onHandBack).toHaveBeenCalledTimes(1)
  })

  it('streams a finished turn\'s open tab again on "Show the live page"', async () => {
    const reopen = vi.fn(async () => ({ ok: true }))
    browserLiveStore.__setWritesForTests({ reopen })
    act(() => { browserLiveStore.applyEvent(ev(OWN, { state: 'done', tab: 'open' })) })
    mount(OWN)
    expect(view().dataset.state).toBe('done')
    await act(async () => { fireEvent.click(screen.getByTestId('app-browser-view-show-live')) })
    expect(reopen).toHaveBeenCalledWith(OWN, 't1')
    act(() => { browserLiveStore.applyEvent(ev(OWN, { state: 'live', turn_over: true })) })
    expect(view().dataset.state).toBe('live')
  })

  it('shows a failed browser as an error, in the gateway\'s words', () => {
    act(() => { browserLiveStore.applyEvent(ev(OWN, { state: 'error', detail: 'Browsing is turned off for this chat.' })) })
    mount(OWN)
    expect(view().dataset.state).toBe('error')
    expect(screen.getByTestId('app-browser-view-error').textContent).toContain('Browsing is turned off for this chat.')
  })

  it('does not call onTakeover for a take over the gateway refused', async () => {
    const onTakeover = vi.fn()
    let refuse: (e: Error) => void = () => {}
    writes.claim.mockImplementation(() => new Promise((_resolve, reject) => { refuse = reject }))
    act(() => { browserLiveStore.applyEvent(ev(OWN)); browserLiveStore.applyFrame(frame(OWN)) })
    mount(OWN, <BrowserView slot={OWN} aria-label="The page" onTakeover={onTakeover} />)
    await act(async () => { fireEvent.click(screen.getByTestId('app-browser-view-take-over')) })
    // The store already shows in control while the claim is pending.
    expect(view().dataset.state).toBe('in_control')
    await act(async () => { refuse(new Error('the turn has ended')) })
    expect(writes.claim).toHaveBeenCalledWith(OWN)
    expect(view().dataset.state).toBe('live')
    expect(onTakeover).not.toHaveBeenCalled()
  })

  it('reports itself off screen while its host surface is hidden, so no takeover is kept alive', () => {
    act(() => { browserLiveStore.applyEvent(ev(OWN, { state: 'in_control' })); browserLiveStore.applyFrame(frame(OWN)) })
    mount(OWN, undefined, ['view'], false)
    expect(view().dataset.state).toBe('in_control')
    expect(writes.visibility).not.toHaveBeenCalledWith(OWN, true)
  })

  it('shows a refused Show the live page as an error, even once the tab is marked closed', async () => {
    const body = JSON.stringify({ error: 'the tab is gone', code: 'tab_gone' })
    const reopen = vi.fn(async () => { throw Object.assign(new Error('the tab is gone'), { body }) })
    browserLiveStore.__setWritesForTests({ reopen })
    act(() => { browserLiveStore.applyEvent(ev(OWN, { state: 'done', tab: 'open' })) })
    mount(OWN)
    await act(async () => { fireEvent.click(screen.getByTestId('app-browser-view-show-live')) })
    expect(view().dataset.state).toBe('reopen_failed')
    expect(screen.getByTestId('app-browser-view-reopen-error').textContent).toContain(i18nT('appSdk.browserView.tab_gone'))
  })

  it('offers no takeover when not interactive', () => {
    act(() => { browserLiveStore.applyEvent(ev(OWN)) })
    mount(OWN, <BrowserView slot={OWN} interactive={false} aria-label="The page" />)
    expect(screen.queryByTestId('app-browser-view-take-over')).toBeNull()
  })

  it('draws marks over the picture, as text', () => {
    act(() => { browserLiveStore.applyEvent(ev(OWN)); browserLiveStore.applyFrame(frame(OWN)) })
    mount(OWN, <BrowserView slot={OWN} aria-label="The page" marks={[{ x: 10, y: 20, width: 30, height: 40, label: '<b>Price</b>' }, { x: 5, y: 5 }]} />)
    const layer = screen.getByTestId('app-browser-view-marks')
    expect(layer.children).toHaveLength(2)
    expect((layer.children[0] as HTMLElement).style.width).toBe('30px')
    expect(layer.textContent).toBe('<b>Price</b>')
  })
})

/** The chat page's own hand-back listener (`useAutoSendIntake`), mounted beside the view. */
function ChatPageIntake({ send }: { send: (text?: string, slot?: string) => Promise<boolean> }) {
  const autoSendRef = useRef<string | null>(null)
  const appLaunchSendRef = useRef<{ slotKey?: string } | null>(null)
  const widgetPrefillRef = useRef<string | null>(null)
  useAutoSendIntake({ connected: false, send, autoSendTick: 0, autoSendRef, appLaunchSendRef, widgetPrefillRef, setInput: () => {}, raisePrefillHint: () => {} })
  return null
}

describe('a hand-back note the agent was not waiting for', () => {
  const handBackWithNote = async () => {
    writes.release.mockResolvedValue({ ok: true, delivered: 'chat' })
    await act(async () => { fireEvent.click(screen.getByTestId('app-browser-view-take-over')) })
    const box = screen.getByTestId('browser-live-handback-note')
    fireEvent.change(box, { target: { value: 'I signed in' } })
    await act(async () => { fireEvent.submit(screen.getByTestId('browser-live-handback')) })
  }

  it('is sent to the slot from an app page, where no chat surface listens', async () => {
    act(() => { browserLiveStore.applyEvent(ev(OWN)); browserLiveStore.applyFrame(frame(OWN)) })
    mount(OWN)
    await handBackWithNote()
    expect(writes.release).toHaveBeenCalledWith(OWN, { note: 'I signed in' })
    expect(writes.chat).toHaveBeenCalledTimes(1)
    expect(writes.chat).toHaveBeenCalledWith(OWN, 'I signed in')
    expect(loadDrafts()[OWN]).toBeUndefined()
  })

  it('goes out as its text alone to its own slot, with nothing staged in another chat', async () => {
    const sendChat = vi.spyOn(api, 'sendChat').mockResolvedValue({ ok: true, dispatched: true } as never)
    browserLiveStore.__setWritesForTests({ chat: restore.chat })
    act(() => { browserLiveStore.applyEvent(ev(OWN)); browserLiveStore.applyFrame(frame(OWN)) })
    mount(OWN)
    await handBackWithNote()
    expect(sendChat).toHaveBeenCalledTimes(1)
    const [message, slot, , , meta] = sendChat.mock.calls[0]
    expect(message).toBe('I signed in')
    expect(slot).toBe(OWN)
    expect(meta).toBeUndefined()
  })

  it('keeps a refused note in the slot\'s draft and says so', async () => {
    writes.chat.mockResolvedValue({ status: 'refused', reason: 'Someone else is typing here.' })
    act(() => { browserLiveStore.applyEvent(ev(OWN)); browserLiveStore.applyFrame(frame(OWN)) })
    mount(OWN)
    await handBackWithNote()
    expect(writes.chat).toHaveBeenCalledTimes(1)
    expect(loadDrafts()[OWN]).toContain('I signed in')
    expect(screen.getByTestId('app-browser-view-control-error').textContent).toContain(i18nT('appSdk.browserView.note_not_sent'))
  })

  it('shows the note itself when the browser cannot store the draft', async () => {
    writes.chat.mockResolvedValue({ status: 'refused' })
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation((k: string) => {
      if (k === 'mc-chat-drafts') throw new DOMException('full', 'QuotaExceededError')
    })
    act(() => { browserLiveStore.applyEvent(ev(OWN)); browserLiveStore.applyFrame(frame(OWN)) })
    mount(OWN)
    await handBackWithNote()
    setItem.mockRestore()
    const notice = screen.getByTestId('app-browser-view-control-error').textContent
    expect(notice).toContain('I signed in')
    expect(notice).toContain(i18nT('appSdk.browserView.note_not_sent_not_saved', { note: '' }).trim())
  })

  it('keeps a note whose answer came too late in the slot\'s draft, and does not send it again', async () => {
    writes.chat.mockResolvedValue({ status: 'response-late' })
    act(() => { browserLiveStore.applyEvent(ev(OWN)); browserLiveStore.applyFrame(frame(OWN)) })
    mount(OWN)
    await handBackWithNote()
    expect(writes.chat).toHaveBeenCalledTimes(1)
    expect(loadDrafts()[OWN]).toContain('I signed in')
    expect(screen.getByTestId('app-browser-view-control-error').textContent).toContain(i18nT('appSdk.browserView.note_unconfirmed'))
  })

  it('puts a refused note into the chat page\'s composer when it shows the slot, not under it in storage', async () => {
    writes.chat.mockResolvedValue({ status: 'refused' })
    const composer: string[] = []
    const off = registerMainComposer((slot, text, showingOnly) => {
      if (slot !== OWN || !showingOnly) return false
      composer.push(text)
      return true
    })
    try {
      act(() => { browserLiveStore.applyEvent(ev(OWN)); browserLiveStore.applyFrame(frame(OWN)) })
      mount(OWN)
      await handBackWithNote()
    } finally {
      off()
    }
    expect(composer).toEqual(['I signed in'])
    expect(loadDrafts()[OWN]).toBeUndefined()
    expect(screen.getByTestId('app-browser-view-control-error').textContent).toContain(i18nT('appSdk.browserView.note_not_sent'))
  })

  it('keeps a note whose send was not confirmed (the connection dropped), and does not send it again', async () => {
    writes.chat.mockResolvedValue({ status: 'transport-error' })
    act(() => { browserLiveStore.applyEvent(ev(OWN)); browserLiveStore.applyFrame(frame(OWN)) })
    mount(OWN)
    await handBackWithNote()
    expect(writes.chat).toHaveBeenCalledTimes(1)
    expect(loadDrafts()[OWN]).toContain('I signed in')
    expect(screen.getByTestId('app-browser-view-control-error').textContent).toContain(i18nT('appSdk.browserView.note_unconfirmed'))
  })

  it('still shows a note that did not go out after the tab closed', async () => {
    writes.chat.mockResolvedValue({ status: 'transport-error' })
    // The gateway folds the view to done with the tab closed as it takes the release.
    writes.release.mockImplementation(async () => {
      browserLiveStore.applyEvent(ev(OWN, { state: 'done', tab: 'closed', turn_over: true }))
      return { ok: true, delivered: 'chat' }
    })
    act(() => { browserLiveStore.applyEvent(ev(OWN)); browserLiveStore.applyFrame(frame(OWN)) })
    mount(OWN)
    await act(async () => { fireEvent.click(screen.getByTestId('app-browser-view-take-over')) })
    const box = screen.getByTestId('browser-live-handback-note')
    fireEvent.change(box, { target: { value: 'I signed in' } })
    await act(async () => { fireEvent.submit(screen.getByTestId('browser-live-handback')) })
    expect(view().dataset.state).toBe('done')
    expect(screen.getByTestId('app-browser-view-control-error').textContent).toContain(i18nT('appSdk.browserView.note_unconfirmed'))
  })

  it('is sent once, by the store, when the chat page is open too: the chat page sends nothing itself', async () => {
    const send = vi.fn(async () => true)
    act(() => { browserLiveStore.applyEvent(ev(OWN)); browserLiveStore.applyFrame(frame(OWN)) })
    mount(OWN, <><ChatPageIntake send={send} /><BrowserView slot={OWN} aria-label="The page" /></>)
    await handBackWithNote()
    expect(send).not.toHaveBeenCalled()
    expect(writes.chat).toHaveBeenCalledTimes(1)
    expect(writes.chat).toHaveBeenCalledWith(OWN, 'I signed in')
  })
})

describe('coordsFor', () => {
  it('maps view points to frame pixels and back, across a letterbox', () => {
    // A 400x400 box showing a 800x400 frame: drawn 400x200, 100 px down.
    const c = coordsFor({ width: 400, height: 400, frame: { w: 800, h: 400 } })
    expect(c.toPage(200, 200)).toEqual({ x: 400, y: 200 })
    expect(c.toPage(200, 50)).toBeNull()
    expect(c.toView(400, 200)).toEqual({ x: 200, y: 200 })
  })

  it('answers null before a frame is drawn', () => {
    expect(coordsFor(undefined).toPage(1, 1)).toBeNull()
    expect(coordsFor({ width: 10, height: 10, frame: null }).toView(1, 1)).toBeNull()
  })
})
