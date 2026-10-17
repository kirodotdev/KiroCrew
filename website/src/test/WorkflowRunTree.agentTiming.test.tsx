/**
 * Render tests for the per-agent time in the workflow run tree (#1652).
 *
 * The span itself is folded in ./runModel and unit-tested in WorkflowsPage.test;
 * what these cover is the half a model test cannot see — that the value reaches
 * the row, that a still-running agent shows none, and that the reading is
 * localized rather than hardcoded English.
 */
import { screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import WorkflowRunTree from '../apps/workflows/WorkflowRunTree'
import { i18next, initI18n } from '../i18n/all'
import { renderWithProviders } from './helpers'

afterEach(async () => {
  await i18next.changeLanguage('en')
})

/** One event with a real `ts`, so a span is measurable. */
function at(type: string, ts: string, data: Record<string, unknown> = {}, seq = 0) {
  return { run_id: 'wf_t', seq, ts, type, data }
}

const FINISHED = [
  at('phase_started', '2026-09-18T10:00:00.000Z', { title: 'Review' }, 0),
  at('agent_started', '2026-09-18T10:00:00.000Z', { agent_id: 'a0', label: 'review:bugs', phase: 'Review' }, 1),
  at('agent_finished', '2026-09-18T10:00:04.200Z', { agent_id: 'a0', ok: true }, 2),
]

describe('WorkflowRunTree per-agent time', () => {
  it('shows the time a finished agent took', async () => {
    await initI18n()
    renderWithProviders(<WorkflowRunTree events={FINISHED} status="finished" />)

    const row = screen.getByText('review:bugs').closest('li')
    expect(row).not.toBeNull()
    expect(within(row!).getByText('4.2s')).toBeInTheDocument()
  })

  it('shows no time while the agent is still running', async () => {
    await initI18n()
    renderWithProviders(
      <WorkflowRunTree
        events={[
          at('phase_started', '2026-09-18T10:00:00.000Z', { title: 'Review' }, 0),
          at('agent_started', '2026-09-18T10:00:00.000Z', { agent_id: 'a0', label: 'review:bugs', phase: 'Review' }, 1),
        ]}
        status="running"
      />,
    )

    const row = screen.getByText('review:bugs').closest('li')
    expect(row).not.toBeNull()
    // Nothing time-shaped at all: a running agent already says so with its
    // spinner, and a 0s would read as a measurement.
    expect(row!.textContent).not.toMatch(/\d+(\.\d+)?\s*[sm]\b/)
  })

  it('renders the reading in the active language', async () => {
    // A hardcoded English unit would survive an en-only assertion, so this is
    // what pins the value through the locale seam.
    await initI18n()
    await i18next.changeLanguage('zh-CN')
    renderWithProviders(<WorkflowRunTree events={FINISHED} status="finished" />)

    const row = screen.getByText('review:bugs').closest('li')
    const expected = new Intl.NumberFormat('zh-CN', {
      style: 'unit', unit: 'second', unitDisplay: 'narrow',
      minimumFractionDigits: 1, maximumFractionDigits: 1,
    }).format(4.2)
    expect(within(row!).getByText(expected)).toBeInTheDocument()
  })
})
