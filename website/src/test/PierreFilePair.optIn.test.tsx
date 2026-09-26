/**
 * The oversized pair's line-by-line opt-in must not cost the card its
 * expand/collapse control in ANY state the opted-in surface can be in
 * (issue #13430), and re-opting in after a collapse must not recompute the
 * diff.
 *
 * Harness: the REAL `pierre/index.tsx`, `pierre/PierreImpl.tsx` and
 * `pierre/workerPoolLifecycle.ts` run, so the branch that decides who draws
 * the card's header — and how the surface reacts to a pool that never starts,
 * or fails after the diff painted — is the code under test. Only the library's
 * imperative renderers are stubbed (`@pierre/diffs/react` builds custom
 * elements and shadow roots that cannot mount in jsdom); the stub records the
 * options it is handed so the "Pierre draws no header of its own" contract is
 * pinned on the props Pierre actually receives, and renders a header slot only
 * when told to, the way `renderDiffChildren` does.
 *
 * `Worker` is a fake that answers the diff worker's protocol with the worker's
 * OWN pure handler, which keeps `diffOffThread`'s cache real. Its postMessage
 * count is therefore the number of diffs actually COMPUTED, which is what the
 * no-recompute assertion reads. The same fake stands in for Pierre's highlight
 * workers, so a test can kill one and drive the real lifecycle into recovery.
 *
 * jsdom lays nothing out, so `scrollHeight` is 0 for every element and
 * `ResizeObserver` never fires — which would leave `WarmSwap` holding its
 * plain fallback forever and make every assertion below vacuously true (the
 * fallback carries a control of its own). Both are stubbed with the geometry a
 * browser reports, additively (a plain-text body has height; Pierre's rows are
 * tall once highlighted and zero before), and the observer is fired by the
 * test when the content it waits on has mounted — so the hold RELEASES here
 * exactly when it does in a browser and the assertions read the tree the
 * reader is looking at.
 */
import { act, cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ReactNode } from 'react'
import type { FileContents } from '@pierre/diffs'
import {
  DIFF_HEADER_BG_CSS,
  DIFF_HEADER_COUNT_MIN_WIDTH_CH,
  DIFF_HEADER_META_W_PX,
  DIFF_HEADER_PADDING_INLINE_PX,
  PIERRE_FILE_PAIR_MAX_LINES_PER_SIDE,
} from '../pierre/config'
import { ROW_CSS_BASE } from '../components/fileChangeChipsCss'
import { handlePairDiffRequest, type PairDiffRequest } from '../pierre/diffWorker'

const state = vi.hoisted(() => ({
  /** Diffs actually computed — one per request that reached the worker. */
  computed: 0,
  /** Whether the Pierre highlight pool can be constructed at all. */
  poolBroken: false,
  /** Pierre's highlight workers, so a test can fail the pool AFTER paint. */
  highlightWorkers: [] as Array<{ emit: (type: string, event: unknown) => void }>,
  /** Whether the mock patch surface has "painted" rows (see scrollHeight). */
  implPainted: true,
  /** Live ResizeObserver callbacks, fired by the test when geometry changes. */
  resizeCallbacks: [] as Array<() => void>,
  /** Options the mock FileDiff was last rendered with. */
  lastFileDiffOptions: undefined as Record<string, unknown> | undefined,
}))

/** Stands in for the diff worker AND for Pierre's highlight workers; the URL
 *  tells them apart. Only the diff worker has a protocol worth answering. */
class FakeWorker {
  onmessage: ((e: MessageEvent<unknown>) => void) | null = null
  onerror: ((e: unknown) => void) | null = null
  listeners = new Map<string, Set<(event: unknown) => void>>()
  terminated = false
  readonly isDiffWorker: boolean

  constructor(readonly url: URL | string, readonly options?: WorkerOptions) {
    this.isDiffWorker = String(url).includes('diffWorker')
    if (!this.isDiffWorker) state.highlightWorkers.push(this)
  }

  addEventListener(type: string, listener: (event: unknown) => void) {
    const l = this.listeners.get(type) ?? new Set()
    l.add(listener)
    this.listeners.set(type, l)
  }

