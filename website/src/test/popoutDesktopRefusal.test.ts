import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

// The shell flag is read once at import, so each case sets it through this
// mutable mock before calling the controller.
const shell = vi.hoisted(() => ({ isElectron: false }))
vi.mock('../lib/electron', () => ({
  get isElectron() { return shell.isElectron },
}))
vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => false) }))

import { isEmbeddedPane } from '../lib/embedded'
import { setHostElectronReader } from '../lib/hostRelay'
import { openPopout, getSnapshot, __resetForTests } from '../utils/chatPopout'
import { i18nT } from '../i18n/t'

class StubChannel {
  onmessage: unknown = null
  postMessage() { /* noop */ }
  close() { /* noop */ }
}

const BROWSER = 'utils.popoutController.your_browser_blocked_the_pop_out_window_allow_po'
const DESKTOP = 'utils.popoutController.desktop_app_could_not_open_pop_out_window'
const PANE = 'utils.popoutController.crew_pane_cannot_pop_out_in_desktop_app'

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

  it('refuses up front in a crew pane of the desktop app, so no tokenless URL reaches the system browser', () => {
    vi.mocked(isEmbeddedPane).mockReturnValue(true)
    setHostElectronReader(() => true)
    const open = vi.spyOn(window, 'open')
    openPopout('chat-1')
    expect(open).not.toHaveBeenCalled()
    expect(alert).toHaveBeenCalledWith(i18nT(PANE))
    expect(getSnapshot().has('chat-1')).toBe(false)
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
