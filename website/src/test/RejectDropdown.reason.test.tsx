import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import RejectDropdown from '../components/RejectDropdown'
import { i18nT } from '../i18n/t'

const BOX = () => i18nT('components.rejectDropdown.reason_placeholder')

async function openMenu(onAction = vi.fn()) {
  render(<RejectDropdown takesReason onAction={onAction} />)
  fireEvent.keyDown(screen.getByRole('button', { name: i18nT('components.chatInput.reject') }), { key: 'Enter' })
  await screen.findByRole('menu')
  const once = screen.getAllByRole('menuitem').find(b => b.textContent?.startsWith('Reject once'))!
  return { once, box: screen.getByRole('textbox', { name: BOX() }), onAction }
}

describe('RejectDropdown reason box', () => {
  it('is reachable by keyboard from the first choice and back', async () => {
    const { once, box } = await openMenu()
    once.focus()
    fireEvent.keyDown(once, { key: 'ArrowUp' })
    expect(box).toHaveFocus()
    fireEvent.keyDown(box, { key: 'ArrowDown' })
    expect(once).toHaveFocus()
  })

  it('keeps typed letters out of the menu and lets Escape close it', async () => {
    const { box } = await openMenu()
    box.focus()
    fireEvent.change(box, { target: { value: 'stale' } })
    fireEvent.keyDown(box, { key: 'r' })
    expect(box).toHaveFocus()
    fireEvent.keyDown(box, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
  })

  it('drops a reason left in a closed menu, so a later rejection sends none', async () => {
    const { once, box, onAction } = await openMenu()
    fireEvent.change(box, { target: { value: 'stale' } })
    fireEvent.keyDown(once, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
    fireEvent.keyDown(screen.getByRole('button', { name: i18nT('components.chatInput.reject') }), { key: 'Enter' })
    expect(await screen.findByRole('textbox', { name: BOX() })).toHaveValue('')
    fireEvent.click(screen.getAllByRole('menuitem').find(b => b.textContent?.startsWith('Reject once'))!)
    expect(onAction).toHaveBeenCalledWith('rejected_once', undefined)
  })
})
