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
    'variants', 'whileHover', 'whileTap', 'onAnimationComplete',
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
    expect(screen.getByText('Red').closest('button')!.className).not.toContain('bg-accent-subtle')
    expect(screen.getByText('Blue').closest('button')!.className).toContain('bg-accent-subtle')
  })

  it('multi-select allows multiple selections', () => {
    render(<QuestionCard questions={multiQuestion} onSubmit={vi.fn()} />)
    fireEvent.click(screen.getByText('Dark mode').closest('button')!)
    fireEvent.click(screen.getByText('Notifications').closest('button')!)
    expect(screen.getByText('Dark mode').closest('button')!.className).toContain('border-accent')
    expect(screen.getByText('Notifications').closest('button')!.className).toContain('border-accent')
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
    expect(screen.getByText('Red').closest('button')!.className).not.toContain('bg-accent-subtle')
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

/* A three-question card with four options each is taller than the viewport: it
   buries the composer and the conversation above it, and before this the user
   could neither fold it nor (on a legacy card) dismiss it. */
describe('QuestionCard — collapsing', () => {
  const twoQuestions = [
    { question: 'Trust model', options: [{ label: 'Carve-out' }, { label: 'Public only' }] },
    { question: 'Environments', options: [{ label: 'staging' }, { label: 'prod' }] },
  ]

  const header = (text: string) => screen.getByText(text).closest('button')!

  it('folds a question away and back on the header toggle', () => {
    render(<QuestionCard questions={singleQuestion} onSubmit={vi.fn()} />)
    expect(screen.getByText('Red')).toBeInTheDocument()

    fireEvent.click(header('What is your favorite color?'))
    expect(screen.queryByText('Red')).not.toBeInTheDocument()
    // The custom-answer input folds with the options — otherwise a "collapsed"
    // question still costs a full row.
    expect(screen.queryByPlaceholderText('Or type a custom answer...')).not.toBeInTheDocument()

    fireEvent.click(header('What is your favorite color?'))
    expect(screen.getByText('Red')).toBeInTheDocument()
  })

  it('reports its state to assistive tech without hiding the question', () => {
    render(<QuestionCard questions={singleQuestion} onSubmit={vi.fn()} />)
    // No aria-label on the toggle: it would replace the accessible name, so every
    // folded row would announce identically and hide the question + answer.
    expect(header('What is your favorite color?')).toHaveAttribute('aria-expanded', 'true')
    expect(header('What is your favorite color?')).not.toHaveAttribute('aria-label')
    fireEvent.click(header('What is your favorite color?'))
    expect(header('What is your favorite color?')).toHaveAttribute('aria-expanded', 'false')
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
    expect((screen.getByText('Submit').closest('button') as HTMLButtonElement).disabled).toBe(true)
  })

  it('shows the chosen answer while folded, so nothing is hidden from the user', () => {
    render(<QuestionCard questions={singleQuestion} onSubmit={vi.fn()} />)
    fireEvent.change(screen.getByPlaceholderText('Or type a custom answer...'), { target: { value: 'Purple' } })
    fireEvent.click(header('What is your favorite color?'))
    expect(screen.getByText('Purple')).toBeInTheDocument()
  })

  it('says a folded question is unanswered, since that is why Submit is locked', () => {
    render(<QuestionCard questions={singleQuestion} onSubmit={vi.fn()} />)
    fireEvent.click(header('What is your favorite color?'))
    expect(screen.getByText('Not answered yet')).toBeInTheDocument()
    expect(screen.getByText('Submit').closest('button')!).toBeDisabled()
  })

  it('auto-folds an answered question on a multi-question card', () => {
    render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} />)
    fireEvent.click(screen.getByText('Carve-out').closest('button')!)
    // Q1 folds to its answer; Q2 stays open, so the card walks DOWN to Submit
    // instead of growing past it.
    expect(screen.queryByText('Public only')).not.toBeInTheDocument()
    expect(screen.getByText('staging')).toBeInTheDocument()
  })

  it('keeps a folded answer submittable', () => {
    const onSubmit = vi.fn()
    render(<QuestionCard questions={twoQuestions} onSubmit={onSubmit} />)
    fireEvent.click(screen.getByText('Carve-out').closest('button')!)
    fireEvent.click(screen.getByText('staging').closest('button')!)
    fireEvent.click(screen.getByText('Submit').closest('button')!)
    expect(onSubmit).toHaveBeenCalledWith({ 'Trust model': 'Carve-out', 'Environments': 'staging' })
  })

  it('does not auto-fold a single-question card', () => {
    // Nothing follows it, so folding only hides the picks the user is comparing.
    render(<QuestionCard questions={singleQuestion} onSubmit={vi.fn()} />)
    fireEvent.click(screen.getByText('Red').closest('button')!)
    expect(screen.getByText('Blue')).toBeInTheDocument()
  })

  it('does not auto-fold a multi-select, which is unfinished after one click', () => {
    const twoWithMulti = [
      { question: 'Which features do you want?', options: [{ label: 'Dark mode' }, { label: 'Notifications' }], multiSelect: true },
      ...twoQuestions.slice(1),
    ]
    render(<QuestionCard questions={twoWithMulti} onSubmit={vi.fn()} />)
    fireEvent.click(screen.getByText('Dark mode').closest('button')!)
    expect(screen.getByText('Notifications')).toBeInTheDocument()
  })

  it('does not fold on a deselect, where there is no answer to summarise', () => {
    render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} />)
    fireEvent.click(screen.getByText('Carve-out').closest('button')!)
    fireEvent.click(header('Trust model'))       // re-open it
    fireEvent.click(screen.getByText('Carve-out').closest('button')!)  // deselect
    expect(screen.getByText('Public only')).toBeInTheDocument()
  })

  it('folds and unfolds every question from one control', () => {
    render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} />)
    fireEvent.click(screen.getByText('Collapse all'))
    expect(screen.queryByText('Carve-out')).not.toBeInTheDocument()
    expect(screen.queryByText('staging')).not.toBeInTheDocument()

    // The label flips to the action now available, so one button covers both.
    fireEvent.click(screen.getByText('Expand all'))
    expect(screen.getByText('Carve-out')).toBeInTheDocument()
    expect(screen.getByText('staging')).toBeInTheDocument()
  })

  it('folds the rest when only some questions are already folded', () => {
    // Mixed state must mean "collapse all", not "expand all" — otherwise the
    // control re-opens what the user just folded.
    render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} />)
    fireEvent.click(header('Trust model'))
    expect(screen.getByText('Collapse all')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Collapse all'))
    expect(screen.queryByText('staging')).not.toBeInTheDocument()
  })

  it('offers no collapse-all on a single question, where the chevron is the same gesture', () => {
    render(<QuestionCard questions={singleQuestion} onSubmit={vi.fn()} />)
    expect(screen.queryByText('Collapse all')).not.toBeInTheDocument()
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
