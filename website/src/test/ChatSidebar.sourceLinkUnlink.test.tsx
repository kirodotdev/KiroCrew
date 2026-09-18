/** Real sidebar + shared session menus: no per-chip destructive controls. */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { useAppSelector, type RootState } from '../store'
import { updateSlot } from '../store/dashboardSlice'
import { initI18n } from '../i18n/all'
import i18next from 'i18next'
import { i18nT } from '../i18n/t'
import type { ChatSlot } from '../types'

const { switchSlotMock, unlinkMock, sourceLinksMock, openSourceMock, device } = vi.hoisted(() => ({
  switchSlotMock: vi.fn(() => ({ type: 'chat/switchSlot/pending', meta: {} })),
  unlinkMock: vi.fn(), sourceLinksMock: vi.fn(), openSourceMock: vi.fn(() => true),
  device: { touch: false },
}))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => device.touch }))
vi.mock('../hooks/useIsTouchDevice', () => ({ useIsTouchDevice: () => device.touch }))
vi.mock('../store/chatSlice', async importOriginal => ({
  ...await importOriginal<typeof import('../store/chatSlice')>(),
  switchSlot: (...args: unknown[]) => switchSlotMock(...args),
}))
vi.mock('../api/client', async importOriginal => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, api: {
    ...actual.api,
    ...Object.fromEntries(['sessions', 'chatSlots', 'chatSlotDetail', 'fetchHistory', 'chatFolders', 'chatTags', 'tagColumns', 'channelTargets'].map(k => [k, vi.fn().mockResolvedValue([])])),
    unlinkSourceLink: (...args: unknown[]) => unlinkMock(...args),
    chatSlotSourceLinks: (...args: unknown[]) => sourceLinksMock(...args),
  } }
})
import ChatSidebar from '../pages/ChatSidebar'
import { sameSlotPlaceholder } from '../components/SourceLinksSubmenu'

