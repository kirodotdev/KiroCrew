import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import CronRowActions from './CronRowActions'

/**
 * Crew-to-crew work migration (issue #7577) — the Schedule-tab entry point.
 *
 * The item is optional on purpose: a surface that cannot host the plan dialog
 * omits the handler and the item is absent. And it must be distinct from the
 * existing folder move, which stays on THIS crew — the two are different
 * operations that would otherwise read the same.
 */

const job = {
  id: 'j1', name: 'nightly', message: 'run backup', enabled: true,
  schedule: { kind: 'cron', cron_expr: '0 3 * * *' },
} as never

const base = {
  job,
  folders: [],
  running: false,
  cancelling: false,
  onRun: () => {},
  onCancelRun: () => {},
  onOpenInChat: () => {},
  onToggleEnabled: () => {},
  onToggleStrict: () => {},
  onMove: () => {},
  onNewFolder: () => undefined,
}

describe('CronRowActions — move to crew', () => {
  it('omits the item when no handler is given', async () => {
    render(<CronRowActions {...base} />)
    await userEvent.click(screen.getByRole('button'))
    expect(screen.queryByText(/move to crew/i)).not.toBeInTheDocument()
  })

  it('renders the item and fires the handler when given', async () => {
    const onMoveToCrew = vi.fn()
    render(<CronRowActions {...base} onMoveToCrew={onMoveToCrew} />)
    await userEvent.click(screen.getByRole('button'))
    const item = await screen.findByText(/move to crew/i)
    await userEvent.click(item)
    expect(onMoveToCrew).toHaveBeenCalledTimes(1)
  })

  it('keeps the crew move distinct from the folder move', async () => {
    const onMove = vi.fn()
    const onMoveToCrew = vi.fn()
    render(<CronRowActions {...base} onMove={onMove} onMoveToCrew={onMoveToCrew} />)
    await userEvent.click(screen.getByRole('button'))
    await userEvent.click(await screen.findByText(/move to crew/i))
    // moving to a crew must not be mistaken for a folder assignment
    expect(onMoveToCrew).toHaveBeenCalledTimes(1)
    expect(onMove).not.toHaveBeenCalled()
  })

  it('is reachable from the Schedule page, not just a prop nobody passes', async () => {
    // Reachability, not existence: SchedulePage must actually pass the handler,
    // otherwise the menu item is dead code. Asserted against the page source so
    // the wiring cannot silently regress.
    const src = await import('../pages/SchedulePage?raw').then(m => m.default as string)
    expect(src).toContain('onMoveToCrew={() => setMovingJobId(j.id)}')
    expect(src).toContain('planCronMove')
  })
})
