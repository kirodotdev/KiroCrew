import { describe, it, expect, vi } from 'vitest'
import { screen, waitFor, within, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { readIndexCss } from './indexCssSource'
import SchedulePage from '../pages/SchedulePage'
import type { CronJob } from '../types'

/**
 * A row tint (hover, or a selected Schedule row) sits at the luminance of
 * `--border`, so a stock Btn's edge vanishes on it. The row re-points that token
 * for its edged buttons. Asserted on source text and classes: jsdom loads no
 * stylesheet and has no `:hover`.
 */

vi.mock('../api/client', () => ({
  api: {
    crons: vi.fn().mockResolvedValue({
      jobs: [
        { id: 'a', name: 'Alpha job', schedule: 'every 1d', message: 'm', enabled: true },
        { id: 'b', name: 'Beta job', schedule: 'every 1d', message: 'm', enabled: true },
        { id: 'c', name: 'Gamma job', schedule: 'every 1d', message: 'm', enabled: true },
      ] as CronJob[],
    }),
    cronFolders: vi.fn().mockResolvedValue([]),
    models: vi.fn().mockResolvedValue([]),
    updateCron: vi.fn().mockResolvedValue({}),
    cronHistoryAll: vi.fn().mockResolvedValue({ runs: [] }),
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    agentCatalog: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    defaultAgent: vi.fn().mockResolvedValue({ default_agent: '' }),
  },
}))

const EDGE = '[&_button.border-border]:[--border:var(--border-strong)]'
const row = (name: string) => screen.getByText(name).closest('tr') as HTMLElement

describe('edged buttons on a tinted table row', () => {
  it('re-points --border for the edged buttons of a hovered body row', () => {
    const css = readIndexCss()
    expect(css).toContain(
      '@media (hover:hover){\n  tbody tr:hover{--row-edge:var(--border-strong)}\n' +
        '  tbody tr:hover button.border-border{--border:var(--row-edge);--border-strong:var(--border-hover)}\n}',
    )
  })

  it('re-points it at rest on a selected or checked Schedule row only', async () => {
    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Gamma job')).toBeInTheDocument())
    const [checked, opened, plain] = ['Alpha job', 'Beta job', 'Gamma job'].map(row)
    fireEvent.click(within(checked).getByRole('checkbox'))
    fireEvent.click(within(opened).getByText('Beta job'))
    await waitFor(() => expect(opened).toHaveClass('bg-accent-subtle'))
    expect(checked).toHaveClass('bg-accent-subtle/60', EDGE)
    expect(opened).toHaveClass(EDGE)
    expect(plain).not.toHaveClass(EDGE)
  })
})
