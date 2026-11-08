/**
 * GraphView truncation — emoji-safe label cuts (#24727).
 *
 * Two sites cut a title with a UTF-16 `.slice(0, n)`: the FOCUS-tree node title
 * (26-unit cap + ellipsis) and the jump-to datalist <option> (50-unit cap, no
 * ellipsis). A cut that lands on the high surrogate of a straddling emoji
 * leaves a lone surrogate that renders as U+FFFD. These tests pin that a cut
 * which would split '🎉' drops it whole, and a cut that ends exactly after it
 * keeps it. Harness mirrors GraphView.cov80.test.tsx.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { DepsResponse } from '../api'

const openRef = vi.hoisted(() => vi.fn())
const depsFn = vi.hoisted(() => vi.fn())
const roCallbacks = vi.hoisted(() => ({ current: [] as Array<(entries: unknown[]) => void> }))

// Context carries NO issue/pull rows, so every node's title resolves purely
// from the deps payload's node map — the titles these tests control.
vi.mock('../context', () => ({
  useIssueRadar: () => ({
    active: { owner: 'zzq', repo: 'fabric', provider: 'github', host: 'github.com' },
    issues: [],
    pulls: [],
    openRef,
    refreshPrefs: { staleTimeMs: 60000, listPollMs: 0, pollInBackground: false },
  }),
}))

vi.mock('../api', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../api')>()
  return { ...mod, issueRadarApi: { ...mod.issueRadarApi, deps: depsFn } }
})

import GraphView from './GraphView'

const EMOJI = '🎉' // U+1F389, UTF-16 \uD83C\uDF89 (two units)
const HIGH_SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])/

/** Two open nodes 10 -> 20 (10 blocks 20), so the tree has both and both are
 *  open items (datalist options). Titles are taken verbatim from the payload. */
function fixture(title10: string, title20: string): DepsResponse {
  return {
    schema: 1,
    edges: [{ blocked: 20, blocker: 10, source: 'native' }],
    nodes: {
      '10': { kind: 'issue', state: 'open', title: title10 },
      '20': { kind: 'issue', state: 'open', title: title20 },
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

async function goFocus() {
  await waitFor(() =>
    expect(screen.getByRole('tab', { name: /FRONTIER/i })).toHaveAttribute('aria-selected', 'true'),
  )
  fireEvent.click(screen.getByRole('tab', { name: /FOCUS TREE/i }))
  await waitFor(() => expect(screen.getByRole('img')).toBeInTheDocument())
}

/** Text of every SVG <text> node currently rendered in the tree. */
function svgTextContents(container: HTMLElement): string[] {
  return [...container.querySelectorAll('text')].map((t) => t.textContent ?? '')
}

/** Displayed text of every jump datalist <option>. */
function optionTexts(container: HTMLElement): string[] {
  return [...container.querySelectorAll('#ir-graph-open-items option')].map((o) => o.textContent ?? '')
}

beforeEach(() => {
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

describe('GraphView node title (26-unit cap) is emoji-safe', () => {
  it('drops an emoji the 26-unit title cap would cut in half', async () => {
    // 25 filler units + emoji at units 25,26 => slice(0,26) splits the pair.
    const title = 'a'.repeat(25) + EMOJI + 'tail'
    depsFn.mockResolvedValue(fixture(title, 'short'))
    const { container } = renderView()
    await goFocus()
    const expected = 'a'.repeat(25) + '…'
    await waitFor(() => expect(svgTextContents(container)).toContain(expected))
    const texts = svgTextContents(container)
    expect(texts).toContain(expected)
    for (const t of texts) expect(t).not.toMatch(HIGH_SURROGATE)
  })

  it('keeps an emoji that ends exactly at the 26-unit title cap', async () => {
    // 24 filler units + emoji at 24,25 (ends at 26) + trailing so length > 26.
    const title = 'a'.repeat(24) + EMOJI + 'morebytes'
    depsFn.mockResolvedValue(fixture(title, 'short'))
    const { container } = renderView()
    await goFocus()
    const expected = 'a'.repeat(24) + EMOJI + '…'
    await waitFor(() => expect(svgTextContents(container)).toContain(expected))
    const texts = svgTextContents(container)
    expect(texts).toContain(expected)
    for (const t of texts) expect(t).not.toMatch(HIGH_SURROGATE)
  })
})

describe('GraphView jump <option> (50-unit cap) is emoji-safe', () => {
  it('drops an emoji the 50-unit option cap would cut in half', async () => {
    // 49 filler units + emoji at units 49,50 => slice(0,50) splits the pair.
    const title = 'b'.repeat(49) + EMOJI + 'tail'
    depsFn.mockResolvedValue(fixture('short', title))
    const { container } = renderView()
    await goFocus()
    const expected = 'b'.repeat(49) // no ellipsis at this site
    await waitFor(() => expect(optionTexts(container)).toContain(expected))
    const opts = optionTexts(container)
    expect(opts).toContain(expected)
    for (const o of opts) expect(o).not.toMatch(HIGH_SURROGATE)
  })

  it('keeps an emoji that ends exactly at the 50-unit option cap', async () => {
    // 48 filler units + emoji at 48,49 (ends at 50) + trailing so it is cut.
    const title = 'b'.repeat(48) + EMOJI + 'morebytes'
    depsFn.mockResolvedValue(fixture('short', title))
    const { container } = renderView()
    await goFocus()
    const expected = 'b'.repeat(48) + EMOJI
    await waitFor(() => expect(optionTexts(container)).toContain(expected))
    const opts = optionTexts(container)
    expect(opts).toContain(expected)
    for (const o of opts) expect(o).not.toMatch(HIGH_SURROGATE)
  })
})
