// Draft snapshots keep custom text as typed so a restored card resumes mid-answer.
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
import { answersAsText } from '../utils/questionAnswers'

const QUESTIONS = [
  { question: 'Which city?', options: [{ label: 'Paris' }, { label: 'Tokyo' }] },
]

const customInput = () => screen.getByRole('textbox', { name: 'Custom answer' })

describe('QuestionCard draft whitespace', () => {
  it('keeps an answer to a question whose text is a prototype key as an own draft entry', () => {
    const onDraftChange = vi.fn()
    render(
      <QuestionCard
        questions={[{ question: '__proto__', options: [{ label: 'Paris' }] }]}
        askId="ask-1"
        onSubmit={vi.fn()}
        onDraftChange={onDraftChange}
      />,
    )
    fireEvent.change(customInput(), { target: { value: 'Lyon' } })
    const draft = onDraftChange.mock.lastCall?.[0] as Record<string, string>
    expect(Object.keys(draft)).toEqual(['__proto__'])
    expect(Object.getOwnPropertyDescriptor(draft, '__proto__')?.value).toBe('Lyon')
  })

  it('publishes the custom text untrimmed so a draft keeps its trailing space', () => {
    const onDraftChange = vi.fn()
    render(<QuestionCard questions={QUESTIONS} askId="ask-1" onSubmit={vi.fn()} onDraftChange={onDraftChange} />)
    fireEvent.change(customInput(), { target: { value: 'New ' } })
    expect(onDraftChange).toHaveBeenLastCalledWith({ 'Which city?': 'New ' })
  })

  it('restores a draft with its trailing space, so continuing the answer keeps the words apart', () => {
    const onSubmit = vi.fn()
    render(
      <QuestionCard questions={QUESTIONS} askId="ask-1" onSubmit={onSubmit} draftAnswers={{ 'Which city?': 'New ' }} />,
    )
    expect(customInput()).toHaveValue('New ')
    fireEvent.change(customInput(), { target: { value: 'New York ' } })
    fireEvent.click(screen.getByRole('button', { name: /send answer|submit/i }))
    expect(onSubmit).toHaveBeenCalledWith({ 'Which city?': 'New York' })
  })

  it('does not record a whitespace-only custom answer as a draft', () => {
    const onDraftChange = vi.fn()
    render(<QuestionCard questions={QUESTIONS} askId="ask-1" onSubmit={vi.fn()} onDraftChange={onDraftChange} />)
    fireEvent.change(customInput(), { target: { value: '   ' } })
    expect(onDraftChange).toHaveBeenLastCalledWith({})
  })

  it('trims answers in the text handed back to a composer', () => {
    expect(answersAsText({ 'Which city?': 'New York ' })).toBe('New York')
  })
})
