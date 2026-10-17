// Blocking cards cap custom answers at ASK_MAX_ANSWER_LEN and warn near the limit.
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'initial', 'animate', 'exit', 'transition',
    'variants', 'custom', 'whileHover', 'whileTap', 'onAnimationComplete',
  ])
  const cache = new Map<string, unknown>()
  const make = (tag: string) =>
    React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
      const clean: Record<string, unknown> = {}
      for (const key of Object.keys(props)) {
        if (key === 'children' || FRAMER_PROPS.has(key)) continue
        clean[key] = props[key]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  const AnimatePresence = ({ children }: { children?: React.ReactNode }) =>
    React.createElement(React.Fragment, null, children ?? null)
  return {
    motion: new Proxy({}, {
      get: (_target, tag: string) => {
        if (!cache.has(tag)) cache.set(tag, make(tag))
        return cache.get(tag)
      },
    }),
    AnimatePresence,
    useReducedMotion: () => false,
  }
})

import QuestionCard from '../components/QuestionCard'
import { ASK_MAX_ANSWER_LEN } from '../utils/askQuestionTool'

const QUESTIONS = [
  { question: 'Pick a trust model', options: [{ label: 'Carve-out' }, { label: 'Public only' }] },
]

const HINT_TEST_ID = 'custom-answer-chars-left'

function typeCustom(text: string) {
  fireEvent.change(screen.getByRole('textbox', { name: 'Custom answer' }), { target: { value: text } })
}

function renderBlockingCard() {
  render(<QuestionCard questions={QUESTIONS} askId="ask-1" onSubmit={vi.fn()} />)
}

describe('QuestionCard custom-answer remaining-characters hint', () => {
  it('keeps the native card at 2000 characters without a hint at the blocking cap', () => {
    render(<QuestionCard questions={QUESTIONS} onSubmit={vi.fn()} />)
    const input = screen.getByRole('textbox', { name: 'Custom answer' })
    expect(input).toHaveAttribute('maxlength', '2000')

    const answer = 'x'.repeat(ASK_MAX_ANSWER_LEN + 1)
    typeCustom(answer)
    expect(input).toHaveValue(answer)
    expect(screen.queryByText(/Answer cut|characters? left/)).toBeNull()
  })

  it('stays absent for a short answer', () => {
    renderBlockingCard()
    typeCustom('a short custom answer')
    expect(screen.queryByText(/characters? left/)).toBeNull()
  })

  it('stays absent just under the 90% threshold', () => {
    renderBlockingCard()
    typeCustom('x'.repeat(Math.floor(ASK_MAX_ANSWER_LEN * 0.9) - 1))
    expect(screen.queryByText(/characters? left/)).toBeNull()
  })

  it('names the remaining count once the answer reaches 90% of the cap', () => {
    renderBlockingCard()
    const typed = Math.floor(ASK_MAX_ANSWER_LEN * 0.9)
    typeCustom('x'.repeat(typed))
    expect(screen.getByText(`${ASK_MAX_ANSWER_LEN - typed} characters left`)).toBeInTheDocument()
  })

  it('counts down to the cap, singular at one, and announces politely', () => {
    renderBlockingCard()
    typeCustom('x'.repeat(ASK_MAX_ANSWER_LEN - 12))
    const hint = screen.getByText('12 characters left')
    expect(hint.closest('[aria-live="polite"]')).not.toBeNull()
    expect(screen.getByTestId(HINT_TEST_ID)).toBe(hint)

    typeCustom('x'.repeat(ASK_MAX_ANSWER_LEN - 1))
    expect(screen.getByText('1 character left')).toBeInTheDocument()
  })

  it('keeps the blocking card at 1482 characters and announces the cut', () => {
    renderBlockingCard()
    expect(screen.getByRole('textbox', { name: 'Custom answer' })).toHaveAttribute('maxlength', String(ASK_MAX_ANSWER_LEN))
    typeCustom('x'.repeat(ASK_MAX_ANSWER_LEN))
    const hint = screen.getByText(`Answer cut at ${ASK_MAX_ANSWER_LEN} characters`)
    expect(screen.getByTestId(HINT_TEST_ID)).toBe(hint)
    expect(screen.queryByText(/characters? left/)).toBeNull()

    typeCustom('x'.repeat(ASK_MAX_ANSWER_LEN - 1))
    expect(screen.getByText('1 character left')).toBeInTheDocument()
    expect(screen.queryByText(/Answer cut/)).toBeNull()
  })

  it('disappears again when the answer is shortened', () => {
    renderBlockingCard()
    typeCustom('x'.repeat(ASK_MAX_ANSWER_LEN - 3))
    expect(screen.getByText('3 characters left')).toBeInTheDocument()
    typeCustom('shorter')
    expect(screen.queryByText(/characters? left/)).toBeNull()
  })
})
