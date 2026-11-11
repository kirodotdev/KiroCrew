/**
 * The batch bar runs each row-menu action once per picked session, skips the
 * sessions the action would not change, and ends the selection when it is done.
 */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { createConnectedTestStore, createTestStore, renderWithProviders } from '../../test/helpers'
import type { ChatFolder } from '../../types'
import SelectionBar from './SelectionBar'
import type { Slot } from './types'

vi.mock('../../api/client', () => ({
  api: { kirocrewConfig: vi.fn().mockResolvedValue({}) },
}))

const slot = (key: string, over: Partial<Slot> = {}): Slot => ({ key, title: key, running: false, ...over }) as Slot
const folder = (id: string, over: Partial<ChatFolder> = {}): ChatFolder => ({ id, name: `Folder ${id}`, ...over }) as ChatFolder

const SLOTS = [slot('a', { pinned: true }), slot('b'), slot('c'), slot('d')]

function renderBar(over: Partial<Parameters<typeof SelectionBar>[0]> = {}, { online = true } = {}) {
  const actions = {
    togglePin: vi.fn(), toggleRead: vi.fn(), move: vi.fn(),
    closeMany: vi.fn().mockResolvedValue(true),
  }
  const onClear = vi.fn()
  renderWithProviders(
    <SelectionBar selected={new Set(['a', 'b', 'c'])} slots={SLOTS} unread={new Set(['b'])} folders={[]}
      actions={actions} onClear={onClear} {...over} />,
    { store: online ? createConnectedTestStore() : createTestStore() },
  )
  return { actions, onClear }
}

const openMenu = () => fireEvent.keyDown(screen.getByTestId('session-selection-actions'), { key: 'Enter' })
const menuItem = (label: string) =>
  screen.getAllByRole('menuitem').find(el => el.textContent?.trim().startsWith(label)) as HTMLElement

describe('SelectionBar', () => {
  it('counts the picked sessions and clears them from its close button', () => {
    const { onClear } = renderBar()
    expect(screen.getByTestId('session-selection-count')).toHaveTextContent('3 selected')
    fireEvent.click(screen.getByRole('button', { name: 'Clear selection' }))
    expect(onClear).toHaveBeenCalledTimes(1)
  })

  it('pins only the picked sessions that are not pinned yet', () => {
    const { actions, onClear } = renderBar()
    openMenu()
    fireEvent.click(menuItem('Pin'))
    expect(actions.togglePin.mock.calls.map(c => c[0])).toEqual(['b', 'c'])
    expect(onClear).toHaveBeenCalledTimes(1)
  })

  it('offers Unpin once every picked session is pinned', () => {
    const { actions } = renderBar({ selected: new Set(['a']) })
    openMenu()
    fireEvent.click(menuItem('Unpin'))
    expect(actions.togglePin.mock.calls.map(c => c[0])).toEqual(['a'])
  })

  it('marks only the unread picked sessions as read', () => {
    const { actions } = renderBar()
    openMenu()
    fireEvent.click(menuItem('Mark as read'))
    expect(actions.toggleRead.mock.calls.map(c => c[0])).toEqual(['b'])
  })

  it('marks every picked session unread when none of them is', () => {
    const { actions } = renderBar({ unread: new Set(['d']) })
    openMenu()
    fireEvent.click(menuItem('Mark as unread'))
    expect(actions.toggleRead.mock.calls.map(c => c[0])).toEqual(['a', 'b', 'c'])
  })

  it('closes the picked sessions in list order on one confirmation', async () => {
    const { actions, onClear } = renderBar()
    openMenu()
    fireEvent.click(menuItem('Close 3 sessions'))
    expect(actions.closeMany).toHaveBeenCalledWith(['a', 'b', 'c'])
    await waitFor(() => expect(onClear).toHaveBeenCalledTimes(1))
  })

  it('keeps the selection when the close is not confirmed', async () => {
    const closeMany = vi.fn().mockResolvedValue(false)
    const { onClear } = renderBar({ actions: { togglePin: vi.fn(), toggleRead: vi.fn(), move: vi.fn(), closeMany } })
    openMenu()
    fireEvent.click(menuItem('Close 3 sessions'))
    await waitFor(() => expect(closeMany).toHaveBeenCalledTimes(1))
    expect(onClear).not.toHaveBeenCalled()
  })

  it('refuses a gateway write while offline and leaves the selection', () => {
    const { actions, onClear } = renderBar({}, { online: false })
    openMenu()
    fireEvent.click(menuItem('Pin'))
    fireEvent.click(menuItem('Close 3 sessions'))
    expect(actions.togglePin).not.toHaveBeenCalled()
    expect(actions.closeMany).not.toHaveBeenCalled()
    expect(onClear).not.toHaveBeenCalled()
  })

  it('offers no move without a folder to move to', () => {
    renderBar()
    openMenu()
    expect(menuItem('Move to folder')).toBeUndefined()
  })

  it('moves every picked session to the chosen folder, except one in a mate folder', () => {
    const folders = [folder('f1'), folder('mate', { mate_home: true })]
    const slots = [slot('a', { folder_id: 'mate' }), slot('b'), slot('c', { folder_id: 'f1' })]
    const { actions, onClear } = renderBar({ slots, folders })
    openMenu()
    fireEvent.keyDown(menuItem('Move to folder'), { key: 'Enter' })
    // The picked sessions sit in two places, so no destination is ticked as theirs.
    const destinations = screen.getAllByRole('menuitem').filter(el => /No folder|Folder f1/.test(el.textContent ?? ''))
    expect(destinations.map(el => el.textContent)).toEqual(['No folder (root)', 'Folder f1'])
    expect(destinations.some(el => el.querySelector('svg.text-accent'))).toBe(false)
    // No chat moves into a mate folder either.
    expect(screen.queryByRole('menuitem', { name: 'Folder mate' })).toBeNull()
    fireEvent.click(destinations[1])
    expect(actions.move.mock.calls).toEqual([['b', 'f1'], ['c', 'f1']])
    expect(onClear).toHaveBeenCalledTimes(1)
  })

  it('offers no move when every picked session sits in a mate folder', () => {
    const folders = [folder('f1'), folder('mate', { mate_home: true })]
    const slots = [slot('a', { folder_id: 'mate' }), slot('b')]
    renderBar({ selected: new Set(['a']), slots, folders })
    openMenu()
    expect(menuItem('Move to folder')).toBeUndefined()
  })
})
