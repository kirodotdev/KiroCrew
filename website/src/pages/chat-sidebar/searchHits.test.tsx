/**
 * The search lane's pieces at their interfaces: what one search reply keeps, which
 * lane an active query draws, and what a row's tail shows with and without a snippet.
 */
import type { ReactNode } from 'react'
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { sidebarSearchHits } from './search'
import { renderedLane } from './lanes'
import { makeSidebarSearchView, SearchHitTail, SidebarSearchContext } from './searchHits'
import type { Slot } from './types'

const local = (key: string): Slot => ({ key, title: key, running: false }) as Slot
const peer = (key: string): Slot => ({ key, title: key, running: false, peer_id: 'inst-a', row_identity: `inst-a:${key}` }) as Slot

describe('sidebarSearchHits', () => {
  it('keeps the rank and the trimmed snippet per local key, first hit winning', () => {
    const hits = sidebarSearchHits([
      { key: 'dashboard_a', snippet: '  …the memory store…  ' },
      { key: 'b' },
      { key: 'a', snippet: 'a weaker duplicate' },
      { key: 'c', snippet: '   ' },
    ], null)
    expect([...hits.ranks]).toEqual([['a', 0], ['b', 1], ['c', 3]])
    expect([...hits.snippets]).toEqual([['a', '…the memory store…']])
  })

  it('drops a remote row, whose key names a session on another gateway', () => {
    const hits = sidebarSearchHits([{ key: 'a', snippet: 'peer text', instance_id: 'inst-a' }], null)
    expect(hits.ranks.size).toBe(0)
    expect(hits.snippets.size).toBe(0)
  })
})

describe('renderedLane while searching', () => {
  const base = { boardColumns: 0, folders: 2, lineageAvailable: true }
  it('draws the flat lane in place of the folder tree', () => {
    expect(renderedLane({ ...base, lane: 'tree', searching: true })).toBe('flat')
    expect(renderedLane({ ...base, folders: 0, lane: 'tree', searching: true })).toBe('flat')
    // A conductor preference with no edges falls to the tree, so it searches flat too.
    expect(renderedLane({ ...base, lineageAvailable: false, lane: 'conductor', searching: true })).toBe('flat')
  })
  it('keeps the conductor lane (which flattens itself), the board, and every lane once cleared', () => {
    expect(renderedLane({ ...base, lane: 'conductor', searching: true })).toBe('conductor')
    expect(renderedLane({ ...base, boardColumns: 1, lane: 'tree', searching: true })).toBe('board')
    expect(renderedLane({ ...base, lane: 'tree', searching: false })).toBe('tree')
    expect(renderedLane({ ...base, lane: 'conductor' })).toBe('conductor')
  })
})

describe('makeSidebarSearchView', () => {
  it('never hands a peer row the snippet or folder of a local key it collides with', () => {
    const view = makeSidebarSearchView(' mem ', new Map([['k', 'memory']]), { k: 'f1' }, new Map([['f1', 'Writing']]))
    expect(view.query).toBe('mem')
    expect(view.snippetOf(local('k'))).toBe('memory')
    expect(view.folderLabelOf(local('k'))).toBe('Writing')
    expect(view.snippetOf(peer('k'))).toBeUndefined()
    expect(view.folderLabelOf(peer('k'))).toBeUndefined()
  })
})

describe('SearchHitTail', () => {
  const preview = <div data-testid="preview">last message</div>
  const state = <div data-testid="state">Needs approval</div>
  const dot = <span data-testid="dot" />
  const view = makeSidebarSearchView('memory', new Map([['k', '…the memory store…']]), {}, new Map())
  const tail = (slot: Slot, t: ReactNode, v = view) => render(
    <SidebarSearchContext.Provider value={v}>
      <SearchHitTail slot={slot} tail={t} preview={preview} dot={dot} />
    </SidebarSearchContext.Provider>,
  )

  it('replaces the last-message preview with the snippet, query highlighted, dot kept', () => {
    const { queryByTestId, container } = tail(local('k'), preview)
    expect(queryByTestId('preview')).toBeNull()
    expect(queryByTestId('dot')).not.toBeNull()
    expect(container.querySelector('[data-session-search-snippet]')?.textContent).toBe('…the memory store…')
    expect(container.querySelector('[data-session-search-snippet] strong')?.textContent).toBe('memory')
  })

  it('keeps a state line as the one tail line, drawing no snippet beside it', () => {
    const { queryByTestId, container } = tail(local('k'), state)
    expect(queryByTestId('state')).not.toBeNull()
    expect(container.querySelector('[data-session-search-snippet]')).toBeNull()
    expect(container.children).toHaveLength(1)
  })

  it('falls back to the preview for a row with no snippet, and outside a search', () => {
    // Queried per container: both renders share the document body.
    expect(tail(local('title-only'), preview).container.querySelector('[data-testid="preview"]')).not.toBeNull()
    const outside = render(<SearchHitTail slot={local('k')} tail={preview} preview={preview} dot={dot} />)
    expect(outside.container.querySelector('[data-testid="preview"]')).not.toBeNull()
    expect(outside.container.querySelector('[data-session-search-snippet]')).toBeNull()
  })
})
