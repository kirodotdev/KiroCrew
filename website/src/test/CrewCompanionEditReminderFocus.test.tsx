// Closing a reminder's in-place editor never strands focus on document.body.
// Enter and Escape return focus to the control the editor was opened from: the
// Edit button, or the More-actions trigger on a recurring row. A pointer blur
// saves but moves focus nowhere (WCAG 3.2.2).

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
const recurring: RemindersPayload = { ...rem, reminders: [{ ...rem.reminders[0], recurrence: { everyMinutes: 60 } }] }

const editName = 'Edit “drink watr”'
const nextFrame = () => act(async () => { await new Promise(requestAnimationFrame) })

function setup(payload: RemindersPayload) {
  const onEdit = vi.fn().mockResolvedValue(undefined)
  render(
    <>
      <RemindersSection rem={payload} remError={null} onAdd={vi.fn()} onSkip={vi.fn()} onRemove={vi.fn()} onEdit={onEdit} />
      <button type="button" data-testid="elsewhere">Elsewhere</button>
    </>,
  )
  return { onEdit }
}

const editButton = () => screen.getByRole('button', { name: editName })
const moreButton = () => screen.getByRole('button', { name: 'More actions' })

async function openFromEdit() {
  fireEvent.click(editButton())
  const input = screen.getByRole('textbox', { name: editName })
  await waitFor(() => expect(input).toHaveFocus())
  return input
}

async function openFromMenu() {
  fireEvent.keyDown(moreButton(), { key: 'Enter' })
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Edit' }))
  const input = await screen.findByRole('textbox', { name: editName })
  await waitFor(() => expect(input).toHaveFocus())
  return input
}

describe('reminder edit focus', () => {
  it('Enter saves and returns focus to the Edit button', async () => {
    const { onEdit } = setup(rem)
    const input = await openFromEdit()
    fireEvent.change(input, { target: { value: 'drink water' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(editButton()).toHaveFocus())
    expect(onEdit).toHaveBeenCalledTimes(1)
    expect(onEdit).toHaveBeenCalledWith('r1', 'drink water')
  })

  it('Enter on unchanged text closes and returns focus to the Edit button', async () => {
    const { onEdit } = setup(rem)
    const input = await openFromEdit()
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(editButton()).toHaveFocus())
    expect(onEdit).not.toHaveBeenCalled()
  })

  it('Escape cancels and returns focus to the Edit button', async () => {
    const { onEdit } = setup(rem)
    const input = await openFromEdit()
    fireEvent.change(input, { target: { value: 'drink water' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    await waitFor(() => expect(editButton()).toHaveFocus())
    expect(onEdit).not.toHaveBeenCalled()
  })

  it('Enter saves and returns focus to the More-actions trigger on a recurring row', async () => {
    const { onEdit } = setup(recurring)
    const input = await openFromMenu()
    fireEvent.change(input, { target: { value: 'drink water' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(moreButton()).toHaveFocus())
    expect(onEdit).toHaveBeenCalledWith('r1', 'drink water')
  })

  it('Escape cancels and returns focus to the More-actions trigger on a recurring row', async () => {
    const { onEdit } = setup(recurring)
    const input = await openFromMenu()
    fireEvent.keyDown(input, { key: 'Escape' })
    await waitFor(() => expect(moreButton()).toHaveFocus())
    expect(onEdit).not.toHaveBeenCalled()
  })

  it('a keyboard save that resolves after the user moved on leaves focus where they put it', async () => {
    let resolve!: () => void
    const onEdit = vi.fn(() => new Promise<void>((r) => { resolve = r }))
    render(
      <>
        <RemindersSection rem={rem} remError={null} onAdd={vi.fn()} onSkip={vi.fn()} onRemove={vi.fn()} onEdit={onEdit} />
        <button type="button" data-testid="elsewhere">Elsewhere</button>
      </>,
    )
    const input = await openFromEdit()
    fireEvent.change(input, { target: { value: 'drink water' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    const elsewhere = screen.getByTestId('elsewhere')
    act(() => { elsewhere.focus() })
    await act(async () => { resolve() })
    await waitFor(() => expect(screen.queryByRole('textbox', { name: editName })).toBeNull())
    await nextFrame()
    expect(elsewhere).toHaveFocus()
  })

  it('a pointer blur saves but leaves focus where the user put it', async () => {
    const { onEdit } = setup(rem)
    const input = await openFromEdit()
    fireEvent.change(input, { target: { value: 'drink water' } })
    const elsewhere = screen.getByTestId('elsewhere')
    act(() => { elsewhere.focus() })
    await waitFor(() => expect(onEdit).toHaveBeenCalledWith('r1', 'drink water'))
    await waitFor(() => expect(screen.queryByRole('textbox', { name: editName })).toBeNull())
    // A frame has passed, so a stray restore would have run.
    await nextFrame()
    expect(elsewhere).toHaveFocus()
  })
})
