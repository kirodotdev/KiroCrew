import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, fireEvent, act, cleanup } from '@testing-library/react'
import AssistantMessage, { fmtTurnElapsed, fmtCredits, fmtTurnModel } from '../pages/chat/AssistantMessage'
import { parseOptions } from '../app-sdk/protocol'
// Imported from the defining module, not the `protocol` barrel, which deliberately
// does not re-export a g-flagged regex. Only `.source` is read below — a string
// copy — so the shared `lastIndex` this const's own docs warn about is untouched.
import { OPTION_MARKER_RE, OPTION_ACTION_MARKER_RE } from '../app-sdk/protocol/optionMarker'

// Mock MarkdownRenderer to avoid complex markdown parsing in tests
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))
// Mock useSmoothStream to passthrough — its rAF loop conflicts with vi.useFakeTimers()
vi.mock('../hooks/useSmoothStream', () => ({
  useSmoothStream: (content: string) => content,
}))
vi.mock('../utils/shareUrl', () => ({ copySessionLink: vi.fn().mockResolvedValue(undefined) }))
import { copySessionLink } from '../utils/shareUrl'
vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn().mockResolvedValue(undefined) }))
import { copyToClipboard } from '../utils/clipboard'

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { act(() => { vi.runAllTimers() }); vi.useRealTimers() })

