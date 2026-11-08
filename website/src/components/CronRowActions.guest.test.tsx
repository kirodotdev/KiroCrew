// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import CronRowActions from './CronRowActions'
import type { CronJob } from '../types'

// On a touch device the menu's submenus render as inline toggles (ui/phoneSubmenu).
const device = vi.hoisted(() => ({ touch: false }))
vi.mock('../hooks/useIsTouchDevice', () => ({ useIsTouchDevice: () => device.touch }))

afterEach(() => { cleanup(); device.touch = false })

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
  it('disables running and pausing it, since a guest could not resume it', () => {
    open(true)
    const run = screen.getByText(/run now/i).closest('[role="menuitem"]')
    const pause = screen.getByText(/^pause$/i).closest('[role="menuitem"]')
    expect(run?.getAttribute('aria-disabled') ?? run?.getAttribute('data-disabled')).not.toBeNull()
    expect(pause?.getAttribute('data-disabled')).not.toBeNull()
    expect(pause?.getAttribute('title')).toBe('Owner only')
  })

  it('keeps pausing available on a schedule the guest may run', () => {
    open(false)
    const pause = screen.getByText(/^pause$/i).closest('[role="menuitem"]')
    expect(pause?.getAttribute('data-disabled')).toBeNull()
  })

  it.each([false, true])('disables moving it to a folder on every device (touch: %s)', touch => {
    device.touch = touch
    open(true)
    const move = screen.getByText(/move to folder/i).closest('[role="menuitem"]')
    expect(move?.getAttribute('data-disabled')).not.toBeNull()
    expect(move?.getAttribute('title')).toBe('Owner only')
    fireEvent.click(screen.getByText(/move to folder/i))
    expect(screen.queryByText(/ungrouped/i)).toBeNull()
  })

  it('still opens the folder submenu on touch when the schedule is not locked', () => {
    device.touch = true
    open(false)
    const toggle = screen.getByText(/move to folder/i).closest('[role="button"]')
    expect(toggle).not.toBeNull()
    fireEvent.click(toggle!)
    expect(screen.getByText(/ungrouped/i)).toBeTruthy()
  })

  it('leaves every action available when the schedule is not locked', () => {
    open(false)
    const run = screen.getByText(/run now/i).closest('[role="menuitem"]')
    expect(run?.getAttribute('data-disabled')).toBeNull()
  })
})
