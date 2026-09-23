import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
/* Render framer-motion elements as plain DOM. jsdom cannot run the height
   animation, and a real AnimatePresence keeps the exiting body mounted for the
   duration of its exit transition — which would make "folded hides the options"
   pass or fail on timing rather than on behaviour. */
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'initial', 'animate', 'exit', 'transition',
    'variants', 'custom', 'whileHover', 'whileTap', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children' || FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  /* One component type per tag, cached. A proxy that minted a fresh type on
     every property read would give React a new element type each render, so it
     would unmount and remount the subtree — detaching any DOM node a test is
     holding and losing focus/caret for real users of this mock. */
  const cache = new Map<string, unknown>()
  return {
    motion: new Proxy({}, {
      get: (_t, tag: string) => {
        if (!cache.has(tag)) cache.set(tag, make(tag))
        return cache.get(tag)
      },
    }),
    AnimatePresence: ({ children }: { children?: React.ReactNode }) =>
      React.createElement(React.Fragment, null, children),
    useReducedMotion: () => false,
  }
})

import QuestionCard from '../components/QuestionCard'

const singleQuestion = [{
  question: 'What is your favorite color?',
  header: 'Preference',
  options: [
    { label: 'Red', description: 'A warm color' },
    { label: 'Blue', description: 'A cool color' },
    { label: 'Green', description: 'Nature color' },
  ],
  multiSelect: false,
}]

