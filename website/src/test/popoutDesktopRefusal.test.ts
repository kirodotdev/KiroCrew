import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

// The shell flag is read once at import, so each case sets it through this
// mutable mock before calling the controller.
const shell = vi.hoisted(() => ({ isElectron: false }))
vi.mock('../lib/electron', () => ({
  get isElectron() { return shell.isElectron },
}))
vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => false) }))

import { isEmbeddedPane } from '../lib/embedded'
import { noteHostCrewPopout, noteHostOrigin, setHostElectronReader } from '../lib/hostRelay'
import { openPopout, getSnapshot, returnSelfToMain, __setNavigateForTests, __resetForTests } from '../utils/chatPopout'
import { i18nT } from '../i18n/t'

class StubChannel {
  onmessage: unknown = null
  postMessage() { /* noop */ }
  close() { /* noop */ }
}

const BROWSER = 'utils.popoutController.your_browser_blocked_the_pop_out_window_allow_po'
const DESKTOP = 'utils.popoutController.desktop_app_could_not_open_pop_out_window'
const PANE = 'utils.popoutController.crew_pane_cannot_pop_out_in_desktop_app'
const HUB = 'http://127.0.0.1:7777'

describe('pop-out refusal message names the real cause', () => {
  let alert: ReturnType<typeof vi.spyOn>
  beforeEach(() => {
    vi.stubGlobal('BroadcastChannel', StubChannel as unknown as typeof BroadcastChannel)
    alert = vi.spyOn(window, 'alert').mockImplementation(() => {})
    vi.spyOn(console, 'warn').mockImplementation(() => {})
  })
  afterEach(() => {
    shell.isElectron = false
    vi.mocked(isEmbeddedPane).mockReturnValue(false)
    setHostElectronReader(null)
    noteHostOrigin(null)
    noteHostCrewPopout(false)
    __resetForTests()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('keeps the pop-up advice in a browser', () => {
    vi.spyOn(window, 'open').mockReturnValue(null)
    openPopout('chat-1')
    expect(alert).toHaveBeenCalledWith(i18nT(BROWSER))
  })

  it('does not tell a desktop-app user to allow pop-ups', () => {
    shell.isElectron = true
    vi.spyOn(window, 'open').mockReturnValue(null)
    openPopout('chat-1')
    expect(alert).toHaveBeenCalledWith(i18nT(DESKTOP))
    expect(i18nT(DESKTOP)).not.toMatch(/pop-ups/i)
    expect(getSnapshot().has('chat-1')).toBe(false)
  })

  it('opens the hub page that frames the pop-out from a crew pane of the desktop app', () => {
    vi.mocked(isEmbeddedPane).mockReturnValue(true)
    setHostElectronReader(() => true)
    noteHostOrigin(HUB)
    noteHostCrewPopout(true)
    const fakeWin = { closed: false, focus: vi.fn() } as unknown as Window
    const open = vi.spyOn(window, 'open').mockReturnValue(fakeWin)
    openPopout('chat-1')
    expect(open).toHaveBeenCalledTimes(1)
    const url = new URL(String(open.mock.calls[0][0]))
    expect(url.origin + url.pathname).toBe(`${HUB}/popout/crew`)
    expect(url.searchParams.get('port')).toBe(window.location.port)
    expect(url.searchParams.get('path')).toBe('/popout/chat?sid=chat-1')
    expect(alert).not.toHaveBeenCalled()
    expect(getSnapshot().has('chat-1')).toBe(true)
    // A second click focuses the window it opened instead of opening another.
    openPopout('chat-1')
    expect(open).toHaveBeenCalledTimes(1)
    expect(fakeWin.focus).toHaveBeenCalledTimes(2)
  })

  it('keeps the notice in a desktop-app crew pane that has no hub origin yet', () => {
    vi.mocked(isEmbeddedPane).mockReturnValue(true)
    setHostElectronReader(() => true)
    const open = vi.spyOn(window, 'open')
    openPopout('chat-1')
    expect(open).not.toHaveBeenCalled()
    expect(alert).toHaveBeenCalledWith(i18nT(PANE))
    expect(getSnapshot().has('chat-1')).toBe(false)
  })

  it('keeps the notice in a desktop-app crew pane whose hub did not advertise the framing page', () => {
    // An older desktop hub: it sends `electron` but serves no /popout/crew.
    vi.mocked(isEmbeddedPane).mockReturnValue(true)
    setHostElectronReader(() => true)
    noteHostOrigin(HUB)
    const open = vi.spyOn(window, 'open')
    openPopout('chat-1')
    expect(open).not.toHaveBeenCalled()
    expect(alert).toHaveBeenCalledWith(i18nT(PANE))
    expect(getSnapshot().has('chat-1')).toBe(false)
  })

  it('asks the desktop hub to close the window a framed pop-out returns from', () => {
    vi.mocked(isEmbeddedPane).mockReturnValue(true)
    setHostElectronReader(() => true)
    noteHostOrigin(HUB)
    const post = vi.spyOn(window.parent, 'postMessage').mockImplementation(() => {})
    const navigate = vi.fn()
    __setNavigateForTests(navigate)
    returnSelfToMain()
    expect(post).toHaveBeenCalledWith({ type: 'mc-close-popout', v: 1 }, HUB)
    expect(navigate).not.toHaveBeenCalled()
  })

  it('keeps the navigate fallback for a pop-out with no host', () => {
    const post = vi.spyOn(window.parent, 'postMessage').mockImplementation(() => {})
    vi.spyOn(window, 'close').mockImplementation(() => {})
    const navigate = vi.fn()
    __setNavigateForTests(navigate)
    returnSelfToMain()
    expect(post).not.toHaveBeenCalled()
    expect(navigate).toHaveBeenCalledWith('/chat')
  })

  it('still opens from a crew pane whose hub is a browser', () => {
    vi.mocked(isEmbeddedPane).mockReturnValue(true)
    setHostElectronReader(() => false)
    const fakeWin = { closed: false, focus: vi.fn() } as unknown as Window
    const open = vi.spyOn(window, 'open').mockReturnValue(fakeWin)
    openPopout('chat-1')
    expect(open).toHaveBeenCalledTimes(1)
    expect(alert).not.toHaveBeenCalled()
    expect(getSnapshot().has('chat-1')).toBe(true)
  })
})
