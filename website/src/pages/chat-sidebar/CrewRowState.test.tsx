import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { CREW_STATE_STYLE, CrewStateWord, type CrewStateKind } from './CrewRowState'
import { crewRowOwnState } from './SessionRow'
import { ULTRA_FLOW_SECONDS } from '../../components/crew/CrewModeMarks'

const fontWeight = (tone: string) => tone.split(' ').find(c => /^font-/.test(c)) ?? 'font-normal'

describe('Ultra Mode row state word', () => {
  it('Running and Waiting differ by shape and weight, not colour alone', () => {
    const run = CREW_STATE_STYLE.running
    const wait = CREW_STATE_STYLE.waiting
    expect(run.mark).toBe('filled')
    expect(wait.mark).toBe('ring')
    expect(fontWeight(run.tone)).toBe('font-semibold')
    expect(fontWeight(wait.tone)).toBe('font-normal')
    expect(run.tone).not.toBe(wait.tone)
  })

  it('Running draws a solid green dot before the word', () => {
    render(<CrewStateWord kind="running" title="Thinking" />)
    const state = screen.getByTestId('crew-root-state')
    expect(state).toHaveTextContent(/^Running$/)
    expect(state).toHaveAttribute('data-state', 'running')
    expect(state).toHaveAttribute('title', 'Thinking')
    expect(state).toHaveClass('font-semibold', 'text-[12px]')
    const mark = screen.getByTestId('crew-root-state-mark')
    expect(mark).toHaveAttribute('data-mark', 'filled')
    expect(mark).toHaveAttribute('fill', 'currentColor')
    expect(mark).toHaveAttribute('aria-hidden', 'true')
    expect(mark).toHaveClass('text-[var(--ok)]')
    // Same glyph size as the session count's bubble.
    expect(mark).toHaveAttribute('width', '11')
    // The mark never moves.
    expect(mark.getAttribute('class')).not.toMatch(/animate-/)
  })

  it('Waiting draws a hollow ring and the muted word', () => {
    render(<CrewStateWord kind="waiting" />)
    const state = screen.getByTestId('crew-root-state')
    expect(state).toHaveTextContent(/^Waiting$/)
    expect(state).toHaveClass('text-muted')
    expect(state).not.toHaveClass('font-semibold')
    expect(state).not.toHaveAttribute('title')
    const mark = screen.getByTestId('crew-root-state-mark')
    expect(mark).toHaveAttribute('data-mark', 'ring')
    expect(mark).toHaveAttribute('fill', 'none')
    expect(mark).toHaveClass('crew-tone-muted')
  })

  it.each<[CrewStateKind, string]>([
    ['needs_you', 'Needs you'],
    ['paused', 'Paused'],
    ['stalled', 'Paused'],
    ['done', 'Done'],
  ])('%s keeps its word and draws no new mark', (kind, word) => {
    render(<CrewStateWord kind={kind} />)
    expect(screen.getByTestId('crew-root-state')).toHaveTextContent(new RegExp(`^${word}$`))
    expect(screen.queryByTestId('crew-root-state-mark')).toBeNull()
  })

  it('maps each row-state branch to its kind', () => {
    expect(crewRowOwnState('running', '')?.kind).toBe('running')
    expect(crewRowOwnState('subagents', '')?.kind).toBe('running')
    expect(crewRowOwnState('subagents_waiting', '')?.kind).toBe('waiting')
    expect(crewRowOwnState('structured_monitor_passive', '')?.kind).toBe('waiting')
    expect(crewRowOwnState('needs_input', '')?.kind).toBe('needs_you')
    expect(crewRowOwnState('interrupted', '')?.kind).toBe('paused')
    expect(crewRowOwnState('goal_loop', '', true)?.kind).toBe('stalled')
    expect(crewRowOwnState('unread', '')).toBeNull()
    expect(crewRowOwnState(undefined, '')).toBeNull()
  })

  it('the Ultra Mode tag flows fast enough to read as moving', () => {
    expect(ULTRA_FLOW_SECONDS).toBeLessThanOrEqual(2.5)
    expect(ULTRA_FLOW_SECONDS).toBeGreaterThan(0)
  })
})
