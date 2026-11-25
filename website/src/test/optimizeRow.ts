import { fireEvent, screen } from '@testing-library/react'

/** "Optimize prompt" is a menu row (#41795), not a send-cluster button: in the
 *  "+" menu where "+" is a menu, else in the ⋯ overflow. This opens the
 *  `index`-th composer's menu when it is closed and returns the row. */
export function optimizeRow(index = 0): HTMLElement {
  const plus = screen.queryAllByTitle('Add files & options')[index]
  if (plus) {
    if (!screen.queryByTestId('composer-menu-optimize')) fireEvent.click(plus)
    return screen.getByTestId('composer-menu-optimize')
  }
  const open = screen.queryByTestId('composer-more-optimize')
  if (open) return open
  fireEvent.keyDown(screen.getAllByTestId('composer-more-trigger')[index], { key: 'Enter' })
  return screen.getByTestId('composer-more-optimize')
}

/** The "+" row is a `<button disabled>`; the ⋯ row is a Radix item. */
export function isOptimizeDisabled(row: HTMLElement): boolean {
  return row.hasAttribute('disabled') || row.hasAttribute('data-disabled')
}

export function clickOptimize(index = 0): void {
  fireEvent.click(optimizeRow(index))
}

/** For a full page, where the menu can mount a frame after the press. */
export async function clickOptimizeAsync(): Promise<void> {
  const plus = screen.queryByTitle('Add files & options')
  if (plus) {
    if (!screen.queryByTestId('composer-menu-optimize')) fireEvent.click(plus)
    fireEvent.click(await screen.findByTestId('composer-menu-optimize'))
    return
  }
  fireEvent.keyDown(screen.getByTestId('composer-more-trigger'), { key: 'Enter' })
  fireEvent.click(await screen.findByTestId('composer-more-optimize'))
}
