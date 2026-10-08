/**
 * The trusted target registry: only an element React rendered through a
 * registering helper is a guide target; the attribute strings alone are not.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { StrictMode } from 'react'
import { uiLocation } from './uiLocation'
import { closestRegistered, guideAnchor, guideConfirm, guidePick, guidePickOf, guideTarget, registeredCopies, registeredId, registeredWithin, registrySize } from './targetRegistry'
import { findUiLocation, pickCandidate, pickItems } from '../guide/guideActions'
import { liveTarget, uiLocationCopies } from '../guide/liveRegistry'
import { ownLocation } from '../guide/findByName'
import { unregisteredMarkers } from '../test/guideTargets'

const flush = () => Promise.resolve()

function drawAll() {
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockReturnValue({ top: 10, left: 10, width: 40, height: 20, right: 50, bottom: 30, x: 10, y: 10, toJSON: () => ({}) } as DOMRect)
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  document.body.replaceChildren()
})

describe('trusted target registry', () => {
  it('an injected SVG carrying a location and a pick owner is never a copy, a target or an owner', () => {
    drawAll()
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
    svg.setAttribute('data-ui-location', 'apps.library.tile-uninstall')
    svg.setAttribute('data-guide-pick-of', 'Command Bar')
    svg.append(document.createElementNS('http://www.w3.org/2000/svg', 'rect'))
    document.body.append(svg)
    render(<div data-ui-location="apps.library.app-list"><button data-guide-pick="Command Bar">Command Bar</button></div>)
    expect(uiLocationCopies('apps.library.tile-uninstall')).toEqual([])
    expect(liveTarget('apps.library.tile-uninstall').status).toBe('unmounted')
    expect(findUiLocation('apps.library.tile-uninstall', () => true)).toBeNull()
    expect(findUiLocation('apps.library.app-list', () => true)).toBeNull()
    const list = document.querySelector('[data-ui-location="apps.library.app-list"]')!
    expect(pickItems(list)).toEqual([])
    expect(closestRegistered(document.querySelector('rect'), 'pickOf')).toBeNull()
    expect(ownLocation(document.querySelector('button')!)).toBeNull()
  })

  it('registers what React renders, under the helper\'s id, with its pick metadata', () => {
    drawAll()
    render(
      <div {...uiLocation('apps.library.app-list')} data-testid="list">
        <button type="button" {...guidePick('Command Bar')}>Command Bar</button>
        <div {...guidePickOf('Command Bar')}><button type="button" {...uiLocation('apps.library.tile-uninstall')}>Uninstall</button></div>
      </div>,
    )
    const list = screen.getByTestId('list')
    expect(registeredCopies('location', 'apps.library.app-list')).toEqual([list])
    expect(liveTarget('apps.library.tile-uninstall').status).toBe('pointable')
    expect(pickCandidate(list, () => true, 'Command Bar')).toBe(screen.getByText('Command Bar'))
    expect(closestRegistered(screen.getByText('Uninstall'), 'pickOf')?.id).toBe('Command Bar')
    expect(registeredWithin('pick', list)).toHaveLength(1)
  })

  it('an unmount removes the registration', async () => {
    const { unmount } = render(<button type="button" {...uiLocation('schedule.delete')}>Delete</button>)
    expect(registeredCopies('location', 'schedule.delete')).toHaveLength(1)
    const before = registrySize()
    unmount()
    await flush()
    expect(registeredCopies('location', 'schedule.delete')).toEqual([])
    // Pruned, not merely filtered out: nothing of the unmounted element is held.
    expect(registrySize()).toBe(before - 1)
  })

  it('a StrictMode double mount leaves exactly one entry', async () => {
    render(<StrictMode><button type="button" {...uiLocation('schedule.delete')}>Delete</button></StrictMode>)
    await flush()
    expect(registeredCopies('location', 'schedule.delete')).toHaveLength(1)
  })

  it('the element\'s own ref still receives the node', () => {
    let seen: Element | null = null
    render(<button type="button" {...uiLocation('schedule.delete', el => { seen = el })}>Delete</button>)
    expect(seen).toBe(screen.getByText('Delete'))
    expect(registeredCopies('location', 'schedule.delete')).toEqual([seen])
  })

  it('a conditional marker dropped while React keeps the node no longer resolves, and an unmount leaves nothing', async () => {
    const start = registrySize()
    function Row({ on }: { on: boolean }) {
      return (
        <div data-testid="item" {...(on ? guidePick('Ops') : {})}>
          <button type="button" data-testid="row" {...(on ? guideTarget('settings.sub.channels.slack') : {})}>Row</button>
        </div>
      )
    }
    const { rerender, unmount } = render(<Row on />)
    const row = screen.getByTestId('row')
    expect(registeredCopies('target', 'settings.sub.channels.slack')).toEqual([row])
    const item = screen.getByTestId('item')
    expect(registeredId(item, 'pick')).toBe('Ops')
    rerender(<Row on={false} />)
    await flush()
    // The same node, still in the document: its registration went with the spread.
    expect(screen.getByTestId('row')).toBe(row)
    expect(registeredCopies('target', 'settings.sub.channels.slack')).toEqual([])
    expect(registeredId(item, 'pick')).toBeUndefined()
    rerender(<Row on />)
    expect(registeredCopies('target', 'settings.sub.channels.slack')).toEqual([row])
    unmount()
    await flush()
    expect(registrySize()).toBe(start)
  })

  it('the coverage proof catches every identity kind a forwarding component drops, not only locations', () => {
    // A spread whose ref a component swallowed: the attribute lands, the registration does not.
    const dropRef = <P extends { ref?: unknown }>(p: P) => { const { ref: _ref, ...rest } = p; return rest }
    render(
      <>
        <div {...dropRef(guidePick('Ops'))}>Ops</div>
        <div {...dropRef(guidePickOf('Ops'))}>menu</div>
        <button type="button" {...dropRef(guideAnchor('mcp.servers-tab'))}>tab</button>
        <button type="button" {...dropRef(guideConfirm())}>Delete</button>
        <button type="button" data-ui-auto="auto:x:y:z">auto</button>
      </>,
    )
    expect(unregisteredMarkers().sort()).toEqual([
      'button[data-guide-anchor="mcp.servers-tab"]',
      'button[data-guide-confirm=""]',
      'button[data-ui-auto="auto:x:y:z"]',
      'div[data-guide-pick-of="Ops"]',
      'div[data-guide-pick="Ops"]',
    ])
  })

  it('re-renders keep one entry per element, not one per render', async () => {
    const start = registrySize()
    const { rerender } = render(<button type="button" {...uiLocation('schedule.delete')}>A</button>)
    for (let i = 0; i < 5; i++) rerender(<button type="button" {...uiLocation('schedule.delete')}>{`A${i}`}</button>)
    await flush()
    expect(registrySize()).toBe(start + 1)
  })
})
