import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

/* A SEPARATE file again, because this needs a third framer-motion stub and
   `vi.mock` is per-file.

   `QuestionCard.test.tsx` renders AnimatePresence as a fragment, so there is no
   exit window at all. `QuestionCard.focusCarry.test.tsx` models the window but
   strips `variants` / `exit` / `custom` along with every other framer prop, so
   nothing the exit variant declares ever reaches the DOM.

   This stub keeps the window AND resolves the exit variant the way
   framer-motion does — function variant called with `custom`, the result applied
   as inline style on the leaving element. That is what makes the assertion
   below behavioural: `pointer-events: none` is a real computed style on a real
   node, and `user-event` refuses a click on it exactly as a browser would,
   rather than the test matching a source string. */
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
    custom,
  }: { children?: React.ReactNode; mode?: string; custom?: unknown }) => {
    const child = (children ?? null) as React.ReactElement | null
    const key = child?.key ?? null
    const [liveKey, setLiveKey] = React.useState(key)
    const onScreen = React.useRef(child)
    if (key === liveKey) onScreen.current = child
    React.useEffect(() => {
      if (key === liveKey) return
      /* 160ms, the component's own transition duration, rather than 0: the test
         drives the clock, so the exit window stays open across an interaction
         instead of closing on the first await. */
      const t = setTimeout(() => setLiveKey(key), 160)
      return () => clearTimeout(t)
    }, [key, liveKey])

    const exiting = mode === 'wait' && key !== liveKey
    const held = onScreen.current
    if (!exiting || !held) return React.createElement(React.Fragment, null, child)

    /* Resolve the leaving child's exit variant: `exit="exit"` names it,
       `variants` holds it, and a function variant takes `custom`. */
    const props = held.props as Record<string, never>
    const variants = props.variants as Record<string, unknown> | undefined
    const name = props.exit as string | undefined
    const raw = variants && name ? variants[name] : undefined
    const resolved = (typeof raw === 'function' ? raw(custom) : raw) as
      | Record<string, unknown>
      | undefined
    const style = {
      ...((props.style as Record<string, unknown>) ?? {}),
      ...(resolved?.pointerEvents ? { pointerEvents: resolved.pointerEvents } : {}),
    }
    return React.createElement(
      React.Fragment,
      null,
      React.cloneElement(held, { style } as never),
    )
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

const QUESTIONS = [
  { question: 'Pick a trust model', options: [{ label: 'Carve-out' }, { label: 'Public only' }] },
  { question: 'Pick a rollout', options: [{ label: 'Staging' }, { label: 'Prod' }] },
]

describe('QuestionCard exiting page is inert', () => {
  /* Picking a single-select on a multi-question card auto-advances, and
     `mode="wait"` leaves the page just answered on screen for the whole exit.
     Its option buttons are still bound to `toggleOption`, whose single-select
     branch CLEARS the set when the option was already selected — so a second
     click inside that window wipes the answer that caused the advance, and the
     card cannot submit it. A sub-160ms double-click is ordinary, not exotic.

     What this asserts is the declaration, on the real node, with the variant
     resolved the way framer-motion resolves it. jsdom does no hit-testing, so
     whether a click is actually refused is not jsdom's to prove — the same
     limit `QuestionCard.tapTarget.test.ts` records for sizes. `pointerEvents`
     missing from the exit variant fails this outright. */
  it('declares the leaving page non-interactive while it is still mounted', () => {
    render(<QuestionCard questions={QUESTIONS} onSubmit={vi.fn()} />)

    const option = screen.getByRole('button', { name: /Carve-out/ })
    /* Synchronous, so the pending exit timer has not run and the leaving page
       is still the one on screen. */
    fireEvent.click(option)

    expect(option, 'mode="wait" holds the leaving page until its exit finishes')
      .toBeInTheDocument()

    const page = option.closest('[style]')
    expect(page, 'the leaving page should carry a resolved exit style').not.toBeNull()
    expect(page).toHaveStyle({ pointerEvents: 'none' })
  })

  it('leaves the settled page interactive', async () => {
    render(<QuestionCard questions={QUESTIONS} onSubmit={vi.fn()} />)

    const option = screen.getByRole('button', { name: /Carve-out/ })
    fireEvent.click(option)
    /* Once the exit completes the entering page is on screen and must NOT
       inherit the inert style, or the card would be dead to the pointer. */
    await waitFor(() => expect(screen.getByText('Pick a rollout')).toBeInTheDocument())

    const landed = screen.getByText('Pick a rollout').closest('[style]')
    if (landed) expect(landed).not.toHaveStyle({ pointerEvents: 'none' })
  })
})
