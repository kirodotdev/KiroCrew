import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

/* A SEPARATE file from QuestionCard.test.tsx because it needs a different
   framer-motion stub, and `vi.mock` is per-file.

   The sibling file renders AnimatePresence as a fragment, so a page change
   unmounts the old question and mounts the new one in ONE commit. Real
   `mode="wait"` does not: it holds the EXITING child for the whole exit
   transition and withholds the entering one until that finishes. Every
   focus-after-page-change bug lives in exactly that gap, and the fragment stub
   cannot see any of them — which is why the walk could break in the browser with
   `carries focus onto the next page when Enter advances the walk` passing.

   So this stub models the gap: while the incoming key differs from the one on
   screen it keeps rendering the previous element, and swaps a tick later. The
   deferral is a timer rather than a real animation, so the ordering is
   deterministic instead of timing-dependent. */
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
    /* Holds the element that is on screen. Written during render only while the
       key is unchanged, which is the pass that has no exit in flight, so the
       held element is never a half-updated one. */
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

const twoQuestions = [
  { question: 'Trust model', options: [{ label: 'Carve-out' }, { label: 'Public only' }] },
  { question: 'Environments', options: [{ label: 'staging' }, { label: 'prod' }] },
]

describe('QuestionCard focus carry across a real exit transition', () => {
  const box = () => screen.getByPlaceholderText('Or type a custom answer...')

  it('lands focus on the input the walk continues in, not the one leaving', async () => {
    /* Focusing from an effect keyed on `page` targets the EXITING input — it is
       still mounted at that commit, so the focus call succeeds and looks right,
       and then the exit completes, the input unmounts and focus falls to <body>.
       The keyboard walk stops after one Enter. Focus therefore has to be attached
       by the entering input itself. */
    render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} />)

    box().focus()
    fireEvent.change(box(), { target: { value: 'tenant-scoped' } })
    fireEvent.keyDown(box(), { key: 'Enter' })

    await waitFor(() => expect(screen.getByText('staging')).toBeInTheDocument())
    expect(document.activeElement).toBe(box())
  })

  it('keeps the walk going for a second Enter on the page it landed on', async () => {
    // The whole point of carrying focus: Enter, Enter, Submit without a pointer.
    const onSubmit = vi.fn()
    render(<QuestionCard questions={twoQuestions} onSubmit={onSubmit} />)

    box().focus()
    fireEvent.change(box(), { target: { value: 'tenant-scoped' } })
    fireEvent.keyDown(box(), { key: 'Enter' })
    await waitFor(() => expect(screen.getByText('staging')).toBeInTheDocument())

    fireEvent.change(document.activeElement as HTMLInputElement, { target: { value: 'prod only' } })
    fireEvent.keyDown(document.activeElement as HTMLInputElement, { key: 'Enter' })

    expect(onSubmit).toHaveBeenCalledWith({
      'Trust model': 'tenant-scoped',
      Environments: 'prod only',
    })
  })

  it('still leaves focus alone when an arrow moves the page', async () => {
    // The carry is keyboard-only. A pointer click must not pull the caret into a
    // text box the user never opened, and the deferred mount must not change that.
    render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} />)
    const next = screen.getByLabelText('Next question')
    next.focus()
    fireEvent.click(next)

    await waitFor(() => expect(screen.getByText('staging')).toBeInTheDocument())
    expect(document.activeElement).not.toBe(box())
  })
})
