import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import WorktreePicker, { WorktreeRow } from '../components/WorktreePicker'

// Mock the API client — WorktreePicker only touches these three methods.
vi.mock('../api/client', () => ({
  api: {
    worktreeList: vi.fn(),
    createWorktree: vi.fn(),
    worktreeRemove: vi.fn(),
  },
}))
import { api } from '../api/client'

const rect = (): DOMRect =>
  ({ top: 400, bottom: 424, left: 100, right: 540, width: 440, height: 24, x: 100, y: 400, toJSON() {} }) as DOMRect

const wt = (over: Partial<WorktreeRow> = {}): WorktreeRow => ({
  path: '/repo',
  branch: 'main',
  head: 'abc1234',
  is_main: true,
  detached: false,
  bare: false,
  locked: false,
  dirty: false,
  active_session: null,
  ...over,
})

function setup(props: Partial<React.ComponentProps<typeof WorktreePicker>> = {}) {
  const onSelect = vi.fn()
  const onOpenChange = vi.fn()
  const utils = render(
    <WorktreePicker
      open
      onOpenChange={onOpenChange}
      anchorRect={rect()}
      repo="/repo"
      activeSlot="chat-1"
      onSelect={onSelect}
      {...props}
    />,
  )
  return { onSelect, onOpenChange, ...utils }
}

describe('WorktreePicker', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.worktreeList as ReturnType<typeof vi.fn>).mockResolvedValue({
      worktrees: [
        wt({ path: '/repo', branch: 'trunk', is_main: true, active_session: 'chat-1' }),
        wt({ path: '/repo-wt-feature', branch: 'feat/upload', is_main: false }),
      ],
    })
  })

  it('lists worktrees for the repo and marks the main + current session', async () => {
    setup()
    await waitFor(() => expect(api.worktreeList).toHaveBeenCalledWith('/repo'))
    expect(await screen.findByText('trunk')).toBeInTheDocument()
    expect(screen.getByText('feat/upload')).toBeInTheDocument()
    // The main worktree carries the "main" badge…
    expect(screen.getByText('main')).toBeInTheDocument()
    // …and the row bound to the active slot reads as "this session".
    expect(screen.getByText(/this session/i)).toBeInTheDocument()
  })

  it('renders nothing when closed', () => {
    const { container } = setup({ open: false })
    expect(container.firstChild).toBeNull()
  })

  it('renders nothing without an anchor rect', () => {
    const { container } = setup({ anchorRect: null })
    expect(container.firstChild).toBeNull()
  })

  it('selecting a worktree hands its path back and closes', async () => {
    const { onSelect, onOpenChange } = setup()
    const row = await screen.findByText('feat/upload')
    fireEvent.click(row)
    expect(onSelect).toHaveBeenCalledWith('/repo-wt-feature')
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('does not let you select a worktree active in another session', async () => {
    ;(api.worktreeList as ReturnType<typeof vi.fn>).mockResolvedValue({
      worktrees: [
        wt({ path: '/repo', branch: 'main', is_main: true, active_session: 'chat-1' }),
        wt({ path: '/repo-wt-busy', branch: 'feat/busy', is_main: false, active_session: 'chat-9' }),
      ],
    })
    const { onSelect } = setup()
    const row = await screen.findByText('feat/busy')
    fireEvent.click(row)
    expect(onSelect).not.toHaveBeenCalled()
    expect(screen.getByText(/^in use$/i)).toBeInTheDocument()
  })

  it('creates a new worktree and switches to it', async () => {
    ;(api.createWorktree as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, path: '/repo-wt-new', branch: 'feat/new' })
    const { onSelect } = setup()
    await screen.findByText('feat/upload')
    fireEvent.change(screen.getByLabelText(/new branch name/i), { target: { value: 'feat/new' } })
    fireEvent.click(screen.getByRole('button', { name: /^create$/i }))
    await waitFor(() => expect(api.createWorktree).toHaveBeenCalledWith('/repo', 'feat/new'))
    expect(onSelect).toHaveBeenCalledWith('/repo-wt-new')
  })

  it('removes a clean worktree and refreshes the list', async () => {
    ;(api.worktreeRemove as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, path: '/repo-wt-feature' })
    setup()
    const removeBtn = await screen.findByRole('button', { name: /remove worktree/i })
    fireEvent.click(removeBtn)
    await waitFor(() => expect(api.worktreeRemove).toHaveBeenCalledWith('/repo', '/repo-wt-feature', false))
    // A successful removal re-lists.
    await waitFor(() => expect(api.worktreeList).toHaveBeenCalledTimes(2))
  })

  it('asks for confirmation before force-removing a dirty worktree', async () => {
    ;(api.worktreeRemove as ReturnType<typeof vi.fn>).mockResolvedValue({ dirty: true, error: 'This worktree has uncommitted changes.' })
    setup()
    const removeBtn = await screen.findByRole('button', { name: /remove worktree/i })
    fireEvent.click(removeBtn)
    // First call is the non-force attempt that the server refused.
    await waitFor(() => expect(api.worktreeRemove).toHaveBeenCalledWith('/repo', '/repo-wt-feature', false))
    const force = await screen.findByRole('button', { name: /remove anyway/i })
    ;(api.worktreeRemove as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, path: '/repo-wt-feature' })
    fireEvent.click(force)
    await waitFor(() => expect(api.worktreeRemove).toHaveBeenCalledWith('/repo', '/repo-wt-feature', true))
  })

  it('shows an empty state when there are no worktrees', async () => {
    ;(api.worktreeList as ReturnType<typeof vi.fn>).mockResolvedValue({ worktrees: [] })
    setup()
    expect(await screen.findByText(/no worktrees found/i)).toBeInTheDocument()
  })
})
