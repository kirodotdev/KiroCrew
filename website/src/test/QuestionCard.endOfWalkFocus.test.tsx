import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

/* Its own file because it needs the exit-window framer-motion stub from
   QuestionCard.focusCarry.test.tsx, and `vi.mock` is per-file. Same stub, same
   reason: `mode="wait"` holds the leaving page for the whole exit and withholds
   the entering one, and a focus bug that hides in that gap is invisible to the
   fragment stub QuestionCard.test.tsx uses. */
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
  const cache = new Map<string, unknown>()

  const AnimatePresence = ({
    children,
    mode,
  }: { children?: React.ReactNode; mode?: string }) => {
    const child = (children ?? null) as React.ReactElement | null
    const key = child?.key ?? null
    const [liveKey, setLiveKey] = React.useState(key)
    const onScreen = React.useRef(child)
    if (key === liveKey) onScreen.current = child
    React.useEffect(() => {
      if (key === liveKey) return
      const t = setTimeout(() => setLiveKey(key), 0)
      return () => clearTimeout(t)
    }, [key, liveKey])
    const rendered = mode === 'wait' && key !== liveKey ? onScreen.current : child
    return React.createElement(React.Fragment, null, rendered)
  }

  return {
    motion: new Proxy({}, {
      get: (_t, tag: string) => {
        if (!cache.has(tag)) cache.set(tag, make(tag))
        return cache.get(tag)
      },
    }),
    AnimatePresence,
    useReducedMotion: () => false,
  }
})

import QuestionCard from '../components/QuestionCard'

/* The first question is a multi-select so that answering it does NOT
   auto-advance: the footer Next has to be the thing that moves the page, or
   the test never exercises it. */
const twoQuestions = [
  { question: 'Gates', multiSelect: true, options: [{ label: 'Unit tests' }, { label: 'Linter' }] },
  { question: 'Environments', options: [{ label: 'staging' }, { label: 'prod' }] },
]
const threeQuestions = [
  ...twoQuestions,
  { question: 'Reporting', options: [{ label: 'Inline' }, { label: 'Toast' }] },
]

const prev = () => screen.getByLabelText('Previous question')
const next = () => screen.getByLabelText('Next question')

/* The invariant every case below checks. A browser drops focus from a control
   that becomes `disabled` or leaves the document, so after the move focus must
   be on a real, enabled control inside the card — never on <body>, where the
   next Tab restarts from the top of the page, and never on a control the page
   just disabled, which is where the browser would drop it from. */
const expectFocusKept = (on: HTMLElement) => {
  const active = document.activeElement as HTMLElement | null
  expect(active).not.toBe(document.body)
  expect(active).toBeInTheDocument()
  expect(active).not.toBeDisabled()
  expect(active).toBe(on)
}

