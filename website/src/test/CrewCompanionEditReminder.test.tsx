// A reminder's text is edited in place: Enter or blur saves, Escape cancels,
// and a failed save keeps the typed text with an error.

import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor, act } from '@testing-library/react'

import RemindersSection from '../apps/crew-companion/RemindersSection'
import type { RemindersPayload } from '../apps/crew-companion/types'

afterEach(cleanup)

const rem: RemindersPayload = {
  reminders: [{ id: 'r1', text: 'drink watr', fireAt: '2099-01-01T09:00:00', recurrence: null }],
  breakNudgesEnabled: true,
  sessionNotificationsEnabled: true,
  breakReminderMins: 45,
  language: 'English',
  present: true,
}

function openEditor(onEdit = vi.fn().mockResolvedValue(undefined)) {
  render(
    <RemindersSection rem={rem} remError={null} onAdd={vi.fn()} onSkip={vi.fn()} onRemove={vi.fn()} onEdit={onEdit} />,
  )
  fireEvent.click(screen.getByRole('button', { name: 'Edit “drink watr”' }))
  return { onEdit, input: screen.getByRole('textbox', { name: 'Edit “drink watr”' }) as HTMLInputElement }
}

describe('editing a reminder in place', () => {
  it('saves the trimmed text on Enter and closes the editor', async () => {
    const { onEdit, input } = openEditor()
    expect(input.value).toBe('drink watr')
    fireEvent.change(input, { target: { value: '  drink water ' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onEdit).toHaveBeenCalledWith('r1', 'drink water')
    await waitFor(() => expect(screen.queryByRole('textbox', { name: /^Edit/ })).toBeNull())
  })

  it('hides Remove while editing, and keeps the editor open through a failed poll', () => {
    const onEdit = vi.fn()
    const view = (remError: string | null) => (
      <RemindersSection rem={rem} remError={remError} onAdd={vi.fn()} onSkip={vi.fn()} onRemove={vi.fn()} onEdit={onEdit} />
    )
    const { rerender } = render(view(null))
    fireEvent.click(screen.getByRole('button', { name: 'Edit “drink watr”' }))
    const input = screen.getByRole('textbox', { name: 'Edit “drink watr”' })
    fireEvent.change(input, { target: { value: 'drink water' } })
    expect(screen.queryByRole('button', { name: 'Remove “drink watr”' })).toBeNull()
    rerender(view('offline'))
    expect(screen.getByRole('textbox', { name: 'Edit “drink watr”' })).toHaveValue('drink water')
  })

  it('keeps an open edit when its reminder fires and drops out of the list', () => {
    const view = (r: RemindersPayload) => (
      <RemindersSection rem={r} remError={null} onAdd={vi.fn()} onSkip={vi.fn()} onRemove={vi.fn()} onEdit={vi.fn()} />
    )
    const { rerender } = render(view(rem))
    fireEvent.click(screen.getByRole('button', { name: 'Edit “drink watr”' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Edit “drink watr”' }), { target: { value: 'drink water' } })
    rerender(view({ ...rem, reminders: [] }))
    expect(screen.getByRole('textbox', { name: 'Edit “drink watr”' })).toHaveValue('drink water')
    fireEvent.keyDown(screen.getByRole('textbox', { name: 'Edit “drink watr”' }), { key: 'Escape' })
    expect(screen.queryByText('drink watr')).toBeNull()
  })

  it('keeps two open edits when both reminders drop out of the list', () => {
    const two: RemindersPayload = { ...rem, reminders: [...rem.reminders, { ...rem.reminders[0], id: 'r2', text: 'stretch' }] }
    const view = (r: RemindersPayload) => (
      <RemindersSection rem={r} remError={null} onAdd={vi.fn()} onSkip={vi.fn()} onRemove={vi.fn()} onEdit={vi.fn(() => new Promise(() => {}))} />
    )
    const { rerender } = render(view(two))
    fireEvent.click(screen.getByRole('button', { name: 'Edit “drink watr”' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Edit “drink watr”' }), { target: { value: 'drink water' } })
    fireEvent.click(screen.getByRole('button', { name: 'Edit “stretch”' }))
    rerender(view({ ...two, reminders: [] }))
    expect(screen.getByRole('textbox', { name: 'Edit “drink watr”' })).toHaveValue('drink water')
    expect(screen.getByRole('textbox', { name: 'Edit “stretch”' })).toHaveValue('stretch')
  })

  it('does not save an untouched draft over a newer polled text', () => {
    const onEdit = vi.fn()
    const view = (r: RemindersPayload) => (
      <RemindersSection rem={r} remError={null} onAdd={vi.fn()} onSkip={vi.fn()} onRemove={vi.fn()} onEdit={onEdit} />
    )
    const { rerender } = render(view(rem))
    fireEvent.click(screen.getByRole('button', { name: 'Edit “drink watr”' }))
    rerender(view({ ...rem, reminders: [{ ...rem.reminders[0], text: 'drink water (from another tab)' }] }))
    fireEvent.blur(screen.getByRole('textbox', { name: /^Edit/ }))
    expect(onEdit).not.toHaveBeenCalled()
  })

  it('saves a revert typed while an earlier save was in flight', async () => {
    let resolve!: () => void
    const onEdit = vi.fn(() => new Promise<void>((r) => { resolve = r }))
    const { input } = openEditor(onEdit)
    fireEvent.change(input, { target: { value: 'drink water' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    fireEvent.change(input, { target: { value: 'drink watr' } })
    await act(async () => { resolve() })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onEdit).toHaveBeenLastCalledWith('r1', 'drink watr')
    expect(onEdit).toHaveBeenCalledTimes(2)
  })

  it('retries a revert to the original text after a failed save', async () => {
    const onEdit = vi.fn().mockRejectedValueOnce(new Error('lost reply')).mockResolvedValue(undefined)
    const { input } = openEditor(onEdit)
    fireEvent.change(input, { target: { value: 'drink water' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await screen.findByRole('alert')
    fireEvent.change(input, { target: { value: 'drink watr' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onEdit).toHaveBeenLastCalledWith('r1', 'drink watr')
    expect(onEdit).toHaveBeenCalledTimes(2)
  })

  it('saves on blur', () => {
    const { onEdit, input } = openEditor()
    fireEvent.change(input, { target: { value: 'drink water' } })
    fireEvent.blur(input)
    expect(onEdit).toHaveBeenCalledWith('r1', 'drink water')
  })

  it('cancels on Escape without saving', () => {
    const { onEdit, input } = openEditor()
    fireEvent.change(input, { target: { value: 'drink water' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(onEdit).not.toHaveBeenCalled()
    expect(screen.getByText('drink watr')).toBeInTheDocument()
  })

  it('does not save blank or unchanged text', () => {
    const { onEdit, input } = openEditor()
    fireEvent.change(input, { target: { value: '   ' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onEdit).not.toHaveBeenCalled()
    expect(screen.queryByRole('textbox', { name: /^Edit/ })).toBeNull()
  })

  it('keeps the draft open with an error when the save fails', async () => {
    const { input } = openEditor(vi.fn().mockRejectedValue(new Error('boom')))
    fireEvent.change(input, { target: { value: 'drink water' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not save: boom')
    expect(input.value).toBe('drink water')
  })

  it('opens the editor from the More menu on a recurring row', async () => {
    const onEdit = vi.fn().mockResolvedValue(undefined)
    const recurring = { ...rem, reminders: [{ ...rem.reminders[0], recurrence: { everyMinutes: 60 } }] }
    render(<RemindersSection rem={recurring} remError={null} onAdd={vi.fn()} onSkip={vi.fn()} onRemove={vi.fn()} onEdit={onEdit} />)
    expect(screen.queryByRole('button', { name: 'Edit “drink watr”' })).toBeNull()
    fireEvent.keyDown(screen.getByRole('button', { name: 'More actions' }), { key: 'Enter' })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Edit' }))
    const input = await screen.findByRole('textbox', { name: 'Edit “drink watr”' })
    await waitFor(() => expect(input).toHaveFocus())
    fireEvent.change(input, { target: { value: 'drink water' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onEdit).toHaveBeenCalledWith('r1', 'drink water')
  })

  it('allows one save at a time and keeps text typed while it is in flight', async () => {
    let resolve!: () => void
    const onEdit = vi.fn(() => new Promise<void>((r) => { resolve = r }))
    const { input } = openEditor(onEdit)
    fireEvent.change(input, { target: { value: 'drink water' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    fireEvent.change(input, { target: { value: 'drink water now' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onEdit).toHaveBeenCalledTimes(1)
    await act(async () => { resolve() })
    expect(input.value).toBe('drink water now')
  })
})
