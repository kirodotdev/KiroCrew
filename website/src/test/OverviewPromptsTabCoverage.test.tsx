/**
 * PromptsTab — the Prompts tab under Agent Capabilities.
 *
 * Pins the four query states (loading / error / empty / loaded), the
 * user-vs-package split with its per-package grouping, the filter's effect on
 * each group, the list-detail selection and its detail fetch (including the
 * stale-response guard and the failure caption), and the "Send to…" slot
 * picker: which label each slot row shows, what the two navigation targets are,
 * and how the picker closes.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import type { ChatSlot } from '../types'

const mockNavigate = vi.fn()
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...actual, useNavigate: () => mockNavigate }
})

const mockApi = vi.hoisted(() => ({
  prompts: vi.fn(),
  promptDetail: vi.fn(),
  chatSlotDetail: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import PromptsTab from '../pages/overview/PromptsTab'

interface Prompt {
  name: string
  fullName: string
  description: string
  path: string
  package: string
  source: string
}

/** A user prompt: `package` is empty, so its detail key is the bare name.
 *  `source` is 'user' — a value this build cannot address for writes, so the
 *  detail header carries no Actions menu and cannot shadow a row lookup. */
const USER: Prompt = { name: 'hello', fullName: 'hello', description: 'Say hello', path: '~/.kiro/prompts/hello.md', package: '', source: 'user' }
/** A package prompt inside a named package. */
const PKG: Prompt = { name: 'review', fullName: 'sage/review', description: 'Review a diff', path: '/pkgs/sage/review.md', package: 'sage', source: 'package' }
/** A package prompt with NO package — groups under "unknown". */
const ORPHAN: Prompt = { name: 'ship', fullName: 'ship', description: 'Ship it', path: '/pkgs/ship.md', package: '', source: 'package' }

const ALL = [USER, PKG, ORPHAN]

const slot = (over: Partial<ChatSlot>): ChatSlot => ({ key: 'k', messages: 0, running: false, ...over })

function renderTab(slots: ChatSlot[] = []) {
  const base = createTestStore().getState()
  // preloadedState REPLACES a slice, so spread the real initial state first.
  const store = createTestStore({ ...base, dashboard: { ...base.dashboard, slots } })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <MemoryRouter initialEntries={['/overview']}>
          <PromptsTab />
        </MemoryRouter>
      </Provider>
    </QueryClientProvider>,
  )
  return { store, ...view }
}

/** A row in the list pane, addressed by the bare stem it displays. The full
 *  invocation name (`@sage/review`) lives in the detail header, not the row. */
const option = (stem: string) => screen.getByRole('option', { name: new RegExp(`@${stem}`) })

/** The list has resolved and its first row is auto-selected, so a detail read
 *  has already fired before any click. Waiting on it keeps a later click from
 *  racing the effect that owns the initial selection. */
const ready = () => waitFor(() => expect(mockApi.promptDetail).toHaveBeenCalled())

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  mockNavigate.mockReset()
  Object.values(mockApi).forEach(m => m.mockReset())
  mockApi.prompts.mockResolvedValue(ALL)
  mockApi.promptDetail.mockResolvedValue({ content: 'PROMPT BODY' })
  mockApi.chatSlotDetail.mockResolvedValue({ messages: [], running: false })
})

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
})

describe('PromptsTab query states', () => {
  it('shows the loading line while the list is in flight', async () => {
    mockApi.prompts.mockReturnValue(new Promise(() => {}))
    renderTab()
    expect(await screen.findByText('Loading prompts…')).toBeInTheDocument()
    // No filter box until there is something to filter.
    expect(screen.queryByPlaceholderText('Filter prompts…')).not.toBeInTheDocument()
  })

  it('surfaces the query error message', async () => {
    mockApi.prompts.mockRejectedValue(new Error('prompts endpoint exploded'))
    renderTab()
    expect(await screen.findByText('prompts endpoint exploded')).toBeInTheDocument()
    // An error is not an empty list — the zero-prompts empty state stays away.
    expect(screen.queryByText('No prompts yet')).not.toBeInTheDocument()
  })

  it('falls back to a generic message when the error carries none', async () => {
    mockApi.prompts.mockRejectedValue(new Error(''))
    renderTab()
    expect(await screen.findByText('Failed to load prompts')).toBeInTheDocument()
  })

  it('names the registry in the tab prose and points the empty state at Create', async () => {
    mockApi.prompts.mockResolvedValue([])
    renderTab()
    // The empty state names the affordance on this page rather than the
    // filesystem path the create flow replaces.
    expect(await screen.findByText('No prompts yet')).toBeInTheDocument()
    expect(screen.queryByText(/\.kiro\/prompts/)).not.toBeInTheDocument()
    expect(screen.getAllByText('Create New Prompt').length).toBeGreaterThan(0)
    // The registry label is still case-folded for prose; it moved out of the
    // install nudge and into the header InfoTip, which carries it as a title.
    expect(screen.getByTitle(/Saved prompts from packages/)).toBeInTheDocument()
  })
})

