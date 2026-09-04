import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import BusySendButton from '../components/BusySendButton'

function renderButton() {
  render(
    <BusySendButton
      mode="steer"
      onModeChange={vi.fn()}
      onFire={vi.fn()}
    />,
  )
}

async function openMenu() {
  renderButton()
  const trigger = screen.getByTestId('busy-send-caret')
  fireEvent.click(trigger)
  const menu = await screen.findByRole('menu')
  const rows = screen.getAllByRole('menuitemradio')
  await waitFor(() => expect(rows[0]).toHaveFocus())
  return { menu, rows, trigger }
}

describe('BusySendButton menu keyboard contract', () => {
  it('wraps ArrowUp from the first row to the last row', async () => {
    const { rows } = await openMenu()

    rows[0].focus()
    fireEvent.keyDown(rows[0], { key: 'ArrowUp' })

    expect(rows[1]).toHaveFocus()
  })

  it('contains Tab inside the open menu', async () => {
    const { menu, rows } = await openMenu()

    rows[1].focus()
    fireEvent.keyDown(rows[1], { key: 'Tab' })

    expect(screen.getByRole('menu')).toBe(menu)
    expect(rows[0]).toHaveFocus()

    fireEvent.keyDown(rows[0], { key: 'Tab', shiftKey: true })

    expect(screen.getByRole('menu')).toBe(menu)
    expect(rows[1]).toHaveFocus()
  })

  it('closes on Escape and restores focus to the trigger', async () => {
    const { rows, trigger } = await openMenu()

    fireEvent.keyDown(rows[0], { key: 'Escape' })

    expect(screen.queryByRole('menu')).toBeNull()
    expect(trigger).toHaveFocus()
  })
})
