import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, act, screen } from '@testing-library/react'
import { useRef } from 'react'
import SelectionToolbar, { type SelectionAction } from '../components/SelectionToolbar'

/* App items (`contributes.selectionActions`) sit in their own row under the
 * host's, so the host row (Quote, Ask, Copy) never grows when an app adds one. */

function Harness({ actions, appActions }: { actions: SelectionAction[]; appActions: SelectionAction[] }) {
  const ref = useRef<HTMLDivElement>(null)
  return (
    <div>
      <div ref={ref} data-testid="container"><p>Selectable text in here</p></div>
      <SelectionToolbar containerRef={ref} actions={actions} appActions={appActions} />
    </div>
  )
}

function selectAllIn(el: HTMLElement) {
  const range = document.createRange()
  range.selectNodeContents(el)
  range.getBoundingClientRect = () => new DOMRect(10, 10, 100, 20)
  const sel = window.getSelection()!
  sel.removeAllRanges()
  sel.addRange(range)
}

const host = (id: string): SelectionAction => ({ id, icon: null, label: id, onClick: vi.fn() })

beforeEach(() => { vi.useFakeTimers({ shouldAdvanceTime: true }) })
afterEach(() => { window.getSelection()?.removeAllRanges(); vi.useRealTimers() })

describe('SelectionToolbar app row', () => {
  it('draws app items in a separate row and hands them the selection', async () => {
    const onApp = vi.fn()
    render(<Harness actions={[host('quote'), host('ask'), host('copy')]} appActions={[{ id: 'app:x', icon: null, label: 'Save to notes', hint: 'Sends the selected text to Notes', onClick: onApp }]} />)
    const container = screen.getByTestId('container')
    selectAllIn(container)
    fireEvent.mouseDown(container)
    fireEvent.mouseUp(container)
    await act(async () => { await vi.advanceTimersByTimeAsync(50) })
    const row = screen.getByTestId('selection-app-row')
    const item = screen.getByRole('button', { name: 'Save to notes' })
    expect(row.contains(item)).toBe(true)
    // The host row is unchanged: its three buttons, none of them in the app row.
    for (const id of ['quote', 'ask', 'copy']) expect(row.contains(screen.getByRole('button', { name: id }))).toBe(false)
    expect(item.getAttribute('aria-describedby')).toBeTruthy()
    // A long title truncates inside the button instead of pushing it off screen.
    expect(item.className).toContain('min-w-0')
    expect(item.querySelector('span.truncate')?.textContent).toBe('Save to notes')
    fireEvent.click(item)
    expect(onApp).toHaveBeenCalledWith('Selectable text in here', expect.anything())
  })

  it('hands an app only the range inside the container, never a second range elsewhere', async () => {
    const onApp = vi.fn()
    const onCopy = vi.fn()
    const outside = document.createElement('p')
    outside.textContent = 'secret file text'
    document.body.append(outside)
    render(<Harness actions={[{ id: 'copy', icon: null, label: 'copy', onClick: onCopy }]} appActions={[{ id: 'app:x', icon: null, label: 'Save', onClick: onApp }]} />)
    const container = screen.getByTestId('container')
    const inner = document.createRange()
    inner.selectNodeContents(container)
    inner.getBoundingClientRect = () => new DOMRect(10, 10, 100, 20)
    const other = document.createRange()
    other.selectNodeContents(outside)
    // A Firefox ctrl+drag selection: two ranges, the second outside the reply.
    const multi = {
      rangeCount: 2,
      isCollapsed: false,
      getRangeAt: (i: number) => (i === 0 ? inner : other),
      toString: () => `${inner.toString()}${other.toString()}`,
      removeAllRanges: () => {},
    } as unknown as Selection
    const spy = vi.spyOn(window, 'getSelection').mockReturnValue(multi)
    try {
      fireEvent.mouseDown(container)
      fireEvent.mouseUp(container)
      await act(async () => { await vi.advanceTimersByTimeAsync(50) })
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))
      expect(onApp).toHaveBeenCalledWith('Selectable text in here', expect.anything())
      expect(onApp.mock.calls[0][0]).not.toContain('secret')
    } finally {
      spy.mockRestore()
      outside.remove()
    }
  })

  it('hands an app the selected words of a link card title, not the hidden link address', async () => {
    const onApp = vi.fn()
    function LinkHarness() {
      const ref = useRef<HTMLDivElement>(null)
      return (
        <div>
          <div ref={ref} data-testid="container"><p><a href="https://example.com/a?b=secret" data-unfurl-url="https://example.com/a?b=secret">Quarterly report title</a></p></div>
          <SelectionToolbar containerRef={ref} actions={[host('copy')]} appActions={[{ id: 'app:x', icon: null, label: 'Save', onClick: onApp }]} />
        </div>
      )
    }
    render(<LinkHarness />)
    const container = screen.getByTestId('container')
    const textNode = container.querySelector('a')!.firstChild!
    const range = document.createRange()
    range.setStart(textNode, 0)
    range.setEnd(textNode, 'Quarterly'.length)
    range.getBoundingClientRect = () => new DOMRect(10, 10, 100, 20)
    const sel = window.getSelection()!
    sel.removeAllRanges()
    sel.addRange(range)
    fireEvent.mouseDown(container)
    fireEvent.mouseUp(container)
    await act(async () => { await vi.advanceTimersByTimeAsync(50) })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(onApp).toHaveBeenCalledWith('Quarterly', expect.anything())
  })

  it('a docked toolbar moves up when the app row appears while it is open', async () => {
    const composer = document.createElement('div')
    composer.className = 'input-area'
    composer.getBoundingClientRect = () => new DOMRect(0, 800, 600, 100)
    document.body.append(composer)
    const offsetHeight = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetHeight')
    // A toolbar with the app row is taller: 40px per row.
    Object.defineProperty(HTMLElement.prototype, 'offsetHeight', {
      configurable: true,
      get(this: HTMLElement) { return this.querySelector('[data-testid="selection-app-row"]') ? 80 : 40 },
    })
    function DockHarness({ appActions }: { appActions: SelectionAction[] }) {
      const ref = useRef<HTMLDivElement>(null)
      return (
        <div>
          <div ref={ref} data-testid="container"><p>Selectable text in here</p></div>
          <SelectionToolbar containerRef={ref} actions={[host('quote')]} appActions={appActions} dock />
        </div>
      )
    }
    try {
      const r = render(<DockHarness appActions={[]} />)
      const container = screen.getByTestId('container')
      selectAllIn(container)
      fireEvent.mouseDown(container)
      fireEvent.mouseUp(container)
      await act(async () => { await vi.advanceTimersByTimeAsync(50) })
      const bar = () => screen.getByRole('button', { name: 'quote' }).closest('[style*="top"]') as HTMLElement
      expect(bar().style.top).toBe('752px')
      // The app list read lands: the Apps row joins the open toolbar.
      r.rerender(<DockHarness appActions={[{ id: 'app-items', icon: null, label: 'Apps', onClick: vi.fn() }]} />)
      await act(async () => { await vi.advanceTimersByTimeAsync(0) })
      expect(screen.getByTestId('selection-app-row')).toBeTruthy()
      expect(bar().style.top).toBe('712px')
    } finally {
      if (offsetHeight) Object.defineProperty(HTMLElement.prototype, 'offsetHeight', offsetHeight)
      composer.remove()
    }
  })

  it('draws no app row when there are no app items', async () => {
    render(<Harness actions={[host('copy')]} appActions={[]} />)
    const container = screen.getByTestId('container')
    selectAllIn(container)
    fireEvent.mouseDown(container)
    fireEvent.mouseUp(container)
    await act(async () => { await vi.advanceTimersByTimeAsync(50) })
    expect(screen.getByRole('button', { name: 'copy' })).toBeTruthy()
    expect(screen.queryByTestId('selection-app-row')).toBeNull()
  })
})
