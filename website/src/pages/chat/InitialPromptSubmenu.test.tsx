/**
 * The session header menu's "Initial prompt" submenu (#25977), driven as a
 * component: the lazy read on open, the load failure and its in-place Retry,
 * Copy (acknowledged, refused, then cleared by a later success), and "Draft a
 * new session from this prompt" (taken view, kept view, refused create). The
 * create rules themselves are pinned in startFromInitialPrompt.test.ts, and
 * the transcript walk in initialPrompt.test.ts, so both are mocked here.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { api } from '../../api/client'
import { DropdownMenu, DropdownMenuContent, DropdownMenuTrigger } from '../../components/ui/dropdown-menu'
import type { RootState } from '../../store'
import { createTestStore } from '../../test/helpers'
import { copyToClipboard } from '../../utils/clipboard'
import { loadInitialPrompt } from './initialPrompt'
import InitialPromptSubmenu from './InitialPromptSubmenu'
import { startFromInitialPrompt } from './startFromInitialPrompt'

vi.mock('./initialPrompt', () => ({ loadInitialPrompt: vi.fn() }))
vi.mock('./startFromInitialPrompt', () => ({ startFromInitialPrompt: vi.fn() }))
vi.mock('../../utils/clipboard', () => ({ copyToClipboard: vi.fn() }))

const loadMock = vi.mocked(loadInitialPrompt)
const startMock = vi.mocked(startFromInitialPrompt)
const copyMock = vi.mocked(copyToClipboard)

const SLOT = { key: 'chat-1-100', agent: 'default', project: '/work/repo', folder_id: 'f1' }

const dashboardState = {
  status: {}, connected: true, slots: [SLOT], approvalMode: 'normal',
  channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
  subagentRunning: {}, subagentDetails: {}, subagentText: {},
  sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
} as unknown as RootState['dashboard']

/** A promise the test settles by hand, so an in-flight state is observed
 *  without a timer. */
function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

function renderSubmenu(onStarted = vi.fn()) {
  const store = createTestStore({ dashboard: dashboardState })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <MemoryRouter>
          <DropdownMenu open>
            <DropdownMenuTrigger>menu</DropdownMenuTrigger>
            <DropdownMenuContent>
              <InitialPromptSubmenu slotKey={SLOT.key} mode="plan" onStarted={onStarted} />
            </DropdownMenuContent>
          </DropdownMenu>
        </MemoryRouter>
      </Provider>
    </QueryClientProvider>,
  )
  return { onStarted }
}

/** Radix opens a sub-trigger on ArrowRight (the path happy-dom handles). */
async function openSubmenu() {
  const trigger = await screen.findByTestId('initial-prompt-trigger')
  fireEvent.keyDown(trigger, { key: 'ArrowRight' })
}

const originalMatchMedia = window.matchMedia

beforeEach(() => {
  vi.clearAllMocks()
  loadMock.mockResolvedValue('Fix the flaky test')
})

afterEach(() => {
  Object.defineProperty(window, 'matchMedia', { writable: true, configurable: true, value: originalMatchMedia })
})