  removeEventListener(type: string, listener: (event: unknown) => void) {
    this.listeners.get(type)?.delete(listener)
  }

  emit(type: string, event: unknown) {
    for (const listener of this.listeners.get(type) ?? []) listener(event)
  }

  postMessage(message: unknown) {
    if (!this.isDiffWorker) return
    state.computed++
    const response = handlePairDiffRequest(message as PairDiffRequest)
    // Asynchronous like the real worker, so the component genuinely passes
    // through its `computing` state instead of resolving inside the click.
    void Promise.resolve().then(() => {
      if (!this.terminated) this.onmessage?.({ data: response } as MessageEvent<unknown>)
    })
  }

  terminate() { this.terminated = true }
}

vi.mock('@pierre/diffs/worker', () => ({
  WorkerPoolManager: class {
    terminate = vi.fn()
    constructor(poolOptions: { poolSize: number; workerFactory: () => unknown }) {
      // A pool that cannot be built is what puts the lifecycle into its
      // `recovering` phase (short retries, then a 30s cooldown) and finally
      // `unavailable` — every one of which hands surfaces NO pool.
      if (state.poolBroken) throw new Error('highlight pool unavailable')
      Array.from({ length: poolOptions.poolSize }, () => poolOptions.workerFactory())
    }
    initialize() { return Promise.resolve() }
  },
}))

/** The library's React layer, reduced to what the slot contract guarantees:
 *  a header slot is rendered when its renderer returns non-null (and the
 *  header is enabled), exactly as `renderDiffChildren` does. */
vi.mock('@pierre/diffs/react', async () => {
  const { createContext } = await import('react')
  const slots = (props: Record<string, unknown>) => {
    const options = (props.options ?? {}) as Record<string, unknown>
    if (options.disableFileHeader === true) return null
    const call = (key: string) => {
      const fn = props[key]
      return typeof fn === 'function' ? (fn as (arg: unknown) => ReactNode)(props.fileDiff ?? props.newFile) : null
    }
    const prefix = call('renderHeaderPrefix')
    const suffix = call('renderHeaderFilenameSuffix')
    const metadata = call('renderHeaderMetadata')
    return (
      <div data-diffs-header="" data-testid="pierre-own-header">
        {prefix != null && <div data-slot="header-prefix">{prefix}</div>}
        {suffix != null && <div data-slot="header-filename-suffix">{suffix}</div>}
        {metadata != null && <div data-slot="header-metadata">{metadata}</div>}
      </div>
    )
  }
  return {
    File: (props: Record<string, unknown>) => <div data-testid="pierre-file">{slots(props)}</div>,
    FileDiff: (props: Record<string, unknown>) => {
      state.lastFileDiffOptions = props.options as Record<string, unknown> | undefined
      return (
        <div data-testid="pierre-patch">
          {slots(props)}
          {/* Real Pierre rows are ~zero height until the highlight worker
              answers; the scrollHeight stub reads `state.implPainted` to
              model that. */}
          <span data-testid="pierre-patch-hunks">hunks</span>
        </div>
      )
    },
    MultiFileDiff: (props: Record<string, unknown>) => <div data-testid="pierre-pair">{slots(props)}</div>,
    Virtualizer: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
    WorkerPoolContext: createContext<unknown>(undefined),
  }
})

const NAME = 'generatedPipeline.ts'
const lines = (count: number, patched = false) => Array.from(
  { length: count },
  (_, i) => (patched && i === 12 ? `const v${i} = ${i} /* patched */` : `const v${i} = ${i}`),
).join('\n')
const OVERSIZED = PIERRE_FILE_PAIR_MAX_LINES_PER_SIDE + 1
const oldFile: FileContents = { name: NAME, contents: lines(OVERSIZED) }
const newFile: FileContents = { name: NAME, contents: lines(OVERSIZED, true) }

/** The caller's own expand/collapse control, injected the way FileChangeChips'
 *  chevron is: into the header PREFIX slot. */