describe('PromptsTab listing', () => {
  it('splits user from package prompts and groups packages by name', async () => {
    renderTab()
    // The user group is a plain-text label now, still counting how many rows
    // survived the (empty) filter.
    expect(await screen.findByText('User Prompts (1)')).toBeInTheDocument()
    // Package prompts sit under their uppercased package name, and one with no
    // package falls into "unknown". There is no aggregate "Packages Prompts"
    // card title any more, so the rows themselves carry that both are listed.
    expect(screen.getByText('SAGE')).toBeInTheDocument()
    expect(screen.getByText('UNKNOWN')).toBeInTheDocument()
    expect(option('review')).toBeInTheDocument()
    expect(option('ship')).toBeInTheDocument()

    // Provenance: a non-package row is badged with its own source, and the
    // description rides along on the row.
    expect(option('hello')).toHaveTextContent('user')
    expect(screen.getByText('Say hello')).toBeInTheDocument()

    // A package prompt reads "Package" instead — a claim that now belongs to
    // the detail header, which is where the badge moved.
    fireEvent.click(option('review'))
    expect(await screen.findByText('Package')).toBeInTheDocument()
  })

  it('filter narrows the user and package groups independently', async () => {
    renderTab()
    await ready()

    fireEvent.change(screen.getByPlaceholderText('Filter prompts…'), { target: { value: 'review' } })
    // Only the package side survives. The of-total count strings are gone, so
    // "narrowed" is asserted on which rows and group labels remain.
    await waitFor(() => expect(screen.queryByText(/User Prompts/)).not.toBeInTheDocument())
    expect(screen.getByText('SAGE')).toBeInTheDocument()
    expect(screen.queryByText('UNKNOWN')).not.toBeInTheDocument()
    expect(screen.getAllByRole('option')).toHaveLength(1)
    expect(option('review')).toBeInTheDocument()

    // Filtering on a description word reaches the user side instead.
    fireEvent.change(screen.getByPlaceholderText('Filter prompts…'), { target: { value: 'SAY' } })
    expect(await screen.findByText('User Prompts (1)')).toBeInTheDocument()
    expect(screen.queryByText('SAGE')).not.toBeInTheDocument()
    expect(screen.getAllByRole('option')).toHaveLength(1)
    expect(option('hello')).toBeInTheDocument()

    // A miss on both sides echoes the query back inside the list pane, rather
    // than falling through to the zero-prompts empty state: the list is
    // non-empty, the filter hid it.
    fireEvent.change(screen.getByPlaceholderText('Filter prompts…'), { target: { value: 'zzz' } })
    expect(await screen.findByText(/No prompts match/)).toBeInTheDocument()
    expect(screen.queryAllByRole('option')).toHaveLength(0)
    expect(screen.queryByText('No prompts yet')).not.toBeInTheDocument()
  })
})

