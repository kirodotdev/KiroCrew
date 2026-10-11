import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import AssistantMessage, { fmtTurnModel } from '../pages/chat/AssistantMessage'
import { setModelLabel, clearModelLabels } from '../lib/modelLabels'

describe('AssistantMessage footer model label', () => {
  beforeEach(() => {
    clearModelLabels()
    setModelLabel('qfmodel', 'Qwen3.8-Flash')
  })
  afterEach(() => {
    cleanup()
    clearModelLabels()
  })

  it('shows the display label for an id the model list named', () => {
    render(<AssistantMessage content="done" isStreaming={false} slotRunning={false} turnStats={{ elapsed_ms: 3_600, model: 'qfmodel' }} />)
    expect(screen.getByTestId('turn-model')).toHaveTextContent('Qwen3.8-Flash')
    expect(screen.getByTestId('turn-model')).not.toHaveTextContent('qfmodel')
  })

  it('keeps the raw id in the footer tooltip', () => {
    render(<AssistantMessage content="done" isStreaming={false} slotRunning={false} turnStats={{ elapsed_ms: 3_600, model: 'qfmodel' }} />)
    expect(screen.getByTestId('turn-stats').title).toContain('qfmodel')
  })

  it('shows the id for a model with no learned label', () => {
    render(<AssistantMessage content="done" isStreaming={false} slotRunning={false} turnStats={{ elapsed_ms: 3_600, model: 'gpt-5.6-luna' }} />)
    expect(screen.getByTestId('turn-model')).toHaveTextContent('gpt-5.6-luna')
  })

  it('shows the id again once the labels are cleared', () => {
    clearModelLabels()
    render(<AssistantMessage content="done" isStreaming={false} slotRunning={false} turnStats={{ elapsed_ms: 3_600, model: 'qfmodel' }} />)
    expect(screen.getByTestId('turn-model')).toHaveTextContent('qfmodel')
  })

  it('fmtTurnModel returns the label, else the id with routing prefixes trimmed', () => {
    expect(fmtTurnModel('qfmodel')).toBe('Qwen3.8-Flash')
    expect(fmtTurnModel('global.anthropic.claude-opus-4-8[1m]')).toBe('claude-opus-4-8[1m]')
  })
})
