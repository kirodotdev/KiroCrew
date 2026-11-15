/**
 * The kept session list's host stays with the slot that adopted it last.
 *
 * When the drawer swaps its slot while open (a phone/desktop switch), the new
 * slot adopts the host before the old one unmounts. The old slot's cleanup must
 * then leave the host where it is, or the open sidebar is left empty.
 */
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { AdoptPaneHost } from '../pages/chat/page/SessionsSidebarChrome'

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
})