describe('QuestionCard', () => {

  const multiQuestion = [{
    question: 'Which features do you want?',
    header: 'Features',
    options: [
      { label: 'Dark mode', description: 'Less eye strain' },
      { label: 'Notifications', description: 'Stay updated' },
    ],
    multiSelect: true,
  }]

  it('renders question text and options', () => {
    render(<QuestionCard questions={singleQuestion} onSubmit={vi.fn()} />)
    expect(screen.getByText('What is your favorite color?')).toBeInTheDocument()
    expect(screen.getByText('Preference')).toBeInTheDocument()
    expect(screen.getByText('Red')).toBeInTheDocument()
    expect(screen.getByText('Blue')).toBeInTheDocument()
    expect(screen.getByText('A warm color')).toBeInTheDocument()
  })

  it('selecting an option highlights it', () => {
    render(<QuestionCard questions={singleQuestion} onSubmit={vi.fn()} />)
    const redBtn = screen.getByText('Red').closest('button')!
    fireEvent.click(redBtn)
    expect(redBtn.className).toContain('border-accent')
  })

  it('single-select deselects previous option', () => {
    render(<QuestionCard questions={singleQuestion} onSubmit={vi.fn()} />)
    fireEvent.click(screen.getByText('Red').closest('button')!)
    fireEvent.click(screen.getByText('Blue').closest('button')!)
    expect(screen.getByText('Red').closest('button')!).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByText('Blue').closest('button')!).toHaveAttribute('aria-pressed', 'true')
  })

  it('multi-select allows multiple selections', () => {
    render(<QuestionCard questions={multiQuestion} onSubmit={vi.fn()} />)
    fireEvent.click(screen.getByText('Dark mode').closest('button')!)
    fireEvent.click(screen.getByText('Notifications').closest('button')!)
    expect(screen.getByText('Dark mode').closest('button')!).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByText('Notifications').closest('button')!).toHaveAttribute('aria-pressed', 'true')
  })

  it('marks multi-select options with a checkbox, and single-select with none', () => {
    // The only on-screen cue that a second pick is allowed: without it the two
    // modes are identical until the user clicks twice and notices the first
    // option stayed lit. Asserted as a PAIR — an indicator on both modes would
    // erase the distinction, so the single-select absence is the other half of
    // the behaviour, not a separate concern.
    const { unmount } = render(<QuestionCard questions={multiQuestion} onSubmit={vi.fn()} />)
    const multiOpt = screen.getByText('Dark mode').closest('button')!
    expect(multiOpt.querySelector('[aria-hidden="true"]')).not.toBeNull()
    unmount()

    render(<QuestionCard questions={singleQuestion} onSubmit={vi.fn()} />)
    expect(screen.getByText('Red').closest('button')!.querySelector('[aria-hidden="true"]')).toBeNull()
  })

  it('keeps the checkbox out of the accessible name, which aria-pressed already carries', () => {
    // A checkbox announced inside a pressed toggle describes one control as two,
    // and the glyph is state the button already exposes programmatically.
    render(<QuestionCard questions={multiQuestion} onSubmit={vi.fn()} />)
    const opt = screen.getByText('Dark mode').closest('button')!
    expect(opt).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(opt)
    expect(opt).toHaveAttribute('aria-pressed', 'true')
    // Still decorative once checked: the tick is inside the aria-hidden box.
    expect(opt.querySelector('[aria-hidden="true"] svg')).not.toBeNull()
    expect(opt).toHaveAccessibleName('Dark mode Less eye strain')
  })

  it('exposes aria-pressed on every option, tracking single-select transitions including deselect', () => {
    // WCAG 4.1.2 Name/Role/Value: the selected state must be programmatic, not
    // CSS-only. aria-pressed (toggle button), not role=radio + aria-checked,
    // because single-select intentionally allows click-again-to-deselect, which
    // radio semantics forbid.
    render(<QuestionCard questions={singleQuestion} onSubmit={vi.fn()} />)
    const red = screen.getByText('Red').closest('button')!
    const blue = screen.getByText('Blue').closest('button')!
    expect(red).toHaveAttribute('aria-pressed', 'false')
    expect(blue).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(red)
    expect(red).toHaveAttribute('aria-pressed', 'true')
    expect(blue).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(red) // deselect: second click on the selected option
    expect(red).toHaveAttribute('aria-pressed', 'false')
    expect(blue).toHaveAttribute('aria-pressed', 'false')
  })

  it('multi-select toggles aria-pressed independently per option', () => {
    render(<QuestionCard questions={multiQuestion} onSubmit={vi.fn()} />)
    const dark = screen.getByText('Dark mode').closest('button')!
    const notif = screen.getByText('Notifications').closest('button')!
    fireEvent.click(dark)
    fireEvent.click(notif)
    expect(dark).toHaveAttribute('aria-pressed', 'true')
    expect(notif).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(dark) // independent toggle off; the other keeps its state
    expect(dark).toHaveAttribute('aria-pressed', 'false')
    expect(notif).toHaveAttribute('aria-pressed', 'true')
  })

  it('submit button disabled when nothing selected', () => {
    render(<QuestionCard questions={singleQuestion} onSubmit={vi.fn()} />)
    const submit = screen.getByText('Submit').closest('button')!
    expect(submit).toBeDisabled()
  })

  it('calls onSubmit with selected option', () => {
    const onSubmit = vi.fn()
    render(<QuestionCard questions={singleQuestion} onSubmit={onSubmit} />)
    fireEvent.click(screen.getByText('Green').closest('button')!)
    fireEvent.click(screen.getByText('Submit').closest('button')!)
    expect(onSubmit).toHaveBeenCalledWith({ 'What is your favorite color?': 'Green' })
  })

  it('calls onSubmit with custom text input', () => {
    const onSubmit = vi.fn()
    render(<QuestionCard questions={singleQuestion} onSubmit={onSubmit} />)
    const input = screen.getByPlaceholderText('Or type a custom answer...')
    fireEvent.change(input, { target: { value: 'Purple' } })
    fireEvent.click(screen.getByText('Submit').closest('button')!)
    expect(onSubmit).toHaveBeenCalledWith({ 'What is your favorite color?': 'Purple' })
  })

  it('custom input clears option selection', () => {
    render(<QuestionCard questions={singleQuestion} onSubmit={vi.fn()} />)
    fireEvent.click(screen.getByText('Red').closest('button')!)
    const input = screen.getByPlaceholderText('Or type a custom answer...')
    fireEvent.change(input, { target: { value: 'Yellow' } })
    expect(screen.getByText('Red').closest('button')!).toHaveAttribute('aria-pressed', 'false')
  })

  it('selecting option clears custom input', () => {
    render(<QuestionCard questions={singleQuestion} onSubmit={vi.fn()} />)
    const input = screen.getByPlaceholderText('Or type a custom answer...')
    fireEvent.change(input, { target: { value: 'Yellow' } })
    fireEvent.click(screen.getByText('Red').closest('button')!)
    expect((input as HTMLInputElement).value).toBe('')
  })

  it('Enter key submits when answer is ready', () => {
    const onSubmit = vi.fn()
    render(<QuestionCard questions={singleQuestion} onSubmit={onSubmit} />)
    const input = screen.getByPlaceholderText('Or type a custom answer...')
    fireEvent.change(input, { target: { value: 'Orange' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onSubmit).toHaveBeenCalledWith({ 'What is your favorite color?': 'Orange' })
  })

  it('multi-select submit joins answers with comma', () => {
    const onSubmit = vi.fn()
    render(<QuestionCard questions={multiQuestion} onSubmit={onSubmit} />)
    fireEvent.click(screen.getByText('Dark mode').closest('button')!)
    fireEvent.click(screen.getByText('Notifications').closest('button')!)
    fireEvent.click(screen.getByText('Submit').closest('button')!)
    expect(onSubmit).toHaveBeenCalledWith({ 'Which features do you want?': 'Dark mode, Notifications' })
  })
})