describe('PromptsTab detail pane', () => {
  it('reads a package prompt with its qualified detail key, and selection is exclusive', async () => {
    renderTab()
    await ready()

    fireEvent.click(option('review'))
    await waitFor(() => expect(mockApi.promptDetail).toHaveBeenCalledWith('sage/review'))
    expect(await screen.findByText('PROMPT BODY')).toBeInTheDocument()
    expect(screen.getByText('/pkgs/sage/review.md')).toBeInTheDocument()
    // aria-selected carries what aria-expanded used to: the content is a pane
    // beside the list, not an in-place disclosure, so "open" is "selected".
    expect(option('review')).toHaveAttribute('aria-selected', 'true')
    expect(option('hello')).toHaveAttribute('aria-selected', 'false')

    // Selecting another row hands the pane over rather than opening a second
    // one — the same exclusivity the collapse-on-second-click used to give.
    fireEvent.click(option('hello'))
    await waitFor(() => expect(option('hello')).toHaveAttribute('aria-selected', 'true'))
    expect(option('review')).toHaveAttribute('aria-selected', 'false')
    expect(screen.getByText('~/.kiro/prompts/hello.md')).toBeInTheDocument()
    expect(screen.queryByText('/pkgs/sage/review.md')).not.toBeInTheDocument()
  })

  it('uses the bare name as detail key when a prompt has no package', async () => {
    renderTab()
    await ready()
    fireEvent.click(option('ship'))
    await waitFor(() => expect(mockApi.promptDetail).toHaveBeenCalledWith('ship'))
  })

  it('selects on Enter and on Space', async () => {
    renderTab()
    await ready()

    fireEvent.keyDown(option('review'), { key: 'Enter' })
    await waitFor(() => expect(option('review')).toHaveAttribute('aria-selected', 'true'))

    fireEvent.keyDown(option('ship'), { key: ' ' })
    await waitFor(() => expect(option('ship')).toHaveAttribute('aria-selected', 'true'))

    // An unrelated key does nothing — the selection stays where it was.
    fireEvent.keyDown(option('hello'), { key: 'a' })
    expect(option('hello')).toHaveAttribute('aria-selected', 'false')
    expect(option('ship')).toHaveAttribute('aria-selected', 'true')
  })

  it('renders empty content rather than crashing when the detail has none', async () => {
    mockApi.promptDetail.mockResolvedValue({})
    renderTab()
    await ready()
    fireEvent.click(option('review'))
    await waitFor(() => expect(option('review')).toHaveAttribute('aria-selected', 'true'))
    expect(screen.queryByText('PROMPT BODY')).not.toBeInTheDocument()
    // The pane still renders, and a body-less response is not a failed fetch,
    // so it must not claim the prompt could not be loaded.
    expect(screen.getByText('/pkgs/sage/review.md')).toBeInTheDocument()
    expect(screen.queryByText(/could not be loaded/)).not.toBeInTheDocument()
  })

  it('still renders the pane, with a failure note, when the detail fetch rejects', async () => {
    mockApi.promptDetail.mockRejectedValue(new Error('404'))
    renderTab()
    await ready()
    fireEvent.click(option('hello'))
    // The inline "(failed to load)" is now the read-only caption, which also
    // has to distinguish a failed fetch from a copy the server redacted.
    expect(await screen.findByText(/could not be loaded/)).toBeInTheDocument()
    expect(screen.queryByText(/filtered for safety/)).not.toBeInTheDocument()
    expect(option('hello')).toHaveAttribute('aria-selected', 'true')
  })

  it('drops a slow first response once a second prompt has been selected', async () => {
    let releaseFirst: ((v: { content: string }) => void) | undefined
    mockApi.promptDetail
      .mockImplementationOnce(() => new Promise<{ content: string }>(res => { releaseFirst = res }))
      .mockResolvedValue({ content: 'SECOND BODY' })

    renderTab()
    // The auto-selected first row owns the read that is left hanging — no click
    // is needed to put a request in flight any more.
    await waitFor(() => expect(mockApi.promptDetail).toHaveBeenCalledWith('hello'))
    fireEvent.click(option('review'))    // supersedes it
    expect(await screen.findByText('SECOND BODY')).toBeInTheDocument()

    releaseFirst?.({ content: 'FIRST BODY' })
    await waitFor(() => expect(screen.getByText('SECOND BODY')).toBeInTheDocument())
    // The stale winner must not steal the pane from the row now selected.
    expect(screen.queryByText('FIRST BODY')).not.toBeInTheDocument()
    expect(option('hello')).toHaveAttribute('aria-selected', 'false')
    expect(option('review')).toHaveAttribute('aria-selected', 'true')
  })

  it('drops a slow first FAILURE once a second prompt has been selected', async () => {
    let rejectFirst: ((e: Error) => void) | undefined
    mockApi.promptDetail
      .mockImplementationOnce(() => new Promise<{ content: string }>((_res, rej) => { rejectFirst = rej }))
      .mockResolvedValue({ content: 'SECOND BODY' })

    renderTab()
    await waitFor(() => expect(mockApi.promptDetail).toHaveBeenCalledWith('hello'))
    fireEvent.click(option('review'))
    expect(await screen.findByText('SECOND BODY')).toBeInTheDocument()

    rejectFirst?.(new Error('too late'))
    await waitFor(() => expect(screen.getByText('SECOND BODY')).toBeInTheDocument())
    // The superseded failure must not replace the pane's body, nor caption it
    // read-only.
    expect(screen.queryByText(/could not be loaded/)).not.toBeInTheDocument()
    expect(option('review')).toHaveAttribute('aria-selected', 'true')
  })
})