describe('QuestionCard keeps focus on a control when the activated control goes away', () => {
  it('forward arrow onto the last page hands focus to the back arrow', async () => {
    // On the last page `›` becomes disabled under the focus that just pressed it.
    render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} />)
    next().focus()
    fireEvent.click(next())

    await waitFor(() => expect(screen.getByText('staging')).toBeInTheDocument())
    expect(next()).toBeDisabled()
    expectFocusKept(prev())
  })

  it('back arrow onto the first page hands focus to the forward arrow', async () => {
    // Symmetric: on page 0 `‹` becomes disabled under the focus that pressed it.
    render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} />)
    fireEvent.click(next())
    await waitFor(() => expect(screen.getByText('staging')).toBeInTheDocument())

    prev().focus()
    fireEvent.click(prev())

    await waitFor(() => expect(screen.getByText('Unit tests')).toBeInTheDocument())
    expect(prev()).toBeDisabled()
    expectFocusKept(next())
  })

  it('footer Next onto the last page hands focus to the back arrow', async () => {
    /* Submit replaces Next on the last page. React reconciles the two <button>s
       at the same position into ONE node, so the focused element does not leave
       the document — it becomes a Submit that is disabled until the question
       just reached is answered, and a browser drops focus from a control that
       becomes disabled exactly as it does from one that unmounts. */
    render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Unit tests' }))
    const footerNext = screen.getByRole('button', { name: /^Next$/ })
    footerNext.focus()
    fireEvent.click(footerNext)

    await waitFor(() => expect(screen.getByText('staging')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /^Next$/ })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Submit/ })).toBeDisabled()
    expectFocusKept(prev())
  })

  it('footer Next onto an unanswered middle page hands focus to the back arrow', async () => {
    // Not the last page, so Next stays mounted — but it is gated on the page's
    // question being answered, and the page just reached has none, so it disables.
    render(<QuestionCard questions={threeQuestions} onSubmit={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Unit tests' }))
    const footerNext = screen.getByRole('button', { name: /^Next$/ })
    footerNext.focus()
    fireEvent.click(footerNext)

    await waitFor(() => expect(screen.getByText('staging')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /^Next$/ })).toBeDisabled()
    expectFocusKept(prev())
  })

  it('footer Next onto a completed last page leaves focus on the enabled Submit', async () => {
    // With every question answered the node this Next becomes is an ENABLED
    // Submit, so focus can stay put and the next Enter finishes the card.
    const onSubmit = vi.fn()
    render(<QuestionCard questions={twoQuestions} onSubmit={onSubmit} />)
    fireEvent.click(screen.getByRole('button', { name: 'Unit tests' }))
    fireEvent.click(next())
    await waitFor(() => expect(screen.getByText('staging')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'staging' }))
    fireEvent.click(prev())
    await waitFor(() => expect(screen.getByText('Unit tests')).toBeInTheDocument())

    const footerNext = screen.getByRole('button', { name: /^Next$/ })
    footerNext.focus()
    fireEvent.click(footerNext)

    await waitFor(() => expect(screen.getByText('staging')).toBeInTheDocument())
    const submit = screen.getByRole('button', { name: /Submit/ })
    expect(submit).toBeEnabled()
    expectFocusKept(submit)
    fireEvent.click(document.activeElement as HTMLElement)
    expect(onSubmit).toHaveBeenCalledWith({ Gates: 'Unit tests', Environments: 'staging' })
  })

  it('leaves focus on the arrow that still has pages ahead of it', async () => {
    // The hand-off is only for a control that is about to go away. `›` on page 0
    // of three keeps working after the move, and moving focus off it would stop a
    // keyboard user pressing Enter again to keep paging.
    render(<QuestionCard questions={threeQuestions} onSubmit={vi.fn()} />)
    next().focus()
    fireEvent.click(next())

    await waitFor(() => expect(screen.getByText('staging')).toBeInTheDocument())
    expectFocusKept(next())
  })

  it('the unanswered jump forward hands focus to the back arrow', async () => {
    /* The jump lands on the unanswered page, where the same control degrades to
       plain text: the <button> is replaced by a <span>, so the focused node
       leaves the document and focus falls to <body>. Forward, so `‹` is the
       arrow pointing back the way the user came. */
    render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Unit tests' }))
    const jump = screen.getByRole('button', { name: /still unanswered/ })
    jump.focus()
    fireEvent.click(jump)

    await waitFor(() => expect(screen.getByText('staging')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /still unanswered/ })).not.toBeInTheDocument()
    expectFocusKept(prev())
  })

  it('the unanswered jump backward hands focus to the forward arrow', async () => {
    // Symmetric: from the last page back to an earlier unanswered question, the
    // arrow guaranteed enabled on the page reached is `›`.
    render(<QuestionCard questions={threeQuestions} onSubmit={vi.fn()} />)
    fireEvent.click(next())
    await waitFor(() => expect(screen.getByText('staging')).toBeInTheDocument())
    fireEvent.click(next())
    await waitFor(() => expect(screen.getByText('Inline')).toBeInTheDocument())

    const jump = screen.getByRole('button', { name: /still unanswered/ })
    jump.focus()
    fireEvent.click(jump)

    await waitFor(() => expect(screen.getByText('Unit tests')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /still unanswered/ })).not.toBeInTheDocument()
    expectFocusKept(next())
  })
})