describe('AssistantMessage', () => {
  it('renders markdown content', () => {
    render(<AssistantMessage content="Hello world" isStreaming={false} slotRunning={false} />)
    expect(screen.getByTestId('md')).toHaveTextContent('Hello world')
  })

  it('does not add streaming-cursor class (replaced by inline gradient)', () => {
    const { container } = render(<AssistantMessage content="typing…" isStreaming={true} slotRunning={true} />)
    expect(container.querySelector('.streaming-cursor')).not.toBeInTheDocument()
  })

  it('does not render inline option buttons (options are surfaced via FollowUpBar now)', () => {
    render(<AssistantMessage content="Pick [OPTIONS: Alpha|Beta]" isStreaming={false} slotRunning={false} />)
    expect(screen.queryByText('Alpha')).not.toBeInTheDocument()
    expect(screen.queryByText('Beta')).not.toBeInTheDocument()
    expect(screen.queryByText(/Send/)).not.toBeInTheDocument()
  })

  // Regression: OPTION_MARKER_RE anchors on a closing bracket that ends the line,
  // so a half-arrived marker can't match it and used to type itself out as prose
  // for the width of the marker line before flipping to pills at turn end.
  it('hides a half-streamed [OPTIONS: marker from the streamed text', () => {
    render(<AssistantMessage content={'All done.\n\n[OPTIONS: Merge it now | Show me the d'} isStreaming={true} slotRunning={true} />)
    expect(screen.getByTestId('md')).toHaveTextContent('All done.')
    expect(screen.getByTestId('md').textContent).not.toMatch(/\[OPTION/i)
  })

  // …but on a FINISHED message an unterminated marker is real content (prose about
  // the syntax, or a truncated turn), so it must render as written.
  it('keeps an unterminated marker once the message is no longer streaming', () => {
    render(<AssistantMessage content={'The tag looks like [OPTIONS: A | B'} isStreaming={false} slotRunning={false} />)
    expect(screen.getByTestId('md')).toHaveTextContent('[OPTIONS: A | B')
  })

  it('shows "Use as Plan" button for valid plan JSON', () => {
    const planContent = '<!-- plan_task_id:test-123 -->\nHere is the plan:\n```json\n[{"title":"Step 1","description":"Do thing"}]\n```'
    render(<AssistantMessage content={planContent} isStreaming={false} slotRunning={false} planTaskId="test-123" onApplyPlan={() => Promise.resolve(true)} />)
    expect(screen.getByText(/Use as Plan/)).toBeInTheDocument()
  })

  it('does not show plan button while streaming', () => {
    const planContent = '```json\n[{"title":"Step 1","description":"Do thing"}]\n```'
    render(<AssistantMessage content={planContent} isStreaming={true} slotRunning={true} planTaskId="test-123" onApplyPlan={() => Promise.resolve(true)} />)
    expect(screen.queryByText(/Use as Plan/)).not.toBeInTheDocument()
  })

  it('shows regenerate button when onRegenerate is provided and not streaming/running', () => {
    const onRegenerate = vi.fn()
    render(<AssistantMessage content="Hi" isStreaming={false} slotRunning={false} onRegenerate={onRegenerate} />)
    const btn = screen.getByTitle('Regenerate')
    expect(btn).toBeInTheDocument()
    fireEvent.click(btn)
    expect(onRegenerate).toHaveBeenCalledTimes(1)
  })

  it('hides regenerate button while slot is running', () => {
    render(<AssistantMessage content="Hi" isStreaming={false} slotRunning={true} onRegenerate={() => {}} />)
    expect(screen.queryByTitle('Regenerate')).not.toBeInTheDocument()
  })

  it('shows variant arrows when multiple variants exist and calls onSwitchVariant', () => {
    const onSwitch = vi.fn()
    const variants = [{ content: 'v1' }, { content: 'v2' }, { content: 'v3' }]
    render(<AssistantMessage content="v2" isStreaming={false} slotRunning={false} variants={variants} variantIdx={1} onSwitchVariant={onSwitch} />)
    expect(screen.getByText('2/3')).toBeInTheDocument()
    fireEvent.click(screen.getByTitle('Previous version'))
    expect(onSwitch).toHaveBeenCalledWith(0)
    fireEvent.click(screen.getByTitle('Next version'))
    expect(onSwitch).toHaveBeenCalledWith(2)
  })

  it('disables previous arrow at first variant', () => {
    const variants = [{ content: 'v1' }, { content: 'v2' }]
    render(<AssistantMessage content="v1" isStreaming={false} slotRunning={false} variants={variants} variantIdx={0} onSwitchVariant={() => {}} />)
    expect(screen.getByTitle('Previous version')).toBeDisabled()
    expect(screen.getByTitle('Next version')).not.toBeDisabled()
  })

  it('does not render variant arrows when only one variant', () => {
    const variants = [{ content: 'v1' }]
    render(<AssistantMessage content="v1" isStreaming={false} slotRunning={false} variants={variants} variantIdx={0} onSwitchVariant={() => {}} />)
    expect(screen.queryByTitle('Previous version')).not.toBeInTheDocument()
    expect(screen.queryByTitle('Next version')).not.toBeInTheDocument()
  })

  it('disables variant arrows when slotRunning', () => {
    const variants = [{ content: 'v1' }, { content: 'v2' }, { content: 'v3' }]
    render(<AssistantMessage content="v2" isStreaming={false} variants={variants} variantIdx={1} onSwitchVariant={() => {}} slotRunning={true} />)
    expect(screen.getByTitle('Previous version')).toBeDisabled()
    expect(screen.getByTitle('Next version')).toBeDisabled()
  })

  it('does not show regenerate button when onRegenerate not provided', () => {
    render(<AssistantMessage content="hello" isStreaming={false} slotRunning={false} />)
    expect(screen.queryByTitle('Regenerate')).not.toBeInTheDocument()
  })

  it('shows read-only variant nav when onSwitchVariant not provided', () => {
    const variants = [{ content: 'v1' }, { content: 'v2' }]
    render(<AssistantMessage content="v1" isStreaming={false} variants={variants} variantIdx={0} />)
    expect(screen.getByTitle('Previous version')).toBeInTheDocument()
  })

  it('defaults to last variant index when variantIdx omitted', () => {
    const variants = [{ content: 'v1' }, { content: 'v2' }, { content: 'v3' }]
    render(<AssistantMessage content="v3" isStreaming={false} variants={variants} onSwitchVariant={() => {}} />)
    expect(screen.getByText('3/3')).toBeInTheDocument()
  })

  it('local variant browsing changes displayed content without calling API', () => {
    const variants = [{ content: 'version one text' }, { content: 'version two text' }]
    render(<AssistantMessage content="version two text" isStreaming={false} variants={variants} variantIdx={1} />)
    expect(screen.getByText('2/2')).toBeInTheDocument()
    fireEvent.click(screen.getByTitle('Previous version'))
    expect(screen.getByText('1/2')).toBeInTheDocument()
    expect(screen.getByTestId('md')).toHaveTextContent('version one text')
  })

  it('calls onSwitchVariant for last message but uses local state for older messages', () => {
    const apiSwitch = vi.fn()
    const variants = [{ content: 'v1' }, { content: 'v2' }]
    const { unmount } = render(<AssistantMessage content="v2" isStreaming={false} variants={variants} variantIdx={1} onSwitchVariant={apiSwitch} />)
    fireEvent.click(screen.getByTitle('Previous version'))
    expect(apiSwitch).toHaveBeenCalledWith(0)
    unmount()
    render(<AssistantMessage content="v2" isStreaming={false} variants={variants} variantIdx={1} />)
    fireEvent.click(screen.getByTitle('Previous version'))
    expect(screen.getByTestId('md')).toHaveTextContent('v1')
  })

  /* Unavailable legacy actions and immediately available stable-ID actions share
   * the overflow trigger. Fully loaded index actions keep the existing row buttons.
   * Radix opens on POINTERDOWN, not click. */
  const openOverflow = () => fireEvent.pointerDown(
    screen.getByTitle('More actions'), { button: 0, ctrlKey: false, pointerType: 'mouse' },
  )

  it('renders fork action when onFork is provided and calls it on click', async () => {
    const onFork = vi.fn()
    render(<AssistantMessage content="Hello world" isStreaming={false} slotRunning={false} onFork={onFork} forkIndex={0} />)
    fireEvent.click(screen.getByTitle('Fork conversation from here'))
    expect(onFork).toHaveBeenCalledTimes(1)
    expect(onFork).toHaveBeenCalledWith(0)
  })

  it('forwards stable message ids from active overflow items', () => {
    const onFork = vi.fn()
    const onPlanFromHere = vi.fn()
    render(
      <AssistantMessage
        content="Hello world"
        isStreaming={false}
        slotRunning={false}
        onFork={onFork}
        onPlanFromHere={onPlanFromHere}
        forkIndex={7}
        forkMessageId="row-42"
      />,
    )
    expect(screen.queryByTitle('Fork conversation from here')).not.toBeInTheDocument()
    const more = screen.getByTestId('assistant-more-actions')
    const row = screen.getByTitle('Copy').parentElement as HTMLElement
    // IN the row for the AVAILABLE state: this state removed the row's two
    // dedicated fork/plan buttons in favour of this menu, so the trigger inside
    // is a net -1 control — it does not grow the row, which is what the
    // out-of-row placement exists to prevent (and the unavailable state, where
    // the row renders no fork/plan at all, still keeps it outside — see the
    // sibling case). A second reveal row carried its own `mt-1`, and with
    // HOVER_NONE_ACTIONS_ROW_CLS making these rows permanently visible at 44px
    // on touch it added a full row of height to EVERY completed turn's footer:
    // rows above a reader growing by that much is a page-scale downward
    // displacement the first time they re-measure.
    expect(row).toContainElement(more)

    openOverflow()
    const forkItem = screen.getByTestId('fork-from-here')
    expect(forkItem).not.toHaveAttribute('aria-disabled')
    expect(screen.queryByTestId('fork-unavailable-reason')).not.toBeInTheDocument()
    fireEvent.click(forkItem)
    expect(onFork).toHaveBeenCalledWith(7, 'row-42')

    cleanup()
    render(
      <AssistantMessage
        content="Hello world"
        isStreaming={false}
        slotRunning={false}
        onPlanFromHere={onPlanFromHere}
        forkIndex={7}
        forkMessageId="row-42"
      />,
    )
    openOverflow()
    fireEvent.click(screen.getByTestId('plan-from-here'))
    expect(onPlanFromHere).toHaveBeenCalledWith(7, 'row-42')
  })

  it('does not render fork button when onFork is undefined', () => {
    render(<AssistantMessage content="Hello world" isStreaming={false} slotRunning={false} />)
    expect(screen.queryByTitle('Fork conversation from here')).not.toBeInTheDocument()
  })

  it('does not grow the pre-existing footer row when forkIndex is undefined', () => {
    const onFork = vi.fn()
    const { container } = render(<AssistantMessage content={'x'.repeat(80)} isStreaming={false} slotRunning={false} onFork={onFork} onPlanFromHere={vi.fn()} onSpeak={vi.fn()} onRegenerate={vi.fn()} />)
    // `max-two-buttons-per-row` measures what the diff ADDS: a bounded window must
    // not put more controls in this row than a fully loaded one does.
    const bounded = container.querySelectorAll('button').length
    // The bounded row collapses fork+plan into ONE overflow trigger, so it carries fewer
    // controls than a loaded row -- never more, which is what this gate measures.
    expect(screen.queryAllByTitle('Fork conversation from here')).toHaveLength(0)
    expect(screen.getAllByTitle('More actions').length).toBeGreaterThan(0)
    cleanup()
    const { container: full } = render(<AssistantMessage content={'x'.repeat(80)} isStreaming={false} slotRunning={false} onFork={onFork} onPlanFromHere={vi.fn()} onSpeak={vi.fn()} onRegenerate={vi.fn()} forkIndex={0} shareEnabled />)
    const loaded = full.querySelectorAll('button').length
    expect(bounded).toBeLessThanOrEqual(loaded)
    // A loaded row restores fork/plan in place, exactly as the base branch had them.
    // The overflow trigger stays: it is Share's permanent home, in its own row
    // (while governance permits Share -- see the social_share block below).
    expect(screen.getByTitle('Fork conversation from here').tagName).toBe('BUTTON')
    expect(screen.getByTitle('Plan from here').tagName).toBe('BUTTON')
    expect(screen.queryAllByTitle('More actions')).toHaveLength(1)
  })

  it('keeps the overflow trigger IN the footer action row in every state', () => {
    // Upstream placed it BELOW the row so the row could not grow by a control.
    // The below-row placement is a second `ACTIONS_REVEAL_CLS` row carrying its
    // own `mt-1`, and HOVER_NONE_ACTIONS_ROW_CLS makes these rows permanently
    // visible with 44px targets on touch — so it added a full row of height to
    // EVERY completed turn's footer. Rows above a reader growing by that much
    // is a page-scale downward displacement the first time they re-measure
    // (reported from a phone at the moment a turn ended), and splitting the
    // placement by state ALSO gave neighbouring messages visibly different
    // footers. One row, same shape in every state, is the contract now.
    const props = { content: 'x'.repeat(80), isStreaming: false, slotRunning: false, onSpeak: vi.fn(), onRegenerate: vi.fn() }
    render(<AssistantMessage {...props} onFork={vi.fn()} onPlanFromHere={vi.fn()} onLoadEarlier={vi.fn()} />)
    const row = screen.getByTitle('Copy').parentElement as HTMLElement
    expect(row).toContainElement(screen.getByTestId('assistant-more-actions'))
    // …and it is the ONLY reveal row: no sibling row was added below it.
    const rows = document.querySelectorAll('[data-role="assistant"] .opacity-0')
    expect(rows.length).toBe(1)
  })

  it('renders no fork or plan affordance at all when handlers are absent (embedded co-author pane)', () => {
    // ChatPage passes onFork/onPlanFromHere as undefined when `embedded` —
    // an embedded pane (scribe/papyrus co-author, artifact chat) forking would
    // mint a top-level session in the Sessions tab and silently switch the
    // GLOBAL active slot. With no handlers, neither the row buttons nor the
    // unavailable-state overflow menu may render.
    render(<AssistantMessage content={'x'.repeat(80)} isStreaming={false} slotRunning={false} forkIndex={2} />)
    expect(screen.queryByTestId('fork-from-here')).not.toBeInTheDocument()
    expect(screen.queryByTestId('plan-from-here')).not.toBeInTheDocument()
    expect(screen.queryByTestId('assistant-more-actions')).not.toBeInTheDocument()
    cleanup()
    // Same with no forkIndex: the (onFork || onPlanFromHere) overflow branch
    // must also stay closed when both handlers are undefined.
    render(<AssistantMessage content={'x'.repeat(80)} isStreaming={false} slotRunning={false} />)
    expect(screen.queryByTestId('fork-from-here')).not.toBeInTheDocument()
    expect(screen.queryByTestId('assistant-more-actions')).not.toBeInTheDocument()
  })

  it('keeps an unavailable fork item reachable, so its reason can actually be read', () => {
    // Radix sets data-disabled AND pointer-events-none for a `disabled` item, so the
    // reason would be unreachable by keyboard nav and by hover alike.
    const onFork = vi.fn()
    render(<AssistantMessage content={'x'.repeat(80)} isStreaming={false} slotRunning={false} onFork={onFork} onPlanFromHere={vi.fn()} onLoadEarlier={vi.fn()} />)
    openOverflow()
    const forkItem = screen.getByTestId('fork-from-here')
    expect(forkItem).toHaveAttribute('role', 'menuitem')
    expect(forkItem).toHaveAttribute('aria-disabled', 'true')
    expect(forkItem).not.toHaveAttribute('data-disabled')
    expect(forkItem.textContent).toContain('Fork conversation from here')
    // The reason is VISIBLE text, not a tooltip: a keyboard user arrow-navigating here
    // gets the why without a pointer, and `title` alone would have hidden it from them.
    const reason = screen.getByTestId('fork-unavailable-reason')
    // At its true INCREMENT: one activation pages ONE page, so "load earlier
    // history" over-promised on a chat that needs many selects.
    expect(reason.textContent).toBe('Select to load the next page of earlier history')
    expect(forkItem).toHaveAttribute('aria-describedby', reason.id)
    expect(forkItem).not.toHaveAttribute('title')
    // The sibling names its OWN action rather than repeating fork's.
    expect(screen.getByText('Plan from here')).toBeTruthy()
    // Reachable is not actionable: the guarded onSelect still refuses to fork.
    fireEvent.click(forkItem)
    expect(onFork).not.toHaveBeenCalled()
  })

  it('states a refusal, not an inert remedy, while the cursor names the chat we left', () => {
    // canForkAtWindow is false in TWO states; only more-history can page, so ChatPage
    // passes no handler for the other. "Select to load" there is an action that no-ops.
    const onFork = vi.fn()
    render(<AssistantMessage content={'x'.repeat(80)} isStreaming={false} slotRunning={false} onFork={onFork} onPlanFromHere={vi.fn()} />)
    openOverflow()
    const reason = screen.getByTestId('fork-unavailable-reason')
    expect(reason.textContent).toBe('Available once this chat finishes opening')
    expect(reason.textContent).not.toMatch(/select to load/i)
    fireEvent.click(screen.getByTestId('fork-from-here'))
    expect(onFork).not.toHaveBeenCalled()
  })

  it('uses the singular noun at count=1, so the remedy does not read as broken copy', () => {
    // i18next selects `_one`/`_other` from the `count` var; one un-suffixed key renders
    // "1 earlier messages remain", which reads as a defect to the reader it is helping.
    render(<AssistantMessage content={'x'.repeat(80)} isStreaming={false} slotRunning={false} onFork={vi.fn()} onPlanFromHere={vi.fn()} onLoadEarlier={vi.fn()} earlierRemaining={1} />)
    openOverflow()
    const reason = screen.getByTestId('fork-unavailable-reason')
    expect(reason.textContent).toContain('1 earlier message remains')
    expect(reason.textContent).not.toMatch(/1 earlier messages/)
  })

  it('states the remaining distance, so repeated paging reads as converging', () => {
    // One activation pages ONE page, so without a count a long session is N blind selects.
    render(<AssistantMessage content={'x'.repeat(80)} isStreaming={false} slotRunning={false} onFork={vi.fn()} onPlanFromHere={vi.fn()} onLoadEarlier={vi.fn()} earlierRemaining={2400} />)
    openOverflow()
    const reason = screen.getByTestId('fork-unavailable-reason')
    expect(reason.textContent).toContain('2400')
    // Still stated as an action: the capture harness asserts this phrasing survives.
    expect(reason.textContent).toMatch(/load earlier history/i)
  })

  it('routes an unavailable fork/plan item to the remedy it names', () => {
    // The reason names a control at the TOP of the transcript, so selecting the item
    // has to take the reader there -- stating a fix and doing nothing is the defect.
    const onFork = vi.fn()
    const onPlanFromHere = vi.fn()
    const onLoadEarlier = vi.fn()
    render(<AssistantMessage content={'x'.repeat(80)} isStreaming={false} slotRunning={false} onFork={onFork} onPlanFromHere={onPlanFromHere} onLoadEarlier={onLoadEarlier} />)
    openOverflow()
    fireEvent.click(screen.getByTestId('fork-from-here'))
    expect(onLoadEarlier).toHaveBeenCalledTimes(1)
    expect(onFork).not.toHaveBeenCalled()
    expect(onPlanFromHere).not.toHaveBeenCalled()
  })

  it('keeps paging from ONE select instead of one page per select', () => {
    // A remedy that advances one page per deliberate re-select is a treadmill on
    // exactly the long chats the bound targets. One select owns the whole walk.
    const onFork = vi.fn()
    const onLoadEarlier = vi.fn()
    const at = (remaining: number) => (
      <AssistantMessage content={'x'.repeat(80)} isStreaming={false} slotRunning={false} onFork={onFork} onLoadEarlier={onLoadEarlier} earlierRemaining={remaining} />
    )
    const view = render(at(30))
    openOverflow()
    fireEvent.click(screen.getByTestId('fork-from-here'))
    expect(onLoadEarlier).toHaveBeenCalledTimes(1)
    // Each landed page shrinks the remainder; paging continues with NO further select.
    view.rerender(at(20))
    expect(onLoadEarlier).toHaveBeenCalledTimes(2)
    view.rerender(at(10))
    expect(onLoadEarlier).toHaveBeenCalledTimes(3)
    expect(onFork).not.toHaveBeenCalled()
  })

  it('stops paging the moment the fork target resolves', () => {
    // Termination arm 1: an index means the row is in the window, so keep walking
    // past it and the reader pages history they never asked for.
    const onFork = vi.fn()
    const onLoadEarlier = vi.fn()
    const at = (remaining: number, forkIndex?: number) => (
      <AssistantMessage content={'x'.repeat(80)} isStreaming={false} slotRunning={false} onFork={onFork} onLoadEarlier={onLoadEarlier} earlierRemaining={remaining} forkIndex={forkIndex} />
    )
    const view = render(at(30))
    openOverflow()
    fireEvent.click(screen.getByTestId('fork-from-here'))
    expect(onLoadEarlier).toHaveBeenCalledTimes(1)
    view.rerender(at(20, 7))
    expect(onLoadEarlier).toHaveBeenCalledTimes(1)
  })

  it('stops paging when a landed page did not shrink the remainder', () => {
    // Termination arm 2, deliberately NOT a page cap: a cap false-reports distant
    // but reachable rows. No progress is the honest signal that walking cannot help.
    const onFork = vi.fn()
    const onLoadEarlier = vi.fn()
    const at = (remaining: number) => (
      <AssistantMessage content={'x'.repeat(80)} isStreaming={false} slotRunning={false} onFork={onFork} onLoadEarlier={onLoadEarlier} earlierRemaining={remaining} />
    )
    const view = render(at(30))
    openOverflow()
    fireEvent.click(screen.getByTestId('fork-from-here'))
    expect(onLoadEarlier).toHaveBeenCalledTimes(1)
    view.rerender(at(20))
    expect(onLoadEarlier).toHaveBeenCalledTimes(2)
    // Remainder unchanged: the walk is not advancing, so it must stop rather than spin.
    view.rerender(at(20))
    expect(onLoadEarlier).toHaveBeenCalledTimes(2)
    view.rerender(at(20))
    expect(onLoadEarlier).toHaveBeenCalledTimes(2)
  })

  it('does NOT route to the remedy once fork is available', () => {
    // Negative control for the branch above: with an index the item must fork, and
    // must not divert to paging.
    const onFork = vi.fn()
    const onLoadEarlier = vi.fn()
    render(<AssistantMessage content={'x'.repeat(80)} isStreaming={false} slotRunning={false} onFork={onFork} forkIndex={3} onLoadEarlier={onLoadEarlier} />)
    fireEvent.click(screen.getByTestId('fork-from-here'))
    expect(onFork).toHaveBeenCalledWith(3)
    expect(onLoadEarlier).not.toHaveBeenCalled()
    // With no reason to state, neither the visible line nor its reference exists.
    expect(screen.queryByTestId('fork-unavailable-reason')).toBeNull()
    expect(screen.getByTestId('fork-from-here')).not.toHaveAttribute('aria-describedby')
  })

  it('restores fork/plan as ROW buttons once the window is loaded, so the everyday case stays one click', () => {
    // The menu exists for the UNAVAILABLE state's visible reason; routing the available
    // controls through it taxed every fully-loaded chat with an extra open.
    const onFork = vi.fn()
    const onPlanFromHere = vi.fn()
    render(<AssistantMessage content={'x'.repeat(80)} isStreaming={false} slotRunning={false} onFork={onFork} onPlanFromHere={onPlanFromHere} forkIndex={4} shareEnabled />)
    // Reachable WITHOUT opening anything, and as a row button rather than a menu item.
    const fork = screen.getByTitle('Fork conversation from here')
    expect(fork.tagName).toBe('BUTTON')
    expect(fork).not.toHaveAttribute('role', 'menuitem')
    // The trigger survives as Share's home, but fork/plan are not inside it.
    expect(screen.queryAllByTitle('More actions')).toHaveLength(1)
    const plan = screen.getByTitle('Plan from here')
    expect(plan.tagName).toBe('BUTTON')
    expect(plan).not.toHaveAttribute('role', 'menuitem')
    fireEvent.click(fork)
    expect(onFork).toHaveBeenCalledWith(4)
    // Clicking one sets busyAction, which disables its sibling (base's own behaviour),
    // so Plan is exercised from a clean render rather than after Fork.
    cleanup()
    render(<AssistantMessage content={'x'.repeat(80)} isStreaming={false} slotRunning={false} onFork={vi.fn()} onPlanFromHere={onPlanFromHere} forkIndex={4} />)
    fireEvent.click(screen.getByTitle('Plan from here'))
    expect(onPlanFromHere).toHaveBeenCalledWith(4)
  })

  it('keeps the menu treatment for the UNAVAILABLE state, reason and remedy intact', () => {
    // Negative control that matters most: making fork look operable without an index
    // re-opens the trust hole the cursor threading exists to close.
    const onLoadEarlier = vi.fn()
    const onFork = vi.fn()
    render(<AssistantMessage content={'x'.repeat(80)} isStreaming={false} slotRunning={false} onFork={onFork} onPlanFromHere={vi.fn()} onLoadEarlier={onLoadEarlier} />)
    // No row button may appear while the index is unknown.
    expect(screen.queryByTitle('Fork conversation from here')).toBeNull()
    expect(screen.getAllByTitle('More actions').length).toBeGreaterThan(0)
    openOverflow()
    const forkItem = screen.getByTestId('fork-from-here')
    expect(forkItem).toHaveAttribute('role', 'menuitem')
    expect(forkItem).toHaveAttribute('aria-disabled', 'true')
    expect(forkItem).toHaveAttribute('aria-describedby')
    expect(screen.getByTestId('fork-unavailable-reason').textContent).toBeTruthy()
    fireEvent.click(forkItem)
    expect(onLoadEarlier).toHaveBeenCalledTimes(1)
    expect(onFork).not.toHaveBeenCalled()
  })

  it('mounts the overflow trigger whenever fork/plan handlers exist, with their items only in the unavailable state', () => {
    // Share lives in the menu, whose presence follows the fork/plan handlers:
    // a top-level chat always passes at least one, an embedded pane passes
    // none and carries no per-message actions at all (Share included).
    const variants = [{ content: 'ready' }, { content: 'ok' }]
    const short = 'ready'
    // 1. No handlers at all (embedded pane / app-SDK renderer): no trigger.
    const a = render(<AssistantMessage content={short} isStreaming={false} slotRunning={false} variants={variants} />)
    expect(screen.queryByTitle('More actions')).toBeNull()
    a.unmount()
    // 2. Handlers but NO index: the trigger appears -- the item's action is the remedy.
    const b = render(<AssistantMessage content={short} isStreaming={false} slotRunning={false} onFork={vi.fn()} onPlanFromHere={vi.fn()} variants={variants} />)
    expect(screen.getAllByTitle('More actions').length).toBeGreaterThan(0)
    b.unmount()
    // 3. Actionable fork: the control returns to the row; the trigger stays for Share.
    const c = render(<AssistantMessage content={short} isStreaming={false} slotRunning={false} onFork={vi.fn()} forkIndex={0} variants={variants} shareEnabled />)
    expect(screen.getAllByTitle('More actions')).toHaveLength(1)
    expect(screen.getByTitle('Fork conversation from here').tagName).toBe('BUTTON')
    c.unmount()
    // 4. Speak and raw-view are ROW buttons and never sit inside the menu;
    //    the trigger keeps to the fork/plan signal, as Share's home.
    const d = render(<AssistantMessage content={'x'.repeat(80)} isStreaming={false} slotRunning={false} onSpeak={vi.fn()} onFork={vi.fn()} variants={variants} />)
    expect(screen.getAllByTitle('More actions')).toHaveLength(1)
    expect(screen.getByTitle('Raw markdown')).toBeTruthy()
    // `speak` is the TITLE and `speak_message` the aria-label, as on base: the
    // relabel that swapped them is out of this PR's scope.
    expect(screen.getByTitle('Speak')).toBeTruthy()
    expect(screen.getByLabelText('Speak message')).toBeTruthy()
    d.unmount()
    // 5. Fork unavailable keeps its disabled-in-place explanation, as documented.
    render(<AssistantMessage content={'x'.repeat(80)} isStreaming={false} slotRunning={false} onFork={vi.fn()} variants={variants} />)
    openOverflow()
    expect(screen.getByTestId('fork-from-here')).toHaveAttribute('aria-disabled', 'true')
  })

  it('shows an in-flight spinner on the unavailable item while earlier history loads', () => {
    // The dead-click defect: selecting the item pages off-screen at the transcript top, so
    // without a cue HERE the reader perceives nothing happening and clicks again.
    const idle = render(<AssistantMessage content={'x'.repeat(80)} isStreaming={false} slotRunning={false} onFork={vi.fn()} onPlanFromHere={vi.fn()} onLoadEarlier={vi.fn()} />)
    openOverflow()
    expect(screen.getByTestId('fork-from-here').querySelector('svg.lucide-git-fork')).toBeInTheDocument()
    expect(screen.getByTestId('fork-from-here').querySelector('svg.lucide-loader-circle')).not.toBeInTheDocument()
    idle.unmount()
    render(<AssistantMessage content={'x'.repeat(80)} isStreaming={false} slotRunning={false} onFork={vi.fn()} onPlanFromHere={vi.fn()} onLoadEarlier={vi.fn()} loadingOlder />)
    openOverflow()
    // Both items carry it, so the cue is on whichever one the reader is looking at.
    expect(screen.getByTestId('fork-from-here').querySelector('svg.lucide-loader-circle')).toBeInTheDocument()
    expect(screen.getByTestId('plan-from-here').querySelector('svg.lucide-loader-circle')).toBeInTheDocument()
  })

  it('does not render fork button while streaming', () => {
    const onFork = vi.fn()
    render(<AssistantMessage content="typing…" isStreaming={true} slotRunning={true} onFork={onFork} forkIndex={0} />)
    expect(screen.queryByTitle('Fork conversation from here')).not.toBeInTheDocument()
  })

  // Steer UX: the [STEERING …] ack chip must appear the moment kiro-cli emits the
  // marker — including mid-stream — so the user sees the agent acknowledge the
  // steer live, not only after turn end (never gated on !isStreaming).
  it('renders the Steered ack chip live during streaming (not gated on turn end)', () => {
    render(<AssistantMessage content={'Working on it [STEERING steer-abc123: switching to the job id]'} isStreaming={true} slotRunning={true} />)
    expect(screen.getByText('Steered')).toBeInTheDocument()
    expect(screen.getByText(/switching to the job id/)).toBeInTheDocument()
  })

  it('strips the raw [STEERING] marker from the streamed prose', () => {
    render(<AssistantMessage content={'Doing X [STEERING steer-abc: did Y]'} isStreaming={true} slotRunning={true} />)
    expect(screen.getByTestId('md')).not.toHaveTextContent('[STEERING')
  })

  // Spinner-scoping: fork and plan each own their spinner slot so clicking one
  // does not spin the other's icon.
  it('spins only the Plan action when Plan is clicked; fork icon stays a GitFork, not a spinner', async () => {
    let resolvePlan!: () => void
    const onPlanFromHere = vi.fn(() => new Promise<void>(res => { resolvePlan = res }))
    const onFork = vi.fn()
    render(<AssistantMessage content="Hello world" isStreaming={false} slotRunning={false} onFork={onFork} onPlanFromHere={onPlanFromHere} forkIndex={0} />)
    fireEvent.click(screen.getByTitle('Plan from here'))
    const planItem = screen.getByTitle('Plan from here')
    const forkItem = screen.getByTitle('Fork conversation from here')
    expect(planItem).toBeDisabled()
    expect(forkItem).toBeDisabled()
    // Fork keeps its GitFork icon -- it must NOT have been swapped for a spinner.
    expect(forkItem.querySelector('svg.lucide-git-fork')).toBeInTheDocument()
    expect(forkItem.querySelector('svg.lucide-loader-circle')).not.toBeInTheDocument()
    // Plan's icon IS the spinner while its own action is in flight.
    expect(planItem.querySelector('svg.lucide-loader-circle')).toBeInTheDocument()
    expect(planItem.querySelector('svg.lucide-clipboard-list')).not.toBeInTheDocument()

    await act(async () => { resolvePlan(); await Promise.resolve() })
    expect(screen.getByTitle('Plan from here')).not.toBeDisabled()
  })

  it('spins only the Fork action when Fork is clicked; plan icon stays a ClipboardList, not a spinner', async () => {
    let resolveFork!: () => void
    const onFork = vi.fn(() => new Promise<void>(res => { resolveFork = res }))
    const onPlanFromHere = vi.fn()
    render(<AssistantMessage content="Hello world" isStreaming={false} slotRunning={false} onFork={onFork} onPlanFromHere={onPlanFromHere} forkIndex={0} />)
    fireEvent.click(screen.getByTitle('Fork conversation from here'))
    const planItem = screen.getByTitle('Plan from here')
    const forkItem = screen.getByTitle('Fork conversation from here')
    expect(forkItem).toBeDisabled()
    expect(planItem).toBeDisabled()
    expect(planItem.querySelector('svg.lucide-clipboard-list')).toBeInTheDocument()
    expect(planItem.querySelector('svg.lucide-loader-circle')).not.toBeInTheDocument()
    expect(forkItem.querySelector('svg.lucide-loader-circle')).toBeInTheDocument()
    expect(forkItem.querySelector('svg.lucide-git-fork')).not.toBeInTheDocument()

    await act(async () => { resolveFork(); await Promise.resolve() })
    expect(screen.getByTitle('Fork conversation from here')).not.toBeDisabled()
  })

  describe('capabilities.social_share governance gate', () => {
    // `shareEnabled` is the server's answer from /api/dashboard/config
    // (`social_share_enabled`). The entry is an egress path for agent output
    // (the intent buttons hand the caption to X / LinkedIn in a URL), so a fleet
    // ceiling must be able to withdraw it, and the frontend must never guess.
    const menuProps = { content: 'x'.repeat(80), isStreaming: false, slotRunning: false }

    it('offers Share in the menu only while governance permits it', () => {
      render(<AssistantMessage {...menuProps} onFork={vi.fn()} onPlanFromHere={vi.fn()} shareEnabled />)
      openOverflow()
      expect(screen.getByTestId('share-message')).toBeInTheDocument()
      cleanup()
      render(<AssistantMessage {...menuProps} onFork={vi.fn()} onPlanFromHere={vi.fn()} shareEnabled={false} />)
      openOverflow()
      // The menu still exists for the unavailable fork/plan items; only Share is gone.
      expect(screen.queryByTestId('share-message')).not.toBeInTheDocument()
      expect(screen.getByTestId('fork-from-here')).toBeInTheDocument()
    })

    it('fails closed: an absent prop hides Share, so a host that forgets the wire cannot leak it', () => {
      render(<AssistantMessage {...menuProps} onFork={vi.fn()} onPlanFromHere={vi.fn()} />)
      openOverflow()
      expect(screen.queryByTestId('share-message')).not.toBeInTheDocument()
    })

    it('withdraws the whole trigger when Share was the only item left (loaded window, fork/plan as row buttons)', () => {
      // Loaded state keeps fork/plan in the row, so the menu held Share alone; a
      // pinned-off Share would leave a trigger that opens an empty menu.
      render(<AssistantMessage {...menuProps} onFork={vi.fn()} onPlanFromHere={vi.fn()} forkIndex={4} shareEnabled={false} />)
      expect(screen.queryByTitle('More actions')).toBeNull()
      // The everyday controls are untouched by the pin.
      expect(screen.getByTitle('Fork conversation from here').tagName).toBe('BUTTON')
      expect(screen.getByTitle('Plan from here').tagName).toBe('BUTTON')
      cleanup()
      // …and it returns the moment governance permits again.
      render(<AssistantMessage {...menuProps} onFork={vi.fn()} onPlanFromHere={vi.fn()} forkIndex={4} shareEnabled />)
      expect(screen.getAllByTitle('More actions')).toHaveLength(1)
      openOverflow()
      expect(screen.getByTestId('share-message')).toBeInTheDocument()
    })
  })

})

describe('action footer on touch devices', () => {
  // The footer is opacity-0 until group-hover, and a touch pointer never
  // hovers — without the hover:none override the actions (copy, speak,
  // regenerate, fork) are permanently invisible on phones. happy-dom does not
  // evaluate media queries, so pin the utility class itself.
  const footer = () => screen.getByTitle('Copy').closest('div') as HTMLElement

  it('reveals the footer where the pointer cannot hover', () => {
    render(<AssistantMessage content="Hello world" isStreaming={false} slotRunning={false} />)
    expect(footer().className).toContain('[@media(hover:none)]:opacity-100')
  })

  it('keeps the footer hover-revealed for hover-capable pointers', () => {
    render(<AssistantMessage content="Hello world" isStreaming={false} slotRunning={false} />)
    const cls = footer().className
    expect(cls).toContain('opacity-0')
    expect(cls).toContain('group-hover/msg:opacity-100')
    expect(cls).toContain('group-focus-within/msg:opacity-100')
  })
})

describe('parseOptions', () => {
  it('parses [OPTIONS: a|b|c] multi syntax', () => {
    const { options, multi, isPlan } = parseOptions('Pick one [OPTIONS: Alpha|Beta|Gamma]')
    expect(options).toEqual(['Alpha', 'Beta', 'Gamma'])
    expect(multi).toBe(true)
    expect(isPlan).toBe(false)
  })

  it('parses [OPTION: a|b] single syntax', () => {
    const { options, multi } = parseOptions('Yes or no? [OPTION: Yes|No]')
    expect(options).toEqual(['Yes', 'No'])
    expect(multi).toBe(false)
  })

  it('returns empty options for content without markers', () => {
    const { options } = parseOptions('Just regular content')
    expect(options).toEqual([])
  })

  // A model intermittently substitutes a fullwidth / CJK lookalike for the ASCII
  // `]`. One wrong codepoint used to break the end anchor, so the marker leaked
  // into the message as literal text and the turn lost its pills. Mirrors the
  // backend's MARKER_CLOSERS.
  it.each([
    ['\u3011', 'U+3011 】'],
    ['\uFF3D', 'U+FF3D ］'],
    ['\u3015', 'U+3015 〕'],
  ])('accepts %s (%s) as a closing bracket', (close) => {
    const { options, multi, text } = parseOptions(`Pick one [OPTIONS: Alpha|Beta${close}`)
    expect(options).toEqual(['Alpha', 'Beta'])
    expect(multi).toBe(true)
    expect(text).toBe('Pick one')
  })

  it('does not treat unrelated CJK closing punctuation as a bracket', () => {
    // U+300D 」 and U+3009 〉 are not square-bracket lookalikes — widening the
    // class must not have swept in every CJK closing glyph.
    for (const ch of ['\u300D', '\u3009']) {
      const { options } = parseOptions(`Pick [OPTIONS: A|B${ch}`)
      expect(options).toEqual([])
    }
  })

  it('flags isPlan when both plan header and stage marker present', () => {
    const content = '📋 Plan for: foo\n\nStage 1: do thing\n[OPTION: approved|rejected]'
    const { isPlan } = parseOptions(content)
    expect(isPlan).toBe(true)
  })

  it('strips the option marker from parsed text', () => {
    const { text } = parseOptions('Pick [OPTIONS: A|B]')
    expect(text).toBe('Pick')
  })

  // Regression: the model often appends a closing line after the marker (a
  // follow-up question, a note, an auto-inserted comment). The old end-anchored
  // regex failed to match these, so the raw "[OPTION: …]" text rendered with no
  // buttons. Parsing must tolerate trailing content and still surface options.
  it('parses options when a trailing note follows the marker', () => {
    const content = '📋 Plan for: foo\n\nStage 1: do thing\n\n[OPTION: Go | Go All | Cancel]\n\nTwo things I\'d like your call on: (a) clearance? (b) scope?'
    const { options, isPlan, text } = parseOptions(content)
    expect(options).toEqual(['Go', 'Go All', 'Cancel'])
    expect(isPlan).toBe(true)
    expect(text).not.toContain('[OPTION:')
    expect(text).toContain('Two things')
  })

  it('parses options when a diff block follows the marker', () => {
    const content = 'Stage 1\n\n[OPTION: Go | Cancel]\n\n```diff\n--- a\n+++ b\n```'
    const { options, text } = parseOptions(content)
    expect(options).toEqual(['Go', 'Cancel'])
    expect(text).not.toContain('[OPTION:')
    expect(text).toContain('```diff')
  })

  // Regression: the model sometimes appends a stray "(OPTIONS)" (or any "(...)")
  // immediately after the marker — "[OPTIONS: A | B | C](OPTIONS)". That both broke
  // the end anchor (marker leaked unparsed) and formed a valid [label](url) Markdown
  // link, so the whole thing rendered as a purple link instead of buttons. The parser
  // now absorbs a tightly-attached link-close: options are surfaced and the stray
  // "(...)" is stripped from the text.
  it('parses options when a stray markdown-link close follows the marker', () => {
    const { options, multi, text } = parseOptions('Pick one.\n[OPTIONS: Alpha | Beta | Gamma](OPTIONS)')
    expect(options).toEqual(['Alpha', 'Beta', 'Gamma'])
    expect(multi).toBe(true)
    expect(text).not.toContain('[OPTIONS:')
    expect(text).not.toContain('(OPTIONS)')
  })

  // The "(" must abut the "]": a spaced "] (note)" is NOT a link close, so the anchor
  // fails and the marker is left unparsed — preserving the deliberate trailing-note case.
  it('does NOT treat a spaced parenthetical after the marker as a link close', () => {
    const { options } = parseOptions('[OPTIONS: A | B] (see note)')
    expect(options).toEqual([])
  })

  it('takes the last marker for options and strips ALL markers from text', () => {
    const { options, text } = parseOptions('[OPTION: A | B]\nlater\n[OPTION: Go | Go All | Cancel]')
    expect(options).toEqual(['Go', 'Go All', 'Cancel'])
    // earlier markers must NOT leak as raw syntax; surrounding prose is preserved
    expect(text).not.toContain('[OPTION:')
    expect(text).toContain('later')
  })

  // A label may itself contain `]`. The block terminates at the last `]` that ends the
  // line, so "[OPTIONS: Alpha ] | Bravo ] | Charlie ]]" yields three labels each ending
  // in `]`. Regression: the old first-`]` regex truncated the block to just "Alpha" and
  // leaked "| Bravo ] | Charlie ]]" into the rendered text.
  it('allows `]` inside option labels (terminates at the line-final bracket)', () => {
    const { options, text } = parseOptions('Here are pills:\n[OPTIONS: Alpha ] | Bravo ] | Charlie ]]')
    expect(options).toEqual(['Alpha ]', 'Bravo ]', 'Charlie ]'])
    expect(text).toBe('Here are pills:')
    expect(text).not.toContain('[OPTIONS:')
  })

  // ReDoS guard: untrusted model output with thousands of unterminated `[OPTIONS:`
  // prefixes must not drive quadratic backtracking in the synchronous render path.
  //
  // Asserted STRUCTURALLY, on the pattern itself, because no timing assertion can
  // work here. This file installs fake timers for every test in `beforeEach`, and
  // vitest's default `toFake` covers the whole clock surface: MEASURED under it,
  // `Date.now()`, `performance.now()` AND `process.hrtime.bigint()` all report a
  // delta of exactly 0 across a 20M-iteration spin. So the original
  // `expect(Date.now() - start).toBeLessThan(500)` could not fail — it passed
  // even against a catastrophically backtracking pattern — and swapping in either
  // other clock does not fix it.
  //
  // A duration budget would be the wrong shape anyway: a backtracking regex is
  // synchronous, so vitest's per-test timeout cannot interrupt it and a regression
  // surfaces as a WEDGED WORKER rather than a failure. MEASURED: substituting a
  // nested-quantifier body `(?:[^\n]+)+` for the tempered one takes this file from
  // 29s to 357s with `tests 0ms`, and 300s standalone without finishing.
  //
  // What makes the pattern linear is the TEMPERED body: each alternative excludes
  // the delimiter that starts a fresh marker, so a failed match cannot re-partition
  // the same run. Pinning that shape catches the regression the timing check was
  // reaching for, deterministically and in microseconds. The behavioural half — an
  // adversarial input still parses to no options — is asserted directly below.
  it('does not catastrophically backtrack on adversarial `[OPTIONS:` input', () => {
    // BOTH markers are pinned, because they share one tempered-body grammar. The
    // body used to exclude only `[OPTIONS?:` — its own head. It was generalised to
    // exclude EVERY head when the action marker was added, because a body that
    // tempers against only its own head still matches ACROSS the other one:
    // MEASURED on the backend TRAILER form, `[OPTIONS: a | b]` followed by
    // `[OPTION-ACTIONS: close=X]` captured the action marker into the content
    // marker's label list and shipped it as a channel button label.
    //
    // So this asserts the PROPERTY (tempered against every head) rather than one
    // literal substring. The previous spelling pinned the exact body text, and
    // generalising the temper broke it while leaving the pattern strictly safer —
    // a ratchet that fails on an improvement is one a future author is tempted to
    // simply delete.
    const heads = ['OPTION-ACTIONS:', 'OPTIONS?:']
    const patterns: [string, RegExp][] = [
      ['OPTION_MARKER_RE', OPTION_MARKER_RE],
      ['OPTION_ACTION_MARKER_RE', OPTION_ACTION_MARKER_RE],
    ]
    for (const [name, re] of patterns) {
      const src = re.source
      // Tempered alternation, NOT a nested quantifier: the `[^[\n]` arm consumes
      // ordinary text and the `\[(?!…)` arm admits a bracket only when it does not
      // open a marker, so a failed match cannot re-partition the same run.
      expect(src, name).toContain('(?:[^[\\n]|\\[(?!')
      // Every head excluded, not just this pattern's own.
      for (const head of heads) expect(src, `${name} tempers ${head}`).toContain(head)
      // No `(x+)+` / `(x*)*` anywhere: that is the shape that backtracks
      // exponentially, and it is what the tempered body replaced.
      expect(src, name).not.toMatch(/\([^)]*[+*]\)[+*]/)
    }
    // And the parse itself still terminates and yields nothing for 20k
    // unterminated prefixes of EITHER head. Under the tempered body this returns
    // in ~2ms; under a backtracking one it would never return, which is a wedge
    // the reviewer reads in the log rather than an assertion failure — hence the
    // shape checks above, which fail first and cheaply.
    expect(parseOptions('[OPTIONS:'.repeat(20000)).options).toEqual([])
    expect(parseOptions('[OPTION-ACTIONS:'.repeat(20000)).action).toBeNull()
  })

  it('shows "Copy link to message" button when messageTs and slotKey are provided', () => {
    render(<AssistantMessage content="Hello" isStreaming={false} slotRunning={false} messageTs="2025-05-13T14:00:00.000Z" slotKey="chat-1" slotTitle="My Chat" />)
    expect(screen.getByTitle('Copy link to message')).toBeInTheDocument()
  })

  it('hides "Copy link to message" button when messageTs is not provided', () => {
    render(<AssistantMessage content="Hello" isStreaming={false} slotRunning={false} slotKey="chat-1" />)
    expect(screen.queryByTitle('Copy link to message')).not.toBeInTheDocument()
  })

  it('hides "Copy link to message" button when slotKey is not provided', () => {
    render(<AssistantMessage content="Hello" isStreaming={false} slotRunning={false} messageTs="2025-05-13T14:00:00.000Z" />)
    expect(screen.queryByTitle('Copy link to message')).not.toBeInTheDocument()
  })

  it('calls copySessionLink with correct args on link button click', () => {
    render(<AssistantMessage content="Hello" isStreaming={false} slotRunning={false} messageTs="2025-05-13T14:00:00.000Z" slotKey="chat-1" slotTitle="My Chat" mode="orchestrator" />)
    fireEvent.click(screen.getByTitle('Copy link to message'))
    expect(copySessionLink).toHaveBeenCalledWith('chat-1', 'My Chat', '2025-05-13T14:00:00.000Z', 'orchestrator')
  })

  it('copy strips the keep-visible marker so pastes carry no literal control tag (#7948)', () => {
    // The marker renders as nothing (HTML comment), so the copied text must
    // not resurface it — copy is the primary action on the substantive
    // deliverables this marker targets. Marker-specific strip (not a
    // whole-comment strip): copy preserves message fidelity and has no
    // fence-protection pass, so comments inside fenced code must survive.
    render(<AssistantMessage content={'Substantive report body\n\n<!-- keep-visible -->'} isStreaming={false} slotRunning={false} />)
    fireEvent.click(screen.getByTitle('Copy'))
    expect(copyToClipboard).toHaveBeenCalledWith('Substantive report body')
  })

  it('does not show "Copy link to message" while streaming', () => {
    render(<AssistantMessage content="typing" isStreaming={true} slotRunning={true} messageTs="2025-05-13T14:00:00.000Z" slotKey="chat-1" />)
    expect(screen.queryByTitle('Copy link to message')).not.toBeInTheDocument()
  })
})

