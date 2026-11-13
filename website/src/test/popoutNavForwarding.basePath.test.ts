import type { PopoutController, PopoutMsg } from '../utils/popoutController'

// Under a sub-path build (KIROCREW_BASE_PATH) the no-main fallback tab must open
// the destination under the build base: `intent.path` is in app space (`/chat`),
// and a tab at `https://host/chat` is outside the `/crew` mount, where the
// prompt the pop-out already removed from its own storage would be lost. The
// base mock is file-level (vitest hoists it), which is why this lives apart from
// `popoutNavForwarding.test.ts`, whose cases run at the root base.

const BASE = '/crew'

vi.mock('../lib/basePath', async (importOriginal) => {
  const real = await importOriginal<typeof import('../lib/basePath')>()
  return { ...real, BASE_PATH: BASE, withBase: (p: string) => real.withBase(p, BASE) }
})

const { createPopoutController, NAV_CLAIM_MS } = await import('../utils/popoutController')

/** Minimal BroadcastChannel stand-in: no main window ever answers, so the fallback fires. */
class SilentChannel {
  onmessage: ((e: { data: PopoutMsg }) => void) | null = null
  constructor(public name: string) {}
  postMessage(): void {}
  close(): void {}
}

const controllers: PopoutController[] = []
function controller(): PopoutController {
  const c = createPopoutController({
    channelName: 'test-nav-base',
    logLabel: 'testNavBase',
    buildUrl: id => `/popout/thing/${id}`,
    windowName: id => `mc-test-${id}`,
    mainViewUrl: id => (id ? `/things/${id}` : '/things'),
  })
  controllers.push(c)
  return c
}

beforeEach(() => {
  vi.stubGlobal('BroadcastChannel', SilentChannel as unknown as typeof BroadcastChannel)
  vi.useFakeTimers()
})

afterEach(() => {
  controllers.splice(0).forEach(c => c.__resetForTests())
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('forwardToMain no-main fallback under a sub-path build', () => {
  it('opens the destination under the build base, query intact', () => {
    const popout = controller()
    const opened: Array<[string, string]> = []
    popout.__setWindowOpenForTests((url, target) => opened.push([url, target]))

    popout.forwardToMain({ path: '/chat', search: '?autoSend=1', pendingInput: 'Open a session here' })
    vi.advanceTimersByTime(NAV_CLAIM_MS)
    expect(opened).toEqual([
      [`${window.location.origin}${BASE}/chat?autoSend=1&prefill=Open+a+session+here&new=1`, '_blank'],
    ])
  })

  it('prefixes a bare path and the root with the base', () => {
    const popout = controller()
    const opened: string[] = []
    popout.__setWindowOpenForTests(url => opened.push(url))

    popout.forwardToMain({ path: '/artifacts' })
    vi.advanceTimersByTime(NAV_CLAIM_MS)
    popout.forwardToMain({ path: '/' })
    vi.advanceTimersByTime(NAV_CLAIM_MS)
    expect(opened).toEqual([
      `${window.location.origin}${BASE}/artifacts`,
      `${window.location.origin}${BASE}/`,
    ])
  })
})