const CONTROL = 'collapse-control'
const slotProps = {
  renderHeaderPrefix: () => <button data-testid={CONTROL}>toggle</button>,
  renderHeaderFilenameSuffix: () => <span data-testid="filename-suffix" />,
  renderHeaderMetadata: () => <span data-testid="row-metadata" />,
  titleClickable: true,
}
const OPTIONS = { collapsed: false, diffStyle: 'unified' as const, overflow: 'wrap' as const, disableFileHeader: false }

/** Controls the reader can actually reach. A paint-hold mounts its children
 *  inside an `aria-hidden` zero-height box while it still shows a fallback, so
 *  counting every match would count a control nobody can click. */
const reachableControls = () =>
  screen.queryAllByTestId(CONTROL).filter(el => el.closest('[aria-hidden="true"]') == null)

/** Header rows the reader can see — the invariant is exactly one, always. */
const visibleHeaders = (container: HTMLElement) =>
  [...container.querySelectorAll('[data-diffs-header]')].filter(el => el.closest('[aria-hidden="true"]') == null)

async function loadPierreFilePair() {
  const { PierreFilePair } = await import('../pierre')
  return PierreFilePair
}

/** Geometry the paint-hold measures, modelled on what a browser reports and
 *  ADDITIVE like a real box: a plain-text body has its own height, and diff
 *  rows are tall once the highlight worker has answered and zero before. */
const PAINTED_PX = 240
const TEXT_PX = 120
let scrollHeightSpy: ReturnType<typeof vi.spyOn> | undefined

function stubScrollHeight() {
  scrollHeightSpy = vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockImplementation(function (this: HTMLElement) {
    let h = 0
    if (this.querySelector('pre')) h += TEXT_PX
    if (this.querySelector('[data-testid="pierre-patch-hunks"]') && state.implPainted) h += PAINTED_PX
    return h
  })
}

/** A browser's ResizeObserver fires when the observed box changes size; here
 *  the test fires the LIVE observers once the content they wait on has
 *  mounted. Disconnected observers are gone, as in a browser. */
const fireResize = () => act(() => { for (const cb of [...state.resizeCallbacks]) cb() })

class FakeResizeObserver {
  constructor(private readonly cb: () => void) {}
  observe() { if (!state.resizeCallbacks.includes(this.cb)) state.resizeCallbacks.push(this.cb) }
  unobserve() { this.disconnect() }
  disconnect() {
    const i = state.resizeCallbacks.indexOf(this.cb)
    if (i >= 0) state.resizeCallbacks.splice(i, 1)
  }
}

beforeEach(() => {
  state.computed = 0
  state.poolBroken = false
  state.highlightWorkers.length = 0
  state.implPainted = true
  state.resizeCallbacks.length = 0
  state.lastFileDiffOptions = undefined
  vi.resetModules()
  vi.stubGlobal('Worker', FakeWorker)
  vi.stubGlobal('ResizeObserver', FakeResizeObserver)
  stubScrollHeight()
  vi.spyOn(console, 'warn').mockImplementation(() => {})
  vi.spyOn(console, 'error').mockImplementation(() => {})
  localStorage.clear()
})

