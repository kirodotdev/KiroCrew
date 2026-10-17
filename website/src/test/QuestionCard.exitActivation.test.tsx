import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

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
      const timer = setTimeout(() => setLiveKey(key), 160)
      return () => clearTimeout(timer)
    }, [key, liveKey])
    const rendered = mode === 'wait' && key !== liveKey ? onScreen.current : child
    return React.createElement(React.Fragment, null, rendered)
  }

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

const QUESTIONS = [
  { question: 'Pick a trust model', options: [{ label: 'Carve-out' }, { label: 'Public only' }] },
  { question: 'Pick a rollout', options: [{ label: 'Staging' }, { label: 'Prod' }] },
]

describe('QuestionCard exiting option activation', () => {
  it('ignores a keyboard click from the focused exiting option', async () => {
    const onSubmit = vi.fn()
    render(<QuestionCard questions={QUESTIONS} onSubmit={onSubmit} />)

    const option = screen.getByRole('button', { name: 'Carve-out' })
    option.focus()
    fireEvent.click(option)

    expect(option).toBeInTheDocument()
    expect(document.activeElement).toBe(option)
    fireEvent.click(option, { detail: 0 })

    await waitFor(() => expect(screen.getByRole('button', { name: 'Staging' })).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Staging' }))
    const submit = screen.getByRole('button', { name: /Submit/ })
    expect(submit).toBeEnabled()
    fireEvent.click(submit)
    expect(onSubmit).toHaveBeenCalledWith({
      'Pick a trust model': 'Carve-out',
      'Pick a rollout': 'Staging',
    })
  })
})
