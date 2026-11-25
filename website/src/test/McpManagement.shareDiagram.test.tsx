import { fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SHARE_DIAGRAM_SEEN_KEY, SHARING_ANCHOR_ID, SharingHero } from '../pages/settings/McpManagementHero'

const realMatchMedia = window.matchMedia

function setReducedMotion(reduce: boolean) {
  window.matchMedia = vi.fn((query: string) => ({
    matches: reduce && query.includes('prefers-reduced-motion: reduce'),
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })) as unknown as typeof window.matchMedia
}

function hero(on: boolean) {
  return (
    <SharingHero on={on} switchDisabled={false} onToggle={() => {}} staleCount={0} reloadBusy={false} onReload={() => {}} />
  )
}

const diagramName = /all chats use one copy/i

describe('first-share diagram', () => {
  beforeEach(() => {
    window.localStorage.clear()
    setReducedMotion(false)
  })
  afterEach(() => {
    window.matchMedia = realMatchMedia
  })

  it('shows the labelled diagram under the title row until sharing is first turned on', () => {
    const { rerender } = render(hero(false))
    const diagram = screen.getByRole('img', { name: diagramName })
    const toggle = screen.getByRole('switch')
    // The switch sits in the title row, above the diagram, not under it.
    expect(toggle.compareDocumentPosition(diagram) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(diagram).toHaveTextContent('Sharing off')
    expect(diagram).toHaveTextContent('Sharing on')

    rerender(hero(true))
    expect(screen.queryByRole('img', { name: diagramName })).toBeNull()
    expect(window.localStorage.getItem(SHARE_DIAGRAM_SEEN_KEY)).toBe('1')
  })

  it('plays the merge: each chat starts with its own server', () => {
    render(hero(false))
    const diagram = screen.getByRole('img', { name: diagramName })
    expect(diagram).toHaveAttribute('data-motion', 'play')
    expect(screen.getAllByTestId('share-diagram-own-server')).toHaveLength(3)
  })

  it('draws the still Off/On pair under reduced motion', () => {
    setReducedMotion(true)
    render(hero(false))
    const diagram = screen.getByRole('img', { name: diagramName })
    expect(diagram).toHaveAttribute('data-motion', 'still')
    // No travelling servers: the On panel is drawn settled, its shared server lit.
    expect(screen.queryByTestId('share-diagram-own-server')).toBeNull()
    expect(screen.getByTestId('share-diagram-halo')).toBeInTheDocument()
  })

  it('names the glyphs: a label under the shared server and a Chat / MCP server legend', () => {
    render(hero(false))
    const diagram = screen.getByRole('img', { name: diagramName })
    expect(diagram).toHaveTextContent('Chat')
    // Once under the shared server, once in the legend: one key for both.
    expect(diagram.textContent?.match(/MCP server/g)).toHaveLength(2)
  })

  it('shows the memory bars: three copies off; on, three shrink to one with two saved', () => {
    render(hero(false))
    const off = screen.getByTestId('share-diagram-bar-off')
    const on = screen.getByTestId('share-diagram-bar-on')
    const count = (bar: HTMLElement, attr: string, value: string) =>
      bar.querySelectorAll(`[data-${attr}="${value}"]`).length
    expect(count(off, 'segment', 'filled')).toBe(3)
    expect(off).toHaveTextContent('3 copies')
    // Start of the merge: all three ON segments are filled.
    expect(count(on, 'start', 'filled')).toBe(3)
    // End: one copy filled, two freed and labelled saved.
    expect(count(on, 'segment', 'filled')).toBe(1)
    expect(count(on, 'segment', 'freed')).toBe(2)
    expect(on).toHaveTextContent('1 copy')
    // The freed segments are dashed outlines; the saved pill ends the play.
    expect(on.querySelectorAll('[data-segment="freed"][data-outline="dashed"]')).toHaveLength(2)
    expect(off.querySelectorAll('[data-outline="dashed"]')).toHaveLength(0)
    expect(within(on).getByTestId('share-diagram-saved')).toHaveTextContent('saved')
    expect(on).not.toHaveTextContent('·')
  })

  it('draws still memory bars under reduced motion', () => {
    setReducedMotion(true)
    render(hero(false))
    const on = screen.getByTestId('share-diagram-bar-on')
    // No starting state: the freed segments are outlines from the first frame.
    expect(on.querySelectorAll('[data-start="filled"]')).toHaveLength(1)
    expect(on).not.toHaveTextContent('3 copies')
    expect(on).toHaveTextContent('1 copy')
    expect(within(on).getByTestId('share-diagram-saved')).toHaveTextContent('saved')
    expect(on.querySelectorAll('[data-outline="dashed"]')).toHaveLength(2)
    expect(screen.getByTestId('share-diagram-bar-off')).toHaveTextContent('3 copies')
  })

  it('toggles from the info button: hides, then shows and replays', () => {
    render(hero(false))
    const info = screen.getByRole('button', { name: /how sharing works/i })
    expect(screen.getByRole('img', { name: diagramName })).toHaveAttribute('data-run', '0')

    fireEvent.click(info)
    expect(screen.queryByRole('img', { name: diagramName })).toBeNull()
    expect(info).toHaveAttribute('aria-expanded', 'false')

    fireEvent.click(info)
    expect(info).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('img', { name: diagramName })).toHaveAttribute('data-run', '1')
    // The replay restarts the merge from the Off state.
    expect(screen.getAllByTestId('share-diagram-own-server')).toHaveLength(3)
  })

  it('gives the sharing card a stable anchor and names the switch by its result', () => {
    const { container } = render(hero(false))
    const card = container.querySelector(`#${SHARING_ANCHOR_ID}`)
    expect(SHARING_ANCHOR_ID).toBe('mcp-sharing')
    expect(card).not.toBeNull()
    expect(card).toContainElement(screen.getByRole('switch', { name: 'Turn on MCP server sharing' }))
  })

  it('waits for the sharing state before showing the diagram, so an on gateway never flashes it', () => {
    const loading = (
      <SharingHero on={false} stateKnown={false} switchDisabled onToggle={() => {}} staleCount={0} reloadBusy={false} onReload={() => {}} />
    )
    const { rerender } = render(loading)
    expect(screen.queryByRole('img', { name: diagramName })).toBeNull()
    // The status arrives: sharing is already on.
    rerender(hero(true))
    expect(screen.queryByRole('img', { name: diagramName })).toBeNull()
    expect(window.localStorage.getItem(SHARE_DIAGRAM_SEEN_KEY)).toBe('1')
  })

  it('stacks the panels in a narrow pane and lets the count row wrap', () => {
    render(hero(false))
    const diagram = screen.getByRole('img', { name: diagramName })
    const grid = diagram.firstElementChild as HTMLElement
    expect(diagram.className).toContain('@container')
    expect(grid.className).toContain('grid-cols-1')
    expect(grid.className).toContain('@[18rem]:grid-cols-2')
    const pill = within(screen.getByTestId('share-diagram-bar-on')).getByTestId('share-diagram-saved')
    expect(pill.parentElement?.className).toContain('flex-wrap')
  })

  it('keeps the diagram behind the info button once sharing has been used', () => {
    window.localStorage.setItem(SHARE_DIAGRAM_SEEN_KEY, '1')
    render(hero(false))
    expect(screen.queryByRole('img', { name: diagramName })).toBeNull()

    const info = screen.getByRole('button', { name: /how sharing works/i })
    expect(info).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(info)
    expect(info).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('img', { name: diagramName })).toHaveAttribute('data-motion', 'play')
  })
})
