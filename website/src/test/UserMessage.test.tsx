import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import UserMessage from '../pages/chat/UserMessage'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn().mockResolvedValue(undefined) }))
vi.mock('../utils/shareUrl', () => ({ copySessionLink: vi.fn().mockResolvedValue(undefined) }))

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { act(() => { vi.runAllTimers() }); vi.useRealTimers() })
import { copyToClipboard } from '../utils/clipboard'
import { copySessionLink } from '../utils/shareUrl'

const renderContent = (content: string) => <span data-testid="content">{content}</span>

describe('UserMessage', () => {
  it('renders message content', () => {
    render(<UserMessage content="hello" renderContent={renderContent} />)
    expect(screen.getByTestId('content')).toHaveTextContent('hello')
  })

  // the bubble must NOT force white-space: pre-wrap. User-typed
  // line breaks (Shift+Enter) are preserved at the markdown level —
  // renderUserContentCb renders through MarkdownRenderer with `softBreaks`,
  // turning soft breaks into <br>. Container pre-wrap is omitted because it
  // makes react-markdown's inter-block newline text nodes render as literal
  // blank lines and inflates the gaps between list items and paragraphs.
  it('does not force white-space: pre-wrap on the bubble', () => {
    const { container } = render(<UserMessage content={'line one\nline two'} renderContent={renderContent} />)
    const bubble = container.querySelector('.msg-content') as HTMLElement
    expect(bubble).toBeInTheDocument()
    expect(bubble.style.whiteSpace).toBe('')
  })

  it('shows timestamp when provided', () => {
    render(<UserMessage content="hi" timestamp="Apr 27, 2026, 08:00 PM" renderContent={renderContent} />)
    expect(screen.getByText('Apr 27, 2026, 08:00 PM')).toBeInTheDocument()
  })

  it('hides timestamp when not provided', () => {
    const { container } = render(<UserMessage content="hi" renderContent={renderContent} />)
    expect(container.querySelector('.font-mono')).not.toBeInTheDocument()
  })

  it('shows edit button when onEditResend is provided', () => {
    render(<UserMessage content="hi" renderContent={renderContent} canEdit onEditResend={() => {}} />)
    expect(screen.getByTitle('Edit & Resend')).toBeInTheDocument()
  })

  it('hides edit button when onEditResend is not provided', () => {
    render(<UserMessage content="hi" renderContent={renderContent} />)
    expect(screen.queryByTitle('Edit & Resend')).not.toBeInTheDocument()
  })

  it('hides edit button when canEdit is false', () => {
    render(<UserMessage content="hi" renderContent={renderContent} onEditResend={() => {}} />)
    expect(screen.queryByTitle('Edit & Resend')).not.toBeInTheDocument()
  })

  it('enters edit mode on pencil click', () => {
    render(<UserMessage content="original" renderContent={renderContent} canEdit onEditResend={() => {}} />)
    fireEvent.click(screen.getByTitle('Edit & Resend'))
    expect(screen.getByRole('textbox')).toHaveValue('original')
    expect(screen.getByText('Send')).toBeInTheDocument()
    expect(screen.getByText('Cancel')).toBeInTheDocument()
  })

  it('cancels edit on Cancel click', () => {
    render(<UserMessage content="original" renderContent={renderContent} canEdit onEditResend={() => {}} />)
    fireEvent.click(screen.getByTitle('Edit & Resend'))
    fireEvent.click(screen.getByText('Cancel'))
    // Back to view mode
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(screen.getByTestId('content')).toBeInTheDocument()
  })

  it('cancels edit on Escape key', () => {
    render(<UserMessage content="original" renderContent={renderContent} canEdit onEditResend={() => {}} />)
    fireEvent.click(screen.getByTitle('Edit & Resend'))
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Escape' })
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  })

  it('calls onEditResend with new content on Send click', () => {
    const onEditResend = vi.fn()
    render(<UserMessage content="original" renderContent={renderContent} canEdit messageIndex={0} messageTs="ts1" onEditResend={onEditResend} />)
    fireEvent.click(screen.getByTitle('Edit & Resend'))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'edited' } })
    fireEvent.click(screen.getByText('Send'))
    expect(onEditResend).toHaveBeenCalledWith(0, 'ts1', 'edited')
  })

  it('calls onEditResend on Enter key (without Shift)', () => {
    const onEditResend = vi.fn()
    render(<UserMessage content="original" renderContent={renderContent} canEdit messageIndex={0} messageTs="ts1" onEditResend={onEditResend} />)
    fireEvent.click(screen.getByTitle('Edit & Resend'))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'new msg' } })
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter', shiftKey: false })
    expect(onEditResend).toHaveBeenCalledWith(0, 'ts1', 'new msg')
  })

  it('does not submit on Shift+Enter (allows newline)', () => {
    const onEditResend = vi.fn()
    render(<UserMessage content="original" renderContent={renderContent} canEdit messageIndex={0} messageTs="ts1" onEditResend={onEditResend} />)
    fireEvent.click(screen.getByTitle('Edit & Resend'))
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter', shiftKey: true })
    expect(onEditResend).not.toHaveBeenCalled()
    expect(screen.getByRole('textbox')).toBeInTheDocument() // still editing
  })

  it('does not call onEditResend when content is empty', () => {
    const onEditResend = vi.fn()
    render(<UserMessage content="original" renderContent={renderContent} canEdit messageIndex={0} messageTs="ts1" onEditResend={onEditResend} />)
    fireEvent.click(screen.getByTitle('Edit & Resend'))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '   ' } })
    fireEvent.click(screen.getByText('Send'))
    expect(onEditResend).not.toHaveBeenCalled()
  })

  it('allows resend with same content (acts as regenerate)', () => {
    const onEditResend = vi.fn()
    render(<UserMessage content="same" renderContent={renderContent} canEdit messageIndex={0} messageTs="ts1" onEditResend={onEditResend} />)
    fireEvent.click(screen.getByTitle('Edit & Resend'))
    fireEvent.click(screen.getByText('Send'))
    expect(onEditResend).toHaveBeenCalledWith(0, 'ts1', 'same')
  })

  it('trims whitespace before sending', () => {
    const onEditResend = vi.fn()
    render(<UserMessage content="original" renderContent={renderContent} canEdit messageIndex={0} messageTs="ts1" onEditResend={onEditResend} />)
    fireEvent.click(screen.getByTitle('Edit & Resend'))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '  trimmed  ' } })
    fireEvent.click(screen.getByText('Send'))
    expect(onEditResend).toHaveBeenCalledWith(0, 'ts1', 'trimmed')
  })

  it('shows copy button always', () => {
    render(<UserMessage content="hello" renderContent={renderContent} />)
    expect(screen.getByTitle('Copy')).toBeInTheDocument()
  })

  it('copies content to clipboard on copy click', async () => {
    render(<UserMessage content="copy me" renderContent={renderContent} />)
    fireEvent.click(screen.getByTitle('Copy'))
    expect(copyToClipboard).toHaveBeenCalledWith('copy me')
  })

  it('shows "Copy link to message" button when slotKey and messageTs are provided', () => {
    render(<UserMessage content="hi" renderContent={renderContent} messageTs="2025-05-13T14:00:00.000Z" slotKey="chat-1" slotTitle="My Chat" />)
    expect(screen.getByTitle('Copy link to message')).toBeInTheDocument()
  })

  it('hides "Copy link to message" button when messageTs is empty', () => {
    render(<UserMessage content="hi" renderContent={renderContent} messageTs="" slotKey="chat-1" />)
    expect(screen.queryByTitle('Copy link to message')).not.toBeInTheDocument()
  })

  it('hides "Copy link to message" button when slotKey is not provided', () => {
    render(<UserMessage content="hi" renderContent={renderContent} messageTs="2025-05-13T14:00:00.000Z" />)
    expect(screen.queryByTitle('Copy link to message')).not.toBeInTheDocument()
  })

  it('calls copySessionLink with correct args on link button click', () => {
    render(<UserMessage content="hi" renderContent={renderContent} messageTs="2025-05-13T14:00:00.000Z" slotKey="chat-1" slotTitle="My Chat" mode="orchestrator" />)
    fireEvent.click(screen.getByTitle('Copy link to message'))
    expect(copySessionLink).toHaveBeenCalledWith('chat-1', 'My Chat', '2025-05-13T14:00:00.000Z', 'orchestrator')
  })

  it('exits edit mode after successful send', () => {
    render(<UserMessage content="original" renderContent={renderContent} canEdit onEditResend={() => {}} />)
    fireEvent.click(screen.getByTitle('Edit & Resend'))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'new' } })
    fireEvent.click(screen.getByText('Send'))
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  })

  // Steer UX: a message injected mid-turn (meta.steer, set by the steer_push WS
  // echo) must be visually distinct from a normal message so the user can see the
  // steer landed.
  it('renders a "Steered into the running turn" badge for a steered message', () => {
    render(<UserMessage content="the job id is 50ec7087" meta={{ steer: true }} messageTs="steer-ts-1" renderContent={renderContent} />)
    expect(screen.getByText('Steered into the running turn')).toBeInTheDocument()
  })

  it('does not render the steer badge for a normal (non-steered) message', () => {
    render(<UserMessage content="normal message" messageTs="normal-ts-1" renderContent={renderContent} />)
    expect(screen.queryByText('Steered into the running turn')).not.toBeInTheDocument()
  })

  it('applies the accent bubble treatment only to a steered message', () => {
    const { container: steered } = render(<UserMessage content="steered" meta={{ steer: true }} messageTs="steer-ts-2" renderContent={renderContent} />)
    const steerBubble = steered.querySelector('.msg-content') as HTMLElement
    expect(steerBubble.className).toContain('bg-accent-subtle')
    expect(steerBubble.className).not.toContain('border-accent')

    const { container: normal } = render(<UserMessage content="normal" messageTs="normal-ts-2" renderContent={renderContent} />)
    const normalBubble = normal.querySelector('.msg-content') as HTMLElement
    expect(normalBubble.className).toContain('bg-card')
    expect(normalBubble.className).not.toContain('bg-accent-subtle')
  })

  // One-shot entrance guard identity: the optimistic bubble mounts with a client
  // ts; the steer_push reconcile stashes it as meta.clientTs and swaps messageTs
  // to the server ts. A later remount (virtualization scroll-away) must key the
  // animatedSteers guard on clientTs so the entrance does NOT replay under the
  // new server ts. The ring-pulse overlay (border-2 border-accent) only renders
  // when the entrance plays.
  it('does not replay the steer entrance on remount after the reconcile swapped in the server ts', () => {
    const ringSelector = '.border-2.border-accent'
    // First mount: optimistic bubble, client ts — entrance plays (ring present).
    const first = render(<UserMessage content="steer me" meta={{ steer: true }} messageTs="client-ts-guard" renderContent={renderContent} />)
    expect(first.container.querySelector(ringSelector)).not.toBeNull()
    first.unmount()
    // Remount post-reconcile: server ts, clientTs stashed in meta — guard must
    // recognize the same message and skip the entrance (no ring).
    const second = render(<UserMessage content="steer me" meta={{ steer: true, clientTs: 'client-ts-guard' }} messageTs="server-ts-guard" renderContent={renderContent} />)
    expect(second.container.querySelector(ringSelector)).toBeNull()
  })
})

