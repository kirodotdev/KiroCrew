/**
 * PromptsTab — the Prompts tab under Agent Capabilities.
 *
 * Pins the four query states (loading / error / empty / loaded), the
 * user-vs-package split with its per-package grouping, the filter counts, the
 * expand-collapse detail fetch (including its stale-response guard and its
 * failure text), and the "Send to…" slot picker: which label each slot row
 * shows, what the two navigation targets are, and how the picker closes.
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

/** A user prompt: `package` is empty, so its detail key is the bare name. */
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

/** The disclosure row for a prompt, addressed by its `@fullName`. */
const row = (fullName: string) => screen.getByRole('button', { name: new RegExp(`@${fullName}`) })

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
    // An error is not an empty list — the "install a package" nudge stays away.
    expect(screen.queryByText(/No prompts found/)).not.toBeInTheDocument()
  })

  it('falls back to a generic message when the error carries none', async () => {
    mockApi.prompts.mockRejectedValue(new Error(''))
    renderTab()
    expect(await screen.findByText('Failed to load prompts')).toBeInTheDocument()
  })

  it('names the registry singular in the empty state', async () => {
    mockApi.prompts.mockResolvedValue([])
    renderTab()
    // "Packages" is de-pluralised for the install nudge.
    expect(await screen.findByText(/No prompts found\. Install a package with prompts/)).toBeInTheDocument()
  })
})