const PR_URL = 'https://github.com/acme/widgets/pull/634'
const ISSUE_URL = 'https://github.com/acme/widgets/issues/701'
const PR_IDENTITY = '["github","github.com","acme","widgets",634,"","change",""]'
const ISSUE_IDENTITY = '["github","github.com","acme","widgets",701,"","issue",""]'
function rows(): ChatSlot[] {
  return [{ key: 's1', title: 'PR session', messages: 1, running: false, last_ts: '2026-01-01T00:00:00Z',
    source_links: [
      { provider: 'github', number: 634, label: '#634', url: PR_URL, state: 'open', kind: 'change', identity: PR_IDENTITY },
      { provider: 'github', number: 701, label: '#701', url: ISSUE_URL, kind: 'issue', identity: ISSUE_IDENTITY },
    ], source_links_total: 2 }]
}
function ConnectedSidebar() {
  const slots = useAppSelector(s => s.dashboard.slots)
  return <ChatSidebar slots={slots} activeSlot="s1" unreadSlots={[]} history={[]} historyHasMore={false} defaultAgent="default" installedAgents={[]} onOpenSource={openSourceMock} />
}
function renderSidebar(list = rows(), connected = true) {
  const store = createTestStore({
    dashboard: { status: { platform: 'darwin' }, connected, slots: list, approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {}, sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear', slotsLoaded: true,
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: 's1', messages: [], slotRunning: false, slotStopping: false, slotState: 'idle', slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      history: [], historyHasMore: false, historyOffset: 0, pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null, subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotActivity: {}, slotHistory: [], slotMessages: {}, slotLoading: false,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  for (const key of ['chat-folders', 'chat-tags', 'tag-columns', 'channel-targets']) qc.setQueryData([key], [])
  render(<QueryClientProvider client={qc}><Provider store={store}><ThemeProvider><MemoryRouter><ConnectedSidebar /></MemoryRouter></ThemeProvider></Provider></QueryClientProvider>)
  return { store, qc }
}
async function openMenu(context = false) {
  if (context) fireEvent.contextMenu(screen.getByText('PR session'), { clientX: 100, clientY: 100 })
  else fireEvent.pointerDown(rowMenuButton(), { button: 0, ctrlKey: false })
  const trigger = await screen.findByRole(device.touch ? 'button' : 'menuitem', { name: 'Unlink a PR or Issue chip' })
  fireEvent.click(trigger)
  return trigger
}
function removal(number = 634) { return screen.getByRole('menuitem', { name: new RegExp(`#${number} ·`) }) }
// Two-step unlink: the first click STAGES the entry (it switches to
// "Select again to unlink" and carries data-confirming); the second click on
// that staged item commits. Only one entry is staged at a time.
function confirmRemoval(_number = 634) { return screen.getByRole('menuitem', { name: /select again to unlink/i }) }
function unlinkVia(number = 634) { fireEvent.click(removal(number)); fireEvent.click(confirmRemoval(number)) }
function sessionRow() { return document.querySelector<HTMLElement>('[data-session-row="s1"]')! }
function rowMenuButton() { return within(sessionRow()).getByRole('button', { name: 'More options' }) }
function chip(number = 634) { return sessionRow().querySelector<HTMLAnchorElement>(`a[href="${number === 634 ? PR_URL : ISSUE_URL}"]`) }

describe('source-link removal through the existing session menu', () => {
  beforeEach(async () => {
    device.touch = false
    switchSlotMock.mockClear(); openSourceMock.mockClear()
    unlinkMock.mockReset().mockResolvedValue({ ok: true, dismissed: true })
    sourceLinksMock.mockReset().mockResolvedValue({ links: rows()[0].source_links, total: 2 })
    localStorage.setItem('mc-session-stale-collapse-ms', '0')
    await initI18n(); await i18next.changeLanguage('en')
  })
  it('requires a per-item confirmation: first select stages, second unlinks', async () => {
    renderSidebar(); await openMenu()
    // First select STAGES the entry — it must NOT fire the DELETE, and the entry
    // switches to a confirm affordance naming the link.
    fireEvent.click(removal())
    expect(unlinkMock).not.toHaveBeenCalled()
    expect(screen.getByRole('menuitem', { name: /select again to unlink/i })).toBeInTheDocument()
    // Second select on the staged entry commits.
    fireEvent.click(confirmRemoval())
    await waitFor(() => expect(unlinkMock).toHaveBeenCalledWith('s1', PR_IDENTITY, expect.any(String)))
    await waitFor(() => expect(chip()).toBeNull())
  })
  it('drops a staged unlink instead of firing it when the same-key session was recreated', async () => {
    const { store } = renderSidebar(); await openMenu()
    // Stage the removal under the current session generation.
    fireEvent.click(removal())
    expect(screen.getByRole('menuitem', { name: /select again to unlink/i })).toBeInTheDocument()
    // A same-key session recreation lands: same slot key, but a fresh
    // ``created`` stamp (a new generation) with its own still-open links. The
    // staged confirm belongs to the OLD session and must be dropped — the entry
    // reverts to its plain label and no DELETE is ever queued.
    act(() => store.dispatch(updateSlot({ key: 's1', created: '2099-12-31T00:00:00Z' })))
    await waitFor(() => expect(screen.queryByRole('menuitem', { name: /select again to unlink/i })).toBeNull())
    expect(unlinkMock).not.toHaveBeenCalled()
    expect(chip()).toBeInTheDocument()
    // And a fresh, in-generation stage+commit on the replacement still works.
    fireEvent.click(removal())
    fireEvent.click(confirmRemoval())
    await waitFor(() => expect(unlinkMock).toHaveBeenCalledWith('s1', PR_IDENTITY, expect.any(String)))
  })
  it('adds no action controls to a strip with multiple links, or extra row triggers', () => {
    renderSidebar()
    const strip = chip()!.parentElement!
    expect(within(strip).getAllByRole('link')).toHaveLength(2)
    expect(within(strip).queryAllByRole('button')).toHaveLength(0)
    expect(within(sessionRow()).getAllByRole('button', { name: 'More options' })).toHaveLength(1)
    expect(sourceLinksMock).not.toHaveBeenCalled()
  })
  it.each([false, true])('offers correct opaque identities through context=%s', async context => {
    renderSidebar(); await openMenu(context)
    expect(screen.getByText(/permanently hide from this session/)).toBeInTheDocument()
    unlinkVia(701)
    await waitFor(() => expect(unlinkMock).toHaveBeenCalledWith('s1', ISSUE_IDENTITY, expect.any(String)))
    await waitFor(() => expect(chip(701)).toBeNull())
    expect(chip()).toBeInTheDocument()
    expect(switchSlotMock).not.toHaveBeenCalled()
  })
  it('supports touch via the stock inline submenu in the mobile menu', async () => {
    device.touch = true
    renderSidebar(); const trigger = await openMenu()
    expect(trigger).toHaveAttribute('aria-expanded', 'true')
    unlinkVia()
    await waitFor(() => expect(chip()).toBeNull())
  })
  it('supports keyboard opening, choosing an entry, and Escape', async () => {
    const user = userEvent.setup()
    renderSidebar()
    rowMenuButton().focus()
    await user.keyboard('{Enter}')
    const trigger = await screen.findByRole(device.touch ? 'button' : 'menuitem', { name: 'Unlink a PR or Issue chip' })
    trigger.focus(); await user.keyboard('{ArrowRight}')
    await waitFor(() => expect(removal()).toHaveFocus())
    await user.keyboard('{Enter}')  // stages
    await user.keyboard('{Enter}')  // confirms
    await waitFor(() => expect(unlinkMock).toHaveBeenCalledWith('s1', PR_IDENTITY, expect.any(String)))
    await user.keyboard('{Escape}{Escape}')
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
  })
  it('keeps keyboard focus on a neighbour after a removal, and on the parent menu after the last one', async () => {
    const user = userEvent.setup()
    renderSidebar()
    rowMenuButton().focus()
    await user.keyboard('{Enter}')
    ;(await screen.findByRole('menuitem', { name: 'Unlink a PR or Issue chip' })).focus()
    await user.keyboard('{ArrowRight}')
    await waitFor(() => expect(removal()).toHaveFocus())
    await user.keyboard('{Enter}')  // stage #634
    await user.keyboard('{Enter}')  // confirm #634
    await waitFor(() => expect(screen.queryByRole('menuitem', { name: /#634 ·/ })).toBeNull())
    // Focus was NOT dropped onto <body> with the unmounted entry: the neighbour holds it.
    expect(removal(701)).toHaveFocus()
    await user.keyboard('{Enter}')  // stage #701
    await user.keyboard('{Enter}')  // confirm #701
    await waitFor(() => expect(unlinkMock).toHaveBeenCalledWith('s1', ISSUE_IDENTITY, expect.any(String)))
    await waitFor(() => expect(screen.queryByRole('menuitem', { name: 'Unlink a PR or Issue chip' })).toBeNull())
    // Nothing left to list, so the whole submenu is gone: focus is handed to the
    // parent menu, whose roving group lands it on the first entry, and arrows move on.
    const landed = document.activeElement
    expect(landed).toHaveAttribute('role', 'menuitem')
    expect(landed?.closest('[role="menu"]')).not.toBeNull()
    await user.keyboard('{ArrowDown}')
    expect(document.activeElement).toHaveAttribute('role', 'menuitem')
    expect(document.activeElement).not.toBe(landed)
  })
  it('leaves focus alone when the user moved on before DELETE settled, and keeps it on a failed entry', async () => {
    let settle!: { resolve: (value: unknown) => void; reject: (reason: Error) => void }
    unlinkMock.mockImplementation(() => new Promise((resolve, reject) => { settle = { resolve, reject } }))
    const user = userEvent.setup()
    renderSidebar(); await openMenu()
    removal().focus(); await user.keyboard('{Enter}')  // stage
    await user.keyboard('{Enter}')  // confirm
    await waitFor(() => expect(unlinkMock).toHaveBeenCalledWith('s1', PR_IDENTITY, expect.any(String)))
    await act(async () => { settle.reject(new Error('store unavailable')) })
    await screen.findByTestId('session-source-unlink-error')
    expect(removal()).toHaveFocus()
    await user.keyboard('{Enter}')  // stage the retry
    await user.keyboard('{Enter}')  // confirm the retry
    await waitFor(() => expect(unlinkMock).toHaveBeenCalledTimes(2))
    await user.keyboard('{Escape}{Escape}')
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
    expect(rowMenuButton()).toHaveFocus()
    await act(async () => { settle.resolve({ ok: true, dismissed: true }) })
    await waitFor(() => expect(chip()).toBeNull())
    expect(rowMenuButton()).toHaveFocus()
  })
  it('masks pending chips and restores a localized anchored error without overwriting a slots push', async () => {
    let reject!: (reason: Error) => void
    unlinkMock.mockImplementation(() => new Promise((_resolve, fail) => { reject = fail }))
    const { store } = renderSidebar(); await openMenu()
    unlinkVia()
    await waitFor(() => expect(chip()).toBeNull())
    act(() => store.dispatch(updateSlot({ key: 's1', title: 'Updated title' })))
    await act(async () => { reject(new Error('network failure diagnostic')) })
    await waitFor(() => expect(chip()).toBeInTheDocument())
    expect(screen.getByText('Updated title')).toBeInTheDocument()
    const notice = screen.getByTestId('session-source-unlink-error')
    expect(notice).toHaveTextContent('Could not unlink. Try again.')
    expect(notice.closest('[role="menu"]')).not.toBeNull()
    expect(screen.getByRole('menuitem', { name: 'Ask the agent' })).toHaveAttribute('aria-describedby', notice.id)
    await act(async () => { await i18next.changeLanguage('zh-CN') })
    expect(notice).toHaveTextContent(i18nT('pages.chatSidebar.unlink_source_link_failed'))
  })
  it('keeps normal navigation and modifier/offline native link fallback', () => {
    const { store } = renderSidebar()
    fireEvent.click(chip()!)
    expect(openSourceMock).toHaveBeenCalledWith('s1', { url: PR_URL, kind: 'change' })
    openSourceMock.mockClear()
    for (const modifier of ['ctrlKey', 'metaKey', 'shiftKey', 'altKey']) expect(fireEvent.click(chip()!, { [modifier]: true })).toBe(true)
    act(() => store.dispatch({ type: 'dashboard/setConnected', payload: false }))
    expect(chip()).toHaveAttribute('href', PR_URL)
    expect(openSourceMock).not.toHaveBeenCalled()
  })
  it('disables removal while offline, without disabling the real links or fetching the rest', async () => {
    const list = rows(); list[0].source_links_total = 5
    renderSidebar(list, false); await openMenu()
    expect(removal()).toHaveAttribute('data-disabled')
    fireEvent.click(removal())
    expect(unlinkMock).not.toHaveBeenCalled()
    expect(sourceLinksMock).not.toHaveBeenCalled()
    expect(fireEvent.click(chip()!)).toBe(true)
    expect(openSourceMock).not.toHaveBeenCalled()
    expect(screen.getByText(/gateway offline/)).toBeInTheDocument()
  })
  it('omits entries without opaque identity while retaining their navigation', async () => {
    const list = rows(); delete list[0].source_links![0].identity
    renderSidebar(list); await openMenu()
    expect(screen.queryByRole('menuitem', { name: /#634 ·/ })).toBeNull()
    expect(removal(701)).toBeInTheDocument(); expect(chip()).toBeInTheDocument()
  })
  it('reports a failed all-links read in the submenu and supports retry', async () => {
    const list = rows(); list[0].source_links = list[0].source_links!.slice(0, 1)
    sourceLinksMock.mockRejectedValueOnce(new Error('read failed'))
    renderSidebar(list); await openMenu()
    const notice = await screen.findByTestId('session-source-unlink-error')
    expect(notice).toHaveTextContent(i18nT('pages.chatSidebar.source_links_expand_failed'))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Retry' }))
    await screen.findByRole('menuitem', { name: /#701 ·/ })
    expect(screen.queryByTestId('session-source-unlink-error')).toBeNull()
  })
  it('retains a failure when the menu is closed before DELETE settles', async () => {
    let reject!: (reason: Error) => void
    unlinkMock.mockImplementation(() => new Promise((_resolve, fail) => { reject = fail }))
    renderSidebar(); await openMenu(); unlinkVia()
    await waitFor(() => expect(chip()).toBeNull())
    await userEvent.setup().keyboard('{Escape}{Escape}')
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
    await act(async () => { reject(new Error('late failure')) })
    await waitFor(() => expect(chip()).toBeInTheDocument())
    await openMenu()
    expect(await screen.findByTestId('session-source-unlink-error')).toHaveTextContent('Could not unlink. Try again.')
  })
  it('does not patch a same-key replacement session when the DELETE settles mid-recreation', async () => {
    // The DELETE fires under session A, then a same-key session B pushes over it
    // (fresh generation) still carrying the same PR identity, and only THEN does
    // the DELETE resolve. onSuccess must not strip B's chip: the request was A's.
    let settle!: (value: unknown) => void
    unlinkMock.mockImplementation(() => new Promise(resolve => { settle = resolve }))
    const { store } = renderSidebar(); await openMenu()
    unlinkVia()
    await waitFor(() => expect(unlinkMock).toHaveBeenCalledWith('s1', PR_IDENTITY, expect.any(String)))
    // Same-key recreation lands mid-flight: new generation, B still has the chip.
    act(() => store.dispatch(updateSlot({ key: 's1', created: '2099-12-31T00:00:00Z' })))
    await act(async () => { settle({ ok: true, dismissed: true }) })
    // B's chip survives — the stale settlement did not patch the replacement.
    await waitFor(() => expect(chip()).toBeInTheDocument())
  })
  it('fetches all links only on expanding the removal submenu and removes an off-preview identity', async () => {
    const list = rows(); list[0].source_links = list[0].source_links!.slice(0, 1)
    const { qc } = renderSidebar(list)
    expect(sourceLinksMock).not.toHaveBeenCalled()
    fireEvent.pointerDown(rowMenuButton(), { button: 0, ctrlKey: false })
    const trigger = await screen.findByRole(device.touch ? 'button' : 'menuitem', { name: 'Unlink a PR or Issue chip' })
    expect(sourceLinksMock).not.toHaveBeenCalled()
    fireEvent.click(trigger)
    await screen.findByRole('menuitem', { name: /#701 ·/ })
    expect(sourceLinksMock).toHaveBeenCalledWith('s1')
    sourceLinksMock.mockResolvedValue({ links: list[0].source_links, total: 1 })
    unlinkVia(701)
    await waitFor(() => expect(unlinkMock).toHaveBeenCalledWith('s1', ISSUE_IDENTITY, expect.any(String)))
    await waitFor(() => expect(screen.queryByRole('menuitem', { name: /#701 ·/ })).toBeNull())
    for (const [, value] of qc.getQueriesData<ChatSlot['source_links']>({ queryKey: ['session-source-links', 's1'] })) expect(value?.some(link => link.identity === ISSUE_IDENTITY)).not.toBe(true)
  })
  it('keeps the off-preview neighbour mounted and focused while the re-keyed full list refetches', async () => {
    // Only #634 is in the preview; #701 is off-preview and known solely from the
    // full-list read. Removing #634 patches the slot, which changes the list
    // query's signature key — the refetch under the new key must not unmount the
    // entries in the meantime, or the focus just handed to #701 lands on <body>.
    const list = rows(); list[0].source_links = list[0].source_links!.slice(0, 1)
    // Every full-list read is settled by hand, so the refetch under the new key
    // is provably still in flight while focus is checked.
    const settlers: Array<(value: unknown) => void> = []
    sourceLinksMock.mockReset().mockImplementation(() => new Promise(resolve => { settlers.push(resolve) }))
    const user = userEvent.setup()
    renderSidebar(list)
    rowMenuButton().focus()
    await user.keyboard('{Enter}')
    ;(await screen.findByRole('menuitem', { name: 'Unlink a PR or Issue chip' })).focus()
    await user.keyboard('{ArrowRight}')
    await waitFor(() => expect(settlers.length).toBe(1))
    await act(async () => { settlers[0]({ links: rows()[0].source_links, total: 2 }) })
    await screen.findByRole('menuitem', { name: /#701 ·/ })
    await waitFor(() => expect(removal()).toHaveFocus())
    await user.keyboard('{Enter}')  // stage #634
    await user.keyboard('{Enter}')  // confirm #634
    await waitFor(() => expect(unlinkMock).toHaveBeenCalledWith('s1', PR_IDENTITY, expect.any(String)))
    await waitFor(() => expect(screen.queryByRole('menuitem', { name: /#634 ·/ })).toBeNull())
    // Refetch is in flight under the new key: the neighbour is still there and holds focus.
    await waitFor(() => expect(settlers.length).toBeGreaterThan(1))
    expect(removal(701)).toHaveFocus()
    expect(screen.getByLabelText(i18nT('pages.chatPage.loading'))).toBeInTheDocument()
    // Arrow keys keep working: the roving group is intact, focus never left the menu.
    await user.keyboard('{ArrowDown}')
    expect(document.activeElement).toHaveAttribute('role', 'menuitem')
    expect(document.activeElement?.closest('[role="menu"]')).not.toBeNull()
    await user.keyboard('{ArrowUp}')
    expect(removal(701)).toHaveFocus()
    await act(async () => { for (const settle of settlers.slice(1)) settle({ links: rows()[0].source_links!.slice(1), total: 1 }) })
    expect(removal(701)).toHaveFocus()
    expect(screen.queryByRole('menuitem', { name: /#634 ·/ })).toBeNull()
    await waitFor(() => expect(screen.queryByLabelText(i18nT('pages.chatPage.loading'))).toBeNull())
  })
  it('carries a placeholder only across the same slot AND same session, never across a recreation', () => {
    const previous = rows()[0].source_links
    const query = (slot: string, sid: string) => ({ queryKey: ['session-source-links', slot, sid, 'sig'] })
    // Same slot + same session identity: reuse.
    expect(sameSlotPlaceholder('s1', 'sess-1')(previous, query('s1', 'sess-1') as never)).toBe(previous)
    // Same key but the session behind it was RECREATED (new identity): drop it,
    // or an old link would stay selectable and unlink the replacement session.
    expect(sameSlotPlaceholder('s1', 'sess-2')(previous, query('s1', 'sess-1') as never)).toBeUndefined()
    // Another slot entirely: drop.
    expect(sameSlotPlaceholder('s1', 'sess-1')(previous, query('s2', 'sess-1') as never)).toBeUndefined()
    expect(sameSlotPlaceholder('s1', 'sess-1')(previous, undefined)).toBeUndefined()
  })
})
