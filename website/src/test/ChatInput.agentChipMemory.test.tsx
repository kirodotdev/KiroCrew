/**
 * Composer agent chip — which of a same-name pair the session runs (#19472).
 *
 * A crewmate and a custom agent of one name read alike on the chip, so the chip
 * names the memory the session runs on, in its visible text and in its spoken
 * name (the aria-label overrides the text for assistive tech). Unset: unchanged.
 */
import { describe, it, expect, vi } from 'vitest'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import '../i18n/all'

vi.mock('../api/client', () => ({ api: {} }))

const props = (over: Record<string, unknown> = {}) => ({
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
  connected: true,
  onAgentClick: vi.fn(),
  onProjectClick: vi.fn(),
  ...over,
})

function agentChip(): HTMLButtonElement {
  const bot = document.querySelector('button > svg.lucide-bot') as SVGElement | null
  const btn = bot?.closest('button') as HTMLButtonElement | null
  if (!btn) throw new Error('no agent chip rendered')
  return btn
}

describe('ChatInput agent chip memory mark', () => {
  it('names own memory for the crewmate of a pair', () => {
    renderWithProviders(<ChatInput {...props({ agentName: 'reviewer', agentMemory: 'own' })} />)
    expect(agentChip().querySelector('span')?.textContent).toBe('reviewer · own memory')
    expect(agentChip()).toHaveAttribute('aria-label', 'Agent: reviewer · own memory')
  })

  it('names shared memory for the custom agent, after the inherited-default marker', () => {
    renderWithProviders(
      <ChatInput {...props({ agentName: 'reviewer', agentLabel: 'reviewer · default', agentIsInheritedDefault: true, agentMemory: 'shared' })} />,
    )
    expect(agentChip().querySelector('span')?.textContent).toBe('reviewer · default · shared memory')
    expect(agentChip().getAttribute('aria-label')).toContain('reviewer · shared memory')
  })

  it('leaves the chip unchanged without a mark', () => {
    renderWithProviders(<ChatInput {...props({ agentName: 'reviewer', agentMemory: null })} />)
    expect(agentChip().querySelector('span')?.textContent).toBe('reviewer')
    expect(agentChip()).toHaveAttribute('aria-label', 'Agent: reviewer')
  })
})
