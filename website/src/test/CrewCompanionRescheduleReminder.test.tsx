// A reminder's schedule is set by hand from its More menu: the same reminder,
// a new repeat and time. An unchanged save writes nothing, Escape cancels, and a
// failed save keeps the editor open with an error.

import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor, act } from '@testing-library/react'

import RemindersSection from '../apps/crew-companion/RemindersSection'
import type { RemindersPayload } from '../apps/crew-companion/types'

// Native selects, so a test can pick an option with a change event.
vi.mock('../hooks/useIsTouchDevice', () => ({ useIsTouchDevice: () => true }))

beforeEach(() => { vi.setSystemTime(new Date('2026-11-04T10:32:00Z')) })
afterEach(() => { cleanup(); vi.useRealTimers() })

const rem: RemindersPayload = {
  reminders: [{ id: 'r1', text: 'draw', fireAt: '2026-11-04T11:32:00.000Z', recurrence: { everyMinutes: 60 } }],
  breakNudgesEnabled: true,
  sessionNotificationsEnabled: true,
  breakReminderMins: 45,
  language: 'English',
  present: true,
}

function openScheduler(onReschedule: ReturnType<typeof vi.fn>, payload = rem) {
  render(
    <RemindersSection rem={payload} remError={null} onAdd={vi.fn()} onSkip={vi.fn()} onRemove={vi.fn()}
      onEdit={vi.fn()} onReschedule={onReschedule} />,
  )
  fireEvent.keyDown(screen.getByRole('button', { name: 'More actions' }), { key: 'Enter' })
  fireEvent.click(screen.getByRole('menuitem', { name: 'Change schedule' }))
}

describe('changing a reminder’s schedule by hand', () => {
  it('saves every day at 07:30 on the same reminder, then closes', async () => {
    const onReschedule = vi.fn().mockResolvedValue(undefined)
    openScheduler(onReschedule)
    const repeat = screen.getByRole('combobox', { name: 'Repeat' })
    expect(repeat).toHaveValue('hourly')
    fireEvent.change(repeat, { target: { value: 'daily' } })
    fireEvent.change(screen.getByLabelText('Time'), { target: { value: '07:30' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Save' })) })
    expect(onReschedule).toHaveBeenCalledWith('r1', '2026-11-05T07:30:00.000Z', 1440)
    await waitFor(() => expect(screen.queryByRole('combobox', { name: 'Repeat' })).toBeNull())
    expect(screen.getByText('draw')).toBeInTheDocument()
  })

  it('keeps the reminder’s text in view while the form is open', () => {
    openScheduler(vi.fn())
    expect(screen.getByRole('combobox', { name: 'Repeat' }).closest('form')).toHaveTextContent('draw')
  })

  it('aligns an hourly reminder to :00', async () => {
    const onReschedule = vi.fn().mockResolvedValue(undefined)
    openScheduler(onReschedule)
    fireEvent.change(screen.getByLabelText('Minutes past the hour'), { target: { value: '0' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Save' })) })
    expect(onReschedule).toHaveBeenCalledWith('r1', '2026-11-04T11:00:00.000Z', 60)
  })

  it('writes nothing for an unchanged schedule, even one the form cannot show', async () => {
    const onReschedule = vi.fn().mockResolvedValue(undefined)
    openScheduler(onReschedule, { ...rem, reminders: [{ ...rem.reminders[0], recurrence: { everyMinutes: 90 } }] })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Save' })) })
    expect(onReschedule).not.toHaveBeenCalled()
    expect(screen.queryByRole('combobox', { name: 'Repeat' })).toBeNull()
  })

  it('writes nothing when only a field the repeat hides changed', async () => {
    const onReschedule = vi.fn().mockResolvedValue(undefined)
    openScheduler(onReschedule)
    const repeat = screen.getByRole('combobox', { name: 'Repeat' })
    fireEvent.change(repeat, { target: { value: 'weekly' } })
    fireEvent.change(screen.getByRole('combobox', { name: 'Day of the week' }), { target: { value: '1' } })
    fireEvent.change(repeat, { target: { value: 'hourly' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Save' })) })
    expect(onReschedule).not.toHaveBeenCalled()
  })

  it('cancels on Escape without saving', () => {
    const onReschedule = vi.fn()
    openScheduler(onReschedule)
    fireEvent.keyDown(screen.getByLabelText('Minutes past the hour'), { key: 'Escape' })
    expect(onReschedule).not.toHaveBeenCalled()
    expect(screen.queryByRole('combobox', { name: 'Repeat' })).toBeNull()
  })

  it('keeps an interval no preset matches when only the time changes', async () => {
    const onReschedule = vi.fn().mockResolvedValue(undefined)
    openScheduler(onReschedule, { ...rem, reminders: [{ ...rem.reminders[0], recurrence: { everyMinutes: 120 } }] })
    expect(screen.getByRole('combobox', { name: 'Repeat' })).toHaveValue('custom')
    fireEvent.change(screen.getByLabelText('Time'), { target: { value: '12:00' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Save' })) })
    expect(onReschedule).toHaveBeenCalledWith('r1', '2026-11-04T12:00:00.000Z', 120)
  })

  it('locks the form while a save is in flight, so its completion loses no draft', async () => {
    let finish: () => void = () => {}
    const onReschedule = vi.fn(() => new Promise<void>((res) => { finish = res }))
    openScheduler(onReschedule)
    fireEvent.change(screen.getByLabelText('Minutes past the hour'), { target: { value: '0' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Save' })) })
    expect(screen.getByLabelText('Minutes past the hour')).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled()
    fireEvent.keyDown(screen.getByLabelText('Minutes past the hour'), { key: 'Escape' })
    expect(screen.getByRole('combobox', { name: 'Repeat' })).toBeInTheDocument()
    await act(async () => { finish() })
    await waitFor(() => expect(screen.queryByRole('combobox', { name: 'Repeat' })).toBeNull())
  })

  it('saves a schedule put back to the opened one after a failed save', async () => {
    const onReschedule = vi.fn().mockRejectedValueOnce(new Error('lost')).mockResolvedValue(undefined)
    openScheduler(onReschedule)
    fireEvent.change(screen.getByLabelText('Minutes past the hour'), { target: { value: '0' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Save' })) })
    await screen.findByRole('alert')
    fireEvent.change(screen.getByLabelText('Minutes past the hour'), { target: { value: '32' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Save' })) })
    expect(onReschedule).toHaveBeenLastCalledWith('r1', '2026-11-04T11:32:00.000Z', 60)
  })

  it('keeps the editor open with an error when the save fails', async () => {
    openScheduler(vi.fn().mockRejectedValue(new Error('boom')))
    fireEvent.change(screen.getByRole('combobox', { name: 'Repeat' }), { target: { value: 'once' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Save' })) })
    expect(await screen.findByRole('alert')).toHaveTextContent('Couldn’t change that schedule — boom')
    expect(screen.getByRole('combobox', { name: 'Repeat' })).toHaveValue('once')
  })
})
