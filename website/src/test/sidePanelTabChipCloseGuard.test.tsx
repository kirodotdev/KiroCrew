/**
 * A file tab's chip × and middle-click close through that tab's own panel
 * guard, not around it.
 *
 * `MarkdownPanel`'s close guard (`guardedClose`, reached from outside through
 * `MarkdownPanelHandle.requestClose`) is what asks before an unsaved buffer, a
 * buffer whose file is gone or unreadable, or an open comment draft is
 * discarded. `SidePanel` keeps each mounted file tab's handle and routes the
 * chip's × and middle-click through it (`closeTabFromChip`), so:
 *
 * 1. The chip's × and its middle-click both ask the tab's guard, and the tab
 *    stays when the guard does not close it (the user answered Cancel).
 * 2. A close the guard allows without asking removes the tab and leaves the
 *    strip's focus on the tab the user was looking at.
 * 3. When the guard is about to ask, a background tab is brought to the front
 *    first, so the question is shown over the buffer it is about.
 * 4. A tab with no guard (a terminal) still closes on its ×.
 * 5. A file tab holding a buffer while its editor is withheld (the chat's
 *    machine still being resolved) takes the batch close's confirmation, so an
 *    unsaved edit is not dropped just because no editor is mounted.
 *
 * `MarkdownPanel` is a stub exposing the real handle shape; each case sets what
 * the stub's guard does (`guard.mode`) and reads which paths it was asked for.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act, within } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'

const guard = vi.hoisted(() => ({
  /** allow: closes without asking. prompt-allow / prompt-decline: asks, then
   *  closes or keeps the tab. */
  mode: 'allow' as 'allow' | 'prompt-allow' | 'prompt-decline',
  asked: [] as string[],
  /** `undefined` while the chat's machine is still being resolved: file tab
   *  bodies, and so their guards, are withheld until it is known. */
  remote: null as null | undefined,
}))

vi.mock('../components/MarkdownPanel', async () => {
  const React = await import('react')
  type Handle = import('../components/MarkdownPanel').MarkdownPanelHandle
  const Stub = React.forwardRef<Handle, { filePath: string; onClose: () => void }>(function Stub({ filePath, onClose }, ref) {
    React.useImperativeHandle(ref, () => ({
      requestClose: (opts) => {
        guard.asked.push(filePath)
        if (guard.mode !== 'allow') opts?.beforePrompt?.()
        if (guard.mode !== 'prompt-decline') onClose()
      },
      requestNavigate: (nav) => nav(() => true),
    }), [filePath, onClose])
    return null
  })
  return { default: Stub }
})
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../components/ArtifactPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/FolderPanel', () => ({ default: () => null }))
vi.mock('../components/WebPreviewPanel', () => ({ default: () => null }))
vi.mock('../components/McpAppFrame', () => ({ default: () => null }))
vi.mock('../components/CliPanel', () => ({
  default: () => null,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn() }),
}))
vi.mock('../utils/terminalRegistry', () => ({
  useTerminalEnabled: () => true,
  useTerminalTitle: () => 'Terminal',
}))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))
// The chat runs on this machine, so its file tab bodies (and their guards) mount.
vi.mock('../components/sessionEnvironmentHooks', () => ({
  useSlotEnvironmentPlacement: () => ({ remote: guard.remote, error: null }),
}))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import SidePanel from '../pages/chat/SidePanel'
import { usePanelTabs, __resetPanelTabs } from '../hooks/usePanelTabs'

const SLOT = 'chat-1'
let ctl: ReturnType<typeof usePanelTabs> | null = null

function Harness() {
  const tabsCtl = usePanelTabs(SLOT)
  ctl = tabsCtl
  return <SidePanel tabsCtl={tabsCtl} slot={SLOT} onFileSave={async () => {}} onClose={() => {}} canDockBottom={false} />
}

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Provider store={createTestStore()}>
        <Harness />
      </Provider>
    </QueryClientProvider>,
  )
}

const fileTab = (name: string) => screen.getByRole('tab', { name: new RegExp(name) })
const closeButton = (name: string) => within(fileTab(name)).getByRole('button', { name: 'Close tab' })
const middleClick = (el: HTMLElement) => { fireEvent(el, new MouseEvent('auxclick', { bubbles: true, cancelable: true, button: 1 })) }
const hasTab = (id: string) => ctl!.tabs.some(t => t.id === id)

function openTwo() {
  act(() => {
    ctl!.openFile('/srv/one.md', 'one', SLOT)
    ctl!.openFile('/srv/two.md', 'two', SLOT)
    ctl!.setActive('file:/srv/two.md')
  })
}

describe('SidePanel chip close runs the file tab guard', () => {
  beforeEach(() => {
    localStorage.clear(); __resetPanelTabs(); ctl = null
    guard.mode = 'allow'; guard.asked = []; guard.remote = null
  })

  it('keeps the tab when the guard declines, for the × and for a middle-click', () => {
    renderPanel()
    openTwo()
    guard.mode = 'prompt-decline'

    act(() => { fireEvent.click(closeButton('two\\.md')) })
    expect(guard.asked).toEqual(['/srv/two.md'])
    expect(hasTab('file:/srv/two.md')).toBe(true)

    act(() => { middleClick(fileTab('two\\.md')) })
    expect(guard.asked).toEqual(['/srv/two.md', '/srv/two.md'])
    expect(hasTab('file:/srv/two.md')).toBe(true)
  })

  it('closes a background tab the guard allows without asking, and keeps focus where it was', () => {
    renderPanel()
    openTwo()

    act(() => { fireEvent.click(closeButton('one\\.md')) })
    expect(guard.asked).toEqual(['/srv/one.md'])
    expect(hasTab('file:/srv/one.md')).toBe(false)
    expect(ctl!.activeId).toBe('file:/srv/two.md')
  })

  it('brings a background tab to the front before the guard asks about it', () => {
    renderPanel()
    openTwo()
    guard.mode = 'prompt-decline'

    act(() => { middleClick(fileTab('one\\.md')) })
    expect(guard.asked).toEqual(['/srv/one.md'])
    expect(hasTab('file:/srv/one.md')).toBe(true)
    expect(ctl!.activeId).toBe('file:/srv/one.md')
  })

  it('closes the tab once the guard has asked and the user confirmed', () => {
    renderPanel()
    openTwo()
    guard.mode = 'prompt-allow'

    act(() => { fireEvent.click(closeButton('one\\.md')) })
    expect(hasTab('file:/srv/one.md')).toBe(false)
    expect(hasTab('file:/srv/two.md')).toBe(true)
  })

  it('closes a tab with no guard (a terminal) directly', () => {
    renderPanel()
    act(() => { ctl!.openTerminal() })
    const term = ctl!.tabs.find(t => t.kind === 'terminal')
    expect(term).toBeDefined()

    act(() => { fireEvent.click(within(screen.getByRole('tab', { name: /Terminal/ })).getByRole('button', { name: 'Close tab' })) })
    expect(guard.asked).toEqual([])
    expect(hasTab(term!.id)).toBe(false)
  })

  it('asks before dropping an unsaved buffer whose editor is not mounted', async () => {
    guard.remote = undefined
    renderPanel()
    openTwo()
    act(() => { ctl!.patchTab('file:/srv/one.md', { content: 'one, edited' }) })

    act(() => { fireEvent.click(closeButton('one\\.md')) })
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('one.md')).toBeInTheDocument()
    expect(guard.asked).toEqual([])
    expect(hasTab('file:/srv/one.md')).toBe(true)

    await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' })) })
    expect(hasTab('file:/srv/one.md')).toBe(true)
  })
})
