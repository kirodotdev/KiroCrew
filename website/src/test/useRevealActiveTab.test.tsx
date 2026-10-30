import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, render, cleanup } from '@testing-library/react'
import { TAB_ID_ATTR, useRevealActiveTab } from '../hooks/useRevealActiveTab'
import { stubTabRow } from './helpers'

let reveal: (id: string) => void = () => {}

function Row({ ids, active, attached = true }: { ids: string[]; active: string | null; attached?: boolean }) {
  const [attach, revealTab] = useRevealActiveTab<HTMLDivElement>(active)
  reveal = revealTab
  return <div data-testid="row" ref={attached ? attach : undefined}>{ids.map(id => <span key={id} {...{ [TAB_ID_ATTR]: id }} />)}</div>
}

const ids = ['a', 'b', 'c', 'd', 'e', 'f']
const inside = (r: DOMRect) => r.left >= 0 && r.right <= 100

afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('useRevealActiveTab', () => {
  it('scrolls the tab that becomes active into view, and again on request', () => {
    const { rerender, getByTestId } = render(<Row ids={ids} active="a" />)
    const row = stubTabRow(getByTestId('row'))
    rerender(<Row ids={ids} active="f" />)
    expect(inside(row.tabRect('f'))).toBe(true)
    rerender(<Row ids={ids} active="a" />)
    expect(inside(row.tabRect('a'))).toBe(true)
    getByTestId('row').scrollLeft = 140
    expect(inside(row.tabRect('a'))).toBe(false)
    reveal('a')
    expect(inside(row.tabRect('a'))).toBe(true)
  })

  it('leaves manual scrolling alone when only the list changes', () => {
    const { rerender, getByTestId } = render(<Row ids={ids} active="a" />)
    const row = stubTabRow(getByTestId('row'))
    getByTestId('row').scrollLeft = 140
    rerender(<Row ids={[...ids, 'g']} active="a" />)
    expect(row.left()).toBe(140)
  })

  it('reveals when the scroller attaches after the active id was set', () => {
    const { rerender, getByTestId } = render(<Row ids={ids} active="f" attached={false} />)
    const row = stubTabRow(getByTestId('row'))
    expect(row.left()).toBe(0)
    rerender(<Row ids={ids} active="f" />)
    expect(inside(row.tabRect('f'))).toBe(true)
  })

  describe('while web fonts load', () => {
    let loaded = () => {}
    beforeEach(() => {
      Object.defineProperty(document, 'fonts', {
        configurable: true,
        value: { status: 'loading', ready: new Promise<void>(resolve => { loaded = resolve }) },
      })
    })
    afterEach(() => { Reflect.deleteProperty(document, 'fonts') })

    const mount = (active: string) => {
      const view = render(<Row ids={ids} active={active} attached={false} />)
      const row = stubTabRow(view.getByTestId('row'))
      view.rerender(<Row ids={ids} active={active} />)
      return { ...view, row }
    }
    // Simulate font growth by pushing tabs right without a scroll event.
    const widened = ['x1', 'x2', 'x3', ...ids]

    it('reveals again once they finish', async () => {
      const { rerender, row } = mount('f')
      rerender(<Row ids={widened} active="f" />)
      expect(inside(row.tabRect('f'))).toBe(false)
      await act(async () => { loaded() })
      expect(inside(row.tabRect('f'))).toBe(true)
    })

    it('reveals the tab that is active by then', async () => {
      const { rerender, row } = mount('f')
      rerender(<Row ids={ids} active="a" />)
      rerender(<Row ids={widened} active="a" />)
      expect(inside(row.tabRect('a'))).toBe(false)
      await act(async () => { loaded() })
      expect(inside(row.tabRect('a'))).toBe(true)
    })

    it('keeps the position the user scrolled to', async () => {
      const { rerender, getByTestId, row } = mount('f')
      getByTestId('row').scrollLeft = 20
      rerender(<Row ids={widened} active="f" />)
      await act(async () => { loaded() })
      expect(row.left()).toBe(20)
    })
  })
})
