import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

/* The question card's header row on a phone.
 *
 * The row is the header badge, the question, and (on a paged card) the pager
 * -- two 36px arrows and an "N/M" indicator. Badge and pager are natural-width,
 * and the question is the `flex-1 min-w-0` remainder, so with both pinned on one
 * line at 320px a 50-character badge left the question a ~56px column. The
 * narrow shape gives the pager a row of its own under the text, and lets the
 * badge wrap instead of pushing the question out; `md:` puts the pager back in
 * its corner.
 *
 * Asserted on class names because jsdom runs no layout (the same reason
 * `narrowFirstBaseline` and QuestionCard.tapTarget read classes): these ARE the
 * layout, and each assertion fails when its class is dropped. Narrow is the
 * baseline (bare classes) and desktop is the `md:` addition -- the repo's
 * narrow-first convention, so `max-md:` must not appear. */
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

const HEADER = 'A HEADER BADGE THAT RUNS TO FIFTY CHARACTERS LONG'
const QUESTION = 'Which environments should this deploy to first?'
const twoQuestions = [
  { header: HEADER, question: QUESTION, options: [{ label: 'staging' }, { label: 'prod' }] },
  { question: 'Reporting', options: [{ label: 'Inline' }, { label: 'Toast' }] },
]

const classes = (el: Element) => el.className.split(/\s+/)
const question = () => screen.getByText(QUESTION)
const row = () => question().parentElement as HTMLElement
const pager = () => screen.getByLabelText('Previous question').parentElement as HTMLElement

describe('QuestionCard header row at phone width', () => {
  it('gives the pager its own row under the text and hands it back its corner from md up', () => {
    render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} />)
    // The pager wraps onto a full-width line of its own until `md:` widens it
    // back to natural width, where it sits on the header row as before.
    expect(classes(row())).toContain('flex-wrap')
    expect(classes(pager())).toContain('w-full')
    expect(classes(pager())).toContain('md:w-auto')
    // Same row, same parent: the pager still belongs to the header row (it is
    // wrapping, not relocated), so the desktop shape is the same DOM.
    expect(pager().parentElement).toBe(row())
  })

  it('keeps the question as the shrinkable remainder and lets the badge wrap', () => {
    render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} />)
    const badge = screen.getByText(HEADER)
    expect(badge.parentElement).toBe(row())
    // A `shrink-0` badge takes its natural width whatever the row has, and the
    // question -- the only shrinkable item -- pays for it.
    expect(classes(badge)).not.toContain('shrink-0')
    expect(classes(question())).toContain('flex-1')
    expect(classes(question())).toContain('min-w-0')
  })

  it('is written narrow-first', () => {
    render(<QuestionCard questions={twoQuestions} onSubmit={vi.fn()} />)
    // `max-md:` is the tell of a desktop-first rule reaching back for the phone.
    for (const el of [row(), pager(), question(), screen.getByText(HEADER)]) {
      expect(el.className).not.toMatch(/(^|\s)max-md:/)
    }
  })
})
