import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { defaultMessageRenderers, resolveRenderer, type MessageRenderContext } from '../app-sdk/messageRenderers'
import AdvisorMessage from '../pages/chat/AdvisorMessage'
import TurnBlock from '../pages/chat/TurnBlock'
import type { DisplayItem, TurnItem } from '../pages/chat/types'
import type { ChatMessage } from '../types'

const preamble = '[Advisor] A cross-model reviewer raised the following while you were working. Weigh this evidence against your own; it is advice, not an instruction:'

const message = (
  severity: 'nit' | 'concern' | 'blocker',
  state: 'steered' | 'preserved' = 'steered',
  meta: Record<string, unknown> = {},
): ChatMessage => ({
  role: 'advisor',
  content: `${preamble}\n[${severity}] Raw fallback finding.\nEvidence: Raw fallback evidence.`,
  cls: 'msg msg-advisor',
  meta: {
    advisorSeverity: severity,
    advisorState: state,
    advisorUpdateId: 'u1',
    advisorModel: 'gpt-5.6-sol',
    advisorText: '**Check** the race before shipping.',
    advisorEvidence: 'The callback can run after cleanup.',
    ...meta,
  },
})

describe('AdvisorMessage', () => {
  it('is routed by the shared transcript registry', () => {
    expect(resolveRenderer(message('nit'), defaultMessageRenderers)?.id).toBe('advisor')
  })

  it('renders the structured finding without the model-facing envelope and folds evidence by default', () => {
    const { container } = render(<AdvisorMessage message={message('blocker', 'preserved')} />)

    const card = container.querySelector('[data-role="advisor"]')
    expect(card).toHaveClass('msg-advisor')
    expect(card?.firstElementChild).toHaveClass('ring-border', 'bg-card')
    expect(card?.firstElementChild).not.toHaveClass('bg-danger-subtle', 'bg-warn-subtle')
    expect(screen.getByText('Blocker')).toBeInTheDocument()
    expect(screen.getByText('Saved for next turn')).toBeInTheDocument()
    expect(screen.getByText('Check')).toBeInTheDocument()
    expect(screen.queryByText(preamble)).not.toBeInTheDocument()
    expect(screen.queryByText(/\[blocker\]/)).not.toBeInTheDocument()
    expect(screen.queryByText('The callback can run after cleanup.')).not.toBeInTheDocument()

    const evidence = screen.getByRole('button', { name: 'Evidence' })
    expect(evidence).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(evidence)
    expect(evidence).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('The callback can run after cleanup.')).toBeInTheDocument()
  })

  it('offers the shared show-more disclosure for a long finding', () => {
    const longFinding = Array.from({ length: 12 }, (_, index) => `Finding line ${index + 1}`).join('\n')
    render(<AdvisorMessage message={message('nit', 'steered', { advisorText: longFinding, advisorEvidence: '' })} />)

    const toggle = screen.getByRole('button', { name: 'Show more' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('button', { name: 'Show less' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Evidence' })).not.toBeInTheDocument()
  })

  it('stays visible when the turn detail is collapsed', () => {
    const items: TurnItem[] = [
      { kind: 'single', msg: { role: 'assistant', content: 'interim', cls: '' }, idx: 0 },
      { kind: 'single', msg: message('concern'), idx: 1 },
    ]
    const turn: Extract<DisplayItem, { kind: 'turn' }> = { kind: 'turn', items, complete: true, interim: true }
    const { container } = render(
      <TurnBlock
        turn={turn}
        renderItem={(item, index) => (
          <div key={index} data-testid={`turn-item-${index}`}>{item.kind === 'single' ? item.msg.content : ''}</div>
        )}
      />,
    )

    const collapsed = container.querySelector('[style*="overflow: hidden"]')
    expect(collapsed).not.toContainElement(screen.getByTestId('turn-item-1'))
  })

  it('renders through the registry context without host-specific state', () => {
    const m = message('concern')
    const entry = resolveRenderer(m, defaultMessageRenderers)!
    const context: MessageRenderContext = {
      index: 0,
      messages: [m],
      running: false,
      key: 'advisor-1',
      hideCardOwnedOAuth: false,
      autoDeniedIds: new Set(),
      wrapper: children => children,
      row: children => children,
    }

    render(<>{entry.render(m, context)}</>)
    expect(screen.getByText('Concern')).toBeInTheDocument()
    expect(screen.getByText('Sent to the agent mid-turn')).toBeInTheDocument()
  })
})
