import { act, fireEvent, screen } from '@testing-library/react'

/**
 * Expand every session row's linked-items strip, the way a person clicks the
 * count on line 3 (#38200). The chip strip is folded by default, so a test of the
 * chips themselves opens it first. Rows render from the store synchronously, so
 * this runs straight after `render`; a row that mounts later stays folded.
 */
export function expandSessionLinks(): void {
  for (const count of screen.queryAllByTestId('session-links-count')) {
    if (count.getAttribute('aria-expanded') === 'true') continue
    act(() => { fireEvent.click(count) })
  }
}
