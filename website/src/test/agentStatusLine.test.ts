import { describe, it, expect } from 'vitest'
import { agentStatusLine, type SceneAgent } from '../hooks/useSceneInteraction'

const base = { id: 'x', name: 'x', x: 0, y: 0 }

function agent(kind: SceneAgent['kind'], running: boolean, detail: string): SceneAgent {
  return { ...base, kind, running, detail }
}

describe('agentStatusLine', () => {
  it('shows Working with detail for a running chat slot', () => {
    expect(agentStatusLine(agent('slot', true, '12 msgs'))).toBe('Working · 12 msgs')
  })

  it('shows Idle with detail for an idle chat slot', () => {
    expect(agentStatusLine(agent('slot', false, '3 msgs'))).toBe('Idle · 3 msgs')
  })

  it('omits the detail separator when detail is empty', () => {
    expect(agentStatusLine(agent('slot', false, ''))).toBe('Idle')
  })

  it('shows the schedule for an idle cron', () => {
    expect(agentStatusLine(agent('cron', false, 'every 15m'))).toBe('Cron · every 15m')
  })

  it('shows running for an active cron', () => {
    expect(agentStatusLine(agent('cron', true, 'every 15m'))).toBe('Cron · running')
  })

  it('shows subagent state for spawns', () => {
    expect(agentStatusLine(agent('spawn', true, 'running'))).toBe('Subagent · running')
  })
})