afterEach(() => {
  cleanup()
  scrollHeightSpy?.mockRestore()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

/** Opt the rendered pair in and let the hold release on the painted rows. */
async function optInAndPaint(container: HTMLElement) {
  const user = userEvent.setup()
  await user.click(screen.getByRole('button', { name: 'Show line-by-line diff' }))
  expect(await screen.findByTestId('pierre-patch')).toBeInTheDocument()
  fireResize()
  await vi.waitFor(() => {
    expect(container.querySelector('[data-pierre-plain-side]')).not.toBeInTheDocument()
  })
}

describe('oversized pair: line-by-line opt-in keeps the card controllable', () => {
  it('keeps exactly one header and one control once the computed patch paints', async () => {
    const PierreFilePair = await loadPierreFilePair()
    const { container } = render(
      <PierreFilePair oldFile={oldFile} newFile={newFile} options={OPTIONS} {...slotProps} />,
    )
    expect(reachableControls()).toHaveLength(1)
    expect(visibleHeaders(container)).toHaveLength(1)

    await optInAndPaint(container)

    expect(reachableControls()).toHaveLength(1)
    expect(visibleHeaders(container)).toHaveLength(1)
    // The header is this component's row, not Pierre's: Pierre is told to
    // draw the body only, so there is no second header for the control to
    // vanish into when Pierre has nothing to draw.
    expect(state.lastFileDiffOptions?.disableFileHeader).toBe(true)
    expect(screen.queryByTestId('pierre-own-header')).not.toBeInTheDocument()
    expect(screen.getByTestId('row-metadata')).toBeInTheDocument()
    // What Pierre's header used to carry moves into this row: exact ± counts
    // from the computed patch (one changed line here), and the filename's
    // open-file cue when the caller says the title is clickable.
    const header = visibleHeaders(container)[0]
    expect(header.querySelector('[data-deletions-count]')).toHaveTextContent('-1')
    expect(header.querySelector('[data-additions-count]')).toHaveTextContent('+1')
    expect(header.querySelector('[data-title]')).toHaveClass('cursor-pointer')
  })

  it('shows no open-file cue on the filename when the caller cannot open files', async () => {
    const PierreFilePair = await loadPierreFilePair()
    const { container } = render(
      <PierreFilePair oldFile={oldFile} newFile={newFile} options={OPTIONS} {...slotProps} titleClickable={false} />,
    )
    expect(container.querySelector('[data-title]')).not.toHaveClass('cursor-pointer')
    await optInAndPaint(container)
    expect(container.querySelector('[data-title]')).not.toHaveClass('cursor-pointer')
  })

  it('counts a removed `-- comment` line in the kept header (the patch marks it `--- comment`)', async () => {
    // The worker's real producer prefixes a removed `-- note` with `-`, which
    // makes it look like a file header to a prefix check; the header must still
    // read the removal it is.
    const commented = Array.from({ length: OVERSIZED }, (_, i) => (i === 12 ? `-- retired note ${i}` : `const v${i} = ${i}`)).join('\n')
    const withoutComment = Array.from({ length: OVERSIZED }, (_, i) => `const v${i} = ${i}`).filter((_, i) => i !== 12).join('\n')
    const PierreFilePair = await loadPierreFilePair()
    const { container } = render(
      <PierreFilePair
        oldFile={{ name: 'query.sql', contents: commented }}
        newFile={{ name: 'query.sql', contents: withoutComment }}
        options={OPTIONS}
        {...slotProps}
      />,
    )
    await optInAndPaint(container)
    const header = visibleHeaders(container)[0]
    expect(header.querySelector('[data-deletions-count]')).toHaveTextContent('-1')
    expect(header.querySelector('[data-additions-count]')).not.toBeInTheDocument()
  })

  it('styles the kept header with the same values FileChangeChips injects into Pierre\u2019s header', async () => {
    // The `unsafeCSS` rules are scoped to Pierre's shadow root and cannot reach
    // a light-DOM row, so the row applies the same numbers inline — read from
    // the one place both sides import them, so the two headers cannot drift.
    expect(ROW_CSS_BASE).toContain(`[data-diffs-header]{background-color:${DIFF_HEADER_BG_CSS}}`)
    expect(ROW_CSS_BASE).toContain(`[data-diffs-header]{padding-inline:${DIFF_HEADER_PADDING_INLINE_PX}px}`)
    expect(ROW_CSS_BASE).toContain(`min-width:${DIFF_HEADER_COUNT_MIN_WIDTH_CH}ch;text-align:right`)
    expect(ROW_CSS_BASE).toContain(`[data-metadata]{flex:0 0 ${DIFF_HEADER_META_W_PX}px;justify-content:space-between}`)
    const PierreFilePair = await loadPierreFilePair()
    const { container } = render(
      <PierreFilePair oldFile={oldFile} newFile={newFile} options={OPTIONS} {...slotProps} />,
    )
    const readHeader = () => {
      const header = visibleHeaders(container)[0] as HTMLElement
      const group = header.querySelector<HTMLElement>('[data-metadata]')
      return {
        paddingInline: header.style.paddingInline,
        groupFlex: group?.style.flex,
        groupJustify: group?.style.justifyContent,
        counts: [...header.querySelectorAll<HTMLElement>('[data-deletions-count],[data-additions-count]')]
          .map(el => `${el.style.minWidth}/${el.style.textAlign}`),
      }
    }
    // The background is a `color-mix()` value jsdom's CSSOM drops, so it is
    // pinned above through ROW_CSS_BASE and compared as a computed colour
    // against Pierre's own header by the browser harness
    // (scripts/capture-diff-card-line-by-line.mjs).
    const before = readHeader()
    expect(before.groupFlex).toBe(`0 0 ${DIFF_HEADER_META_W_PX}px`)
    expect(before.groupJustify).toBe('space-between')
    expect(before.paddingInline).toBe(`${DIFF_HEADER_PADDING_INLINE_PX}px`)
    await optInAndPaint(container)
    const after = readHeader()
    expect(after).toMatchObject({ paddingInline: before.paddingInline, groupFlex: before.groupFlex, groupJustify: before.groupJustify })
    expect(after.counts).toEqual([`${DIFF_HEADER_COUNT_MIN_WIDTH_CH}ch/right`, `${DIFF_HEADER_COUNT_MIN_WIDTH_CH}ch/right`])
  })

  it('keeps the control when the highlight pool never starts', async () => {
    state.poolBroken = true
    const user = userEvent.setup()
    const PierreFilePair = await loadPierreFilePair()
    const { container } = render(
      <PierreFilePair oldFile={oldFile} newFile={newFile} options={OPTIONS} {...slotProps} />,
    )

    // The fallback state already offers the control; opting in must not take it.
    expect(reachableControls()).toHaveLength(1)
    await user.click(screen.getByRole('button', { name: 'Show line-by-line diff' }))

    // The computed patch arrives as readable text even with no pool …
    await vi.waitFor(() => { expect(container.textContent).toContain('@@') })
    fireResize()
    // … the plain two-side hold releases on that text …
    await vi.waitFor(() => {
      expect(container.querySelector('[data-pierre-plain-side]')).not.toBeInTheDocument()
    })
    // … and the card is still collapsible, which is the whole regression.
    expect(reachableControls()).toHaveLength(1)
    expect(visibleHeaders(container)).toHaveLength(1)
  })

  it('keeps the control when the pool fails AFTER the diff painted', async () => {
    const PierreFilePair = await loadPierreFilePair()
    const { container } = render(
      <PierreFilePair oldFile={oldFile} newFile={newFile} options={OPTIONS} {...slotProps} />,
    )
    await optInAndPaint(container)
    expect(reachableControls()).toHaveLength(1)
    expect(state.highlightWorkers.length).toBeGreaterThan(0)

    // A highlight worker dies: the lifecycle publishes `recovering` with no
    // generation, and the patch surface drops to header-less plain text.
    act(() => { state.highlightWorkers[0].emit('error', { message: 'boom' }) })

    expect(screen.queryByTestId('pierre-patch')).not.toBeInTheDocument()
    expect(container.textContent).toContain('@@')
    expect(reachableControls()).toHaveLength(1)
    expect(visibleHeaders(container)).toHaveLength(1)
  })

  it('keeps the control when the reader switches to plain diffs after opting in', async () => {
    const PierreFilePair = await loadPierreFilePair()
    const { PLAIN_DIFF_KEY } = await import('../hooks/usePlainDiff')
    const { container } = render(
      <PierreFilePair oldFile={oldFile} newFile={newFile} options={OPTIONS} {...slotProps} />,
    )
    await optInAndPaint(container)

    // Settings → Display → plain diffs, flipped live: the persisted bool
    // broadcasts to same-tab siblings on `mc:persisted-bool`, so every surface
    // re-renders without remounting (the exact path a live toggle takes).
    act(() => {
      localStorage.setItem(PLAIN_DIFF_KEY, '1')
      window.dispatchEvent(new CustomEvent('mc:persisted-bool', { detail: { key: PLAIN_DIFF_KEY, value: true } }))
    })

    await vi.waitFor(() => {
      expect(screen.queryByTestId('pierre-patch')).not.toBeInTheDocument()
    })
    expect(container.textContent).toContain('@@')
    expect(reachableControls()).toHaveLength(1)
    expect(visibleHeaders(container)).toHaveLength(1)
  })

  it('holds the plain view until the diff has painted rows, then swaps the body under one header', async () => {
    state.implPainted = false
    const user = userEvent.setup()
    const PierreFilePair = await loadPierreFilePair()
    const { container } = render(
      <PierreFilePair oldFile={oldFile} newFile={newFile} options={OPTIONS} {...slotProps} />,
    )
    await user.click(screen.getByRole('button', { name: 'Show line-by-line diff' }))
    expect(await screen.findByTestId('pierre-patch')).toBeInTheDocument()

    // The patch surface is mounted but its rows have no height yet: the held
    // plain view must still own the layout, with its own header and control,
    // and the ready-state header must not be on screen yet — it would double
    // the row the fallback is already showing.
    fireResize()
    expect(container.querySelector('[data-pierre-plain-side]')).toBeInTheDocument()
    expect(container.querySelector('[aria-live="polite"]')).toHaveTextContent(/Computing line-by-line diff/)
    expect(reachableControls()).toHaveLength(1)
    expect(visibleHeaders(container)).toHaveLength(1)

    // Rows paint: the hold releases, the body swaps, the header count holds.
    state.implPainted = true
    fireResize()
    await vi.waitFor(() => {
      expect(container.querySelector('[data-pierre-plain-side]')).not.toBeInTheDocument()
    })
    expect(reachableControls()).toHaveLength(1)
    const headers = visibleHeaders(container)
    expect(headers).toHaveLength(1)
    // The header sits ABOVE the measured box, never inside it, so its own
    // height can never pass for painted rows (the hold measures the box).
    const box = headers[0].nextElementSibling
    expect(box).not.toBeNull()
    expect(box!.querySelector('[data-testid="pierre-patch"]')).not.toBeNull()
  })

  it('does not recompute the diff when the reader opts in again after a collapse', async () => {
    const user = userEvent.setup()
    const PierreFilePair = await loadPierreFilePair()
    const first = render(<PierreFilePair oldFile={oldFile} newFile={newFile} options={OPTIONS} {...slotProps} />)

    await optInAndPaint(first.container)
    expect(state.computed).toBe(1)

    // Collapsing the row unmounts the surface (FileChangeChips swaps in its
    // lightweight header), so a remount is what re-expanding does. The opt-in
    // is component-local and starts over — the patch does not.
    first.unmount()
    const second = render(<PierreFilePair oldFile={oldFile} newFile={newFile} options={OPTIONS} {...slotProps} />)
    expect(reachableControls()).toHaveLength(1)
    expect(state.computed).toBe(1)

    await user.click(screen.getByRole('button', { name: 'Show line-by-line diff' }))
    expect(await screen.findByTestId('pierre-patch')).toBeInTheDocument()
    fireResize()
    expect(reachableControls()).toHaveLength(1)
    expect(visibleHeaders(second.container)).toHaveLength(1)
    // Served from diffOffThread's cache: no second worker round-trip.
    expect(state.computed).toBe(1)
  })

  it('does not opt a pair in that nobody asked to see line by line', async () => {
    const PierreFilePair = await loadPierreFilePair()
    const other: FileContents = { name: 'other.ts', contents: lines(OVERSIZED, true) }
    await act(async () => {
      render(<PierreFilePair oldFile={oldFile} newFile={other} options={OPTIONS} {...slotProps} />)
    })

    expect(screen.getByRole('button', { name: 'Show line-by-line diff' })).toBeInTheDocument()
    expect(screen.queryByTestId('pierre-patch')).not.toBeInTheDocument()
    expect(state.computed).toBe(0)
  })
})