describe('turn stats footer (elapsed time + credits)', () => {
  it('renders elapsed and credits on a completed turn', () => {
    render(<AssistantMessage content="done" isStreaming={false} slotRunning={false} turnStats={{ elapsed_ms: 84_000, credits: 2.5 }} />)
    const stats = screen.getByTestId('turn-stats')
    expect(stats).toHaveTextContent('1m 24s')
    expect(stats).toHaveTextContent('2.50 credits')
  })

  it('puts the billed amount before the elapsed time', () => {
    render(<AssistantMessage content="done" isStreaming={false} slotRunning={false} turnStats={{ elapsed_ms: 84_000, credits: 2.5 }} />)
    // Collapse whitespace: the cost must read first, elapsed second.
    const text = screen.getByTestId('turn-stats').textContent!.replace(/\s+/g, ' ').trim()
    expect(text).toMatch(/^2\.50 credits ·\s*1m 24s$/)
  })

  it('puts the dollar cost before the elapsed time too', () => {
    render(<AssistantMessage content="done" isStreaming={false} slotRunning={false} turnStats={{ elapsed_ms: 8_400, cost_usd: 0.0231 }} />)
    const text = screen.getByTestId('turn-stats').textContent!.replace(/\s+/g, ' ').trim()
    expect(text).toMatch(/^\$0\.02 ·\s*8\.4s$/)
  })

  it('renders cost_usd when the provider bills in dollars (no credits)', () => {
    render(<AssistantMessage content="done" isStreaming={false} slotRunning={false} turnStats={{ elapsed_ms: 8_400, cost_usd: 0.0231 }} />)
    const stats = screen.getByTestId('turn-stats')
    expect(stats).toHaveTextContent('8.4s')
    expect(stats).toHaveTextContent('$0.02')
    expect(stats).not.toHaveTextContent('credits')
  })

  it('renders elapsed alone when nothing was billed', () => {
    render(<AssistantMessage content="done" isStreaming={false} slotRunning={false} turnStats={{ elapsed_ms: 42_000 }} />)
    const stats = screen.getByTestId('turn-stats')
    expect(stats).toHaveTextContent('42s')
    expect(stats).not.toHaveTextContent('credits')
    expect(stats).not.toHaveTextContent('$')
  })

  it('leads with the served model when the backend resolved one', () => {
    render(<AssistantMessage content="done" isStreaming={false} slotRunning={false} turnStats={{ elapsed_ms: 84_000, credits: 2.5, model: 'claude-sonnet-4.6' }} />)
    const text = screen.getByTestId('turn-stats').textContent!.replace(/\s+/g, ' ').trim()
    expect(text).toMatch(/^claude-sonnet-4\.6 ·\s*2\.50 credits ·\s*1m 24s$/)
  })

  it('trims routing prefixes from the inline model label but keeps the full id in the tooltip', () => {
    render(<AssistantMessage content="done" isStreaming={false} slotRunning={false} turnStats={{ elapsed_ms: 8_400, credits: 1.2, model: 'global.anthropic.claude-opus-4-8[1m]' }} />)
    expect(screen.getByTestId('turn-model')).toHaveTextContent('claude-opus-4-8[1m]')
    expect(screen.getByTestId('turn-model')).not.toHaveTextContent('global.anthropic')
    expect(screen.getByTestId('turn-stats').title).toContain('global.anthropic.claude-opus-4-8[1m]')
  })

  it('omits the model chip when the backend did not resolve one', () => {
    render(<AssistantMessage content="done" isStreaming={false} slotRunning={false} turnStats={{ elapsed_ms: 42_000, credits: 1.0 }} />)
    expect(screen.queryByTestId('turn-model')).not.toBeInTheDocument()
  })

  // An Auto turn arrives as the literal `auto`, not a model id, because Auto's
  // per-turn choice is not disclosed on the wire. It still renders: a blank
  // chip there is indistinguishable from a turn with no measurement at all,
  // which is exactly the reading this chip exists to prevent.
  it('shows the bare auto sentinel for a turn the backend routed itself', () => {
    render(<AssistantMessage content="done" isStreaming={false} slotRunning={false} turnStats={{ elapsed_ms: 6_100, credits: 0.64, model: 'auto' }} />)
    expect(screen.getByTestId('turn-model')).toHaveTextContent('auto')
    const text = screen.getByTestId('turn-stats').textContent!.replace(/\s+/g, ' ').trim()
    expect(text).toMatch(/^auto ·\s*0\.64 credits ·\s*6\.1s$/)
  })

  // The tooltip is four whole-sentence catalog keys, one per combination of the
  // two optional clauses. Nothing else asserts the `title`, so without these a
  // wrong key or a dropped clause would render silently and every visible-text
  // assertion above would still pass.
  it('spells the whole sentence in the tooltip for each billing combination', () => {
    const title = (stats: { elapsed_ms: number; credits?: number; cost_usd?: number }) => {
      const { unmount } = render(<AssistantMessage content="done" isStreaming={false} slotRunning={false} turnStats={stats} />)
      const value = screen.getByTestId('turn-stats').getAttribute('title')
      unmount()
      return value
    }
    expect(title({ elapsed_ms: 42_000 })).toBe('Turn took 42s')
    expect(title({ elapsed_ms: 84_000, credits: 2.5 })).toBe('Turn took 1m 24s and used 2.50 credits')
    expect(title({ elapsed_ms: 8_400, cost_usd: 0.0231 })).toBe('Turn took 8.4s ($0.0231 API cost)')
    expect(title({ elapsed_ms: 84_000, credits: 2.5, cost_usd: 0.0231 }))
      .toBe('Turn took 1m 24s and used 2.50 credits ($0.0231 API cost)')
  })

  it('hidden while streaming', () => {
    render(<AssistantMessage content="typing…" isStreaming={true} slotRunning={true} turnStats={{ elapsed_ms: 5_000, credits: 1 }} />)
    expect(screen.queryByTestId('turn-stats')).not.toBeInTheDocument()
  })

  it('hidden when showFooter is false (mid-turn assistant segment)', () => {
    render(<AssistantMessage content="segment" isStreaming={false} slotRunning={false} showFooter={false} turnStats={{ elapsed_ms: 5_000, credits: 1 }} />)
    expect(screen.queryByTestId('turn-stats')).not.toBeInTheDocument()
  })

  it('hidden without turnStats (old messages persisted before the feature)', () => {
    render(<AssistantMessage content="old" isStreaming={false} slotRunning={false} />)
    expect(screen.queryByTestId('turn-stats')).not.toBeInTheDocument()
  })

  it('fmtTurnElapsed formats sub-10s, sub-minute, and minutes', () => {
    expect(fmtTurnElapsed(3_450)).toBe('3.5s')
    expect(fmtTurnElapsed(42_400)).toBe('42s')
    expect(fmtTurnElapsed(154_000)).toBe('2m 34s')
    // Second-remainder that rounds up to 60 must roll into the next minute,
    // never render the invalid "1m 60s".
    expect(fmtTurnElapsed(119_600)).toBe('2m 0s')
    expect(fmtTurnElapsed(179_600)).toBe('3m 0s')
  })

  it('fmtCredits trims to 2 decimals under 10, 1 above', () => {
    expect(fmtCredits(0.25)).toBe('0.25')
    expect(fmtCredits(12.53)).toBe('12.5')
  })

  it('fmtTurnModel drops region/vendor routing prefixes and keeps unknown shapes intact', () => {
    expect(fmtTurnModel('global.anthropic.claude-opus-4-8[1m]')).toBe('claude-opus-4-8[1m]')
    expect(fmtTurnModel('us.anthropic.claude-sonnet-4-6')).toBe('claude-sonnet-4-6')
    expect(fmtTurnModel('anthropic.claude-haiku-4-5')).toBe('claude-haiku-4-5')
    expect(fmtTurnModel('claude-sonnet-4.6')).toBe('claude-sonnet-4.6')
    expect(fmtTurnModel('gpt-5.6-luna')).toBe('gpt-5.6-luna')
    // The Auto sentinel is passed through verbatim — the trimmer must not
    // mistake it for a vendor-prefixed id and leave an empty label behind.
    expect(fmtTurnModel('auto')).toBe('auto')
  })
})

