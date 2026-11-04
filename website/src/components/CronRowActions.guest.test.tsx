// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import CronRowActions from './CronRowActions'
import type { CronJob } from '../types'

afterEach(cleanup)

const job = { id: 'j1', name: 'j', message: 'm', enabled: true, schedule: 'every 1h', last_status: 'ok', has_result: true } as CronJob

function open(locked: boolean) {
  render(
    <CronRowActions
      job={job} folders={[]} running={false} cancelling={false} locked={locked}
      onRun={vi.fn()} onCancelRun={vi.fn()} onOpenInChat={vi.fn()} onToggleEnabled={vi.fn()}
      onToggleStrict={vi.fn()} onMove={vi.fn()} onNewFolder={vi.fn()}
    />,
  )
  fireEvent.pointerDown(screen.getAllByRole('button')[0], { button: 0, ctrlKey: false })
}

describe('CronRowActions on an owner-only schedule seen by a guest', () => {
  it('disables running it and keeps pausing available', () => {
    open(true)
    const run = screen.getByText(/run now/i).closest('[role="menuitem"]')
    const pause = screen.getByText(/^pause$/i).closest('[role="menuitem"]')
    expect(run?.getAttribute('aria-disabled') ?? run?.getAttribute('data-disabled')).not.toBeNull()
    expect(pause?.getAttribute('data-disabled')).toBeNull()
  })

  it('leaves every action available when the schedule is not locked', () => {
    open(false)
    const run = screen.getByText(/run now/i).closest('[role="menuitem"]')
    expect(run?.getAttribute('data-disabled')).toBeNull()
  })
})
