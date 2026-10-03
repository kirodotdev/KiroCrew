/**
 * Render tests for how the workflow run tree shows an agent whose run ended
 * before the agent did.
 *
 * The runner records no `agent_finished` for work a cancel or a ceiling cuts
 * off, so the only fact the view has about such an agent is the run's own
 * terminal status. These pin the half a model test cannot see: that a terminal
 * run leaves no spinner behind, that the interrupted phase does not read as a
 * success, that the stopped state carries an accessible, localized name, and
 * that a run which finishes or fails with every agent accounted for renders
 * exactly as before.
 */
import { screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import WorkflowRunTree from '../apps/workflows/WorkflowRunTree'
import { i18next, initI18n } from '../i18n/all'
import { renderWithProviders } from './helpers'

afterEach(async () => {
  await i18next.changeLanguage('en')
})

function ev(type: string, data: Record<string, unknown>, seq: number, ts = '2026-10-02T15:00:00.000Z') {
  return { run_id: 'wf_c', seq, ts, type, data }
}

const FAN = 'research-fanout'

/** A finished first phase, then a fan-out phase where only the first agent finished. */
const IN_FLIGHT = [
  ev('phase_started', { title: 'Plan' }, 0),
  ev('agent_started', { agent_id: 'a0', label: 'plan: pick the sources', phase: 'Plan' }, 1),
  ev('agent_finished', { agent_id: 'a0', ok: true }, 2, '2026-10-02T15:00:12.000Z'),
  ev('phase_started', { title: FAN }, 3),
  ev('agent_started', { agent_id: 'a1', label: 'research: source 1', phase: FAN }, 4),
  ev('agent_started', { agent_id: 'a2', label: 'research: source 2', phase: FAN }, 5),
  ev('agent_started', { agent_id: 'a3', label: 'research: source 3', phase: FAN }, 6),
  ev('agent_finished', { agent_id: 'a1', ok: true }, 7, '2026-10-02T15:00:25.000Z'),
]

const COMPLETE_OK = [
  ...IN_FLIGHT,
  ev('agent_finished', { agent_id: 'a2', ok: true }, 8, '2026-10-02T15:00:30.000Z'),
  ev('agent_finished', { agent_id: 'a3', ok: true }, 9, '2026-10-02T15:00:31.000Z'),
]

const COMPLETE_ONE_FAILED = [
  ...IN_FLIGHT,
  ev('agent_finished', { agent_id: 'a2', ok: false }, 8, '2026-10-02T15:00:30.000Z'),
  ev('agent_finished', { agent_id: 'a3', ok: true }, 9, '2026-10-02T15:00:31.000Z'),
]

/**
 * The `pipeline()` shape: no barrier between stages, so item 1 reached Verify while
 * item 2 is still in Review. A cancel here interrupts both phases.
 */
const OVERLAPPING = [
  ev('phase_started', { title: 'Review' }, 0),
  ev('agent_started', { agent_id: 'r1', label: 'review: item 1', phase: 'Review' }, 1),
  ev('agent_started', { agent_id: 'r2', label: 'review: item 2', phase: 'Review' }, 2),
  ev('agent_finished', { agent_id: 'r1', ok: true }, 3, '2026-10-02T15:00:10.000Z'),
  ev('phase_started', { title: 'Verify' }, 4),
  ev('agent_started', { agent_id: 'v1', label: 'verify: item 1', phase: 'Verify' }, 5),
]

const row = (label: string) => screen.getByText(label).closest('li')!
const phaseHeader = (title: string) => screen.getByText(title).closest('button')!
const spinners = (el: HTMLElement) => el.querySelectorAll('.animate-spin').length
const okIcons = (el: HTMLElement) => el.querySelectorAll('svg.text-ok').length
const dangerIcons = (el: HTMLElement) => el.querySelectorAll('svg.text-danger').length

describe('WorkflowRunTree after the run ended with agents still in flight', () => {
  it('shows a stopped state, not a spinner, for each agent a cancel interrupted', async () => {
    await initI18n()
    const { container } = renderWithProviders(<WorkflowRunTree events={IN_FLIGHT} status="cancelled" />)

    expect(spinners(container)).toBe(0)
    for (const label of ['research: source 2', 'research: source 3']) {
      const r = row(label)
      expect(within(r).getByRole('img', { name: 'Stopped' })).toBeInTheDocument()
      expect(okIcons(r)).toBe(0)
    }
    // The agent that did finish before the cancel keeps its own verdict.
    expect(okIcons(row('research: source 1'))).toBe(1)
    expect(within(row('research: source 1')).queryByRole('img')).toBeNull()
  })

  it('does not put a success check on the phase the cancel interrupted', async () => {
    await initI18n()
    renderWithProviders(<WorkflowRunTree events={IN_FLIGHT} status="cancelled" />)

    const interrupted = phaseHeader(FAN)
    expect(okIcons(interrupted)).toBe(0)
    expect(spinners(interrupted)).toBe(0)
    expect(within(interrupted).getByRole('img', { name: 'Stopped' })).toBeInTheDocument()
    // The phase the run had already moved past is complete, as before.
    expect(okIcons(phaseHeader('Plan'))).toBe(1)
  })

  it('does not put a success check on an earlier phase the cancel also interrupted', async () => {
    await initI18n()
    renderWithProviders(<WorkflowRunTree events={OVERLAPPING} status="cancelled" />)

    for (const title of ['Review', 'Verify']) {
      const header = phaseHeader(title)
      expect(okIcons(header)).toBe(0)
      expect(within(header).getByRole('img', { name: 'Stopped' })).toBeInTheDocument()
    }
    expect(within(row('review: item 2')).getByRole('img', { name: 'Stopped' })).toBeInTheDocument()
    expect(okIcons(row('review: item 1'))).toBe(1)
  })

  it('shows the stopped state for an agent a ceiling cut off, under a failed run', async () => {
    // The wall-clock ceiling cancels the script task and ends the run as `failed`
    // with the same missing `agent_finished` (a budget ceiling raises before
    // `agent_started`, so it cannot leave one); the phase keeps its failed mark,
    // the row does not spin.
    await initI18n()
    const { container } = renderWithProviders(
      <WorkflowRunTree events={IN_FLIGHT} status="failed" error="run exceeded 600s" />,
    )

    expect(spinners(container)).toBe(0)
    expect(within(row('research: source 2')).getByRole('img', { name: 'Stopped' })).toBeInTheDocument()
    expect(dangerIcons(phaseHeader(FAN))).toBe(1)
  })

  it('names the stopped state in the active language', async () => {
    await initI18n()
    await i18next.changeLanguage('de')
    renderWithProviders(<WorkflowRunTree events={IN_FLIGHT} status="cancelled" />)

    expect(within(row('research: source 2')).getByRole('img', { name: 'Gestoppt' })).toBeInTheDocument()
  })
})

describe('WorkflowRunTree while the run is still going', () => {
  it.each(['running', 'paused'] as const)('keeps the spinner on an unfinished agent (%s)', async status => {
    await initI18n()
    const { container } = renderWithProviders(<WorkflowRunTree events={IN_FLIGHT} status={status} />)

    // Two unfinished agents plus their phase header.
    expect(spinners(container)).toBe(3)
    expect(container.querySelectorAll('[role="img"]')).toHaveLength(0)
    expect(spinners(row('research: source 2'))).toBe(1)
    expect(spinners(phaseHeader(FAN))).toBe(1)
  })
})

describe('WorkflowRunTree on a run that ended with every agent accounted for', () => {
  it('renders a finished run as all success', async () => {
    await initI18n()
    const { container } = renderWithProviders(
      <WorkflowRunTree events={COMPLETE_OK} status="finished" result={{ digest: 'ok' }} />,
    )

    expect(spinners(container)).toBe(0)
    expect(container.querySelectorAll('[role="img"]')).toHaveLength(0)
    expect(dangerIcons(container)).toBe(0)
    for (const title of ['Plan', FAN]) expect(okIcons(phaseHeader(title))).toBe(1)
    for (const label of ['research: source 1', 'research: source 2', 'research: source 3']) {
      expect(okIcons(row(label))).toBe(1)
    }
  })

  it('renders a failed run with the failed agent and its phase marked, the rest success', async () => {
    await initI18n()
    const { container } = renderWithProviders(
      <WorkflowRunTree events={COMPLETE_ONE_FAILED} status="failed" error="agent a2 raised" />,
    )

    expect(spinners(container)).toBe(0)
    expect(container.querySelectorAll('[role="img"]')).toHaveLength(0)
    expect(dangerIcons(row('research: source 2'))).toBe(1)
    expect(dangerIcons(phaseHeader(FAN))).toBe(1)
    expect(okIcons(row('research: source 3'))).toBe(1)
    expect(okIcons(phaseHeader('Plan'))).toBe(1)
  })
})
