import { act, render } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { ListDock } from '../components/ListDock'

/**
 * The dock floats over the list and the list pads its top by the dock's live
 * height (`--list-dock-h`), so a chip row appearing or an error notice
 * mounting never hides the first row.
 */
describe('ListDock', () => {
  it('writes the measured dock height to --list-dock-h and keeps the dock click-through between its children', () => {
    const heights = [44, 84]
    let call = 0
    const rect = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(() => ({
      height: heights[Math.min(call++, heights.length - 1)], width: 280, x: 0, y: 0, top: 0, left: 0, right: 280, bottom: 0, toJSON: () => ({}),
    }) as DOMRect)
    const observers: Array<(entries: unknown[]) => void> = []
    const RO = vi.fn(function (this: { observe: () => void; disconnect: () => void }, cb: (entries: unknown[]) => void) {
      observers.push(cb)
      this.observe = vi.fn()
      this.disconnect = vi.fn()
    })
    vi.stubGlobal('ResizeObserver', RO)
    try {
      const { getByTestId, container } = render(
        <ListDock field={<input aria-label="Search" />} shelf={<button type="button">Starred</button>}>
          <ul data-testid="rows"><li>one</li></ul>
        </ListDock>,
      )
      const wrapper = container.firstElementChild as HTMLElement
      expect(wrapper.style.getPropertyValue('--list-dock-h')).toBe('44px')
      const dock = getByTestId('list-dock')
      expect(dock.className).toContain('pointer-events-none')
      // The controls re-arm, and so does the opaque shelf as a whole (a click
      // beside a chip lands inert, never on a row the scrim hides); the
      // transparent margin around the field stays click-through.
      for (const cls of ['[&_.liquid-glass]:pointer-events-auto', '[&_button]:pointer-events-auto', '[&_a]:pointer-events-auto', '[&_input]:pointer-events-auto', '[&_select]:pointer-events-auto', '[&_textarea]:pointer-events-auto', '[&_[tabindex]]:pointer-events-auto', '[&_[role=button]]:pointer-events-auto', '[&_[role=alert]]:pointer-events-auto']) expect(dock.className).toContain(cls)
      expect(dock.className).not.toContain('[&>*]:pointer-events-auto')
      // Above a pinned folder header (FOLDER_ROW_STICKY_Z = 20) so a header
      // pushed off its pin never paints over the glass.
      expect(dock.className).toContain('z-30')
      expect(dock.contains(container.querySelector('input'))).toBe(true)
      // The shelf carries the scrim class and holds what hangs under the field.
      const shelf = getByTestId('list-dock-shelf')
      expect(shelf.className).toContain('list-dock-shelf')
      expect(shelf.className).toContain('pointer-events-auto')
      expect(shelf.contains(container.querySelector('button'))).toBe(true)
      expect(getByTestId('rows').parentElement).toBe(wrapper)

      // A chip row appears: the observer fires and the padding var follows.
      act(() => { observers[0]([{ target: dock }]) })
      expect(wrapper.style.getPropertyValue('--list-dock-h')).toBe('84px')
    } finally {
      rect.mockRestore()
      vi.unstubAllGlobals()
    }
  })
})
