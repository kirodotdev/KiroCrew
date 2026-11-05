import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import { KnowledgePicker } from './KnowledgePicker'
import type { KnowledgeResult } from './useKnowledgeFetch'

vi.mock('../../components/ErrorNotice', () => ({
  default: (props: { title?: string; message?: string; onDismiss?: () => void }) => (
    <div data-testid="error-notice">
      <span>{props.title}</span>
      <span>{props.message}</span>
      <button type="button" onClick={props.onDismiss}>notice-dismiss</button>
    </div>
  ),
}))

function result(overrides: Partial<KnowledgeResult> & { id: string }): KnowledgeResult {
  return {
    title: `Title ${overrides.id}`,
    source: null,
    match_type: 'keyword',
    tokens: 10,
    summary: `Summary ${overrides.id}`,
    content: `Content ${overrides.id}`,
    ...overrides,
  }
}

const noop = () => {}

describe('KnowledgePicker', () => {
  it('shows the searching state with no selectable results while loading', () => {
    render(
      <KnowledgePicker
        results={[result({ id: 'a' })]}
        query="deploy"
        loading
        onInject={noop}
        onSkip={noop}
      />,
    )
    expect(screen.getByText(/Searching knowledge for “deploy”/)).toBeInTheDocument()
    expect(screen.queryByRole('checkbox')).toBeNull()
  })

  it('shows the failure notice with the query title and the backend message', () => {
    render(
      <KnowledgePicker
        results={[]}
        query="deploy"
        loading={false}
        error="gateway refused the search"
        onInject={noop}
        onSkip={noop}
      />,
    )
    expect(screen.getByTestId('error-notice')).toHaveTextContent(/Knowledge search failed for “deploy”/)
    expect(screen.getByText('gateway refused the search')).toBeInTheDocument()
  })

  it('re-runs the failed search from the Retry button and dismisses via the notice', () => {
    const onRetry = vi.fn()
    const onSkip = vi.fn()
    render(
      <KnowledgePicker
        results={[]}
        query="deploy"
        loading={false}
        error="boom"
        onRetry={onRetry}
        onInject={noop}
        onSkip={onSkip}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(onRetry).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: 'notice-dismiss' }))
    expect(onSkip).toHaveBeenCalledTimes(1)
  })

  it('omits the Retry button when no retry handler is given', () => {
    render(
      <KnowledgePicker
        results={[]}
        query="deploy"
        loading={false}
        error="boom"
        onInject={noop}
        onSkip={noop}
      />,
    )
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
  })

  it('reports no knowledge found and calls onSkip from its dismiss button', () => {
    const onSkip = vi.fn()
    render(
      <KnowledgePicker
        results={[]}
        query="nothing here"
        loading={false}
        onInject={noop}
        onSkip={onSkip}
      />,
    )
    expect(screen.getByText(/No knowledge found for “nothing here”/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(onSkip).toHaveBeenCalledTimes(1)
  })

  it('pre-selects only the first result and totals its tokens', () => {
    render(
      <KnowledgePicker
        results={[
          result({ id: 'a', tokens: 120 }),
          result({ id: 'b', tokens: 40 }),
        ]}
        query="deploy"
        loading={false}
        onInject={noop}
        onSkip={noop}
      />,
    )
    expect(screen.getByRole('checkbox', { name: 'Title a' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Title b' })).not.toBeChecked()
    expect(screen.getByText('1 selected · 120 tokens')).toBeInTheDocument()
  })

  it('toggles a result on and off and keeps the running token total in step', () => {
    render(
      <KnowledgePicker
        results={[
          result({ id: 'a', tokens: 120 }),
          result({ id: 'b', tokens: 40 }),
        ]}
        query="deploy"
        loading={false}
        onInject={noop}
        onSkip={noop}
      />,
    )
    const b = screen.getByRole('checkbox', { name: 'Title b' })
    fireEvent.click(b)
    expect(b).toBeChecked()
    expect(screen.getByText('2 selected · 160 tokens')).toBeInTheDocument()

    fireEvent.click(b)
    expect(b).not.toBeChecked()
    expect(screen.getByText('1 selected · 120 tokens')).toBeInTheDocument()
  })

  it('injects exactly the selected results in result order', () => {
    const onInject = vi.fn()
    const a = result({ id: 'a' })
    const b = result({ id: 'b' })
    const c = result({ id: 'c' })
    render(<KnowledgePicker results={[a, b, c]} query="q" loading={false} onInject={onInject} onSkip={noop} />)

    fireEvent.click(screen.getByRole('checkbox', { name: 'Title c' }))
    fireEvent.click(screen.getByRole('button', { name: 'Inject Context' }))

    expect(onInject).toHaveBeenCalledTimes(1)
    expect(onInject).toHaveBeenCalledWith([a, c])
  })

  it('cancel and the header close button both call onSkip', () => {
    const onSkip = vi.fn()
    render(<KnowledgePicker results={[result({ id: 'a' })]} query="q" loading={false} onInject={noop} onSkip={onSkip} />)

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss knowledge results' }))
    expect(onSkip).toHaveBeenCalledTimes(2)
  })

  it('shows each result source when it has one and per-result token counts', () => {
    render(
      <KnowledgePicker
        results={[result({ id: 'a', source: 'wiki/runbook.md' }), result({ id: 'b', source: null })]}
        query="q"
        loading={false}
        onInject={noop}
        onSkip={noop}
      />,
    )
    expect(screen.getByText('wiki/runbook.md')).toBeInTheDocument()
    expect(screen.getAllByText(/^\d+ tokens$/)).toHaveLength(2)
  })

  it('marks vector and graph matches with the accent icon and keyword matches with the muted one', () => {
    render(
      <KnowledgePicker
        results={[
          result({ id: 'v', match_type: 'vector', title: 'Vec' }),
          result({ id: 'g', match_type: 'graph', title: 'Gra' }),
          result({ id: 'k', match_type: 'keyword', title: 'Key' }),
        ]}
        query="q"
        loading={false}
        onInject={noop}
        onSkip={noop}
      />,
    )
    const iconOf = (matchType: string) =>
      screen.getByText(matchType).closest('span')!.querySelector('svg')!

    expect(iconOf('vector')).toHaveClass('text-accent')
    expect(iconOf('graph')).toHaveClass('text-accent')
    expect(iconOf('keyword')).toHaveClass('text-muted')
    expect(iconOf('keyword')).not.toHaveClass('text-accent')
  })
})