/* A stacked multi-question card was taller than the viewport: it buried the
   composer and the conversation above it. Paging bounds the card by construction
   — one question on screen — which is what retired the fold-all-but-the-first,
   the auto-fold hand-off and their shared height cap. */
describe('QuestionCard — paging', () => {
  const twoQuestions = [
    { question: 'Trust model', options: [{ label: 'Carve-out' }, { label: 'Public only' }] },
    { question: 'Environments', options: [{ label: 'staging' }, { label: 'prod' }] },
  ]

  const nav = (label: string) => screen.getByLabelText(label) as HTMLButtonElement
  const at = (current: number, total: number) => `Question ${current} of ${total}`

  it('shows one question at a time, with the position in the corner', () => {
    render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} />)
    expect(screen.getByText('Carve-out')).toBeInTheDocument()
    // The second question is UNMOUNTED, not hidden: that is what bounds the
    // card's height to its tallest single question.
    expect(screen.queryByText('staging')).not.toBeInTheDocument()
    expect(screen.getByText(at(1, 2))).toBeInTheDocument()
    expect(screen.getByText('1/2')).toBeInTheDocument()
  })

  it('offers no pager on a single question, where both arrows would be dead', () => {
    // "1/1" between two permanently disabled arrows says nothing and costs the
    // header its width.
    render(<QuestionCard questions={singleQuestion} onSubmit={vi.fn()} />)
    expect(screen.getByText('Red')).toBeInTheDocument()
    expect(screen.queryByLabelText('Next question')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Previous question')).not.toBeInTheDocument()
  })

  it('starts a replaced question set back at the first question', () => {
    // The payload reset has to return the page as well as the answers. Leaving a
    // stale index would open a new card mid-way through, or out of range.
    const { rerender } = render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} />)
    fireEvent.click(nav('Next question'))
    expect(screen.getByText(at(2, 2))).toBeInTheDocument()

    rerender(<QuestionCard questions={[...twoQuestions].reverse()} onSubmit={vi.fn()} />)
    expect(screen.getByText(at(1, 2))).toBeInTheDocument()
    expect(screen.getByText('staging')).toBeInTheDocument()
  })

  it('survives a replacement question set shorter than the page it is on', () => {
    // The reset schedules setPage(0), but React finishes the current pass with the
    // stale index, so a shorter replacement would read past the end of the new
    // array and throw during render. That escapes to the root boundary and takes
    // the shell down — and it is reachable by design, since a stateless card
    // keyed by its slot does not remount and the draft guard deliberately keeps it
    // mounted exactly when a page has been answered.
    const { rerender } = render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} />)
    fireEvent.click(nav('Next question'))
    expect(screen.getByText(at(2, 2))).toBeInTheDocument()

    expect(() =>
      rerender(
        <QuestionCard
          questions={[{ question: 'Only one now', options: [{ label: 'ok' }] }]}
          onSubmit={vi.fn()}
        />,
      ),
    ).not.toThrow()
    expect(screen.getByText('Only one now')).toBeInTheDocument()
    expect(screen.getByText('ok')).toBeInTheDocument()
  })

  it('walks forward and back through the questions', () => {
    render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} />)

    fireEvent.click(nav('Next question'))
    expect(screen.getByText('staging')).toBeInTheDocument()
    expect(screen.queryByText('Carve-out')).not.toBeInTheDocument()
    expect(screen.getByText(at(2, 2))).toBeInTheDocument()

    fireEvent.click(nav('Previous question'))
    expect(screen.getByText('Carve-out')).toBeInTheDocument()
    expect(screen.getByText(at(1, 2))).toBeInTheDocument()
  })

  it('stops at both ends instead of wrapping', () => {
    // Wrapping from the last question to the first reads as a jump backwards with
    // no cue, and hides that the card has been walked to the end.
    render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} />)
    expect(nav('Previous question').disabled).toBe(true)
    expect(nav('Next question').disabled).toBe(false)

    fireEvent.click(nav('Next question'))
    expect(nav('Previous question').disabled).toBe(false)
    expect(nav('Next question').disabled).toBe(true)
  })

  it('announces the position rather than leaving the swap silent', () => {
    // The question text changes with no focus move, so without a live region a
    // screen-reader user gets no signal that the card advanced. The digits are
    // aria-hidden and the spelled-out phrase carries it, so "1/2" is not read as
    // a date or a fraction.
    render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} />)
    const live = screen.getByText(at(1, 2)).closest('[aria-live]')!
    expect(live).toHaveAttribute('aria-live', 'polite')
    expect(live).toHaveAttribute('aria-atomic', 'true')
    expect(screen.getByText('1/2')).toHaveAttribute('aria-hidden', 'true')
  })

  it('keeps in-progress answers when the same card is re-dispatched', () => {
    // A websocket reconnect re-sends the still-pending card with a freshly parsed
    // array. Resetting on array identity would silently discard what the user had
    // already typed, so the reset keys off the serialized payload.
    const { rerender } = render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} />)
    fireEvent.change(screen.getAllByPlaceholderText('Or type a custom answer...')[0], { target: { value: 'mini.local' } })

    rerender(<QuestionCard questions={structuredClone(twoQuestions)} onSubmit={vi.fn()} />)
    expect((screen.getAllByPlaceholderText('Or type a custom answer...')[0] as HTMLInputElement).value).toBe('mini.local')

    // A genuinely different question set still resets.
    rerender(<QuestionCard questions={[{ question: 'Something else', options: [{ label: 'ok' }] }]} onSubmit={vi.fn()} />)
    expect((screen.getByPlaceholderText('Or type a custom answer...') as HTMLInputElement).value).toBe('')
  })

  it('resets when the prompt is reused with different options', () => {
    // Same question text, new choices: keying on the prompt alone would retain the
    // pick and submit a label that is not on the current card.
    const onSubmit = vi.fn()
    const { rerender } = render(<QuestionCard questions={twoQuestions} onSubmit={onSubmit} />)
    fireEvent.click(screen.getByText('Carve-out').closest('button')!)

    const samePromptNewOptions = [
      { question: 'Trust model', options: [{ label: 'Tenant-scoped' }, { label: 'Org-wide' }] },
      ...twoQuestions.slice(1),
    ]
    rerender(<QuestionCard questions={samePromptNewOptions} onSubmit={onSubmit} />)

    expect(screen.queryByText('Carve-out')).not.toBeInTheDocument()
    // The retained-answer proof, read off the control that exists on question 1:
    // a surviving pick would have left Next unlocked.
    expect((screen.getByText('Next').closest('button') as HTMLButtonElement).disabled).toBe(true)
  })

  it('offers Next until the last question, where Submit takes over', () => {
    // A multi-select does not auto-advance (one click does not finish it), so
    // without this the only primary control is a disabled Submit and the only way
    // on is the corner arrow.
    const threeQuestions = [
      ...twoQuestions,
      { question: 'Rollout', options: [{ label: 'canary' }, { label: 'full' }] },
    ]
    render(<QuestionCard questions={threeQuestions} onSubmit={vi.fn()} />)
    expect(screen.getByText('Next')).toBeInTheDocument()
    expect(screen.queryByText('Submit')).not.toBeInTheDocument()

    // Single-select auto-advance carries each step; Next is what a question that
    // does NOT auto-advance falls back on.
    fireEvent.click(screen.getByText('Carve-out').closest('button')!)
    expect(screen.getByText(at(2, 3))).toBeInTheDocument()
    expect(screen.getByText('Next')).toBeInTheDocument()
    expect(screen.queryByText('Submit')).not.toBeInTheDocument()

    fireEvent.click(screen.getByText('staging').closest('button')!)
    expect(screen.getByText(at(3, 3))).toBeInTheDocument()
    expect(screen.getByText('Submit')).toBeInTheDocument()
    expect(screen.queryByText('Next')).not.toBeInTheDocument()
  })

  it('carries a multi-select forward on Next, keeping every pick', () => {
    const onSubmit = vi.fn()
    const withMulti = [
      { question: 'Which features do you want?', options: [{ label: 'Dark mode' }, { label: 'Notifications' }], multiSelect: true },
      ...twoQuestions.slice(1),
    ]
    render(<QuestionCard questions={withMulti} onSubmit={onSubmit} />)
    fireEvent.click(screen.getByText('Dark mode').closest('button')!)
    fireEvent.click(screen.getByText('Notifications').closest('button')!)
    // Still on question 1: the mode is not finished by a click, so Next is the
    // "done picking" signal rather than the second selection being one.
    expect(screen.getByText(at(1, 2))).toBeInTheDocument()

    fireEvent.click(screen.getByText('Next'))
    fireEvent.click(screen.getByText('staging').closest('button')!)
    fireEvent.click(screen.getByText('Submit'))
    expect(onSubmit).toHaveBeenCalledWith({
      'Which features do you want?': 'Dark mode, Notifications',
      'Environments': 'staging',
    })
  })

  it('withholds Submit before the last question even when every answer is in', () => {
    // Strictly positional: one card is one atomic ask, so Submit lives at the end
    // of the walk and nowhere else. Completing out of order costs a trip there,
    // which the corner arrow makes one click.
    render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} />)
    fireEvent.click(nav('Next question'))
    fireEvent.click(screen.getByText('staging').closest('button')!)   // answer Q2 first
    fireEvent.click(nav('Previous question'))
    fireEvent.click(screen.getByText('Carve-out').closest('button')!)  // then Q1

    expect(screen.getByText(at(1, 2))).toBeInTheDocument()
    expect(screen.queryByText('Submit')).not.toBeInTheDocument()
    expect((screen.getByText('Next').closest('button') as HTMLButtonElement).disabled).toBe(false)

    fireEvent.click(screen.getByText('Next'))
    expect((screen.getByText('Submit').closest('button') as HTMLButtonElement).disabled).toBe(false)
  })

  it('offers Submit alone on a single-question card', () => {
    // It is the last question as well as the first, so Next would point nowhere.
    render(<QuestionCard questions={singleQuestion} onSubmit={vi.fn()} />)
    expect(screen.getByText('Submit')).toBeInTheDocument()
    expect(screen.queryByText('Next')).not.toBeInTheDocument()
  })

  it('keeps Next disabled until the question on screen is answered', () => {
    // Submit's allAnswered gate is at the FAR END of the card, so without this a
    // skipped question first surfaces several pages after it was skipped.
    render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} />)
    const next = () => screen.getByText('Next').closest('button') as HTMLButtonElement
    expect(next().disabled).toBe(true)

    // A typed custom answer counts, and does not auto-advance — so Next is the
    // way forward and must unlock on it, not only on an option click.
    fireEvent.change(screen.getByPlaceholderText('Or type a custom answer...'), { target: { value: 'tenant-scoped' } })
    expect(next().disabled).toBe(false)
    fireEvent.click(next())
    expect(screen.getByText(at(2, 2))).toBeInTheDocument()
  })

  it('leaves the corner arrows ungated, since review is not progress', () => {
    // A user going back to check an earlier answer must not be held there by the
    // same gate that governs forward progress.
    render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} />)
    expect((screen.getByText('Next').closest('button') as HTMLButtonElement).disabled).toBe(true)
    expect(nav('Next question').disabled).toBe(false)
    fireEvent.click(nav('Next question'))
    expect(screen.getByText(at(2, 2))).toBeInTheDocument()
    expect(nav('Previous question').disabled).toBe(false)
  })

  it('advances on Enter in the custom box, and submits there on the last question', () => {
    const onSubmit = vi.fn()
    render(<QuestionCard questions={twoQuestions} onSubmit={onSubmit} />)
    const box = () => screen.getByPlaceholderText('Or type a custom answer...')

    fireEvent.change(box(), { target: { value: 'tenant-scoped' } })
    fireEvent.keyDown(box(), { key: 'Enter' })
    expect(screen.getByText(at(2, 2))).toBeInTheDocument()

    fireEvent.change(box(), { target: { value: 'mini.local' } })
    fireEvent.keyDown(box(), { key: 'Enter' })
    expect(onSubmit).toHaveBeenCalledWith({ 'Trust model': 'tenant-scoped', 'Environments': 'mini.local' })
  })

  it('does not let a stray Enter skip an unanswered question', () => {
    render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} />)
    fireEvent.keyDown(screen.getByPlaceholderText('Or type a custom answer...'), { key: 'Enter' })
    expect(screen.getByText(at(1, 2))).toBeInTheDocument()
  })

  it('will not submit on Enter from a page that shows no Submit', () => {
    // Answer the second question via the arrows, come back to the first, retype
    // it: every question now has an answer, but this page's visible primary
    // action is Next. Submitting here resumes the agent on a keystroke that the
    // footer says means "move on", from a page with no Submit button on it.
    const onSubmit = vi.fn()
    render(<QuestionCard questions={twoQuestions} onSubmit={onSubmit} />)
    const box = () => screen.getByPlaceholderText('Or type a custom answer...')

    fireEvent.click(nav('Next question'))
    fireEvent.click(screen.getByText('staging'))
    fireEvent.click(nav('Previous question'))
    fireEvent.change(box(), { target: { value: 'tenant-scoped' } })

    fireEvent.keyDown(box(), { key: 'Enter' })
    expect(onSubmit).not.toHaveBeenCalled()
    // It advances instead, which is what the footer on this page offers.
    expect(screen.getByText(at(2, 2))).toBeInTheDocument()
  })

  it('carries focus onto the next page when Enter advances the walk', () => {
    // The page body is keyed by index, so advancing unmounts the focused input and
    // focus falls to <body>. The keyboard walk would then stop after one Enter.
    render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} />)
    const box = () => screen.getByPlaceholderText('Or type a custom answer...')

    box().focus()
    fireEvent.change(box(), { target: { value: 'tenant-scoped' } })
    fireEvent.keyDown(box(), { key: 'Enter' })

    expect(screen.getByText(at(2, 2))).toBeInTheDocument()
    expect(document.activeElement).toBe(box())
  })

  it('leaves focus alone when an arrow moves the page', () => {
    // Clicking a corner arrow is not a keyboard walk, so it must not yank the
    // caret into a text box the user never opened.
    render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} />)
    const next = nav('Next question')
    next.focus()
    fireEvent.click(next)

    expect(screen.getByText(at(2, 2))).toBeInTheDocument()
    expect(document.activeElement).not.toBe(
      screen.getByPlaceholderText('Or type a custom answer...'),
    )
  })

  it('names how many questions are unanswered and jumps to the first', () => {
    // Paging unmounts the other questions, so an unanswered one is invisible and
    // Submit is disabled with nothing on screen saying why. This is what replaces
    // the answer summary a folded row used to carry.
    render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} />)
    fireEvent.click(nav('Next question'))
    expect(screen.getByText('2 still unanswered')).toBeInTheDocument()

    fireEvent.click(screen.getByText('2 still unanswered'))
    expect(screen.getByText(at(1, 2))).toBeInTheDocument()
    expect(screen.getByText('Carve-out')).toBeInTheDocument()
  })

  it('states the count without offering a jump when the outstanding question is on screen', () => {
    // goTo(firstUnanswered) would land on the page the user is already reading,
    // so the click produces no visible change and the control reads as broken.
    render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} />)
    expect(screen.getByText('2 still unanswered').closest('button')).toBeNull()

    // Still a jump from anywhere the target is genuinely elsewhere.
    fireEvent.click(nav('Next question'))
    expect(screen.getByText('2 still unanswered').closest('button')).not.toBeNull()
  })

  it('keeps the jump out of the row that carries Dismiss and Next', () => {
    // Three peer controls in one horizontal group carry no ranking, so the row
    // has to be read label-by-label before anything can be clicked, and it is
    // the first thing to clip under width pressure.
    render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} onDismiss={vi.fn()} />)
    fireEvent.click(nav('Next question'))
    const jump = screen.getByText('2 still unanswered').closest('button')!
    const next = screen.getByText('Dismiss').closest('button')!.parentElement!
    expect(next).not.toContainElement(jump)
    expect(next.querySelectorAll('button')).toHaveLength(2)
  })

  it('drops the unanswered notice once every question is answered', () => {
    render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} />)
    fireEvent.click(screen.getByText('Carve-out').closest('button')!)
    expect(screen.getByText('1 still unanswered')).toBeInTheDocument()
    fireEvent.click(screen.getByText('staging').closest('button')!)
    expect(screen.queryByText(/still unanswered/)).not.toBeInTheDocument()
    expect((screen.getByText('Submit').closest('button') as HTMLButtonElement).disabled).toBe(false)
  })

  it('auto-advances to the next unanswered question on a single-select answer', () => {
    render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} />)
    fireEvent.click(screen.getByText('Carve-out').closest('button')!)
    // One gesture per question: answering IS the Next click. Without this the
    // card costs an answer plus an arrow for every question it holds.
    expect(screen.queryByText('Public only')).not.toBeInTheDocument()
    expect(screen.getByText('staging')).toBeInTheDocument()
    expect(screen.getByText(at(2, 2))).toBeInTheDocument()
  })

  it('submits answers picked across separate pages', () => {
    const onSubmit = vi.fn()
    render(<QuestionCard questions={twoQuestions} onSubmit={onSubmit} />)
    fireEvent.click(screen.getByText('Carve-out').closest('button')!)
    fireEvent.click(screen.getByText('staging').closest('button')!)
    fireEvent.click(screen.getByText('Submit').closest('button')!)
    // An unmounted page must not lose its answer: the selections map is keyed by
    // question index, not by what is on screen.
    expect(onSubmit).toHaveBeenCalledWith({ 'Trust model': 'Carve-out', 'Environments': 'staging' })
  })

  it('holds still on the last answer rather than jumping somewhere arbitrary', () => {
    render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} />)
    fireEvent.click(screen.getByText('Carve-out').closest('button')!)
    fireEvent.click(screen.getByText('staging').closest('button')!)
    // Nothing is outstanding, so there is nowhere to advance TO and Submit is the
    // only thing left. Moving anyway would re-show a question already settled.
    expect(screen.getByText(at(2, 2))).toBeInTheDocument()
    expect(screen.getByText('prod')).toBeInTheDocument()
  })

  it('does not advance away when an already-answered question is changed', () => {
    // The hand-off targets the first question with NO answer, not `page + 1`.
    // Targeting the next index would throw the user forward off a question they
    // deliberately came back to.
    const threeQuestions = [
      ...twoQuestions,
      { question: 'Rollout', options: [{ label: 'canary' }, { label: 'full' }] },
    ]
    render(<QuestionCard questions={threeQuestions} onSubmit={vi.fn()} />)
    fireEvent.click(screen.getByText('Carve-out').closest('button')!)
    fireEvent.click(screen.getByText('staging').closest('button')!)
    fireEvent.click(screen.getByText('canary').closest('button')!)

    fireEvent.click(nav('Previous question'))
    fireEvent.click(nav('Previous question'))
    expect(screen.getByText(at(1, 3))).toBeInTheDocument()
    fireEvent.click(screen.getByText('Public only').closest('button')!)
    expect(screen.getByText(at(1, 3))).toBeInTheDocument()
  })

  it('does not auto-advance a single-question card', () => {
    // There is nowhere to go, and the options the user is comparing must stay up.
    render(<QuestionCard questions={singleQuestion} onSubmit={vi.fn()} />)
    fireEvent.click(screen.getByText('Red').closest('button')!)
    expect(screen.getByText('Blue')).toBeInTheDocument()
  })

  it('does not auto-advance a multi-select, which is unfinished after one click', () => {
    // Advancing here would steal the second pick the mode exists to allow.
    const twoWithMulti = [
      { question: 'Which features do you want?', options: [{ label: 'Dark mode' }, { label: 'Notifications' }], multiSelect: true },
      ...twoQuestions.slice(1),
    ]
    render(<QuestionCard questions={twoWithMulti} onSubmit={vi.fn()} />)
    fireEvent.click(screen.getByText('Dark mode').closest('button')!)
    expect(screen.getByText('Notifications')).toBeInTheDocument()
    expect(screen.getByText(at(1, 2))).toBeInTheDocument()
  })

  it('does not auto-advance on a deselect, where the user is still choosing', () => {
    render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} />)
    fireEvent.click(screen.getByText('Carve-out').closest('button')!)
    fireEvent.click(nav('Previous question'))
    fireEvent.click(screen.getByText('Carve-out').closest('button')!)  // deselect
    expect(screen.getByText('Public only')).toBeInTheDocument()
    expect(screen.getByText(at(1, 2))).toBeInTheDocument()
  })

  it('publishes draft-active for a pending option selection, and clears it on deselect', () => {
    // GPT round-10: an unsubmitted option pick is in-progress work exactly
    // like typed custom text — the store must know, or auto-retirement
    // destroys it. Deselecting (single-select second click) must clear the
    // flag so the card becomes retirable again.
    const onDraftChange = vi.fn()
    render(<QuestionCard questions={singleQuestion} onSubmit={vi.fn()} onDraftChange={onDraftChange} />)
    onDraftChange.mockClear() // initial effect publishes false
    fireEvent.click(screen.getByText('Red').closest('button')!)
    expect(onDraftChange).toHaveBeenLastCalledWith(true)
    fireEvent.click(screen.getByText('Red').closest('button')!) // deselect
    expect(onDraftChange).toHaveBeenLastCalledWith(false)
  })

  it('publishes draft-active for custom text and clears the flag on unmount', () => {
    const onDraftChange = vi.fn()
    const { unmount } = render(
      <QuestionCard questions={singleQuestion} onSubmit={vi.fn()} onDraftChange={onDraftChange} />,
    )
    fireEvent.change(screen.getByPlaceholderText(/custom answer/i), { target: { value: 'maybe teal' } })
    expect(onDraftChange).toHaveBeenLastCalledWith(true)
    // A card removed for any other reason (resolution, dismiss) must not
    // leave a stale draftActive blocking a future card's retirement.
    unmount()
    expect(onDraftChange).toHaveBeenLastCalledWith(false)
  })

  it('says what Dismiss does, on the row and on the control', () => {
    // Dismiss is the only exit for a question nobody will answer, and the bare
    // label reads as "hide this for now": a user who suspects it might discard
    // the question leaves the dead card parked above the composer. The
    // consequence is stated as a visible line (a tooltip is not there for touch
    // or for a keyboard user reading the row) and repeated as the control's own
    // title.
    render(<QuestionCard questions={singleQuestion} onSubmit={vi.fn()} onDismiss={vi.fn()} />)
    const hint = /stops the agent waiting for an answer/i
    expect(screen.getByText(hint)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /dismiss question without answering/i }))
      .toHaveAttribute('title', expect.stringMatching(hint) as unknown as string)
  })

  it('shows no dismiss consequence line when the card cannot be dismissed', () => {
    // The line describes a control, so it must not appear without it.
    render(<QuestionCard questions={singleQuestion} onSubmit={vi.fn()} />)
    expect(screen.queryByText(/stops the agent waiting for an answer/i)).toBeNull()
  })

  it('caps its height and scrolls the questions, keeping the action row out of the scroller', () => {
    // A card taller than the column it mounts in grew PAST the top of the
    // viewport and was clipped there, so the first questions were neither
    // readable nor reachable. The questions must live in their own bounded
    // scroller, and Submit / Dismiss must sit outside it so they stay reachable
    // without scrolling to the end.
    render(<QuestionCard questions={singleQuestion} onSubmit={vi.fn()} onDismiss={vi.fn()} />)
    const card = screen.getByText('What is your favorite color?').closest('div.rounded-xl')!
    expect(card.className).toContain('max-h-[min(60vh,32rem)]')
    const scroller = screen.getByText('What is your favorite color?').closest('.overflow-y-auto')
    expect(scroller).not.toBeNull()
    // The action row is a sibling of the scroller, never inside it.
    const submitRow = screen.getByText('Submit').closest('div')!
    expect(scroller!.contains(submitRow)).toBe(false)
  })
})
