/**
 * A group that holds only reasoning rows is not a tool-call group.
 *
 * The app-sdk ChatMessageList groups reasoning rows and permissions, so the rows
 * its group counts are reasoning steps. Its pill read "N tool calls" with a wrench,
 * and opening it showed "Thought process" rows. A reasoning-only group now reads
 * "Worked through N steps" (TurnBlock's words for a reasoning fold) with no wrench;
 * a tool group keeps the wrench and "N tool calls".
 */
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import CollapsibleToolGroup from '../pages/chat/CollapsibleToolGroup'

const headerOf = (c: HTMLElement) => c.querySelector<HTMLButtonElement>('button[aria-expanded]')!

describe('CollapsibleToolGroup — a reasoning-only group says what it holds', () => {
  it('labels a reasoning-only group as steps, with no wrench', () => {
    const { container } = render(
      <CollapsibleToolGroup count={3} reasoningOnly><div>row</div></CollapsibleToolGroup>,
    )
    const btn = headerOf(container)
    expect(btn.textContent).toContain('Worked through 3 steps')
    expect(btn.textContent).not.toMatch(/tool call/i)
    expect(btn.querySelector('svg.lucide-wrench')).toBeNull()
    expect(btn.getAttribute('aria-label')).toContain('Worked through 3 steps')
  })

  it('keeps the wrench and the tool-call count for a tool group', () => {
    const { container } = render(<CollapsibleToolGroup count={2}><div>row</div></CollapsibleToolGroup>)
    const btn = headerOf(container)
    expect(btn.textContent).toMatch(/2 tool calls/)
    expect(btn.querySelector('svg.lucide-wrench')).not.toBeNull()
  })

  it('keeps the approval state on a reasoning-only group with a pending permission', () => {
    const { container } = render(
      <CollapsibleToolGroup
        count={1}
        reasoningOnly
        hasPermission
        permissionMeta={{ tool_input: { command: 'rm -rf build' } }}
        onApprove={async () => undefined}
      >
        <div>row</div>
      </CollapsibleToolGroup>,
    )
    const btn = headerOf(container)
    expect(btn.textContent).not.toContain('Worked through')
    expect(btn.querySelector('.animate-ping')).not.toBeNull()
  })
})
