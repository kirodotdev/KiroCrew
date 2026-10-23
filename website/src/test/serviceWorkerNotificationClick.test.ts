import { afterEach, describe, it, expect, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import path from 'node:path'

// Clicking a pushed notification must never replace a page that is already
// open: a tab can hold unsaved work (a settings draft lives only in React
// state), and WindowClient.navigate() reloads the document with no page-side
// guard. These tests run the REAL sw.js notificationclick handler.

const SW_PATH = path.resolve(__dirname, '..', '..', 'public', 'sw.js')
const ORIGIN = 'https://dash.example'

type FakeClient = {
  url: string
  visibilityState: 'visible' | 'hidden'
  focus: ReturnType<typeof vi.fn>
  navigate: ReturnType<typeof vi.fn>
  postMessage: ReturnType<typeof vi.fn>
}

// How a fake tab answers the worker's push probe: `connected` / `disconnected`
// reply on the transferred port like lib/swConnectedProbe.ts does; `silent` is an
// older page (or a frozen one) that never answers.
type ProbeAnswer = 'connected' | 'disconnected' | 'silent'

function client(
  url: string,
  visibilityState: 'visible' | 'hidden' = 'hidden',
  answer: ProbeAnswer = 'silent',
): FakeClient {
  const postMessage = vi.fn((msg: { type?: string }, ports?: MessagePort[]) => {
    if (answer === 'silent' || msg?.type !== 'kc-push-probe' || !ports?.[0]) return
    ports[0].postMessage({ connected: answer === 'connected' })
  })
  return {
    url,
    visibilityState,
    focus: vi.fn(() => Promise.resolve()),
    navigate: vi.fn(() => Promise.resolve()),
    postMessage,
  }
}

const MOCHI_PANEL = `${ORIGIN}/app-windows/mochi/panel.html`

function loadWorker(clients: FakeClient[]) {
  const listeners: Record<string, (e: unknown) => void> = {}
  const openWindow = vi.fn((_url: string) => Promise.resolve())
  const showNotification = vi.fn((_title: string, _opts: unknown) => Promise.resolve())
  const fakeSelf = {
    addEventListener: (kind: string, fn: (e: unknown) => void) => { listeners[kind] = fn },
    location: { origin: ORIGIN },
    skipWaiting: () => {},
    clients: { claim: () => {}, matchAll: async () => clients, openWindow },
    registration: { showNotification },
  }
  new Function('self', 'caches', 'fetch', readFileSync(SW_PATH, 'utf8'))(
    fakeSelf, { open: async () => ({ addAll: async () => {} }), keys: async () => [] }, () => Promise.resolve({}),
  )
  return { listeners, openWindow, showNotification }
}

async function push(clients: FakeClient[]) {
  const { listeners, showNotification } = loadWorker(clients)
  let pending: Promise<unknown> = Promise.resolve()
  listeners.push({
    data: { json: () => ({ web_push: 8030, notification: { title: 'Job done', body: 'b', tag: 't1' } }) },
    waitUntil: (p: Promise<unknown>) => { pending = p },
  })
  await pending
  return showNotification
}

async function click(data: unknown, clients: FakeClient[]) {
  const { listeners, openWindow } = loadWorker(clients)
  let pending: Promise<unknown> = Promise.resolve()
  const close = vi.fn()
  listeners.notificationclick({
    notification: { close, data },
    waitUntil: (p: Promise<unknown>) => { pending = p },
  })
  await pending
  expect(close).toHaveBeenCalledTimes(1)
  return { openWindow }
}

describe('sw.js notificationclick', () => {
  it('opens a deep link in its own window instead of navigating an open tab', async () => {
    const settings = client(`${ORIGIN}/settings/browser`)
    const { openWindow } = await click({ url: '/chat/abc' }, [settings])
    expect(settings.navigate).not.toHaveBeenCalled()
    expect(settings.focus).not.toHaveBeenCalled()
    expect(openWindow).toHaveBeenCalledWith('/chat/abc')
  })

  it('focuses a tab that already shows the target', async () => {
    const other = client(`${ORIGIN}/settings/browser`)
    const chat = client(`${ORIGIN}/chat/abc`)
    const { openWindow } = await click({ url: '/chat/abc' }, [other, chat])
    expect(chat.focus).toHaveBeenCalledTimes(1)
    expect(other.focus).not.toHaveBeenCalled()
    expect(chat.navigate).not.toHaveBeenCalled()
    expect(openWindow).not.toHaveBeenCalled()
  })

  it('focuses an open tab for a root click without navigating it', async () => {
    const settings = client(`${ORIGIN}/settings/browser`)
    const { openWindow } = await click(undefined, [settings])
    expect(settings.focus).toHaveBeenCalledTimes(1)
    expect(settings.navigate).not.toHaveBeenCalled()
    expect(openWindow).not.toHaveBeenCalled()
  })

  it('opens a window when no tab is open', async () => {
    const { openWindow } = await click({ navigate: '/notifications' }, [])
    expect(openWindow).toHaveBeenCalledWith('/notifications')
  })
})

describe('sw.js push', () => {
  // The no-answer case drives the probe's timeout with fake timers; restore them
  // after every test so a failing assertion cannot leak them into the next one.
  afterEach(() => {
    vi.useRealTimers()
  })

  it('shows the banner when only a standalone app window is visible', async () => {
    const showNotification = await push([client(MOCHI_PANEL, 'visible'), client(`${ORIGIN}/chat/abc`, 'hidden')])
    expect(showNotification).toHaveBeenCalledTimes(1)
    expect(showNotification.mock.calls[0][0]).toBe('Job done')
  })

  it('suppresses the banner when a visible dashboard tab says it is connected', async () => {
    const panel = client(MOCHI_PANEL, 'visible', 'connected')
    const tab = client(`${ORIGIN}/chat/abc`, 'visible', 'connected')
    const showNotification = await push([client(MOCHI_PANEL, 'hidden'), panel, tab])
    expect(tab.postMessage).toHaveBeenCalledTimes(1)
    // A standalone app window is never asked: it does not render general notes.
    expect(panel.postMessage).not.toHaveBeenCalled()
    expect(showNotification).not.toHaveBeenCalled()
  })

  it('shows the banner when a visible dashboard tab is disconnected', async () => {
    // Its sign-in expired: the shell is served but /api/ws was refused, so the
    // tab never receives the note. Suppressing here would lose it.
    const showNotification = await push([client(`${ORIGIN}/`, 'visible', 'disconnected')])
    expect(showNotification).toHaveBeenCalledTimes(1)
    expect(showNotification.mock.calls[0][1]).toMatchObject({ tag: 't1' })
  })

  it('shows the banner when a visible dashboard tab never answers', async () => {
    vi.useFakeTimers()
    const pending = push([client(`${ORIGIN}/`, 'visible', 'silent')])
    await vi.advanceTimersByTimeAsync(600)
    const showNotification = await pending
    expect(showNotification).toHaveBeenCalledTimes(1)
  })

  it('suppresses when any visible dashboard tab is connected', async () => {
    const showNotification = await push([
      client(`${ORIGIN}/`, 'visible', 'disconnected'),
      client(`${ORIGIN}/chat/x`, 'visible', 'connected'),
    ])
    expect(showNotification).not.toHaveBeenCalled()
  })

  it.each(['/popout/terminal', '/popout/chat/abc', '/embed/chat/abc', '/embed/sessions'])(
    'shows the banner when only a connected %s window is visible',
    async (pathname) => {
      // These layouts run the socket but mount no notification banner, so a
      // "connected" answer from them must not swallow the push.
      const frame = client(`${ORIGIN}${pathname}`, 'visible', 'connected')
      const showNotification = await push([frame])
      expect(frame.postMessage).not.toHaveBeenCalled()
      expect(showNotification).toHaveBeenCalledTimes(1)
    },
  )

  it('a root click opens the dashboard instead of focusing a popout or embed window', async () => {
    const popout = client(`${ORIGIN}/popout/terminal`, 'visible')
    const embed = client(`${ORIGIN}/embed/chat/abc`, 'visible')
    const { openWindow } = await click(undefined, [popout, embed])
    expect(popout.focus).not.toHaveBeenCalled()
    expect(embed.focus).not.toHaveBeenCalled()
    expect(openWindow).toHaveBeenCalledWith('/')
  })

  it('never probes a hidden tab', async () => {
    const hidden = client(`${ORIGIN}/`, 'hidden', 'connected')
    const showNotification = await push([hidden])
    expect(hidden.postMessage).not.toHaveBeenCalled()
    expect(showNotification).toHaveBeenCalledTimes(1)
  })

  it('a root click focuses a dashboard tab, never an app window', async () => {
    const panel = client(MOCHI_PANEL, 'visible')
    const chat = client(`${ORIGIN}/chat/abc`)
    const { openWindow } = await click(undefined, [panel, chat])
    expect(panel.focus).not.toHaveBeenCalled()
    expect(chat.focus).toHaveBeenCalledTimes(1)
    expect(openWindow).not.toHaveBeenCalled()
  })

  it('a root click with only an app window open opens the dashboard', async () => {
    const panel = client(MOCHI_PANEL, 'visible')
    const { openWindow } = await click(undefined, [panel])
    expect(panel.focus).not.toHaveBeenCalled()
    expect(openWindow).toHaveBeenCalledWith('/')
  })
})