describe('InitialPromptSubmenu', () => {
  it('reads the prompt only once the submenu opens, through the slot-detail API', async () => {
    const detail = vi.spyOn(api, 'chatSlotDetail').mockResolvedValue({} as Awaited<ReturnType<typeof api.chatSlotDetail>>)
    loadMock.mockImplementation(async (key, fetchPage) => {
      await fetchPage(key, 50, 50)
      return 'Fix the flaky test'
    })
    renderSubmenu()
    await screen.findByTestId('initial-prompt-trigger')
    expect(loadMock).not.toHaveBeenCalled()

    await openSubmenu()
    expect(await screen.findByTestId('initial-prompt-text')).toHaveTextContent('Fix the flaky test')
    expect(loadMock).toHaveBeenCalledWith(SLOT.key, expect.any(Function))
    expect(detail).toHaveBeenCalledWith(SLOT.key, 50, 50)
    detail.mockRestore()
  })

  it('says so when no prompt has been sent yet', async () => {
    loadMock.mockResolvedValue('')
    renderSubmenu()
    await openSubmenu()
    expect(await screen.findByTestId('initial-prompt-empty')).toBeInTheDocument()
    expect(screen.queryByTestId('initial-prompt-copy')).not.toBeInTheDocument()
  })

  it('shows a load failure with Retry, a loading line while the retry runs, then the prompt', async () => {
    loadMock.mockRejectedValueOnce(new Error('history unreadable'))
    renderSubmenu()
    await openSubmenu()
    expect(await screen.findByTestId('initial-prompt-load-error')).toBeInTheDocument()
    const retry = screen.getByTestId('initial-prompt-retry')

    const retryRead = deferred<string>()
    loadMock.mockReturnValueOnce(retryRead.promise)
    fireEvent.click(retry)
    // The notice does not stay up over a live retry.
    expect(await screen.findByRole('status')).toHaveTextContent('Loading the initial prompt')
    expect(screen.queryByTestId('initial-prompt-load-error')).not.toBeInTheDocument()

    retryRead.resolve('Second try prompt')
    expect(await screen.findByTestId('initial-prompt-text')).toHaveTextContent('Second try prompt')
    expect(screen.queryByTestId('initial-prompt-retry')).not.toBeInTheDocument()
  })

  it('acknowledges a copy, keeps a refused copy said, and clears it on the next success', async () => {
    renderSubmenu()
    await openSubmenu()
    const copy = await screen.findByTestId('initial-prompt-copy')

    copyMock.mockResolvedValueOnce(true)
    fireEvent.click(copy)
    await waitFor(() => expect(screen.getByTestId('initial-prompt-copy-label')).toHaveTextContent('Copied'))
    expect(copyMock).toHaveBeenCalledWith('Fix the flaky test')
    expect(screen.queryByTestId('initial-prompt-copy-error')).not.toBeInTheDocument()

    copyMock.mockResolvedValueOnce(false)
    fireEvent.click(screen.getByTestId('initial-prompt-copy'))
    expect(await screen.findByTestId('initial-prompt-copy-error')).toBeInTheDocument()
    expect(screen.getByTestId('initial-prompt-copy')).toHaveAttribute('aria-describedby')

    copyMock.mockResolvedValueOnce(true)
    fireEvent.click(screen.getByTestId('initial-prompt-copy'))
    await waitFor(() => expect(screen.queryByTestId('initial-prompt-copy-error')).not.toBeInTheDocument())
    expect(screen.getByTestId('initial-prompt-copy')).not.toHaveAttribute('aria-describedby')
  })

  it('starts a new session from the prompt and tells the host once it took the view', async () => {
    const create = deferred<boolean>()
    startMock.mockReturnValueOnce(create.promise)
    const { onStarted } = renderSubmenu()
    await openSubmenu()
    const item = await screen.findByTestId('initial-prompt-new-session')
    fireEvent.click(item)

    // While the create runs the row says so and a second pick is refused.
    await waitFor(() => expect(item).toHaveTextContent('Opening a new draft session'))
    expect(item).toHaveAttribute('data-disabled')
    fireEvent.click(item)
    expect(startMock).toHaveBeenCalledTimes(1)
    expect(startMock).toHaveBeenCalledWith(
      expect.any(Function),
      expect.any(Function),
      { prompt: 'Fix the flaky test', source: expect.objectContaining({ key: SLOT.key, project: SLOT.project }), mode: 'plan' },
    )

    create.resolve(true)
    await waitFor(() => expect(onStarted).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(item).toHaveTextContent('Draft a new session from this prompt'))
  })

  it('leaves the menu to the host when the user moved to another session meanwhile', async () => {
    startMock.mockResolvedValueOnce(false)
    const { onStarted } = renderSubmenu()
    await openSubmenu()
    fireEvent.click(await screen.findByTestId('initial-prompt-new-session'))
    await waitFor(() => expect(startMock).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByTestId('initial-prompt-new-session')).not.toHaveAttribute('data-disabled'))
    expect(onStarted).not.toHaveBeenCalled()
    expect(screen.queryByTestId('initial-prompt-new-session-error')).not.toBeInTheDocument()
  })

  it('renders a refused create in place, with the error message, and clears it on the next try', async () => {
    startMock.mockRejectedValueOnce(new Error('gateway refused the create'))
    const { onStarted } = renderSubmenu()
    await openSubmenu()
    fireEvent.click(await screen.findByTestId('initial-prompt-new-session'))
    const notice = await screen.findByTestId('initial-prompt-new-session-error')
    expect(notice).toHaveTextContent('gateway refused the create')
    expect(screen.getByTestId('initial-prompt-new-session')).toHaveAttribute('aria-describedby')

    // A rejection with no message of its own falls back to the stock detail.
    startMock.mockRejectedValueOnce(undefined)
    fireEvent.click(screen.getByTestId('initial-prompt-new-session'))
    await waitFor(() =>
      expect(screen.getByTestId('initial-prompt-new-session-error')).toHaveTextContent('No session was created'))
    expect(onStarted).not.toHaveBeenCalled()
  })

  it('renders inline inside the parent menu in a window too narrow for a flyout', async () => {
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      configurable: true,
      value: (query: string) => ({
        matches: query === '(max-width: 767px)',
        media: query,
        addEventListener: () => {},
        removeEventListener: () => {},
        addListener: () => {},
        removeListener: () => {},
        dispatchEvent: () => false,
      }),
    })
    renderSubmenu()
    // Inline, the trigger toggles the section open on a click.
    fireEvent.click(await screen.findByTestId('initial-prompt-trigger'))
    expect(await screen.findByTestId('initial-prompt-text')).toHaveTextContent('Fix the flaky test')
    expect(screen.getByTestId('initial-prompt-panel').className).toContain('max-w-[min(320px,calc(100vw-64px))]')
  })
})
