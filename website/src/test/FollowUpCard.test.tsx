import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import FollowUpCard from '../components/FollowUpCard'
import reducer, { setFollowupCard, clearFollowupCard, dismissFollowupItem, deleteSlot } from '../store/chatSlice'
import { sseSlots } from '../store/dashboardSlice'
import type { FollowupItem } from '../store/chatSlice'

const item = (over: Partial<FollowupItem> = {}): FollowupItem => ({
  title: 'Add rate limiting',
  description: 'The upload endpoint is unbounded.',
  prompt: 'Add a token-bucket limiter to POST /api/upload.',
  ...over,
})

function setup(props: Partial<React.ComponentProps<typeof FollowUpCard>> = {}) {
  const onAddToSession = vi.fn()
  const onStartInWorktree = vi.fn().mockResolvedValue(undefined)
  const onStartInNewChat = vi.fn().mockResolvedValue(undefined)
  const onSkip = vi.fn()
  // A router, because one-click mode links its footer to Settings.
  const utils = render(
    <FollowUpCard
      items={[item()]}
      projectDir="/repo"
      onAddToSession={onAddToSession}
      onStartInWorktree={onStartInWorktree}
      onStartInNewChat={onStartInNewChat}
      onSkip={onSkip}
      {...props}
    />,
    { wrapper: MemoryRouter },
  )
  return { onAddToSession, onStartInWorktree, onStartInNewChat, onSkip, ...utils }
}

