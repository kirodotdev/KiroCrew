/**
 * The ask row is the ONLY way to save a reminder whose text carries no time.
 *
 * "30秒后喊我起床" is such a case: the parser finds no schedule it supports, so it
 * asks instead of guessing, and the three pills are the answer. That makes them a
 * save path, not decoration — if they do not call through, the reminder cannot be
 * added at all, which is exactly how it was reported.
 *
 * Rendered rather than unit-tested through the callback: the bug shipped BECAUSE the
 * pills looked wrong and the wiring was never exercised together with them.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'

import { ReminderInput } from '../apps/crew-companion/ReminderInput'

afterEach(cleanup)

/** Type text with no time signal, so the component falls into the ask row. */
async function typeUnscheduled(onAdd: ReturnType<typeof vi.fn>) {
  const { container } = render(<ReminderInput onAdd={onAdd} />)
  const input = container.querySelector('input')!
  fireEvent.change(input, { target: { value: 'wake me up' } })
  fireEvent.submit(container.querySelector('form')!)
  return container
}

describe('ask row saves a reminder with no time in it', () => {
  it('asks instead of silently dropping it', async () => {
    const onAdd = vi.fn().mockResolvedValue(true)
    await typeUnscheduled(onAdd)
    // Nothing saved yet — the component asked rather than guessing a time.
    await waitFor(() => expect(screen.getByText(/When\?/i)).toBeInTheDocument())
    expect(onAdd).not.toHaveBeenCalled()
  })

  it('the pills are labelled with copy, never with their own catalog key', async () => {
    const onAdd = vi.fn().mockResolvedValue(true)
    const container = await typeUnscheduled(onAdd)
    await waitFor(() => expect(screen.getByText(/When\?/i)).toBeInTheDocument())
    const labels = Array.from(container.querySelectorAll('button')).map((b) => b.textContent ?? '')
    expect(labels.some((l) => l.includes('panel.ask'))).toBe(false)
  })

  it('clicking a pill actually saves — the reported "cannot add"', async () => {
    const onAdd = vi.fn().mockResolvedValue(true)
    const container = await typeUnscheduled(onAdd)
    await waitFor(() => expect(screen.getByText(/When\?/i)).toBeInTheDocument())
    // The pills are the buttons that appeared alongside the question.
    const pill = Array.from(container.querySelectorAll('button'))
      .find((b) => /1h|tomorrow|daily/i.test(b.textContent ?? ''))
    expect(pill, 'an ask pill should be rendered').toBeDefined()
    fireEvent.click(pill!)
    await waitFor(() => expect(onAdd).toHaveBeenCalledTimes(1))
    // It must carry a concrete fireAt: the backend refuses an add without one.
    const [text, fireAtIso] = onAdd.mock.calls[0]
    expect(text).toBeTruthy()
    expect(Number.isFinite(Date.parse(fireAtIso))).toBe(true)
  })
})
