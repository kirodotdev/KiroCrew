import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { DevFleetSyncStepper, DevFleetProvStepper } from './DevFleetPageRow'
import type { ProvRun, SyncRun, Worktree } from './devFleetPageModel'
import { i18nT } from '../../i18n/t'

/* The Log toggles on the Pull+Build and provision steppers open and close a log
 * panel, so they say whether it is open (aria-expanded) in every state the
 * stepper renders, not only through their visible "Log ▴/▾" text. */

const syncRun = (status: SyncRun['status']): SyncRun => ({
  rid: 'r1', status, lines: ['step'], startedAt: 0, last: 'boom',
})

const syncToggle = () => screen.getByRole('button', { name: i18nT('pages.devFleetPage.toggle_log') })
const provToggle = () => screen.getByRole('button', { name: i18nT('pages.devFleetPage.toggle_provision_log') })

describe('DevFleetSyncStepper Log toggle', () => {
  it.each(['running', 'done', 'error'] as const)('reports the panel state while %s', (status) => {
    const { rerender } = render(
      <DevFleetSyncStepper syncRun={syncRun(status)} syncLogOpen={false} setSyncLogOpen={vi.fn()} dismissSync={vi.fn()} />,
    )
    expect(syncToggle()).toHaveAttribute('aria-expanded', 'false')
    rerender(
      <DevFleetSyncStepper syncRun={syncRun(status)} syncLogOpen setSyncLogOpen={vi.fn()} dismissSync={vi.fn()} />,
    )
    expect(syncToggle()).toHaveAttribute('aria-expanded', 'true')
  })
})

describe('DevFleetProvStepper Log toggle', () => {
  const w = { name: 'wt-a' } as Worktree
  const runs: Record<string, ProvRun> = {
    running: { status: 'running', lines: ['x'], startedAt: 0 },
    failed: { status: 'failed', failed: true, lines: ['x'], startedAt: 0, exit: 1 },
  }

  it.each(Object.keys(runs))('reports the panel state while %s', (key) => {
    const props = { w, prov: { [w.name]: runs[key] }, dismissProv: vi.fn(async () => {}), toggleProvLog: vi.fn() }
    const { rerender } = render(<DevFleetProvStepper {...props} provLogOpen={{}} />)
    expect(provToggle()).toHaveAttribute('aria-expanded', 'false')
    rerender(<DevFleetProvStepper {...props} provLogOpen={{ [w.name]: true }} />)
    expect(provToggle()).toHaveAttribute('aria-expanded', 'true')
  })
})
