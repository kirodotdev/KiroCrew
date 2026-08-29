import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import UserMessage from '../pages/chat/UserMessage'
import { readFile } from 'node:fs/promises'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn().mockResolvedValue(undefined) }))
vi.mock('../utils/shareUrl', () => ({ copySessionLink: vi.fn().mockResolvedValue(undefined) }))

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { act(() => { vi.runAllTimers() }); vi.useRealTimers() })
import { copyToClipboard } from '../utils/clipboard'
import { copySessionLink } from '../utils/shareUrl'

const renderContent = (content: string) => <span data-testid="content">{content}</span>

describe('UserMessage', () => {
  // Demoted to `deliveryUnresolved` by a later send's confirm, then confirmed itself: nothing
  // clears that demotion, so without gating on the positive proof the bubble stays muted.
  it('drops the doubt once the send is confirmed, even after a demotion', () => {
    const { container } = render(
      <UserMessage content="hi" meta={{ deliveryUnresolved: true, deliveryConfirmed: true }} renderContent={renderContent} />)

    const bubble = container.querySelector('.message-bubble')
    expect(bubble?.className, 'a delivered bubble must not render muted').not.toMatch(/opacity-70/)
    expect(bubble?.className, 'nor dashed-outlined as uncertain').not.toMatch(/outline-dashed/)
    const caption = container.querySelector('[data-delivery-caption]')
    expect(caption, 'and it must carry no doubt caption at all').toBeNull()
    expect(container.textContent ?? '', 'the past-tense spent caption contradicts the composer')
      .not.toMatch(/never confirmed/i)
  })

  // Negative control: the demotion must still mute a send that was NEVER confirmed, or the gate
  // above would have silently retired the whole spent-doubt marking.
  it('keeps the doubt for a demoted send that never confirmed', () => {
    const { container } = render(
      <UserMessage content="hi" meta={{ deliveryUnresolved: true }} renderContent={renderContent} />)

    expect(container.querySelector('.message-bubble')?.className).toMatch(/opacity-70/)
    expect(container.querySelector('[data-delivery-caption]')?.textContent).toMatch(/never confirmed/i)
  })

  // The composer echo announces the identical "Delivery unconfirmed" string, so making this
  // caption a live region too has a screen reader read the same state twice.
  it('states the unconfirmed delivery in words but is not a second live region', () => {
    const { container } = render(
      <UserMessage content="hi" meta={{ deliveryUnknown: true }} renderContent={renderContent} />)
    const caption = Array.from(container.querySelectorAll('div'))
      .find(el => /Delivery unconfirmed/i.test(el.textContent ?? '') && el.children.length === 0)
    expect(caption, 'the caption must still state the state in words').toBeTruthy()
    expect(container.querySelectorAll('[role="status"]').length).toBe(0)
  })

  // Dropping the live region above left the capture harness with nothing to select, so the
  // hook it greps for is a CONTRACT: assert the component and the script still agree.
  it('carries the capture hook the delivery screenshot script selects on', async () => {
    const { container } = render(
      <UserMessage content="hi" meta={{ deliveryUnknown: true }} renderContent={renderContent} />)
    const hooked = container.querySelectorAll('[data-delivery-caption]')
    expect(hooked.length, 'exactly one caption must carry the capture hook').toBe(1)
    expect(hooked[0].textContent).toMatch(/Delivery unconfirmed/i)

    const script = await readFile('scripts/capture-user-message-delivery-unknown.mjs', 'utf8')
    // Positive control: prove we actually read the script, not an empty string.
    expect(script, 'failed to read the capture script').toMatch(/data-capture-root/)
    expect(script, 'the capture script no longer selects the hook this caption renders')
      .toMatch(/\[data-capture-root\] \[data-delivery-caption\]/)
  })

  // A spent nag must not become an ordinary prompt: the doubt outlives the caption's urgency,
  // or the transcript silently vouches for an instruction the agent may never have received.
  it('keeps the muted outline AND words for a spent nag, at lower urgency', () => {
    const { container } = render(
      <UserMessage content="hi" meta={{ deliveryUnresolved: true }} renderContent={renderContent} />)
    const bubble = container.querySelector('.message-bubble')
    expect(bubble?.className, 'the dimming and dashed outline must survive').toMatch(/outline-dashed/)
    expect(bubble?.className).toMatch(/opacity-70/)
    const caption = container.querySelector('[data-delivery-caption]')
    expect(caption, 'dimming with no words reads as deleted or glitched').toBeTruthy()
    expect(caption?.textContent).toMatch(/never confirmed/i)
    // Muted, not warn: the row is no longer asking the user to do anything about it.
    expect(caption?.className).toMatch(/text-muted/)
    expect(caption?.className).not.toMatch(/text-warn/)
  })

  it('gives live and spent doubt DIFFERENT words, not just different colours', () => {
    // A colour-only distinction is unreadable to anyone who cannot compare the two hues side
    // by side, so each state has to say which one it is.
    const live = render(
      <UserMessage content="hi" meta={{ deliveryUnknown: true }} renderContent={renderContent} />)
    const liveText = live.container.querySelector('[data-delivery-caption]')?.textContent
    const spent = render(
      <UserMessage content="hi" meta={{ deliveryUnresolved: true }} renderContent={renderContent} />)
    const spentText = spent.container.querySelector('[data-delivery-caption]')?.textContent
    expect(liveText, 'the live caption must carry words').toBeTruthy()
    expect(spentText, 'the spent caption must carry words').toBeTruthy()
    expect(spentText, 'live and spent doubt must not read identically').not.toBe(liveText)
  })

  it('keeps the LIVE nag at warn urgency', () => {
    // Negative control: if both states rendered identically the muting above would be vacuous.
    const { container } = render(
      <UserMessage content="hi" meta={{ deliveryUnknown: true }} renderContent={renderContent} />)
    const caption = container.querySelector('[data-delivery-caption]')
    expect(caption?.className).toMatch(/text-warn/)
    expect(caption?.className).not.toMatch(/text-muted/)
  })

  it('leaves an ordinary prompt unmarked', () => {
    // Negative control: without this the outline assertion above passes on any bubble.
    const { container } = render(
      <UserMessage content="hi" renderContent={renderContent} />)
    const bubble = container.querySelector('.message-bubble')
    expect(bubble?.className).not.toMatch(/outline-dashed/)
    expect(bubble?.className).not.toMatch(/opacity-70/)
  })

  it('renders message content', () => {    render(<UserMessage content="hello" renderContent={renderContent} />)
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

  // #7246: the badge asserts the message reached the RUNNING turn, which only a
  // backend `steering_consumed` echo proves. A steer whose bytes were merely
  // accepted (`written`), or which the turn ended without taking and the teardown
  // requeued (`requeued`), was never injected -- so neither may render the badge.
  it('does not render the steer badge for a written-but-unconfirmed steer', () => {
    render(<UserMessage content="go north" meta={{ steer: true, steerState: 'written' }} messageTs="steer-written" renderContent={renderContent} />)
    expect(screen.queryByText('Steered into the running turn')).not.toBeInTheDocument()
  })

  it('does not render the steer badge for a requeued steer', () => {
    render(<UserMessage content="go north" meta={{ steer: true, steerState: 'requeued' }} messageTs="steer-requeued" renderContent={renderContent} />)
    expect(screen.queryByText('Steered into the running turn')).not.toBeInTheDocument()
  })

  it('renders the steer badge once the backend confirms consumption', () => {
    render(<UserMessage content="go north" meta={{ steer: true, steerState: 'consumed' }} messageTs="steer-consumed" renderContent={renderContent} />)
    expect(screen.getByText('Steered into the running turn')).toBeInTheDocument()
  })

  // The client mints its own bubble as `{ steer: true, optimistic: true }` with no
  // state before the server has answered at all -- the least confirmed a steer can
  // be. It must not fall through to the legacy state-less case and claim success.
  it('does not render the steer badge on the client optimistic bubble', () => {
    render(<UserMessage content="go north" meta={{ steer: true, optimistic: true }} messageTs="steer-optimistic" renderContent={renderContent} />)
    expect(screen.queryByText('Steered into the running turn')).not.toBeInTheDocument()
  })

  it('renders the steer badge on a reconciled optimistic bubble the backend confirmed', () => {
    render(<UserMessage content="go north" meta={{ steer: true, optimistic: true, steerState: 'consumed' }} messageTs="steer-optimistic-consumed" renderContent={renderContent} />)
    expect(screen.getByText('Steered into the running turn')).toBeInTheDocument()
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

  // The wrapper-cap invariant itself is pinned by the two pre-existing tests
  // that guard this chain (UserMessage.bubbleHug.test.tsx source pin,
  // userBubbleMobileOverflow.test.tsx rendered-wrapper pin); no third copy here.

  // The entrance ring must be drawn INSIDE the bubble box (inset-0), because the
  // transcript row wrapper is overflow-hidden and hugs the bubble's edges — a
  // ring drawn outside (-inset-*) is clipped flat on the right for every steer.
  it('draws the entrance ring inside the bubble box so the row clip cannot cut it', () => {
    const { container } = render(<UserMessage content="fresh steer" meta={{ steer: true }} messageTs={`steer-ring-${Date.now()}`} renderContent={renderContent} />)
    const ring = container.querySelector('[aria-hidden="true"].absolute.border-accent') as HTMLElement
    expect(ring).not.toBeNull()
    expect(ring.className).toContain('inset-0')
    expect(ring.className).not.toContain('-inset-0.5')
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

  // The PRODUCTION path, which the test above sidesteps by mounting without
  // `optimistic`. A real steer mounts as `{ steer: true, optimistic: true }` with
  // no `steerState` -- the least-confirmed state, where the entrance must NOT
  // play -- and `steerState: 'consumed'` is patched onto the SAME row later. That
  // patch carries no key change, so React reuses the instance and there is no
  // remount: a mount-only `useState` initializer would stay false forever and the
  // entrance would never play at all. Reading the ring overlay because it renders
  // only while the entrance is playing.
  it('plays the steer entrance when consumed arrives after the optimistic mount', () => {
    const ringSelector = '.border-2.border-accent'
    const { container, rerender } = render(
      <UserMessage content="steer me later" meta={{ steer: true, optimistic: true }} messageTs="ts-late-consume" renderContent={renderContent} />,
    )
    // Nothing is confirmed yet, so no entrance -- this is the state the change exists to protect.
    expect(container.querySelector(ringSelector)).toBeNull()

    rerender(
      <UserMessage content="steer me later" meta={{ steer: true, steerState: 'consumed' }} messageTs="ts-late-consume" renderContent={renderContent} />,
    )
    expect(container.querySelector(ringSelector)).not.toBeNull()
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
    expect(cls).toContain('[@media(hover:none)]:[&_button]:p-3')
    expect(cls).toContain('[@media(hover:none)]:[&_svg]:h-4')
    expect(cls).toContain('[@media(hover:none)]:[&_svg]:w-4')
    // Three 40px actions plus a localized timestamp can exceed a narrow
    // phone's width, so the grown row must wrap rather than clip.
    expect(cls).toContain('[@media(hover:none)]:flex-wrap')
  })

  it('keeps the compact sizing on the buttons for pointer devices', () => {
    render(<UserMessage content="hello" renderContent={renderContent} />)
    expect(screen.getByTitle('Copy').className).toContain('p-0.5')
  })

  // The pin toggle is a stateful control: assistive tech needs its on/off
  // state via aria-pressed, not only the title/aria-label text swap.
  it('exposes aria-pressed on the pin toggle reflecting the pinned prop', () => {
    const { rerender } = render(
      <UserMessage content="hi" renderContent={renderContent} messageTs="ts-pin" onTogglePin={() => {}} />
    )
    const unpinned = screen.getByTitle('Pin message')
    expect(unpinned).toHaveAttribute('aria-pressed', 'false')

    rerender(
      <UserMessage content="hi" renderContent={renderContent} messageTs="ts-pin" pinned onTogglePin={() => {}} />
    )
    const pinned = screen.getByTitle('Unpin message')
    expect(pinned).toHaveAttribute('aria-pressed', 'true')
  })
})
