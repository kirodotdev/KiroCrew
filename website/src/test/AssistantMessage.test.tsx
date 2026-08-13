import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import AssistantMessage, { fmtTurnElapsed, fmtCredits, fmtTurnModel } from '../pages/chat/AssistantMessage'
import { parseOptions } from '../app-sdk/protocol'
// Imported from the defining module, not the `protocol` barrel, which deliberately
// does not re-export a g-flagged regex. Only `.source` is read below — a string
// copy — so the shared `lastIndex` this const's own docs warn about is untouched.
import { OPTION_MARKER_RE } from '../app-sdk/protocol/optionMarker'

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

  it('renders fork button when onFork is provided and calls it on click', () => {
    const onFork = vi.fn()
    render(<AssistantMessage content="Hello world" isStreaming={false} slotRunning={false} onFork={onFork} forkIndex={0} />)
    const forkBtn = screen.getByTitle('Fork conversation from here')
    fireEvent.click(forkBtn)
    expect(onFork).toHaveBeenCalledTimes(1)
    expect(onFork).toHaveBeenCalledWith(0)
  })

  it('does not render fork button when onFork is undefined', () => {
    render(<AssistantMessage content="Hello world" isStreaming={false} slotRunning={false} />)
    expect(screen.queryByTitle('Fork conversation from here')).not.toBeInTheDocument()
  })

  it('does not render fork button when forkIndex is undefined (gated by parent)', () => {
    const onFork = vi.fn()
    render(<AssistantMessage content="Hello world" isStreaming={false} slotRunning={false} onFork={onFork} />)
    expect(screen.queryByTitle('Fork conversation from here')).not.toBeInTheDocument()
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
  it('spins only the Plan button when Plan is clicked; fork icon stays a GitFork, not a spinner', async () => {
    let resolvePlan!: () => void
    const onPlanFromHere = vi.fn(() => new Promise<void>(res => { resolvePlan = res }))
    const onFork = vi.fn()
    render(<AssistantMessage content="Hello world" isStreaming={false} slotRunning={false} onFork={onFork} onPlanFromHere={onPlanFromHere} forkIndex={0} />)

    const planBtn = screen.getByTitle('Plan from here')
    const forkBtn = screen.getByTitle('Fork conversation from here')

    fireEvent.click(planBtn)

    // Plan button shows its Loader2 spinner (aria-hidden svg has no title, so
    // assert via the disabled state + absence of the ClipboardList icon class
    // is fragile; instead assert both buttons are disabled (busyAction !== null)
    // while only the fork button still renders its GitFork icon svg).
    expect(planBtn).toBeDisabled()
    expect(forkBtn).toBeDisabled()
    // Fork icon (GitFork, lucide class "lucide-git-fork") must remain the fork
    // button's icon -- it must NOT have been swapped for a spinner.
    expect(forkBtn.querySelector('svg.lucide-git-fork')).toBeInTheDocument()
    expect(forkBtn.querySelector('svg.lucide-loader-circle')).not.toBeInTheDocument()
    // Plan button's icon IS the spinner while its action is in flight.
    expect(planBtn.querySelector('svg.lucide-loader-circle')).toBeInTheDocument()
    expect(planBtn.querySelector('svg.lucide-clipboard-list')).not.toBeInTheDocument()

    await act(async () => { resolvePlan(); await Promise.resolve() })
    expect(planBtn).not.toBeDisabled()
  })

  it('spins only the Fork button when Fork is clicked; plan icon stays a ClipboardList, not a spinner', async () => {
    let resolveFork!: () => void
    const onFork = vi.fn(() => new Promise<void>(res => { resolveFork = res }))
    const onPlanFromHere = vi.fn()
    render(<AssistantMessage content="Hello world" isStreaming={false} slotRunning={false} onFork={onFork} onPlanFromHere={onPlanFromHere} forkIndex={0} />)

    const planBtn = screen.getByTitle('Plan from here')
    const forkBtn = screen.getByTitle('Fork conversation from here')

    fireEvent.click(forkBtn)

    expect(forkBtn).toBeDisabled()
    expect(planBtn).toBeDisabled()
    expect(planBtn.querySelector('svg.lucide-clipboard-list')).toBeInTheDocument()
    expect(planBtn.querySelector('svg.lucide-loader-circle')).not.toBeInTheDocument()
    expect(forkBtn.querySelector('svg.lucide-loader-circle')).toBeInTheDocument()
    expect(forkBtn.querySelector('svg.lucide-git-fork')).not.toBeInTheDocument()

    await act(async () => { resolveFork(); await Promise.resolve() })
    expect(forkBtn).not.toBeDisabled()
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
    const src = OPTION_MARKER_RE.source
    // The label body: tempered alternation, NOT a nested quantifier.
    expect(src).toContain('(?:[^[\\n]|\\[(?!OPTIONS?:))*')
    // No `(x+)+` / `(x*)*` anywhere: that is the shape that backtracks
    // exponentially, and it is what the tempered body above replaced.
    expect(src).not.toMatch(/\([^)]*[+*]\)[+*]/)
    // And the parse itself still terminates and yields nothing for 20k
    // unterminated prefixes. Under the tempered body this returns in ~2ms; under
    // a backtracking one it would never return, which is a wedge the reviewer
    // reads in the log rather than an assertion failure — hence the shape checks
    // above, which fail first and cheaply.
    expect(parseOptions('[OPTIONS:'.repeat(20000)).options).toEqual([])
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
    expect(footer.className).toContain('[@media(hover:none)]:[&_button]:p-2.5')
    expect(footer.className).toContain('[@media(hover:none)]:[&_svg]:h-5')
    expect(footer.className).toContain('[@media(hover:none)]:[&_svg]:w-5')
    // The grown row exceeds a phone's width, so it must wrap rather than
    // crush the timestamp and clip the trailing actions.
    expect(footer.className).toContain('[@media(hover:none)]:flex-wrap')
  })

  it('keeps the compact sizing on the buttons for pointer devices', () => {
    render(<AssistantMessage content="Hi" isStreaming={false} slotRunning={false} onRegenerate={() => {}} />)
    expect(screen.getByTitle('Regenerate').className).toContain('p-0.5')
  })
})