describe('FollowUpCard', () => {
  it('renders the title, description and all three actions', () => {
    setup()
    expect(screen.getByText('Add rate limiting')).toBeInTheDocument()
    expect(screen.getByText('The upload endpoint is unbounded.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /start in a separate copy/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /add to this session/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /skip/i })).toBeInTheDocument()
  })

  it('pre-fills by default: the footer says nothing is sent and Start shows no send glyph', () => {
    const { container } = setup()
    const start = screen.getByRole('button', { name: /start in a separate copy/i })
    // The worktree footer says what the copy is, so the label needs no git term.
    expect(start).toHaveAccessibleDescription(/^Start in a separate copy opens a new session on a fresh copy of this project, in a new folder next to it, with the prompt in its message box\. Your current files and this session stay as they are\..*Nothing is sent until you press send\.$/)
    expect(container.querySelector('.lucide-send')).toBeNull()
    expect(screen.queryByText(/sends the prompt/i)).not.toBeInTheDocument()
  })

  it('describes both Start buttons with the footer that says Start sends when one-click send is on', () => {
    const { container } = setup({ startSends: true })
    expect(screen.getByRole('button', { name: /run in a separate copy/i })).toHaveAccessibleDescription(/sends the prompt to a new session/i)
    expect(container.querySelector('.lucide-send')).not.toBeNull()
    setup({ projectDir: undefined, startSends: true })
    expect(screen.getByRole('button', { name: /run in new chat/i })).toHaveAccessibleDescription(/run in new chat sends the prompt/i)
  })

  it('states, with one-click send on, that Start sends the prompt and Add only puts it in the message box', () => {
    setup({ projectDir: undefined, startSends: true })
    expect(screen.getByText(/run in new chat sends the prompt/i)).toBeInTheDocument()
    expect(screen.getByText(/puts the prompt in your message box and sends nothing/i)).toBeInTheDocument()
  })

  it('explains on the worktree route where Start sends and that this session is left alone', () => {
    setup({ startSends: true })
    const footer = screen.getByText(/fresh copy of this project, in a new folder next to it/i)
    expect(footer).toHaveTextContent(/Composer\. Run in a separate copy sends the prompt to a new session/)
    expect(footer).toHaveTextContent(/your current files and this session stay as they are/i)
    // The run starts in the background, so the footer says the user stays put.
    expect(footer).toHaveTextContent(/You stay where you are, and the new session shows in the sidebar\. Add to this session/)
    expect(footer).toHaveTextContent(/puts the prompt in your message box and sends nothing/i)
  })

  it('gives one-click Start its own verb, so it never looks like the pre-fill Start', () => {
    setup({ startSends: true })
    expect(screen.getByRole('button', { name: 'Run in a separate copy' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /start in a separate copy/i })).not.toBeInTheDocument()
    setup({ projectDir: undefined, startSends: true })
    expect(screen.getByRole('button', { name: 'Run in new chat' })).toBeInTheDocument()
  })

  it.each([
    ['the worktree route', {}, /^Sends right away because Send follow-ups with one click is on\. Turn it off in Settings → Chat → Composer\. Run in a separate copy sends/],
    ['a plain new chat', { projectDir: undefined }, /^Sends right away because Send follow-ups with one click is on\. Turn it off in Settings → Chat → Composer\. Run in new chat sends/],
    ['a project directory that is not a repository', { projectIsRepo: false }, /^Sends right away because Send follow-ups with one click is on\. Turn it off in Settings → Chat → Composer\. Run in new chat sends the prompt as the first message of a new chat in this session's project directory/],
  ])('opens the one-click footer on %s with why it sends and where to turn it off', (_route, props, lead) => {
    setup({ ...props, startSends: true })
    const link = screen.getByRole('link', { name: 'Send follow-ups with one click' })
    expect(link).toHaveAttribute('href', '/settings/chat/composer?highlight=chat.send-follow-ups-with-one-click')
    expect(link.closest('span')).toHaveTextContent(lead)
  })

  it('shows no one-click notice or Settings link in the default pre-fill mode', () => {
    setup()
    expect(screen.queryByText(/sends right away/i)).not.toBeInTheDocument()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })

  it('passes the mode the footer described to Start', () => {
    const off = setup()
    fireEvent.click(screen.getByRole('button', { name: /start in a separate copy/i }))
    expect(off.onStartInWorktree).toHaveBeenCalledWith(item(), 0, 'prefill')
    off.unmount()
    const on = setup({ startSends: true })
    fireEvent.click(screen.getByRole('button', { name: /run in a separate copy/i }))
    expect(on.onStartInWorktree).toHaveBeenCalledWith(item(), 0, 'send')
  })

  it('shows a short prompt in full with no toggle', () => {
    setup()
    // The exact text Start will send is on the card before any click.
    expect(screen.getByTestId('followup-prompt')).toHaveTextContent('Add a token-bucket limiter to POST /api/upload.')
    expect(screen.queryByRole('button', { name: 'Show prompt' })).not.toBeInTheDocument()
  })

  it('shows the first three lines of a longer prompt and expands the rest', () => {
    const prompt = 'line one\nline two\nline three\nline four: the rest of the task'
    setup({ items: [item({ prompt })] })
    const box = screen.getByTestId('followup-prompt')
    // The trailing ellipsis says the prompt goes on past what is shown.
    expect(box.textContent).toBe('line one\nline two\nline three…')
    const toggle = screen.getByRole('button', { name: 'Show prompt' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(toggle).toHaveAttribute('aria-controls', box.id)
    fireEvent.click(toggle)
    const hide = screen.getByRole('button', { name: 'Hide prompt' })
    expect(hide).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByTestId('followup-prompt').textContent).toBe(prompt)
    fireEvent.click(hide)
    expect(screen.getByTestId('followup-prompt').textContent).toBe('line one\nline two\nline three…')
  })

  it('starts the preview at the first line with text', () => {
    // Counting the blank lines would fill all three preview lines and show only "…".
    setup({ items: [item({ prompt: '\n\n\nline one\nline two\nline three\nline four' })] })
    expect(screen.getByTestId('followup-prompt').textContent).toBe('line one\nline two\nline three…')
    expect(screen.getByRole('button', { name: 'Show prompt' })).toBeInTheDocument()
  })

  it('shows a short prompt after blank lines in full with no toggle', () => {
    setup({ items: [item({ prompt: '\n\n\nonly line' })] })
    expect(screen.getByTestId('followup-prompt')).toHaveTextContent('only line')
    expect(screen.queryByRole('button', { name: 'Show prompt' })).not.toBeInTheDocument()
  })

  it('offers Show prompt for one long line that wraps past three lines', () => {
    setup({ items: [item({ prompt: 'x'.repeat(241) })] })
    expect(screen.getByTestId('followup-prompt').textContent).toBe(`${'x'.repeat(240)}…`)
    expect(screen.getByRole('button', { name: 'Show prompt' })).toBeInTheDocument()
  })

  it('expands one item\'s prompt without expanding its siblings', () => {
    render(
      <FollowUpCard
        items={[item({ title: 'First', prompt: 'a\nb\nc\nfirst tail' }), item({ title: 'Second', prompt: 'a\nb\nc\nsecond tail' })]}
        projectDir="/repo"
        onAddToSession={vi.fn()}
        onStartInWorktree={vi.fn()}
        onStartInNewChat={vi.fn()}
        onSkip={vi.fn()}
      />,
    )
    fireEvent.click(screen.getAllByRole('button', { name: 'Show prompt' })[1])
    const boxes = screen.getAllByTestId('followup-prompt')
    expect(boxes[1]).toHaveTextContent('second tail')
    expect(boxes[0]).not.toHaveTextContent('first tail')
  })

  it('calls onAddToSession with the item', () => {
    const { onAddToSession } = setup()
    fireEvent.click(screen.getByRole('button', { name: /add to this session/i }))
    expect(onAddToSession).toHaveBeenCalledWith(item(), 0)
  })

  it('passes the clicked item and its index to Start and Add', async () => {
    const items = [item({ title: 'A' }), item({ title: 'B' }), item({ title: 'C' })]
    const { onAddToSession, onStartInWorktree } = setup({ items })
    fireEvent.click(screen.getAllByRole('button', { name: /start in a separate copy/i })[1])
    expect(onStartInWorktree).toHaveBeenCalledWith(items[1], 1, 'prefill')
    // Every action is disabled while a start is pending.
    await waitFor(() => expect(screen.getAllByRole('button', { name: /add to this session/i })[2]).toBeEnabled())
    fireEvent.click(screen.getAllByRole('button', { name: /add to this session/i })[2])
    expect(onAddToSession).toHaveBeenCalledWith(items[2], 2)
  })

  it('passes the item index to onSkip so siblings survive', () => {
    const onSkip = vi.fn()
    render(
      <FollowUpCard
        items={[item({ title: 'First' }), item({ title: 'Second' })]}
        projectDir="/repo"
        onAddToSession={vi.fn()}
        onStartInWorktree={vi.fn()}
        onStartInNewChat={vi.fn()}
        onSkip={onSkip}
      />,
    )
    fireEvent.click(screen.getAllByRole('button', { name: /skip/i })[1])
    expect(onSkip).toHaveBeenCalledWith(1)
  })

  it('keeps the worktree action and label in a git repository, with the worktree footer', () => {
    const { onStartInWorktree, onStartInNewChat } = setup({ projectIsRepo: true })
    // The label says what it is in plain words; the git term is in the tooltip.
    const worktree = screen.getByRole('button', { name: 'Start in a separate copy' })
    expect(worktree.className).toContain('bg-accent')
    expect(worktree).toHaveAttribute('title', 'Create a git worktree (a separate copy of /repo on its own branch) and open a new session there')
    expect(screen.queryByRole('button', { name: /start in new chat/i })).not.toBeInTheDocument()
    fireEvent.click(worktree)
    expect(onStartInWorktree).toHaveBeenCalledWith(item(), 0, 'prefill')
    expect(onStartInNewChat).not.toHaveBeenCalled()
  })

  it('offers an enabled "Start in new chat" when the session has no project dir', () => {
    const { onStartInWorktree, onStartInNewChat } = setup({ projectDir: undefined })
    expect(screen.queryByRole('button', { name: /start in a separate copy/i })).not.toBeInTheDocument()
    const newChat = screen.getByRole('button', { name: /start in new chat/i })
    expect(newChat).not.toBeDisabled()
    // The spawn route is the primary action again, so it keeps the accent look.
    expect(newChat.className).toContain('bg-accent')
    // The in-session route stays available too.
    expect(screen.getByRole('button', { name: /add to this session/i })).not.toBeDisabled()
    fireEvent.click(newChat)
    expect(onStartInNewChat).toHaveBeenCalledWith(item(), 0, 'prefill')
    expect(onStartInWorktree).not.toHaveBeenCalled()
  })

  it('keeps the standard footer for an unscoped session, with no git terms', () => {
    setup({ projectDir: undefined })
    expect(screen.getByText('Start and Add to this session both put the prompt in a message box. Nothing is sent until you press send.')).toBeInTheDocument()
    expect(screen.queryByText(/unavailable|worktree|git/i)).not.toBeInTheDocument()
  })

  it('keeps the one-click footer for an unscoped session free of git terms too', () => {
    setup({ projectDir: undefined, startSends: true })
    expect(screen.getByText(/Run in new chat sends the prompt as the first message of a new chat\. You stay where you are, and the new session shows in the sidebar\. Add to this session puts the prompt in your message box and sends nothing\.$/)).toBeInTheDocument()
    expect(screen.queryByText(/unavailable|worktree|git/i)).not.toBeInTheDocument()
  })

  it('offers an enabled "Start in new chat" when the project is not a git repo', async () => {
    const { onStartInWorktree, onStartInNewChat } = setup({ projectIsRepo: false })
    expect(screen.queryByRole('button', { name: /start in a separate copy/i })).not.toBeInTheDocument()
    const newChat = screen.getByRole('button', { name: /start in new chat/i })
    expect(newChat).not.toBeDisabled()
    expect(newChat.className).toContain('bg-accent')
    // The tooltip carries the reason; the always-visible footer says where the
    // chat opens, in session terms.
    expect(newChat.getAttribute('title')).toContain('/repo')
    expect(newChat.getAttribute('title')).toMatch(/not a git repository/)
    fireEvent.click(newChat)
    await waitFor(() => expect(onStartInNewChat).toHaveBeenCalledWith(item(), 0, 'prefill'))
    expect(onStartInWorktree).not.toHaveBeenCalled()
    expect(screen.getByText(/nothing is sent until you press send\. The new chat opens in this session's project directory/i)).toBeInTheDocument()
    expect(screen.queryByText(/unavailable|worktree|git/i)).not.toBeInTheDocument()
  })

  it('offers "Start in new chat" for a plain folder inside a repository rooted higher up', async () => {
    // The probe says "in git", but the folder is not the work-tree root, so a
    // worktree would branch the whole enclosing repository.
    const { onStartInWorktree, onStartInNewChat } = setup({
      projectDir: '/home/me/notes', projectIsRepo: true, enclosingRepoRoot: '/home/me',
    })
    expect(screen.queryByRole('button', { name: /start in a separate copy/i })).not.toBeInTheDocument()
    const newChat = screen.getByRole('button', { name: /start in new chat/i })
    expect(newChat).not.toBeDisabled()
    expect(newChat).toHaveAttribute(
      'title',
      'Open a new chat in /home/me/notes without a worktree, because it is inside the git repository at /home/me, not its root',
    )
    fireEvent.click(newChat)
    await waitFor(() => expect(onStartInNewChat).toHaveBeenCalledWith(item(), 0, 'prefill'))
    expect(onStartInWorktree).not.toHaveBeenCalled()
    expect(screen.getByText(/The new chat opens in this session's project directory/i)).toBeInTheDocument()
  })

  it('says where the one-click new chat opens when the project is not a git repo', () => {
    setup({ projectIsRepo: false, startSends: true })
    expect(screen.getByText(/sends the prompt as the first message of a new chat in this session's project directory/i)).toBeInTheDocument()
  })

  it('ignores an enclosing repository root when the session has no project dir', () => {
    setup({ projectDir: undefined, enclosingRepoRoot: '/home/me' })
    expect(screen.getByRole('button', { name: /start in new chat/i })).toHaveAttribute(
      'title', 'Open a new chat without a worktree, because this session has no project directory',
    )
  })

  it('renders a new-chat failure inline and lets the user retry', async () => {
    const onStartInNewChat = vi.fn().mockRejectedValue(new Error('The new chat could not be opened.'))
    setup({ projectIsRepo: false, onStartInNewChat })
    fireEvent.click(screen.getByRole('button', { name: /start in new chat/i }))
    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('The new chat could not be opened.')
    })
    expect(screen.getByRole('button', { name: /start in new chat/i })).not.toBeDisabled()
  })

  it('does not open a second chat while one is being opened', async () => {
    let release: (() => void) | undefined
    const onStartInNewChat = vi.fn(() => new Promise<void>(res => { release = res }))
    setup({ projectDir: undefined, onStartInNewChat })
    const btn = screen.getByRole('button', { name: /start in new chat/i })
    fireEvent.click(btn)
    await waitFor(() => expect(screen.getByText(/opening chat/i)).toBeInTheDocument())
    fireEvent.click(btn)
    expect(onStartInNewChat).toHaveBeenCalledTimes(1)
    release?.()
  })

  it('keeps the worktree action enabled while the repo probe is unresolved', () => {
    // The server stays the backstop; an unknown probe must not suppress a real repo.
    setup({ projectIsRepo: undefined })
    expect(screen.getByRole('button', { name: /start in a separate copy/i })).not.toBeDisabled()
  })

  it('renders the worktree failure inline instead of throwing', async () => {
    const onStartInWorktree = vi.fn().mockRejectedValue(new Error('Branch already exists: feat/x'))
    setup({ onStartInWorktree })
    fireEvent.click(screen.getByRole('button', { name: /start in a separate copy/i }))
    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('Branch already exists: feat/x')
    })
    // Button is usable again so the user can retry after fixing the branch.
    expect(screen.getByRole('button', { name: /start in a separate copy/i })).not.toBeDisabled()
  })

  it('does not fire a second worktree call while one is in flight', async () => {
    let release: (() => void) | undefined
    const onStartInWorktree = vi.fn(() => new Promise<void>(res => { release = res }))
    setup({ onStartInWorktree })
    const btn = screen.getByRole('button', { name: /start in a separate copy/i })
    fireEvent.click(btn)
    await waitFor(() => expect(screen.getByText(/creating the copy/i)).toBeInTheDocument())
    fireEvent.click(btn)
    expect(onStartInWorktree).toHaveBeenCalledTimes(1)
    release?.()
  })

  it('ignores a worktree failure that lands after the items changed', async () => {
    // The rejection would otherwise write its error against the NEW list's
    // index, misattributing it to a different suggestion.
    let reject: ((e: Error) => void) | undefined
    const onStartInWorktree = vi.fn(() => new Promise<void>((_res, rej) => { reject = rej }))
    const a = item({ title: 'A' })
    const b = item({ title: 'B' })
    const props = { projectDir: '/repo', onAddToSession: vi.fn(), onStartInWorktree, onStartInNewChat: vi.fn(), onSkip: vi.fn() }
    const { rerender } = render(<FollowUpCard items={[a, b]} {...props} />)
    fireEvent.click(screen.getAllByRole('button', { name: /start in a separate copy/i })[0])
    await waitFor(() => expect(onStartInWorktree).toHaveBeenCalled())
    rerender(<FollowUpCard items={[b]} {...props} />)
    reject?.(new Error('Branch already exists'))
    await waitFor(() => expect(screen.getByText('B')).toBeInTheDocument())
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('drops a failed item\'s error when the item list changes', async () => {
    // Errors are keyed by array index; skipping the failed item shifts its
    // sibling into that index. Without the reset, B would render under A's error.
    const a = item({ title: 'A' })
    const b = item({ title: 'B' })
    const onStartInWorktree = vi.fn().mockRejectedValue(new Error('Branch already exists: feat/a'))
    const { rerender } = render(
      <FollowUpCard
        items={[a, b]}
        projectDir="/repo"
        onAddToSession={vi.fn()}
        onStartInWorktree={onStartInWorktree}
        onStartInNewChat={vi.fn()}
        onSkip={vi.fn()}
      />,
    )
    fireEvent.click(screen.getAllByRole('button', { name: /start in a separate copy/i })[0])
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument())
    rerender(
      <FollowUpCard
        items={[b]}
        projectDir="/repo"
        onAddToSession={vi.fn()}
        onStartInWorktree={onStartInWorktree}
        onStartInNewChat={vi.fn()}
        onSkip={vi.fn()}
      />,
    )
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
    expect(screen.getByText('B')).toBeInTheDocument()
  })
})

describe('followup card reducers', () => {
  const initial = reducer(undefined, { type: 'init' })

  it('sets and clears a card for its own slot', () => {
    const withCard = reducer(initial, setFollowupCard({ slot: 'chat-1', items: [item()], ts: 10 }))
    expect(withCard.followups['chat-1'].items).toHaveLength(1)
    expect(reducer(withCard, clearFollowupCard({ slot: 'chat-1' })).followups['chat-1']).toBeUndefined()
  })

  it('a card in one session does not evict another session\'s card', () => {
    const a = reducer(initial, setFollowupCard({ slot: 'chat-a', items: [item({ title: 'A' })], ts: 1 }))
    const both = reducer(a, setFollowupCard({ slot: 'chat-b', items: [item({ title: 'B' })], ts: 2 }))
    expect(both.followups['chat-a'].items[0].title).toBe('A')
    expect(both.followups['chat-b'].items[0].title).toBe('B')
  })

  it('clearing one session leaves the other intact', () => {
    const a = reducer(initial, setFollowupCard({ slot: 'chat-a', items: [item()], ts: 1 }))
    const both = reducer(a, setFollowupCard({ slot: 'chat-b', items: [item()], ts: 2 }))
    const cleared = reducer(both, clearFollowupCard({ slot: 'chat-a' }))
    expect(cleared.followups['chat-a']).toBeUndefined()
    expect(cleared.followups['chat-b']).toBeDefined()
  })

  it('a stale clear does not remove a newer card for the same slot', () => {
    const older = reducer(initial, setFollowupCard({ slot: 'chat-1', items: [item({ title: 'old' })], ts: 100 }))
    const newer = reducer(older, setFollowupCard({ slot: 'chat-1', items: [item({ title: 'new' })], ts: 200 }))
    // An in-flight worktree action started against ts=100 completes now.
    const after = reducer(newer, clearFollowupCard({ slot: 'chat-1', ts: 100 }))
    expect(after.followups['chat-1']?.items[0].title).toBe('new')
  })

  it('a matching clear does remove the card', () => {
    const withCard = reducer(initial, setFollowupCard({ slot: 'chat-1', items: [item()], ts: 100 }))
    expect(reducer(withCard, clearFollowupCard({ slot: 'chat-1', ts: 100 })).followups['chat-1']).toBeUndefined()
  })

  it('dismissing one item keeps the rest', () => {
    const withCard = reducer(
      initial,
      setFollowupCard({ slot: 'chat-1', items: [item({ title: 'A' }), item({ title: 'B' })], ts: 1 }),
    )
    const after = reducer(withCard, dismissFollowupItem({ slot: 'chat-1', index: 0 }))
    expect(after.followups['chat-1'].items.map(i => i.title)).toEqual(['B'])
  })

  it('dismissing with the item removes it even after another removal shifted its index', () => {
    const [a, b, c] = [item({ title: 'A' }), item({ title: 'B' }), item({ title: 'C' })]
    let s = reducer(initial, setFollowupCard({ slot: 'chat-1', items: [a, b, c], ts: 10 }))
    // A start of C (rendered at index 2) is in flight when A is skipped.
    s = reducer(s, dismissFollowupItem({ slot: 'chat-1', index: 0, ts: 10 }))
    s = reducer(s, dismissFollowupItem({ slot: 'chat-1', index: 2, ts: 10, item: c }))
    expect(s.followups['chat-1'].items).toEqual([b])
  })

  it('dismissing an item that is already gone removes nothing', () => {
    const [a, b] = [item({ title: 'A' }), item({ title: 'B' })]
    const s = reducer(initial, setFollowupCard({ slot: 'chat-1', items: [a, b], ts: 10 }))
    const after = reducer(s, dismissFollowupItem({ slot: 'chat-1', index: 0, ts: 10, item: item({ title: 'Z' }) }))
    expect(after.followups['chat-1'].items).toEqual([a, b])
  })

  it('dismissing the last item clears the card', () => {
    const withCard = reducer(initial, setFollowupCard({ slot: 'chat-1', items: [item()], ts: 1 }))
    expect(reducer(withCard, dismissFollowupItem({ slot: 'chat-1', index: 0 })).followups['chat-1']).toBeUndefined()
  })

  it('a new card replaces an unacted-on one in the SAME slot rather than stacking', () => {
    const first = reducer(initial, setFollowupCard({ slot: 'chat-1', items: [item({ title: 'A' })], ts: 1 }))
    const second = reducer(first, setFollowupCard({ slot: 'chat-1', items: [item({ title: 'B' })], ts: 2 }))
    expect(second.followups['chat-1'].items).toHaveLength(1)
    expect(second.followups['chat-1'].items[0].title).toBe('B')
  })

  it('a stale dismiss does not delete an index from a newer card', () => {
    // A replacement card can land between render and Skip click; an
    // unqualified dismiss would drop that index from a card never seen.
    const withCard = reducer(initial, setFollowupCard({ slot: 'chat-1', items: [item({ title: 'A' }), item({ title: 'B' })], ts: 10 }))
    const replaced = reducer(withCard, setFollowupCard({ slot: 'chat-1', items: [item({ title: 'C' })], ts: 20 }))
    const stale = reducer(replaced, dismissFollowupItem({ slot: 'chat-1', index: 0, ts: 10 }))
    expect(stale.followups['chat-1'].items.map(i => i.title)).toEqual(['C'])
    // A matching ts still dismisses.
    const fresh = reducer(replaced, dismissFollowupItem({ slot: 'chat-1', index: 0, ts: 20 }))
    expect(fresh.followups['chat-1']).toBeUndefined()
  })

  it('refuses to index the map with a prototype-pollution key', () => {
    // Slot names are server-normalized to [\w\-.], which PERMITS __proto__.
    const own = (map: object, key: string) => Object.prototype.hasOwnProperty.call(map, key)
    for (const key of ['__proto__', 'constructor', 'prototype']) {
      const after = reducer(initial, setFollowupCard({ slot: key, items: [item()], ts: 1 }))
      // `constructor`/`prototype` resolve through the prototype chain, so assert
      // no OWN entry was written rather than an undefined read.
      expect(own(after.followups, key)).toBe(false)
      expect(Object.getPrototypeOf(after.followups)).toBe(Object.prototype)
      // The clear/dismiss reducers must be equally inert on such a key.
      expect(own(reducer(after, clearFollowupCard({ slot: key })).followups, key)).toBe(false)
      expect(
        own(reducer(after, dismissFollowupItem({ slot: key, index: 0 })).followups, key),
      ).toBe(false)
    }
  })

  it('ignores an empty item list', () => {
    expect(reducer(initial, setFollowupCard({ slot: 'chat-1', items: [], ts: 1 })).followups['chat-1']).toBeUndefined()
  })

  it('drops a deleted slot\'s follow-up card', () => {
    const withCard = reducer(initial, setFollowupCard({ slot: 'chat-1', items: [item()], ts: 1 }))
    const after = reducer(withCard, { type: deleteSlot.fulfilled.type, payload: 'chat-1' })
    expect(after.followups['chat-1']).toBeUndefined()
  })

  it('prunes follow-up cards for slots the server no longer lists', () => {
    let s = reducer(initial, setFollowupCard({ slot: 'chat-live', items: [item({ title: 'A' })], ts: 1 }))
    s = reducer(s, setFollowupCard({ slot: 'chat-gone', items: [item({ title: 'B' })], ts: 2 }))
    const after = reducer(s, sseSlots([{ key: 'chat-live' }] as never))
    expect(after.followups['chat-live']).toBeDefined()
    expect(after.followups['chat-gone']).toBeUndefined()
  })
})
