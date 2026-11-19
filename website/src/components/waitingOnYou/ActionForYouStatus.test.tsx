/** The "Action for you" status on the grid pane and the sidebar row, and the lazy
 *  composer stack: each shows the newest ask's title, gives way to a live question
 *  or approval, and renders the same sentence before and after its chunk loads. */
import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderWithProviders } from '../../test/helpers'
import { i18nT } from '../../i18n/t'
import type { WaitingOnYouAsk } from '../../types'
import { ActionForYouPill as StyledPill, ActionForYouRowLine as StyledRowLine } from './ActionForYouStatus'
import { ActionForYouPill, ActionForYouRowLine } from './LazyActionForYouStatus'
import LazyWaitingOnYouStack from './LazyWaitingOnYouStack'

const older: WaitingOnYouAsk = { key: 'older', ask_id: 'wy-0000000000000001', title: 'Pick a reviewer', detail: '', options: [], ts: 1 }
const newest: WaitingOnYouAsk = { key: 'newest', ask_id: 'wy-0000000000000002', title: 'Post the override', detail: '', options: [], ts: 2 }
const slot = (over: Record<string, unknown> = {}) => ({ waiting_on_you: [older, newest], pending_approval: false, needs_input: false, ...over })
const label = i18nT('components.waitingOnYou.status_line', { title: newest.title })

// Chain: lazyRetry -> React.lazy -> the dynamic import of the styled module -> the
// Suspense commit. Under a loaded coverage run that can pass the 1000 ms default,
// so waits on a lazily loaded view allow 5000 ms.
const LAZY_CHAIN = { timeout: 5000 }

describe('the styled status', () => {
  it('names the newest ask on the grid pane pill', () => {
    renderWithProviders(<StyledPill slot={slot()} />)
    expect(screen.getByTestId('action-for-you-status')).toHaveTextContent(label)
  })

  it('shows no pill when nothing is pinned or something more urgent is live', () => {
    const { container, rerender } = renderWithProviders(<StyledPill slot={slot({ waiting_on_you: [] })} />)
    expect(container).toBeEmptyDOMElement()
    rerender(<StyledPill slot={slot({ needs_input: true })} />)
    expect(container).toBeEmptyDOMElement()
    rerender(<StyledPill slot={null} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('names the newest ask on the sidebar row line', () => {
    renderWithProviders(<StyledRowLine slot={slot()} lineCls="row-line" iconPx={11} />)
    const line = screen.getByTestId('row-action-for-you')
    expect(line).toHaveTextContent(label)
    expect(line).toHaveClass('row-line')
  })
})

describe('the lazy status', () => {
  it('shows the sentence, then the styled pill once its chunk loads', async () => {
    renderWithProviders(<ActionForYouPill slot={slot()} />)
    expect(screen.getByText(label)).toBeInTheDocument()
    expect(await screen.findByTestId('action-for-you-status', {}, LAZY_CHAIN)).toHaveTextContent(label)
  })

  it('shows the sentence, then the styled row line once its chunk loads', async () => {
    renderWithProviders(<ActionForYouRowLine slot={slot()} lineCls="row-line" iconPx={11} />)
    expect(screen.getByText(label)).toBeInTheDocument()
    expect(await screen.findByTestId('row-action-for-you', {}, LAZY_CHAIN)).toHaveTextContent(label)
  })

  it('renders nothing while a live approval outranks the ask', () => {
    const { container } = renderWithProviders(
      <>
        <ActionForYouPill slot={slot({ pending_approval: true })} />
        <ActionForYouRowLine slot={slot({ pending_approval: true })} lineCls="row-line" iconPx={11} />
      </>,
    )
    expect(container).toBeEmptyDOMElement()
  })
})

describe('the lazy composer stack', () => {
  it('loads the stack and shows the newest ask', async () => {
    // Loaded first, so the lazy import resolves from the module cache.
    await import('./WaitingOnYouStack')
    renderWithProviders(<LazyWaitingOnYouStack slotKey="chat-1" asks={[older, newest]} agent="kirocrew" />)
    const card = await screen.findByTestId('waiting-on-you-card', {}, LAZY_CHAIN)
    expect(card).toHaveTextContent(newest.title)
  })
})