describe('PromptsTab listing', () => {
  it('splits user from package prompts and groups packages by name', async () => {
    renderTab()
    expect(await screen.findByRole('heading', { name: 'User Prompts (1)' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Packages Prompts (2)' })).toBeInTheDocument()

    // Group headers: the named package, and "unknown" for the one without.
    expect(screen.getByText('sage')).toBeInTheDocument()
    expect(screen.getByText('unknown')).toBeInTheDocument()

    // Provenance badge: package prompts read "Package", others read their source.
    expect(screen.getAllByText('Package')).toHaveLength(2)
    expect(screen.getByText('user')).toBeInTheDocument()

    // Collapsed by default.
    expect(row('hello')).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByText('Say hello')).toBeInTheDocument()
  })

  it('filter narrows both sections independently and shows of-total counts', async () => {
    renderTab()
    await screen.findByRole('heading', { name: 'User Prompts (1)' })

    fireEvent.change(screen.getByPlaceholderText('Filter prompts…'), { target: { value: 'review' } })
    // Only the package side survives, and its title switches to the of-total form.
    expect(await screen.findByRole('heading', { name: 'Packages Prompts (1 of 2)' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: /User Prompts/ })).not.toBeInTheDocument()
    expect(screen.queryByText('unknown')).not.toBeInTheDocument()

    // Filtering on a description word reaches the user side instead.
    fireEvent.change(screen.getByPlaceholderText('Filter prompts…'), { target: { value: 'SAY' } })
    expect(await screen.findByRole('heading', { name: 'User Prompts (1 of 1)' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: /Packages Prompts/ })).not.toBeInTheDocument()

    // A miss on both sides leaves only the header card.
    fireEvent.change(screen.getByPlaceholderText('Filter prompts…'), { target: { value: 'zzz' } })
    await waitFor(() => expect(screen.queryByRole('heading', { name: /User Prompts/ })).not.toBeInTheDocument())
    expect(screen.queryByRole('heading', { name: /Packages Prompts/ })).not.toBeInTheDocument()
    // Not the empty state either — the list itself is non-empty.
    expect(screen.queryByText(/No prompts found/)).not.toBeInTheDocument()
  })
})

describe('PromptsTab detail disclosure', () => {
  it('expands a package prompt with its qualified detail key and collapses again', async () => {
    renderTab()
    await screen.findByRole('heading', { name: 'User Prompts (1)' })

    fireEvent.click(row('sage/review'))
    await waitFor(() => expect(mockApi.promptDetail).toHaveBeenCalledWith('sage/review'))
    expect(await screen.findByText('PROMPT BODY')).toBeInTheDocument()
    expect(screen.getByText('/pkgs/sage/review.md')).toBeInTheDocument()
    expect(row('sage/review')).toHaveAttribute('aria-expanded', 'true')

    fireEvent.click(row('sage/review'))
    await waitFor(() => expect(screen.queryByText('PROMPT BODY')).not.toBeInTheDocument())
    expect(row('sage/review')).toHaveAttribute('aria-expanded', 'false')
  })

  it('uses the bare name as detail key when a prompt has no package', async () => {
    renderTab()
    await screen.findByRole('heading', { name: 'User Prompts (1)' })
    fireEvent.click(row('hello'))
    await waitFor(() => expect(mockApi.promptDetail).toHaveBeenCalledWith('hello'))
  })

  it('expands on Enter and on Space', async () => {
    renderTab()
    await screen.findByRole('heading', { name: 'User Prompts (1)' })

    fireEvent.keyDown(row('hello'), { key: 'Enter' })
    await waitFor(() => expect(row('hello')).toHaveAttribute('aria-expanded', 'true'))

    fireEvent.keyDown(row('hello'), { key: ' ' })
    await waitFor(() => expect(row('hello')).toHaveAttribute('aria-expanded', 'false'))

    // An unrelated key does nothing.
    fireEvent.keyDown(row('hello'), { key: 'a' })
    expect(row('hello')).toHaveAttribute('aria-expanded', 'false')
  })

  it('renders empty content rather than crashing when the detail has none', async () => {
    mockApi.promptDetail.mockResolvedValue({})
    renderTab()
    await screen.findByRole('heading', { name: 'User Prompts (1)' })
    fireEvent.click(row('hello'))
    await waitFor(() => expect(row('hello')).toHaveAttribute('aria-expanded', 'true'))
    expect(screen.queryByText('PROMPT BODY')).not.toBeInTheDocument()
  })

  it('still expands, with a failure note, when the detail fetch rejects', async () => {
    mockApi.promptDetail.mockRejectedValue(new Error('404'))
    renderTab()
    await screen.findByRole('heading', { name: 'User Prompts (1)' })
    fireEvent.click(row('hello'))
    expect(await screen.findByText('(failed to load)')).toBeInTheDocument()
    expect(row('hello')).toHaveAttribute('aria-expanded', 'true')
  })

  it('drops a slow first response once a second prompt has been clicked', async () => {
    let releaseFirst: ((v: { content: string }) => void) | undefined
    mockApi.promptDetail
      .mockImplementationOnce(() => new Promise<{ content: string }>(res => { releaseFirst = res }))
      .mockResolvedValue({ content: 'SECOND BODY' })

    renderTab()
    await screen.findByRole('heading', { name: 'User Prompts (1)' })

    fireEvent.click(row('hello'))          // in flight, nothing expanded yet
    fireEvent.click(row('sage/review'))    // supersedes it
    expect(await screen.findByText('SECOND BODY')).toBeInTheDocument()

    releaseFirst?.({ content: 'FIRST BODY' })
    await waitFor(() => expect(screen.getByText('SECOND BODY')).toBeInTheDocument())
    // The stale winner must not steal the open row.
    expect(screen.queryByText('FIRST BODY')).not.toBeInTheDocument()
    expect(row('hello')).toHaveAttribute('aria-expanded', 'false')
    expect(row('sage/review')).toHaveAttribute('aria-expanded', 'true')
  })

  it('drops a slow first FAILURE once a second prompt has been clicked', async () => {
    let rejectFirst: ((e: Error) => void) | undefined
    mockApi.promptDetail
      .mockImplementationOnce(() => new Promise<{ content: string }>((_res, rej) => { rejectFirst = rej }))
      .mockResolvedValue({ content: 'SECOND BODY' })

    renderTab()
    await screen.findByRole('heading', { name: 'User Prompts (1)' })

    fireEvent.click(row('hello'))
    fireEvent.click(row('sage/review'))
    expect(await screen.findByText('SECOND BODY')).toBeInTheDocument()

    rejectFirst?.(new Error('too late'))
    await waitFor(() => expect(screen.getByText('SECOND BODY')).toBeInTheDocument())
    // The superseded failure must not replace the open row's body.
    expect(screen.queryByText('(failed to load)')).not.toBeInTheDocument()
    expect(row('sage/review')).toHaveAttribute('aria-expanded', 'true')
  })
})

describe('PromptsTab slot picker', () => {
  async function openPicker(slots: ChatSlot[] = []) {
    const ctx = renderTab(slots)
    await screen.findByRole('heading', { name: 'User Prompts (1)' })
    fireEvent.click(row('hello'))
    await waitFor(() => expect(row('hello')).toHaveAttribute('aria-expanded', 'true'))
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
    await waitFor(() => expect(mockApi.chatSlotDetail).toHaveBeenCalledWith('chat/7'))
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