describe('action footer touch sizing', () => {
  // happy-dom does not evaluate media queries, so the hover-none utility
  // classes themselves are pinned, the same way the footer reveal is.
  it('enlarges the actions to 40px touch targets where the pointer cannot hover', () => {
    render(<AssistantMessage content="Hi" isStreaming={false} slotRunning={false} onRegenerate={() => {}} />)
    const footer = screen.getByTitle('Regenerate').parentElement!
    expect(footer.className).toContain('[@media(hover:none)]:[&_button]:p-3')
    expect(footer.className).toContain('[@media(hover:none)]:[&_svg]:h-4')
    expect(footer.className).toContain('[@media(hover:none)]:[&_svg]:w-4')
    // The grown row exceeds a phone's width, so it must wrap rather than
    // crush the timestamp and clip the trailing actions.
    expect(footer.className).toContain('[@media(hover:none)]:flex-wrap')
  })

  it('keeps the compact sizing on the buttons for pointer devices', () => {
    render(<AssistantMessage content="Hi" isStreaming={false} slotRunning={false} onRegenerate={() => {}} />)
    expect(screen.getByTitle('Regenerate').className).toContain('p-0.5')
  })
})

describe('pin toggle a11y state', () => {
  // The pin toggle is a stateful control: assistive tech needs its on/off
  // state via aria-pressed, not only the title/aria-label text swap.
  it('exposes aria-pressed on the pin toggle reflecting the pinned prop', () => {
    const { rerender } = render(
      <AssistantMessage content="Hi" isStreaming={false} slotRunning={false} messageTs="ts-pin" onTogglePin={() => {}} />
    )
    expect(screen.getByTitle('Pin message')).toHaveAttribute('aria-pressed', 'false')

    rerender(
      <AssistantMessage content="Hi" isStreaming={false} slotRunning={false} messageTs="ts-pin" pinned onTogglePin={() => {}} />
    )
    expect(screen.getByTitle('Unpin message')).toHaveAttribute('aria-pressed', 'true')
  })
})