describe('PromptsTab slot picker', () => {
  async function openPicker(slots: ChatSlot[] = []) {
    const ctx = renderTab(slots)
    // The first row is auto-selected, so the detail header the picker now hangs
    // off is already on screen — nothing has to be expanded first.
    await waitFor(() => expect(mockApi.promptDetail).toHaveBeenCalled())
    fireEvent.click(screen.getByRole('button', { name: /Use in Chat/ }))
    expect(await screen.findByText('Send to…')).toBeInTheDocument()
    return ctx
  }

  it('labels each slot by title, then agent, then key — and dots the running one', async () => {
    await openPicker([
      slot({ key: 'a', title: 'Alpha session', running: true }),
      slot({ key: 'b', title: 'b', agent: 'researcher' }),
      slot({ key: 'c' }),
    ])
    expect(screen.getByRole('button', { name: 'Alpha session' })).toBeInTheDocument()
    // title === key is treated as no title, so the agent name wins.
    expect(screen.getByRole('button', { name: 'researcher' })).toBeInTheDocument()
    // No title and no agent leaves the raw key.
    expect(screen.getByRole('button', { name: 'c' })).toBeInTheDocument()
  })

  it('says so when there is no chat to send to', async () => {
    await openPicker()
    expect(screen.getByText('No active chats')).toBeInTheDocument()
  })

  it('New Chat seeds the mention and routes to a fresh session', async () => {
    const { store } = await openPicker()
    fireEvent.click(screen.getByRole('button', { name: '+ New Chat' }))
    expect(store.getState().chat.pendingInput).toBe('@hello')
    expect(mockNavigate).toHaveBeenCalledWith('/chat?autoSend=1&newSession=1')
    // Picker closes on send.
    await waitFor(() => expect(screen.queryByText('Send to…')).not.toBeInTheDocument())
  })

  it('picking a slot switches to it and routes without newSession', async () => {
    const { store } = await openPicker([slot({ key: 'chat/7', title: 'Seven' })])
    fireEvent.click(screen.getByRole('button', { name: 'Seven' }))
    expect(store.getState().chat.pendingInput).toBe('@hello')
    await waitFor(() => expect(mockApi.chatSlotDetail).toHaveBeenCalledWith('chat/7', expect.any(Number)))
    expect(mockNavigate).toHaveBeenCalledWith('/chat?autoSend=1')
    expect(mockNavigate).not.toHaveBeenCalledWith('/chat?autoSend=1&newSession=1')
  })

  it('sends from the keyboard too', async () => {
    await openPicker([slot({ key: 'chat/9', title: 'Nine' })])
    fireEvent.keyDown(screen.getByRole('button', { name: 'Nine' }), { key: 'Enter' })
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith('/chat?autoSend=1'))

    fireEvent.click(screen.getByRole('button', { name: /Use in Chat/ }))
    await screen.findByText('Send to…')
    fireEvent.keyDown(screen.getByRole('button', { name: '+ New Chat' }), { key: ' ' })
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith('/chat?autoSend=1&newSession=1'))
  })

  it('ignores other keys on the picker rows', async () => {
    await openPicker([slot({ key: 'chat/1', title: 'One' })])
    fireEvent.keyDown(screen.getByRole('button', { name: 'One' }), { key: 'x' })
    fireEvent.keyDown(screen.getByRole('button', { name: '+ New Chat' }), { key: 'Tab' })
    expect(mockNavigate).not.toHaveBeenCalled()
    expect(screen.getByText('Send to…')).toBeInTheDocument()
  })

  it('closes on an outside mousedown but survives one inside itself', async () => {
    await openPicker([slot({ key: 'chat/1', title: 'One' })])
    fireEvent.mouseDown(screen.getByText('Send to…'))
    expect(screen.getByText('Send to…')).toBeInTheDocument()

    fireEvent.mouseDown(document.body)
    await waitFor(() => expect(screen.queryByText('Send to…')).not.toBeInTheDocument())
  })

  it('the Use in Chat button toggles the picker back off', async () => {
    await openPicker()
    fireEvent.click(screen.getByRole('button', { name: /Use in Chat/ }))
    await waitFor(() => expect(screen.queryByText('Send to…')).not.toBeInTheDocument())
  })
})