describe('action footer on touch devices', () => {
  // happy-dom does not evaluate media queries, so the hover-none utility
  // classes themselves are pinned, the same idiom as AssistantMessage's footer.
  const footer = () => screen.getByTitle('Copy').parentElement as HTMLElement

  it('reveals the footer where the pointer cannot hover', () => {
    render(<UserMessage content="hello" renderContent={renderContent} />)
    expect(footer().className).toContain('[@media(hover:none)]:opacity-100')
  })

  it('keeps the footer hover-revealed for hover-capable pointers', () => {
    render(<UserMessage content="hello" renderContent={renderContent} />)
    const cls = footer().className
    expect(cls).toContain('opacity-0')
    expect(cls).toContain('group-hover/msg:opacity-100')
    expect(cls).toContain('group-focus-within/msg:opacity-100')
  })

  it('enlarges the actions to 40px touch targets where the pointer cannot hover', () => {
    render(<UserMessage content="hello" renderContent={renderContent} />)
    const cls = footer().className
    expect(cls).toContain('[@media(hover:none)]:[&_button]:p-2.5')
    expect(cls).toContain('[@media(hover:none)]:[&_svg]:h-5')
    expect(cls).toContain('[@media(hover:none)]:[&_svg]:w-5')
    // Three 40px actions plus a localized timestamp can exceed a narrow
    // phone's width, so the grown row must wrap rather than clip.
    expect(cls).toContain('[@media(hover:none)]:flex-wrap')
  })

  it('keeps the compact sizing on the buttons for pointer devices', () => {
    render(<UserMessage content="hello" renderContent={renderContent} />)
    expect(screen.getByTitle('Copy').className).toContain('p-0.5')
  })
})
