/**
 * The browser rail must not split a narrow file panel.
 *
 * A 240-300px rail seated as a flex sibling inside a ~320px panel leaves the
 * editor a few dozen pixels wide, so below `RAIL_SPLIT_MIN_W` the rail is taken
 * out of flow and floated over the content. Both cases render the SAME rail node
 * with the same state -- only its placement changes -- so the assertion is on
 * whether an out-of-flow wrapper is present.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../pierre', () => ({
  PierreCode: () => null,
  PierrePatch: () => null,
  PierreFilePair: () => null,
  PierreEditor: () => null,
  fenceLanguage: () => 'text',
}))

const { default: MarkdownPanel } = await import('../components/MarkdownPanel')

/** Drives the ResizeObserver the panel measures itself with. */
let emit: ((w: number) => void) | null = null

beforeEach(() => {
  emit = null
  vi.stubGlobal('ResizeObserver', class {
    constructor(private cb: ResizeObserverCallback) {
      emit = (width: number) =>
        this.cb([{ contentRect: { width } } as ResizeObserverEntry], this as unknown as ResizeObserver)
    }
    observe() {}
    disconnect() {}
    unobserve() {}
  })
})
afterEach(() => vi.unstubAllGlobals())

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <MarkdownPanel
          embedded
          filePath="/tmp/notes.md"
          content="# Title"
          onContentChange={vi.fn()}
          onSave={vi.fn(async () => {})}
          onClose={vi.fn()}
          initialDiffMode={false}
          railOpen
          onRailToggle={vi.fn()}
          browserRail={<div data-testid="rail">rail</div>}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const railWrapper = () => screen.getByTestId('rail').parentElement!

describe('MarkdownPanel browser rail placement', () => {
  it('seats the rail in flow when the panel is wide', async () => {
    mount()
    emit!(1200)
    await waitFor(() => expect(railWrapper().className).not.toMatch(/absolute/))
  })

  it('floats the rail over the content when the panel is too narrow to split', async () => {
    mount()
    emit!(320)
    await waitFor(() => {
      const cls = railWrapper().className
      expect(cls).toMatch(/absolute/)
      expect(cls).toMatch(/right-0/)
    })
  })
})
