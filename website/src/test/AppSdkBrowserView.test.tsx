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
 *
 * The app turned off (rule: no one sees or drives an off app's slot): the gateway
 * refuses every live-view route that keeps a view (test_browser_live_app_off.py);
 * the view shows why at mount, stops at once when the host's re-read app list says
 * off (the gateway's `app_state` frame triggers that re-read in every tab), and a
 * refused take over says why.
 *
 * stayLive: off by default (the fold waits for Show the live page); on, a turn
 * that folds with its tab open is streamed again, once per turn, and not for a
 * closed tab, a hidden view or another app's slot; until the stream is back the
 * picture is held (no done card, no take over), and a refusal ends the hold.
 *
 * Focus: Take over (button, frame click, a second takeover, or the first picture
 * arriving during one) puts the keyboard on the page; a hand back or Show the
 * live page puts it on Take over, unless the person had moved elsewhere.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useRef, type ReactNode } from 'react'
import { Provider } from 'react-redux'
import '../i18n/all'
import { i18nT } from '../i18n/t'
import { BrowserView } from '../app-sdk/index'
import { VIEW_KEEPALIVE_MS, coordsFor, useBrowserView } from '../app-sdk/browserViewHooks'
import { useAutoSendIntake } from '../pages/chat/page/launchIntake'
import { AppIdentityProvider } from '../app-sdk/identity'
import { AppScopedApiProvider } from '../app-sdk/scopedApi'
import { browserLiveStore } from '../hooks/useBrowserLive'
import type { DecodedBrowserFrame } from '../hooks/useBrowserLive.frame'
import { PREVIEW_APP_BROWSER_SDK, cachePreviewFlag } from '../utils/previewFlags'
import type { RootState } from '../store'
import { createTestStore } from './helpers'
import { __resetAppStateForTests } from '../app-sdk/appState'
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
  __resetAppStateForTests()
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
function mount(slot: string, node?: ReactNode, verbs: string[] | null = ['view'], active = true, enabled?: boolean) {
  const defaults = createTestStore().getState()
  const store = createTestStore({ dashboard: { ...defaults.dashboard, slots: SLOTS } as unknown as RootState['dashboard'] })
  return render(
    <Provider store={store}>
      <AppIdentityProvider appId="foo" origin="external">
        <AppScopedApiProvider allowedApiPaths={[]} navigateFn={() => {}} approvedBrowserVerbs={verbs ?? undefined} active={active} appEnabled={enabled}>
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

describe('the app turned off', () => {
  /*
   * Why the first version passed here and failed in the pod: these tests re-rendered
   * the page with `appEnabled={false}`, as if the host re-read the app. The real
   * host (`AppPage`) reads its app once, so `info.enabled` stays true; the view now
   * follows the gateway's `app_state` frame, which the tests below send the way
   * `useWebSocket` relays it, with the host's own read left stale.
   */
  let seq = 0
  const appState = (enabled: boolean, at = ++seq, boot = 'g1') => act(() => {
    window.dispatchEvent(new CustomEvent('mc:app-state', { detail: { app: 'foo', enabled, seq: at, boot } }))
  })

  it('stops a mounted live view on the gateway\'s app_state frame while the host still says on', () => {
    act(() => { browserLiveStore.applyEvent(ev(OWN, { state: 'in_control' })); browserLiveStore.applyFrame(frame(OWN)) })
    mount(OWN, undefined, ['view'], true, true)
    expect(view().dataset.state).toBe('in_control')
    appState(false)
    expect(view().dataset.state).toBe('app_off')
    expect(screen.queryByTestId('browser-live-canvas')).toBeNull()
    expect(writes.visibility).toHaveBeenLastCalledWith(OWN, false)
  })

  it('turns a done face with Show the live page into the app-off face (the pod case)', () => {
    act(() => { browserLiveStore.applyEvent(ev(OWN, { state: 'done', tab: 'open' })) })
    mount(OWN, undefined, ['view'], true, true)
    expect(screen.getByTestId('app-browser-view-show-live')).toBeTruthy()
    appState(false)
    expect(view().dataset.state).toBe('app_off')
    expect(screen.queryByTestId('app-browser-view-show-live')).toBeNull()
  })

  it('comes back when the app is turned on again, even over a host that read it off', () => {
    act(() => { browserLiveStore.applyEvent(ev(OWN)); browserLiveStore.applyFrame(frame(OWN)) })
    mount(OWN, undefined, ['view'], true, false)
    expect(view().dataset.state).toBe('app_off')
    appState(true)
    expect(view().dataset.state).toBe('live')
  })

  it('keeps a newer on when an older off from the same gateway arrives late', () => {
    act(() => { browserLiveStore.applyEvent(ev(OWN)); browserLiveStore.applyFrame(frame(OWN)) })
    mount(OWN, undefined, ['view'], true, true)
    appState(true, 20)
    appState(false, 10)
    expect(view().dataset.state).toBe('live')
  })

  it('takes a smaller seq from a restarted gateway (a new boot)', () => {
    act(() => { browserLiveStore.applyEvent(ev(OWN)); browserLiveStore.applyFrame(frame(OWN)) })
    mount(OWN, undefined, ['view'], true, true)
    appState(true, 20, 'g1')
    appState(false, 5, 'g2')
    expect(view().dataset.state).toBe('app_off')
  })

  it('ignores a frame about another app', () => {
    act(() => { browserLiveStore.applyEvent(ev(OWN)); browserLiveStore.applyFrame(frame(OWN)) })
    mount(OWN, undefined, ['view'], true, true)
    act(() => { window.dispatchEvent(new CustomEvent('mc:app-state', { detail: { app: 'foo-bar', enabled: false, seq: 1, boot: 'g1' } })) })
    expect(view().dataset.state).toBe('live')
  })

  /** The page the host renders, re-rendered with the app list it just re-read. */
  function Page({ enabled }: { enabled: boolean }) {
    return (
      <AppScopedApiProvider allowedApiPaths={[]} navigateFn={() => {}} approvedBrowserVerbs={['view']} appEnabled={enabled}>
        <BrowserView slot={OWN} aria-label="The page" />
      </AppScopedApiProvider>
    )
  }

  it('shows the reason and sends nothing when the app is already off at mount', () => {
    act(() => { browserLiveStore.applyEvent(ev(OWN)); browserLiveStore.applyFrame(frame(OWN)) })
    mount(OWN, undefined, ['view'], true, false)
    expect(view().dataset.state).toBe('app_off')
    expect(view().textContent).toBe(i18nT('appSdk.browserView.app_off'))
    expect(writes.visibility).not.toHaveBeenCalled()
  })

  it('stops a mounted live view at once when the app list says the app is off', () => {
    const defaults = createTestStore().getState()
    const store = createTestStore({ dashboard: { ...defaults.dashboard, slots: SLOTS } as unknown as RootState['dashboard'] })
    act(() => { browserLiveStore.applyEvent(ev(OWN, { state: 'in_control' })); browserLiveStore.applyFrame(frame(OWN)) })
    const tree = (enabled: boolean) => (
      <Provider store={store}>
        <AppIdentityProvider appId="foo" origin="external"><Page enabled={enabled} /></AppIdentityProvider>
      </Provider>
    )
    const { rerender } = render(tree(true))
    expect(view().dataset.state).toBe('in_control')
    expect(writes.visibility).toHaveBeenLastCalledWith(OWN, true)
    rerender(tree(false))
    expect(view().dataset.state).toBe('app_off')
    expect(screen.queryByTestId('browser-live-canvas')).toBeNull()
    // The view no longer reports itself on screen, so the stream stops.
    expect(writes.visibility).toHaveBeenLastCalledWith(OWN, false)
  })

  it('says why when the gateway refuses a take over for an app turned off elsewhere', async () => {
    const body = JSON.stringify({ error: 'the app that owns this chat is turned off', code: 'app_disabled' })
    writes.claim.mockRejectedValue(Object.assign(new Error('the app that owns this chat is turned off'), { body }))
    act(() => { browserLiveStore.applyEvent(ev(OWN)); browserLiveStore.applyFrame(frame(OWN)) })
    mount(OWN)
    await act(async () => { fireEvent.click(screen.getByTestId('app-browser-view-take-over')) })
    expect(view().dataset.state).toBe('live')
    expect(screen.getByTestId('app-browser-view-control-error').textContent).toContain('turned off')
  })
})

describe('where the keyboard goes', () => {
  /** The frame, or the typing proxy it hands focus to: either sends keys to the page. */
  const inFrame = () => {
    const pic = screen.getByTestId('app-browser-view-picture')
    return !!document.activeElement && pic.contains(document.activeElement)
  }
  const takeOver = () => screen.getByTestId('app-browser-view-take-over')

  it('takes the keyboard to the page on Take over, so typing and Esc reach it', async () => {
    act(() => { browserLiveStore.applyEvent(ev(OWN)); browserLiveStore.applyFrame(frame(OWN)) })
    mount(OWN)
    takeOver().focus()
    await act(async () => { fireEvent.click(takeOver()) })
    expect(view().dataset.state).toBe('in_control')
    expect(inFrame()).toBe(true)
    // Esc on the frame hands back, the canvas's own gesture.
    await act(async () => { fireEvent.keyDown(screen.getByTestId('browser-live-canvas'), { key: 'Escape', code: 'Escape' }) })
    expect(writes.release).toHaveBeenCalledWith(OWN)
  })

  it('puts focus on Take over after a hand back, and on the page again for a second takeover', async () => {
    act(() => { browserLiveStore.applyEvent(ev(OWN)); browserLiveStore.applyFrame(frame(OWN)) })
    mount(OWN)
    await act(async () => { fireEvent.click(takeOver()) })
    await act(async () => { fireEvent.click(screen.getByTestId('app-browser-view-hand-back')) })
    expect(view().dataset.state).toBe('live')
    expect(document.activeElement).toBe(takeOver())
    await act(async () => { fireEvent.click(takeOver()) })
    expect(inFrame()).toBe(true)
  })

  it('focuses the page when the first picture arrives during a takeover', async () => {
    act(() => { browserLiveStore.applyEvent(ev(OWN)) })
    mount(OWN)
    await act(async () => { fireEvent.click(takeOver()) })
    expect(view().dataset.state).toBe('in_control')
    act(() => { browserLiveStore.applyFrame(frame(OWN)) })
    expect(inFrame()).toBe(true)
  })

  it('puts focus on Take over when Show the live page brings the page back', async () => {
    const reopen = vi.fn(async () => { browserLiveStore.applyEvent(ev(OWN, { state: 'live', turn_over: true })); return { ok: true } })
    browserLiveStore.__setWritesForTests({ reopen })
    act(() => { browserLiveStore.applyEvent(ev(OWN, { state: 'done', tab: 'open' })) })
    mount(OWN)
    const show = screen.getByTestId('app-browser-view-show-live')
    show.focus()
    await act(async () => { fireEvent.click(show) })
    expect(view().dataset.state).toBe('live')
    expect(document.activeElement).toBe(takeOver())
  })

  it('leaves focus alone when the person is elsewhere as control comes back', async () => {
    act(() => { browserLiveStore.applyEvent(ev(OWN, { state: 'in_control' })); browserLiveStore.applyFrame(frame(OWN)) })
    mount(OWN, <><input data-testid="elsewhere" aria-label="Somewhere else" /><BrowserView slot={OWN} aria-label="The page" /></>)
    const elsewhere = screen.getByTestId('elsewhere')
    elsewhere.focus()
    // The gateway hands control back on its own (an idle bound, another tab).
    act(() => { browserLiveStore.applyEvent(ev(OWN, { state: 'live' })) })
    expect(document.activeElement).toBe(elsewhere)
  })
})

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
    const [message, slot, , , meta, steer, responsive] = sendChat.mock.calls[0]
    expect(message).toBe('I signed in')
    expect(slot).toBe(OWN)
    expect(meta).toBeUndefined()
    // Compared with the crewmate pane's send it replaces: responsive (a Bot's DM
    // gets it now), no steer.
    expect(responsive).toBe(true)
    expect(steer).toBeUndefined()
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

describe('stayLive', () => {
  const stay = <BrowserView slot={OWN} stayLive aria-label="The page" />
  const liveThenFold = (over: Record<string, unknown> = {}) => {
    act(() => { browserLiveStore.applyEvent(ev(OWN)) })
    act(() => { browserLiveStore.applyEvent(ev(OWN, { state: 'done', tab: 'open', ...over })) })
  }

  it('is off by default: a folded turn waits for Show the live page', async () => {
    const reopen = vi.fn(async () => ({ ok: true }))
    browserLiveStore.__setWritesForTests({ reopen })
    mount(OWN)
    await act(async () => { liveThenFold() })
    expect(view().dataset.state).toBe('done')
    expect(screen.getByTestId('app-browser-view-show-live')).toBeTruthy()
    expect(reopen).not.toHaveBeenCalled()
  })

  it('streams the open tab again when the turn folds', async () => {
    const reopen = vi.fn(async () => ({ ok: true }))
    browserLiveStore.__setWritesForTests({ reopen })
    mount(OWN, stay)
    await act(async () => { liveThenFold() })
    expect(reopen).toHaveBeenCalledTimes(1)
    expect(reopen).toHaveBeenCalledWith(OWN, 't1')
    act(() => { browserLiveStore.applyEvent(ev(OWN, { state: 'live', turn_over: true })) })
    expect(view().dataset.state).toBe('live')
  })

  it('stays folded when the tab is closed', async () => {
    const reopen = vi.fn(async () => ({ ok: true }))
    browserLiveStore.__setWritesForTests({ reopen })
    mount(OWN, stay)
    await act(async () => { liveThenFold({ tab: 'closed' }) })
    expect(reopen).not.toHaveBeenCalled()
    expect(view().dataset.state).not.toBe('live')
  })

  it('reopens once per turn: a refusal or a second fold of that turn does not loop', async () => {
    const reopen = vi.fn(async () => { throw new Error('busy') })
    browserLiveStore.__setWritesForTests({ reopen })
    mount(OWN, stay)
    await act(async () => { liveThenFold() })
    expect(reopen).toHaveBeenCalledTimes(1)
    // The same turn shown and folded again (the person folded it elsewhere).
    reopen.mockImplementation(async () => ({ ok: true }))
    await act(async () => { browserLiveStore.applyEvent(ev(OWN, { state: 'live', turn_over: true })) })
    await act(async () => { browserLiveStore.applyEvent(ev(OWN, { state: 'done', tab: 'open' })) })
    expect(reopen).toHaveBeenCalledTimes(1)
    // A later turn that folds gets its own reopen.
    await act(async () => { browserLiveStore.applyEvent(ev(OWN, { turn_id: 't2' })) })
    await act(async () => { browserLiveStore.applyEvent(ev(OWN, { turn_id: 't2', state: 'done', tab: 'open' })) })
    expect(reopen).toHaveBeenCalledTimes(2)
    expect(reopen).toHaveBeenLastCalledWith(OWN, 't2')
  })

  it('does not reopen while its host surface is hidden', async () => {
    const reopen = vi.fn(async () => ({ ok: true }))
    browserLiveStore.__setWritesForTests({ reopen })
    mount(OWN, stay, ['view'], false)
    await act(async () => { liveThenFold() })
    expect(reopen).not.toHaveBeenCalled()
  })

  it('defers a hidden document\'s fold until visible, once per turn', async () => {
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    const reopen = vi.fn(async () => ({ ok: true }))
    browserLiveStore.__setWritesForTests({ reopen })
    mount(OWN, stay)
    await act(async () => { liveThenFold() })
    expect(view().dataset.state).toBe('done')
    expect(reopen).not.toHaveBeenCalled()

    visibility.mockReturnValue('visible')
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')) })
    expect(reopen).toHaveBeenCalledTimes(1)
    expect(reopen).toHaveBeenCalledWith(OWN, 't1')

    visibility.mockReturnValue('hidden')
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')) })
    visibility.mockReturnValue('visible')
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')) })
    expect(reopen).toHaveBeenCalledTimes(1)
  })

  /** A reopen the test answers itself, so the fold can be seen while it is in flight. */
  const pendingReopen = () => {
    let answer: (refusal?: Error) => void = () => {}
    const reopen = vi.fn(() => new Promise<unknown>((resolve, reject) => {
      answer = (refusal) => { if (refusal) reject(refusal); else resolve({ live: true }) }
    }))
    browserLiveStore.__setWritesForTests({ reopen })
    return { reopen, answer: (refusal?: Error) => answer(refusal) }
  }
  const held = () => {
    expect(screen.queryByTestId('app-browser-view-show-live')).toBeNull()
    expect(screen.getByTestId('app-browser-view-picture')).toBeTruthy()
    expect(screen.queryByTestId('app-browser-view-take-over')).toBeNull()
  }

  it('holds the live picture over the fold until the stream is back', async () => {
    const { reopen, answer } = pendingReopen()
    mount(OWN, stay)
    act(() => { browserLiveStore.applyEvent(ev(OWN)); browserLiveStore.applyFrame(frame(OWN)) })
    expect(screen.getByTestId('app-browser-view-take-over')).toBeTruthy()
    act(() => { browserLiveStore.applyEvent(ev(OWN, { state: 'done', tab: 'open' })) })
    expect(reopen).toHaveBeenCalledTimes(1)
    held()
    await act(async () => { answer() })
    held()
    act(() => { browserLiveStore.applyEvent(ev(OWN, { state: 'live', turn_over: true })) })
    expect(view().dataset.state).toBe('live')
    expect(screen.getByTestId('app-browser-view-take-over')).toBeTruthy()
  })

  it.each(['separate', 'batched'] as const)('keeps the next turn held when an earlier reopen is refused (%s turn events)', async (events) => {
    const first = pendingReopen()
    mount(OWN, stay)
    act(() => { browserLiveStore.applyEvent(ev(OWN)); browserLiveStore.applyFrame(frame(OWN)) })
    act(() => { browserLiveStore.applyEvent(ev(OWN, { state: 'done', tab: 'open' })) })
    expect(first.reopen).toHaveBeenCalledWith(OWN, 't1')
    held()

    const second = pendingReopen()
    const start = () => {
      browserLiveStore.applyEvent(ev(OWN, { turn_id: 't2' }))
      browserLiveStore.applyFrame(frame(OWN))
    }
    if (events === 'separate') act(start)
    act(() => {
      if (events === 'batched') start()
      browserLiveStore.applyEvent(ev(OWN, { turn_id: 't2', state: 'done', tab: 'open' }))
    })
    expect(second.reopen).toHaveBeenCalledWith(OWN, 't2')
    held()
    await act(async () => { first.answer(new Error('busy')) })
    held()
    expect(first.reopen).toHaveBeenCalledTimes(1)
    expect(second.reopen).toHaveBeenCalledTimes(1)
    await act(async () => { second.answer() })
  })

  it('ends the hold on a refused reopen and says why', async () => {
    const { answer } = pendingReopen()
    mount(OWN, stay)
    act(() => { browserLiveStore.applyEvent(ev(OWN)) })
    act(() => { browserLiveStore.applyEvent(ev(OWN, { state: 'done', tab: 'open' })) })
    held()
    await act(async () => { answer(new Error('busy')) })
    expect(view().dataset.state).toBe('reopen_failed')
    expect(screen.queryByTestId('app-browser-view-picture')).toBeNull()
  })

  it('folds a reopened turn the person closes again to the done card', async () => {
    const { answer } = pendingReopen()
    mount(OWN, stay)
    act(() => { browserLiveStore.applyEvent(ev(OWN)) })
    act(() => { browserLiveStore.applyEvent(ev(OWN, { state: 'done', tab: 'open' })) })
    held()
    await act(async () => { answer() })
    act(() => { browserLiveStore.applyEvent(ev(OWN, { state: 'live', turn_over: true })) })
    act(() => { browserLiveStore.applyEvent(ev(OWN, { state: 'done', tab: 'open' })) })
    expect(view().dataset.state).toBe('done')
    expect(screen.getByTestId('app-browser-view-show-live')).toBeTruthy()
    expect(screen.queryByTestId('app-browser-view-picture')).toBeNull()
  })

  it('does not reopen another app\'s slot', async () => {
    const reopen = vi.fn(async () => ({ ok: true }))
    browserLiveStore.__setWritesForTests({ reopen })
    act(() => { browserLiveStore.applyEvent(ev(FOREIGN, { state: 'done', tab: 'open' })) })
    mount(FOREIGN, <BrowserView slot={FOREIGN} stayLive aria-label="The page" />)
    await act(async () => {})
    expect(view().dataset.state).toBe('denied')
    expect(reopen).not.toHaveBeenCalled()
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

/*
 * A second view (`tabId`): one of the slot's own tabs beside the slot's own view,
 * for looking only. The gateway's checks (own tab only, preview, `view` verb, the
 * app off) are test_browser_live_views.py; here: it opens only when the page may
 * show the slot, shows the view's picture under the view's key with no take over,
 * says why when the gateway refuses, and closes the view on unmount.
 */
describe('BrowserView second view (tabId)', () => {
  const VIEW = `${OWN}.view1`

  it('opens a look-only view of the tab, shows it under its own key, and closes it on unmount', async () => {
    const open = vi.spyOn(api, 'browserLiveViewOpen').mockResolvedValue({ view_key: VIEW, turn_id: 'v1' })
    const close = vi.spyOn(api, 'browserLiveViewClose').mockResolvedValue({ ok: true } as never)
    act(() => { browserLiveStore.applyEvent(ev(OWN)); browserLiveStore.applyEvent(ev(VIEW, { turn_id: 'v1', url: 'https://b.example/' })) })
    act(() => { browserLiveStore.applyFrame(frame(VIEW)) })
    const { unmount } = mount(OWN, <BrowserView slot={OWN} tabId="TB" aria-label="Tab B" />)
    expect(open).toHaveBeenCalledWith(OWN, 'TB')
    await act(async () => {})
    expect(view().dataset.state).toBe('live')
    expect(view().textContent).toContain('b.example')
    expect(screen.queryByTestId('app-browser-view-take-over')).toBeNull()
    expect(writes.visibility).toHaveBeenLastCalledWith(VIEW, true)
    unmount()
    // close is called with the key and the turn_id returned by open.
    expect(close).toHaveBeenCalledWith(VIEW, 'v1')
  })

  it('keeps a view on screen looked at, and opens it again once the gateway ended it', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    try {
      const open = vi.spyOn(api, 'browserLiveViewOpen').mockResolvedValue({ view_key: VIEW, turn_id: 'v1' })
      vi.spyOn(api, 'browserLiveViewClose').mockResolvedValue({ ok: true } as never)
      // The keepalive now posts directly to the API (not through the store), so
      // it always refreshes the gateway's idle timer even for a still page.
      const keepAlive = vi.spyOn(api, 'browserLiveVisibility').mockResolvedValue({ ok: true } as never)
      act(() => { browserLiveStore.applyEvent(ev(VIEW, { turn_id: 'v1' })); browserLiveStore.applyFrame(frame(VIEW)) })
      mount(OWN, <BrowserView slot={OWN} tabId="TB" aria-label="Tab B" />)
      await act(async () => {})
      await act(async () => { vi.advanceTimersByTime(VIEW_KEEPALIVE_MS) })
      expect(keepAlive).toHaveBeenCalledWith(VIEW, true)
      // A reloaded page's leftover is ended by the gateway once idle; a view that
      // was ended while mounted is opened again.
      expect(open).toHaveBeenCalledTimes(1)
      act(() => { browserLiveStore.applyEvent(ev(VIEW, { turn_id: 'v1', state: 'done' })) })
      await act(async () => {})
      expect(open).toHaveBeenCalledTimes(2)
    } finally {
      vi.useRealTimers()
    }
  })

  it('opens nothing for a slot the page may not show', () => {
    const open = vi.spyOn(api, 'browserLiveViewOpen').mockResolvedValue({ view_key: 'x' })
    mount(FOREIGN, <BrowserView slot={FOREIGN} tabId="TB" aria-label="Tab B" />)
    expect(view().dataset.state).toBe('denied')
    expect(open).not.toHaveBeenCalled()
  })

  it("says why when the gateway refuses the tab", async () => {
    vi.spyOn(api, 'browserLiveViewOpen').mockRejectedValue(new Error('that tab cannot be shown in a view'))
    mount(OWN, <BrowserView slot={OWN} tabId="NOT-MINE" aria-label="Tab" />)
    await act(async () => {})
    expect(view().dataset.state).toBe('view_error')
    expect(view().textContent).toContain('that tab cannot be shown in a view')
  })

  it("lists the slot's own tabs through useBrowserView(slot).tabs()", async () => {
    vi.spyOn(api, 'browserLiveViewTabs').mockResolvedValue({ tabs: [{ tab_id: 'TA', url: 'https://a/', title: 'A', current: true }, { bad: 1 }] })
    let got: unknown = null
    function Probe() {
      const v = useBrowserView(OWN)
      return <button onClick={() => { void v.tabs().then((t) => { got = t }) }}>list</button>
    }
    mount(OWN, <Probe />)
    await act(async () => { fireEvent.click(screen.getByText('list')) })
    expect(got).toEqual([{ tabId: 'TA', url: 'https://a/', title: 'A', current: true }])
  })

  it('drops the store session and frame rings on unmount', async () => {
    vi.spyOn(api, 'browserLiveViewOpen').mockResolvedValue({ view_key: VIEW, turn_id: 'v1' })
    vi.spyOn(api, 'browserLiveViewClose').mockResolvedValue({ ok: true } as never)
    // Spy on forgetView to confirm it is called on unmount.
    const forget = vi.spyOn(browserLiveStore, 'forgetView')
    act(() => { browserLiveStore.applyEvent(ev(VIEW, { turn_id: 'v1' })); browserLiveStore.applyFrame(frame(VIEW)) })
    const { unmount } = mount(OWN, <BrowserView slot={OWN} tabId="TB" aria-label="Tab B" />)
    await act(async () => {})
    // Session was created by the backend open.
    expect(forget).not.toHaveBeenCalled()
    unmount()
    await act(async () => {})
    // After unmount forgetView must have been called for VIEW.
    expect(forget).toHaveBeenCalledWith(VIEW)
  })

  it('a late done event does not recreate a forgotten view session', async () => {
    // After forgetView, a late `done` event from the gateway must not recreate
    // the session for other connected owner tabs, so their frame rings are released.
    act(() => { browserLiveStore.applyEvent(ev(VIEW, { turn_id: 'v1' })) })
    expect((browserLiveStore as unknown as { sessions: Map<string, unknown> }).sessions.has(VIEW)).toBe(true)
    browserLiveStore.forgetView(VIEW)
    expect((browserLiveStore as unknown as { sessions: Map<string, unknown> }).sessions.has(VIEW)).toBe(false)
    // A late `done` event arrives -- the backend always sends one when the view ends.
    act(() => { browserLiveStore.applyEvent(ev(VIEW, { turn_id: 'v1', state: 'done' })) })
    // The session must NOT be recreated (tombstone prevents it).
    expect((browserLiveStore as unknown as { sessions: Map<string, unknown> }).sessions.has(VIEW)).toBe(false)
  })
})

describe('applyEvent ring eviction scope', () => {
  const VIEW2 = `${OWN}.view1234`  // a typical secondary-view key (registered via reviveView)

  it('a secondary_view marker in the event registers the key in secondaryViewKeys', () => {
    // Other dashboard tabs receive secondary_view: true from the gateway; they must
    // register the key so their ring eviction runs on done, without the opening tab
    // calling reviveView on their behalf.
    const otherViewKey = `${OWN}.viewabcd`
    act(() => {
      browserLiveStore.applyEvent({ ...ev(otherViewKey, { turn_id: 'w1' }), secondary_view: true })
    })
    type S = { secondaryViewKeys: Set<string> }
    expect((browserLiveStore as unknown as S).secondaryViewKeys.has(otherViewKey)).toBe(true)
    // A done event must now evict the ring.
    act(() => { browserLiveStore.applyFrame(frame(otherViewKey)) })
    type SS = { sessions: Map<string, { turns: Map<string, { ring: { length: number } }> }> }
    const before = (browserLiveStore as unknown as SS).sessions.get(otherViewKey)?.turns.get('w1')?.ring?.length
    expect(before).toBeGreaterThan(0)
    act(() => { browserLiveStore.applyEvent({ ...ev(otherViewKey, { turn_id: 'w1', state: 'done' }), secondary_view: true }) })
    const after = (browserLiveStore as unknown as SS).sessions.get(otherViewKey)?.turns.get('w1')?.ring?.length
    expect(after).toBe(0)
  })
  it('a done event does not clear rings for an ordinary slot whose name contains .view', () => {
    // The ring-eviction must only apply to keys in secondaryViewKeys (registered by
    // reviveView on a successful /views/open), not to ordinary slots whose name
    // happens to match any pattern. Here "foo-1.viewing" is an ordinary slot -- it
    // is never passed to reviveView, so its ring must survive a done event.
    const ordinarySlot = 'foo-1.viewing'
    act(() => { browserLiveStore.applyEvent(ev(ordinarySlot, { turn_id: 't1' })) })
    act(() => { browserLiveStore.applyFrame(frame(ordinarySlot)) })
    type S = { sessions: Map<string, { turns: Map<string, { ring: { length: number } }> }> }
    const s = (browserLiveStore as unknown as S).sessions.get(ordinarySlot)
    const ringBefore = s?.turns.get('t1')?.ring
    expect(ringBefore?.length).toBeGreaterThan(0)
    // Apply a `done` event -- ring must survive for ordinary slot.
    act(() => { browserLiveStore.applyEvent(ev(ordinarySlot, { turn_id: 't1', state: 'done' })) })
    const ringAfter = s?.turns.get('t1')?.ring
    expect(ringAfter).toBe(ringBefore)
    expect(ringAfter?.length).toBeGreaterThan(0)
  })

  it('a keepalive refusal renders an error on the second view', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    try {
      vi.spyOn(api, 'browserLiveViewOpen').mockResolvedValue({ view_key: VIEW2, turn_id: 'v1' })
      vi.spyOn(api, 'browserLiveViewClose').mockResolvedValue({ ok: true } as never)
      // Keepalive is refused -- this happens when capabilities.browse is revoked.
      vi.spyOn(api, 'browserLiveVisibility').mockRejectedValue(new Error('policy_denied'))
      act(() => { browserLiveStore.applyEvent(ev(VIEW2, { turn_id: 'v1' })); browserLiveStore.applyFrame(frame(VIEW2)) })
      mount(OWN, <BrowserView slot={OWN} tabId="TB" aria-label="Tab B" />)
      await act(async () => {})
      await act(async () => { vi.advanceTimersByTime(VIEW_KEEPALIVE_MS) })
      await act(async () => {})
      // After a keepalive refusal the view should show an error notice.
      expect(view().dataset.state).toBe('view_error')
    } finally {
      vi.useRealTimers()
    }
  })

  it('a keepalive error clears after a successful heartbeat', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    try {
      const VIEW3 = `${OWN}.view9999`
      vi.spyOn(api, 'browserLiveViewOpen').mockResolvedValue({ view_key: VIEW3, turn_id: 'v2' })
      vi.spyOn(api, 'browserLiveViewClose').mockResolvedValue({ ok: true } as never)
      const visibilityMock = vi.spyOn(api, 'browserLiveVisibility')
        .mockRejectedValueOnce(new Error('policy_denied'))
        .mockResolvedValue({ ok: true } as never)
      act(() => { browserLiveStore.applyEvent(ev(VIEW3, { turn_id: 'v2' })); browserLiveStore.applyFrame(frame(VIEW3)) })
      mount(OWN, <BrowserView slot={OWN} tabId="TB" aria-label="Tab B" />)
      await act(async () => {})
      // First tick: rejection sets error.
      await act(async () => { vi.advanceTimersByTime(VIEW_KEEPALIVE_MS) })
      await act(async () => {})
      expect(view().dataset.state).toBe('view_error')
      // Second tick: success clears error.
      await act(async () => { vi.advanceTimersByTime(VIEW_KEEPALIVE_MS) })
      await act(async () => {})
      expect(view().dataset.state).not.toBe('view_error')
      void visibilityMock  // suppress unused var
    } finally {
      vi.useRealTimers()
    }
  })
})
