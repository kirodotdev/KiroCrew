/**
 * A kept host stays with the slot that adopted it last, and a host made with
 * `keepScrollAndFocus` gets back what each move takes from its content.
 *
 * When a surface is swapped while open (a phone/desktop switch), the new slot
 * adopts the host before the old one unmounts. The old slot's cleanup must then
 * leave the host where it is, or the open surface is left empty.
 *
 * A browser puts a box that moves to a new parent back at its top and sends the
 * focus to the page. happy-dom drops the focus the same way but keeps offsets,
 * so the scrolled box here is a `modelBrowserScroll` one.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { render, renderHook } from '@testing-library/react'
import { AdoptPaneHost, useKeptPaneHost } from '../components/AdoptPaneHost'
import { modelBrowserScroll } from './browserScrollModel'

function Slots({ host, a, b }: { host: HTMLElement; a: boolean; b: boolean }) {
  return (
    <>
      {a && <div data-testid="slot-a"><AdoptPaneHost host={host} /></div>}
      {b && <div data-testid="slot-b"><AdoptPaneHost host={host} /></div>}
    </>
  )
}

describe('AdoptPaneHost', () => {
  it('keeps the host in the newer slot when the older slot unmounts after it', () => {
    const host = document.createElement('div')
    const view = render(<Slots host={host} a b={false} />)
    expect(view.getByTestId('slot-a').contains(host)).toBe(true)

    view.rerender(<Slots host={host} a b />)
    expect(view.getByTestId('slot-b').contains(host)).toBe(true)

    view.rerender(<Slots host={host} a={false} b />)
    expect(view.getByTestId('slot-b').contains(host)).toBe(true)
  })

  it('lets go of the host when its slot is the one that leaves', () => {
    const host = document.createElement('div')
    const view = render(<Slots host={host} a b={false} />)
    view.rerender(<Slots host={host} a={false} b={false} />)
    expect(host.isConnected).toBe(false)
  })

  it('tells its owner when the slot comes and goes', () => {
    const host = document.createElement('div')
    const seen: boolean[] = []
    const onMountChange = (mounted: boolean) => { seen.push(mounted) }
    const view = render(<AdoptPaneHost host={host} onMountChange={onMountChange} />)
    view.rerender(<AdoptPaneHost host={host} onMountChange={onMountChange} />)
    expect(seen).toEqual([true])
    view.unmount()
    expect(seen).toEqual([true, false])
  })
})

describe('useKeptPaneHost', () => {
  it('keeps one detached, box-less host for the life of its owner', () => {
    const { result, rerender } = renderHook(() => useKeptPaneHost())
    const host = result.current
    expect(host).toBeInstanceOf(HTMLDivElement)
    expect(host!.style.display).toBe('contents')
    expect(host!.isConnected).toBe(false)
    rerender()
    expect(result.current).toBe(host)
  })
})

describe('a kept host with keepScrollAndFocus, across moves', () => {
  const outside = document.createElement('button')
  afterEach(() => {
    ;(document.activeElement as HTMLElement | null)?.blur?.()
    outside.remove()
  })

  /** A host holding a scrolled box and a field, as the side panel's does. */
  function keptContent(options?: { keepScrollAndFocus?: boolean }) {
    const host = renderHook(() => useKeptPaneHost(options)).result.current!
    const box = document.createElement('div')
    const field = document.createElement('textarea')
    host.append(box, field)
    const size = modelBrowserScroll(box, () => host.parentNode)
    document.body.appendChild(outside)
    return { host, box, field, size }
  }

  it('puts the scroll offset and the focus back after a direct move, as an unfold makes', () => {
    const { host, box, field } = keptContent({ keepScrollAndFocus: true })
    const view = render(<Slots host={host} a b={false} />)
    box.scrollTop = 500
    field.focus()

    view.rerender(<Slots host={host} a b />)
    expect(view.getByTestId('slot-b').contains(host)).toBe(true)
    expect(box.scrollTop).toBe(500)
    expect(document.activeElement).toBe(field)
  })

  it('puts them back after a move with a gap, as a fold makes, ignoring a scroll that lands off the page', () => {
    const { host, box, field } = keptContent({ keepScrollAndFocus: true })
    const view = render(<Slots host={host} a b={false} />)
    box.scrollTop = 500
    field.focus()

    view.rerender(<Slots host={host} a={false} b={false} />)
    expect(host.isConnected).toBe(false)
    // A scroll queued in the frame before the fold, read with the box off the page.
    box.dispatchEvent(new Event('scroll'))
    view.rerender(<Slots host={host} a={false} b />)
    expect(box.scrollTop).toBe(500)
    expect(document.activeElement).toBe(field)
  })

  it('keeps a box that followed its end at the end when the new width wraps it to another height', () => {
    const { host, box, size } = keptContent({ keepScrollAndFocus: true })
    const view = render(<Slots host={host} a b={false} />)
    box.scrollTop = size.height - size.client
    size.height = 3000

    view.rerender(<Slots host={host} a b />)
    expect(box.scrollTop).toBe(3000 - size.client)
  })

  it('does not hand the focus back to a field the person left for another place', async () => {
    const { host, field } = keptContent({ keepScrollAndFocus: true })
    const view = render(<Slots host={host} a b={false} />)
    field.focus()
    outside.focus()
    outside.blur()
    await Promise.resolve()

    view.rerender(<Slots host={host} a b />)
    expect(document.activeElement).toBe(document.body)
  })

  it('leaves the focus with whatever took it during the move, and then forgets the field', () => {
    const { host, field } = keptContent({ keepScrollAndFocus: true })
    const view = render(<Slots host={host} a b={false} />)
    field.focus()
    view.rerender(<Slots host={host} a={false} b={false} />)
    outside.focus()

    view.rerender(<Slots host={host} a={false} b />)
    expect(document.activeElement).toBe(outside)

    outside.blur()
    view.rerender(<Slots host={host} a b />)
    expect(view.getByTestId('slot-a').contains(host)).toBe(true)
    expect(document.activeElement).toBe(document.body)
  })

  it('keeps the field when the browser reports the focus leaving from inside a move, as Chromium does', async () => {
    const { host, field } = keptContent({ keepScrollAndFocus: true })
    const view = render(<Slots host={host} a b={false} />)
    field.focus()
    // Chromium fires `focusout` at a focused field whose box moves: on an
    // unfold just after the new slot takes the host, and on a fold while the
    // host is still on the page, before it leaves.
    const leaving = () => field.dispatchEvent(new FocusEvent('focusout', { bubbles: true }))
    const append = Node.prototype.appendChild
    const remove = Element.prototype.remove
    Node.prototype.appendChild = function <T extends Node>(this: Node, node: T): T {
      const out = append.call(this, node) as T
      if ((node as Node) === host) leaving()
      return out
    }
    Element.prototype.remove = function (this: Element) {
      if (this === host && host.isConnected) leaving()
      remove.call(this)
    }
    try {
      view.rerender(<Slots host={host} a b />)
      await Promise.resolve()
      expect(document.activeElement).toBe(field)

      view.rerender(<Slots host={host} a={false} b />)
      view.rerender(<Slots host={host} a={false} b={false} />)
      await Promise.resolve()
      view.rerender(<Slots host={host} a b={false} />)
      await Promise.resolve()
    } finally {
      Node.prototype.appendChild = append
      Element.prototype.remove = remove
    }
    expect(view.getByTestId('slot-a').contains(host)).toBe(true)
    expect(document.activeElement).toBe(field)
  })

  it('puts nothing back for a host made without it, as the sessions list makes', () => {
    const { host, box, field } = keptContent()
    const view = render(<Slots host={host} a b={false} />)
    box.scrollTop = 500
    field.focus()

    view.rerender(<Slots host={host} a b />)
    expect(box.scrollTop).toBe(0)
    expect(document.activeElement).toBe(document.body)
  })
})
