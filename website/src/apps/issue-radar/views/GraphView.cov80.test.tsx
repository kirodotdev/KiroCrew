/**
 * GraphView — the dependency dashboard tab (#5187), FRONTIER + FOCUS rewrite.
 *
 * Covers the states a viewer reaches: loading, unavailable (retry), empty, the
 * FRONTIER list (newly-unlocked + waiting rows, expandable WHY, open-tree), the
 * FOCUS tree (component render, re-root on click, Enter opens in-app, jump-to
 * input), and the narrow branch (defaults to FRONTIER, tree still reachable).
 * The deps fetch and app context are mocked; the layout math is covered by
 * lib/deps.test.ts.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { DepsResponse } from '../api'

const openRef = vi.hoisted(() => vi.fn())
const depsFn = vi.hoisted(() => vi.fn())
const roCallbacks = vi.hoisted(() => ({ current: [] as Array<(entries: unknown[]) => void> }))

vi.mock('../context', () => ({
  useIssueRadar: () => ({
    active: { owner: 'zzq', repo: 'fabric', provider: 'github', host: 'github.com' },
    issues: [
      { number: 10, title: 'dependent issue', state: 'open' },
      { number: 20, title: 'ready issue', state: 'open' },
      { number: 30, title: 'downstream issue', state: 'open' },
    ],
    pulls: [
      // 5 is merged, so 10's only blocker is satisfied → 10 is newly unlocked.
      { number: 5, title: 'blocker PR', url: '', state: 'closed', draft: false, labels: [], updated_at: '', merged_at: '2026-08-23T01:00:00Z' },
    ],
    openRef,
    refreshPrefs: { staleTimeMs: 60000, listPollMs: 0, pollInBackground: false },
  }),
}))

vi.mock('../api', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../api')>()
  return { ...mod, issueRadarApi: { ...mod.issueRadarApi, deps: depsFn } }
})

import GraphView from './GraphView'

// 5 (merged PR) blocks 10; 10 blocks 30; 20 blocks 30 and 20 is open.
// So: 10 newly unlocked (blocker 5 merged), 30 waiting (blockers 10 & 20 open).
function fixture(): DepsResponse {
  return {
    schema: 1,
    edges: [
      { blocked: 10, blocker: 5, source: 'native' },
      { blocked: 30, blocker: 10, source: 'native' },
      { blocked: 30, blocker: 20, source: 'inferred' },
    ],
    nodes: {
      '5': { kind: 'pull', state: 'merged', title: 'blocker PR' },
      '10': { kind: 'issue', state: 'open', title: 'dependent issue' },
      '20': { kind: 'issue', state: 'open', title: 'ready issue' },
      '30': { kind: 'issue', state: 'open', title: 'downstream issue' },
    },
  }
}

function renderView() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <GraphView />
    </QueryClientProvider>,
  )
}

async function frontierReady() {
  await waitFor(() =>
    expect(screen.getByRole('tab', { name: /FRONTIER/i })).toHaveAttribute('aria-selected', 'true'),
  )
}

beforeEach(() => {
  // jsdom reports zero-size boxes; the shell reads its width to pick the narrow
  // branch, so a wide default keeps the tab full-width. The narrow test drives
  // the ResizeObserver callback with a small width instead.
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
    width: 1280, height: 800, top: 0, left: 0, right: 1280, bottom: 800, x: 0, y: 0,
    toJSON: () => ({}),
  } as DOMRect)
  vi.stubGlobal(
    'ResizeObserver',
    class {
      cb: (entries: unknown[]) => void
      constructor(cb: (entries: unknown[]) => void) {
        this.cb = cb
        roCallbacks.current.push(cb)
      }
      observe() {}
      disconnect() {}
    },
  )
  vi.stubGlobal(
    'matchMedia',
    vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
  )
  depsFn.mockReset()
  openRef.mockReset()
  roCallbacks.current = []
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('GraphView states', () => {
  it('shows the loading line while the deps query is in flight', () => {
    depsFn.mockReturnValue(new Promise(() => {}))
    renderView()
    expect(screen.getByText(/Loading dependencies/i)).toBeInTheDocument()
  })

  it('renders the unavailable board on error and retries on the button', async () => {
    depsFn.mockRejectedValueOnce(new Error('zzq boom')).mockResolvedValueOnce(fixture())
    renderView()
    await waitFor(() =>
      expect(screen.getByText(/Dependency data unavailable/i)).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByRole('button', { name: /RETRY/i }))
    await frontierReady()
    expect(depsFn).toHaveBeenCalledTimes(2)
  })

  it('renders the empty board when the repo has no edges', async () => {
    depsFn.mockResolvedValue({ schema: 1, edges: [], nodes: {} })
    renderView()
    await waitFor(() => expect(screen.getByText(/No dependencies yet/i)).toBeInTheDocument())
  })
})

describe('GraphView FRONTIER', () => {
  it('lists newly unlocked and waiting rows and expands the WHY line', async () => {
    depsFn.mockResolvedValue(fixture())
    renderView()
    await frontierReady()
    // newly unlocked: IS-10 (blocker 5 merged)
    const unlocked = screen.getByRole('button', { name: /IS-10/ })
    expect(unlocked).toBeInTheDocument()
    // waiting: IS-30
    expect(screen.getByRole('button', { name: /IS-30/ })).toBeInTheDocument()
    // expand the unlocked row's WHY line
    fireEvent.click(unlocked)
    await waitFor(() =>
      expect(screen.getByText(/All blockers satisfied/i)).toBeInTheDocument(),
    )
    // the WHY line offers an open-tree affordance
    expect(screen.getByRole('button', { name: /open tree/i })).toBeInTheDocument()
  })

  it('expands a waiting row and shows what it is waiting on', async () => {
    depsFn.mockResolvedValue(fixture())
    renderView()
    await frontierReady()
    fireEvent.click(screen.getByRole('button', { name: /IS-30/ }))
    await waitFor(() => expect(screen.getByText(/Waiting on/i)).toBeInTheDocument())
  })

  it('opens the FOCUS tree from a row and refreshes on the header button', async () => {
    depsFn.mockResolvedValue(fixture())
    renderView()
    await frontierReady()
    fireEvent.click(screen.getByRole('button', { name: /IS-10/ }))
    fireEvent.click(await screen.findByRole('button', { name: /open tree/i }))
    // FOCUS renders an SVG tree
    await waitFor(() => expect(screen.getByRole('img')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /Refresh dependency data/i }))
    await waitFor(() => expect(depsFn.mock.calls.length).toBeGreaterThan(1))
  })
})

describe('GraphView FOCUS tree', () => {
  async function goFocus() {
    depsFn.mockResolvedValue(fixture())
    renderView()
    await frontierReady()
    fireEvent.click(screen.getByRole('tab', { name: /FOCUS TREE/i }))
    await waitFor(() => expect(screen.getByRole('img')).toBeInTheDocument())
  }

  it('renders the component nodes and re-roots on a node click', async () => {
    await goFocus()
    const nodes = screen.getAllByRole('button', { name: /IS-10|IS-20|IS-30/ })
    expect(nodes.length).toBeGreaterThan(0)
    fireEvent.click(nodes[0]) // re-root, still an SVG tree
    expect(screen.getByRole('img')).toBeInTheDocument()
  })

  it('opens a node in-app on Enter', async () => {
    await goFocus()
    const node = screen.getAllByRole('button', { name: /IS-10/ })[0]
    fireEvent.keyDown(node, { key: 'Enter' })
    expect(openRef).toHaveBeenCalled()
  })

  it('re-roots from the jump-to-issue input on Enter', async () => {
    await goFocus()
    const input = screen.getByRole('combobox', { name: /Jump to issue/i })
    fireEvent.change(input, { target: { value: '30' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    // still rendering a tree after the jump
    expect(screen.getByRole('img')).toBeInTheDocument()
  })
})

describe('GraphView FRONTIER overflow + jump', () => {
  // One shared open blocker (900) gates N downstream issues, so every downstream
  // is a WAITING row. With N > WAITING_CAP (30) the tail is hidden until the
  // disclosure control expands the list.
  function bigWaitingFixture(n: number): DepsResponse {
    const edges: DepsResponse['edges'] = []
    const nodes: DepsResponse['nodes'] = {
      '900': { kind: 'issue', state: 'open', title: 'shared blocker' },
    }
    for (let i = 0; i < n; i++) {
      const id = 100 + i
      nodes[String(id)] = { kind: 'issue', state: 'open', title: `waiting item ${id}` }
      edges.push({ blocked: id, blocker: 900, source: 'native' })
    }
    return { schema: 1, edges, nodes }
  }

  it('expands the waiting list to reveal a row beyond the cap', async () => {
    // 40 waiting rows: ids 100..139. WAITING_CAP is 30, so IS-135 is hidden.
    depsFn.mockResolvedValue(bigWaitingFixture(40))
    renderView()
    await frontierReady()
    // a row within the cap is present; a row past it is not, until expanded
    expect(screen.getByRole('button', { name: /IS-100\b/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /IS-135\b/ })).not.toBeInTheDocument()
    // the overflow control is a real button reporting collapsed state
    const disclosure = screen.getByRole('button', { name: /\+\d+ more/ })
    expect(disclosure).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(disclosure)
    // the hidden row is now revealed, and the control flips to expanded
    await waitFor(() => expect(screen.getByRole('button', { name: /IS-135\b/ })).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /Show fewer/i })).toHaveAttribute('aria-expanded', 'true')
  })

  it('FRONTIER jump reaches a waiting row that sits inside the overflow', async () => {
    Element.prototype.scrollIntoView = vi.fn()
    depsFn.mockResolvedValue(bigWaitingFixture(40))
    renderView()
    await frontierReady()
    // IS-138 is past the cap and hidden on load
    expect(screen.queryByRole('button', { name: /IS-138\b/ })).not.toBeInTheDocument()
    const input = screen.getByRole('combobox', { name: /Jump to issue/i })
    fireEvent.change(input, { target: { value: '138' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    // the jump expands the overflow so the target row is now in the DOM
    await waitFor(() => expect(screen.getByRole('button', { name: /IS-138\b/ })).toBeInTheDocument())
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled()
  })

  it('FRONTIER jump falls back to the tree for an item with no frontier row', async () => {
    // The picker's datalist is every OPEN item, which is a superset of the rows
    // FRONTIER draws: 900 is the shared blocker, so it is neither unblocked nor
    // blocked-and-listed and has no row here. Picking it must still do something
    // -- otherwise the input offers a suggestion that silently does nothing.
    Element.prototype.scrollIntoView = vi.fn()
    depsFn.mockResolvedValue(bigWaitingFixture(40))
    renderView()
    await frontierReady()
    expect(screen.queryByRole('button', { name: /IS-900\b/ })).not.toBeInTheDocument()
    const input = screen.getByRole('combobox', { name: /Jump to issue/i })
    fireEvent.change(input, { target: { value: '900' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    // It answers by opening the focus tree rooted there, rather than no-opping.
    await waitFor(() => expect(screen.getByRole('img')).toBeInTheDocument())
  })

  it('a second jump inside the ring window clears the first row ring', async () => {
    // GPT round 2: the effect cleanup cancelled the pending removal timer without
    // removing the attribute, so jumping A then B within REVEAL_RING_MS left A
    // ringed forever -- two highlighted rows, one of them lying about the match.
    Element.prototype.scrollIntoView = vi.fn()
    depsFn.mockResolvedValue(bigWaitingFixture(40))
    renderView()
    await frontierReady()
    const input = screen.getByRole('combobox', { name: /Jump to issue/i })

    fireEvent.change(input, { target: { value: '100' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    const rowA = await waitFor(() => {
      const el = document.querySelector('[data-ir-reveal="1"]')
      expect(el).not.toBeNull()
      return el as Element
    })

    // Second jump well inside the 1.6s window, so the timer never fired.
    fireEvent.change(input, { target: { value: '101' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    // Exactly one row is ringed, and it is not the first one.
    await waitFor(() => {
      const ringed = document.querySelectorAll('[data-ir-reveal="1"]')
      expect(ringed).toHaveLength(1)
      expect(ringed[0]).not.toBe(rowA)
    })
  })
})

describe('GraphView narrow branch', () => {
  it('drops to FRONTIER when the pane goes narrow', async () => {
    depsFn.mockResolvedValue(fixture())
    renderView()
    await frontierReady()
    // move to FOCUS, then shrink the pane: the tab returns to FRONTIER
    fireEvent.click(screen.getByRole('tab', { name: /FOCUS TREE/i }))
    await waitFor(() => expect(screen.getByRole('img')).toBeInTheDocument())
    act(() => {
      for (const cb of roCallbacks.current) cb([{ contentRect: { width: 480 } }])
    })
    await waitFor(() =>
      expect(screen.getByRole('tab', { name: /FRONTIER/i })).toHaveAttribute('aria-selected', 'true'),
    )
  })
})
