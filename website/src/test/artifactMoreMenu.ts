/**
 * Drive the artifact toolbar's "More" overflow menu (ArtifactDetailPage).
 *
 * Radix opens a DropdownMenu on pointerdown, not click, and renders its items in
 * a portal only while open, so every test that reaches a moved toolbar action
 * opens the menu first and then picks the item by its role.
 */
import { fireEvent, screen, waitFor } from '@testing-library/react'

export const MORE_ACTIONS = 'More actions'

export function moreButton(): HTMLElement {
  return screen.getByRole('button', { name: MORE_ACTIONS })
}

export function openMore(): void {
  fireEvent.pointerDown(moreButton(), { button: 0, ctrlKey: false, pointerType: 'mouse' })
}

/** Open the menu and resolve the item (plain or checkbox), without selecting it. */
export async function findMoreItem(name: string | RegExp): Promise<HTMLElement> {
  if (!screen.queryByRole('menu')) openMore()
  return waitFor(() => {
    const item = screen.queryByRole('menuitem', { name }) ?? screen.queryByRole('menuitemcheckbox', { name })
    if (!item) throw new Error(`no "More" menu item named ${String(name)}`)
    return item
  })
}

/** Whether the menu currently offers an item with this name. */
export async function hasMoreItem(name: string | RegExp): Promise<boolean> {
  if (!screen.queryByRole('menu')) openMore()
  await screen.findByRole('menu')
  const found = !!(screen.queryByRole('menuitem', { name }) ?? screen.queryByRole('menuitemcheckbox', { name }))
  await closeMore()
  return found
}

/** Open the menu and select one item. */
export async function chooseMore(name: string | RegExp): Promise<void> {
  fireEvent.click(await findMoreItem(name))
}

/** Close an open menu the way a keyboard user does, and wait until it is gone. */
export async function closeMore(): Promise<void> {
  const menu = screen.queryByRole('menu')
  if (!menu) return
  fireEvent.keyDown(menu, { key: 'Escape' })
  await waitFor(() => { if (screen.queryByRole('menu')) throw new Error('menu still open') })
}

/** Whether the comments sidebar is open, read off the menu item's label. */
export async function commentsShown(): Promise<boolean> {
  if (!screen.queryByRole('menu')) openMore()
  await screen.findByRole('menu')
  const shown = !!screen.queryByRole('menuitem', { name: /Hide comments/ })
  await closeMore()
  return shown
}
