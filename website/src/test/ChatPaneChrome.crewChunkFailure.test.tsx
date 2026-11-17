/**
 * A failed chunk load of the lazy crew window stays inside the crew cover.
 *
 * The crew window is drawn over the chat pane and loads in its own chunk. When
 * that chunk fails to load, the failure must stay inside the cover: the sessions
 * sidebar and the chat page around it stay mounted, so the person can pick
 * another session. The cover's guard keeps holding while the failure shows, as
 * it does while the chunk loads, so the window still counts as shown (the
 * close-chat shortcut closes it, not the covered local chat). Try Again must run
 * the import again, because React.lazy caches a rejected import, and Reload page
 * is the way back for a browser that keeps failing the same chunk. Ask the agent
 * opens a local chat under the cover, so it must close the window too: with no
 * local chat active before, nothing else closes it, and the chat it opened would
 * sit under the cover with its composer inert.
 */
import '../i18n/all'
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import ChatPaneChrome from '../pages/chat/page/ChatPaneChrome'
import ErrorBoundary from '../components/ErrorBoundary'
import { crewWindowShown } from '../pages/chat/crew-window/crewWindowStore'
import { i18nT } from '../i18n/t'

const chunk = vi.hoisted(() => ({ fail: true, imports: 0 }))

// The window's module as its chunk: an import that rejects the way a browser
// rejects a module script it could not fetch, until the test restores it.
const crewWindowChunk = vi.hoisted(() => () => {
  chunk.imports += 1
  if (chunk.fail) throw new TypeError('Importing a module script failed.')
  return { default: () => <div data-testid="crew-chat-window" /> }
})
vi.mock('../pages/chat/crew-window/CrewChatWindow', crewWindowChunk)
vi.mock('../components/SnipOverlay', () => ({ default: () => null }))
vi.mock('../pages/chat/page/ChatPaneNotices', () => ({ default: () => null }))
vi.mock('../components/SessionTabStrip', () => ({ default: () => null }))

/** A named ceiling for the lazy import, its rejection and the boundary's commit on a loaded worker. */
const CHUNK_SETTLE_TIMEOUT_MS = 5000

type PaneProps = Parameters<typeof ChatPaneChrome>[0]
const paneProps = {
  messages: [], sessionTabs: { tabs: [], cue: null }, isMobile: false,
  crewWindow: { instanceId: 'i1', key: 'k1' }, embedded: false, navigate: vi.fn(),
} as unknown as PaneProps

beforeEach(() => {
  chunk.fail = true
  chunk.imports = 0
})
afterEach(() => { vi.restoreAllMocks() })

describe('ChatPaneChrome when the crew window chunk fails to load', () => {
  it('keeps the failure inside the crew cover, and Try Again imports the chunk again', async () => {
    // React logs the caught render error; the boundary handling it is the point.
    vi.spyOn(console, 'error').mockImplementation(() => {})
    render(
      // The chat route's own boundary (App.tsx), standing in for "the whole
      // chat page was replaced".
      <ErrorBoundary fallback={<div data-testid="chat-route-crashed" />}>
        <aside data-testid="sessions-sidebar" />
        <div data-testid="chat-pane" className="relative">
          <ChatPaneChrome {...paneProps} />
          <div data-testid="local-transcript" />
        </div>
      </ErrorBoundary>,
    )
    const tryAgain = await screen.findByRole(
      'button', { name: i18nT('components.errorBoundary.try_again') }, { timeout: CHUNK_SETTLE_TIMEOUT_MS },
    ).catch(() => null)
    expect(chunk.imports).toBe(1)

    expect.soft(screen.queryByTestId('chat-route-crashed'), 'the failure replaced the whole chat page').toBeNull()
    expect.soft(screen.queryByTestId('sessions-sidebar'), 'the sessions sidebar survives').toBeInTheDocument()
    expect.soft(
      tryAgain?.closest('[data-crew-cover]') ?? null,
      'the failure shows inside the crew cover',
    ).not.toBeNull()
    expect.soft(
      screen.queryByText(i18nT('pages.chat.crewWindow.window_load_failed')),
      'the failure names what could not load',
    ).toBeInTheDocument()
    expect.soft(crewWindowShown(), 'the cover\'s guard still holds while the failure shows').toBe(true)
    const reload = screen.queryByRole('button', { name: i18nT('components.errorBoundary.reload_page') })
    expect.soft(reload, 'the failure offers Reload page').toBeInTheDocument()
    if (reload) {
      const original = window.location
      const reloadSpy = vi.fn()
      Object.defineProperty(window, 'location', { configurable: true, value: { ...original, reload: reloadSpy } })
      try {
        fireEvent.click(reload)
      } finally {
        Object.defineProperty(window, 'location', { configurable: true, value: original })
      }
      expect(reloadSpy).toHaveBeenCalledTimes(1)
    }

    expect(tryAgain, 'the failure offers Try Again').not.toBeNull()
    chunk.fail = false
    fireEvent.click(tryAgain!)
    expect(
      await screen.findByTestId('crew-chat-window', {}, { timeout: CHUNK_SETTLE_TIMEOUT_MS }),
      'Try Again mounts the crew window once its chunk loads',
    ).toBeInTheDocument()
    expect(chunk.imports, 'Try Again runs the import again').toBe(2)
  })

  it('closes the crew window when Ask the agent hands the failure to a new chat', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    // A fresh module graph, so the window's chunk starts unloaded whichever case
    // ran first. A mocked module's load outlives resetModules, and registering
    // its factory again drops it.
    vi.resetModules()
    vi.doMock('../pages/chat/crew-window/CrewChatWindow', crewWindowChunk)
    const { default: FreshPaneChrome } = await import('../pages/chat/page/ChatPaneChrome')
    const { openCrewWindow, useCrewWindow } = await import('../pages/chat/crew-window/crewWindowStore')
    const { installSoftNavigate, __resetNavSeamForTests } = await import('../utils/errorReport')
    const navigated: string[] = []
    installSoftNavigate(to => { navigated.push(to) })
    onTestFinished(() => { __resetNavSeamForTests() })
    // The full chat page hands the pane the store's window, so closing the
    // window takes the cover down. No local chat is active.
    function Pane() {
      const crewWindow = useCrewWindow()
      return (
        <div data-testid="chat-pane" className="relative">
          <FreshPaneChrome {...paneProps} activeSlot={null} crewWindow={crewWindow} />
          <div data-testid="local-transcript" />
        </div>
      )
    }
    openCrewWindow({ instanceId: 'i1', key: 'k1' })
    render(<Pane />)

    const ask = await screen.findByRole(
      'button', { name: i18nT('components.askAgent.ask_the_agent') }, { timeout: CHUNK_SETTLE_TIMEOUT_MS },
    )
    expect(ask.closest('[data-crew-cover]'), 'the failure offers the hand-off inside the cover').not.toBeNull()
    expect(screen.getByTestId('local-transcript'), 'the cover holds the chat behind it inert').toHaveAttribute('inert')
    fireEvent.click(ask)
    expect(navigated, 'the hand-off goes ahead to the chat').toEqual(['/chat'])
    await waitFor(() => {
      expect(document.querySelector('[data-crew-cover]'), 'the hand-off closes the crew cover').toBeNull()
    }, { timeout: CHUNK_SETTLE_TIMEOUT_MS })
    expect(screen.getByTestId('local-transcript'), 'the chat the hand-off opened is usable').not.toHaveAttribute('inert')
  })
})
