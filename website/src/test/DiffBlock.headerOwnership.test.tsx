/**
 * A chat diff block's Open / Copy controls must be reachable for the block's
 * whole life, whatever the highlight worker pool does after the diff painted.
 *
 * Harness: the REAL `components/DiffBlock.tsx`, `pierre/index.tsx`,
 * `pierre/PierreImpl.tsx` and `pierre/workerPoolLifecycle.ts` run, so the code
 * that decides who draws the block's header — and how Pierre's patch surface
 * reacts to a pool that fails after paint — is the code under test. Only the
 * library's imperative renderers are stubbed (`@pierre/diffs/react` builds
 * custom elements and shadow roots that cannot mount here); the stub records
 * the options Pierre is handed and renders a header slot only when told to,
 * the way `renderDiffChildren` does. `Worker` is a fake that stands in for the
 * highlight workers, so a test can kill one and drive the real lifecycle into
 * its recovery phase — the state in which `PierrePatchImpl` drops to
 * header-less plain text.
 *
 * Nothing lays out here, so `scrollHeight` is 0 everywhere and a
 * `ResizeObserver` never fires; both are stubbed with the geometry a browser
 * reports so `WarmSwap` reveals the impl the way it does on screen. Controls
 * inside a hidden warm-up box are not counted: a reader cannot reach them.
 */
import { act, cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ReactNode } from 'react'

const state = vi.hoisted(() => ({
  /** Pierre's highlight workers, so a test can fail the pool AFTER paint. */
  highlightWorkers: [] as Array<{ emit: (type: string, event: unknown) => void }>,
  /** Live ResizeObserver callbacks, fired by the test when geometry changes. */
  resizeCallbacks: [] as Array<() => void>,
  /** Options the mock FileDiff was last rendered with. */
  lastFileDiffOptions: undefined as Record<string, unknown> | undefined,
  /** What the Copy control handed to the clipboard. */
  copied: [] as string[],
}))

/** Stands in for Pierre's highlight workers; nothing here answers a protocol. */
class FakeWorker {
  listeners = new Map<string, Set<(event: unknown) => void>>()

  constructor(readonly url: URL | string, readonly options?: WorkerOptions) {
    state.highlightWorkers.push(this)
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

  postMessage() {}

  terminate() {}
}

vi.mock('@pierre/diffs/worker', () => ({
  WorkerPoolManager: class {
    terminate = vi.fn()
    constructor(poolOptions: { poolSize: number; workerFactory: () => unknown }) {
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
    const fn = props.renderHeaderMetadata
    const metadata = typeof fn === 'function' ? (fn as () => ReactNode)() : null
    return (
      <div data-diffs-header="" data-testid="pierre-own-header">
        {metadata != null && <div data-slot="header-metadata">{metadata}</div>}
      </div>
    )
  }
  return {
    File: () => <div data-testid="pierre-file" />,
    FileDiff: (props: Record<string, unknown>) => {
      state.lastFileDiffOptions = props.options as Record<string, unknown> | undefined
      return (
        <div data-testid="pierre-patch">
          {slots(props)}
          <span data-testid="pierre-patch-hunks">hunks</span>
        </div>
      )
    },
    MultiFileDiff: () => <div data-testid="pierre-pair" />,
    Virtualizer: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
    WorkerPoolContext: createContext<unknown>(undefined),
  }
})

vi.mock('../utils/clipboard', () => ({
  copyToClipboard: (text: string) => {
    state.copied.push(text)
    return Promise.resolve(true)
  },
}))

const PATCH = `--- a/file.ts
+++ b/file.ts
@@ -1,3 +1,4 @@
 const a = 1
-const b = 2
+const b = 3
+const c = 4
 const d = 5`

const OPEN_NAME = /^Open .* in side panel$/

/** Two files as `git diff` writes them: a modification, then a rename that also
 *  changes the basename and adds a line. */
const MULTI_FILE_PATCH = `diff --git a/src/a.ts b/src/a.ts
index 1111111..2222222 100644
--- a/src/a.ts
+++ b/src/a.ts
@@ -1,2 +1,2 @@
-const a = 1
+const a = 2
 export { a }
diff --git a/src/old.ts b/src/new.ts
similarity index 90%
rename from src/old.ts
rename to src/new.ts
--- a/src/old.ts
+++ b/src/new.ts
@@ -1,1 +1,2 @@
 const n = 1
+const m = 2`

/** Controls the reader can actually reach: a warm-up box mounts the impl inside
 *  an `aria-hidden` zero-height box until it paints, so counting every match
 *  would count a control nobody can click. */
const reachable = (els: HTMLElement[]) => els.filter(el => el.closest('[aria-hidden="true"]') == null)
const reachableCopy = () => reachable(screen.queryAllByTitle('Copy patch'))
const reachableOpen = () => reachable(screen.queryAllByTitle(OPEN_NAME))

/** Header rows the reader can see — the invariant is exactly one, always. */
const visibleHeaders = (container: HTMLElement) =>
  [...container.querySelectorAll<HTMLElement>('[data-diffs-header]')].filter(el => el.closest('[aria-hidden="true"]') == null)

async function loadDiffBlock() {
  const { default: DiffBlock } = await import('../components/DiffBlock')
  return DiffBlock
}

/** Geometry the warm-up measures, modelled on what a browser reports and
 *  ADDITIVE like a real box: a plain-text body has its own height, and diff
 *  rows are tall once the highlight worker has answered. */
const PAINTED_PX = 240
const TEXT_PX = 120
let scrollHeightSpy: ReturnType<typeof vi.spyOn> | undefined

/** Pierre's painted bodies the reader can see (`FileDiff` stub instances). */
const paintedBodies = () => screen.queryAllByTestId('pierre-patch')

/** Kill the pool the way a real crash does — an `error` event on the highlight
 *  workers — and require Pierre's patch surfaces to have dropped to plain text
 *  before returning. EVERY fake worker is failed, not `highlightWorkers[0]`:
 *  the live generation's workers are among them whatever else a slow runner
 *  let land in the array first, and a failure reported to a lifecycle no
 *  mounted surface reads changes nothing. The lifecycle publishes
 *  `recovering` synchronously; the wait is for React's commit of that store
 *  update, bounded by `waitFor`, never a fixed timer. */
async function failHighlightPool() {
  expect(state.highlightWorkers.length).toBeGreaterThan(0)
  act(() => { for (const worker of state.highlightWorkers) worker.emit('error', { message: 'boom' }) })
  await vi.waitFor(() => {
    expect(
      paintedBodies(),
      `Pierre still painted after every one of ${state.highlightWorkers.length} highlight workers failed`,
    ).toHaveLength(0)
  })
}

function stubScrollHeight() {
  scrollHeightSpy = vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockImplementation(function (this: HTMLElement) {
    let h = 0
    if (this.querySelector('pre')) h += TEXT_PX
    if (this.querySelector('[data-testid="pierre-patch-hunks"]')) h += PAINTED_PX
    return h
  })
}

/** A browser's ResizeObserver fires when the observed box changes size; here
 *  the test fires the LIVE observers once the content they wait on has
 *  mounted. */
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
  state.highlightWorkers.length = 0
  state.resizeCallbacks.length = 0
  state.lastFileDiffOptions = undefined
  state.copied.length = 0
  vi.resetModules()
  vi.stubGlobal('Worker', FakeWorker)
  vi.stubGlobal('ResizeObserver', FakeResizeObserver)
  // The Open affordance is offered once a HEAD probe says the file exists.
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({ ok: true })))
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

/** Mount a complete, within-budget block and let Pierre paint it. */
async function renderPainted(onFileOpen: (path: string) => void) {
  const DiffBlock = await loadDiffBlock()
  const view = render(<DiffBlock code={PATCH} complete onFileOpen={onFileOpen} />)
  expect(await screen.findByTestId('pierre-patch')).toBeInTheDocument()
  fireResize()
  await vi.waitFor(() => {
    expect(reachableCopy()).toHaveLength(1)
    expect(reachableOpen()).toHaveLength(1)
  })
  expect(state.highlightWorkers.length).toBeGreaterThan(0)
  return view
}

describe('diff block: the header survives the highlight pool', () => {
  it('keeps Open and Copy reachable and working after the pool fails post-paint', async () => {
    const onFileOpen = vi.fn()
    const { container } = await renderPainted(onFileOpen)
    expect(visibleHeaders(container)).toHaveLength(1)

    // A highlight worker dies AFTER the diff painted: the lifecycle publishes
    // `recovering` with no generation and Pierre's patch surface drops to
    // header-less plain text — the recover path.
    await failHighlightPool()
    expect(container.textContent).toContain('-const b = 2')

    // The block's controls are not Pierre's to take away.
    expect(reachableCopy()).toHaveLength(1)
    expect(reachableOpen()).toHaveLength(1)
    expect(visibleHeaders(container)).toHaveLength(1)

    // … and they still do their jobs.
    const user = userEvent.setup()
    await user.click(reachableOpen()[0])
    expect(onFileOpen).toHaveBeenCalledWith('file.ts')
    await user.click(reachableCopy()[0])
    expect(state.copied).toEqual([PATCH])
  })

  /** The fallback `WarmSwap` holds while Pierre mounts must have the SAME shape
   *  as the plain text Pierre prints when its pool is down: the hold measures
   *  the impl's own height against the held fallback, so a verbatim hold over a
   *  hunks-only impl body released at the wrong height. Same shape — but the
   *  hold does not report: the toggle it would gate still shapes the diff that
   *  is about to land, and the row says nothing about a plain view. */
  it('holds a fallback of the same hunks-only shape as the degraded body, without reporting it', async () => {
    const DiffBlock = await loadDiffBlock()
    const { container } = render(<DiffBlock code={PATCH} complete onFileOpen={vi.fn()} />)
    // The impl has mounted inside the hidden warm-up box; the fallback is what
    // the reader sees until the box measures painted rows.
    expect(await screen.findByTestId('pierre-patch')).toBeInTheDocument()
    const held = container.querySelector('pre.pierre-plain')
    expect(held).not.toBeNull()
    expect(held?.closest('[aria-hidden="true"]')).toBeNull()
    expect(held).toHaveTextContent('-const b = 2')
    for (const plumbing of ['--- a/file.ts', '+++ b/file.ts', '@@ -1,3 +1,4 @@']) {
      expect(held?.textContent).not.toContain(plumbing)
    }
    expect(visibleHeaders(container)[0]).not.toHaveTextContent('Plain view')
    expect(reachable(screen.getAllByTitle('Unified view'))[0]).toBeEnabled()
    // Painted: the fallback leaves; the row is unchanged.
    fireResize()
    await vi.waitFor(() => expect(container.querySelector('pre.pierre-plain')).toBeNull())
    expect(reachable(screen.getAllByTitle('Unified view'))[0]).toBeEnabled()
  })

  /** The plain body Pierre shows while its pool is down sits under the block's
   *  own row, which already says which file and how many lines. So the body
   *  prints the hunks' content only — no `---`/`+++`/`@@` plumbing under a
   *  tidy row — the row says why it is plain, and the split/unified toggle, a
   *  Pierre layout option that changes nothing over plain text, is withheld
   *  the way it is while frames stream: with Open offered, the row is Open and
   *  Copy, two actions (`max-two-buttons-per-row`), and the label carries the
   *  why. Copy still hands out the whole patch. */
  it('under a dead pool prints the hunks only, labels the row and withholds the layout toggle so the row keeps two actions', async () => {
    const { container } = await renderPainted(vi.fn())
    const row = () => visibleHeaders(container)[0]
    // Painted: no label, and the toggle is live (unseeded default is split).
    expect(row().querySelector('[data-label]')).toBeNull()
    const live = reachable(screen.getAllByTitle('Unified view'))[0]
    expect(live).toBeEnabled()
    expect(row().querySelectorAll('button')).toHaveLength(3)

    await failHighlightPool()
    const body = container.querySelector('pre.pierre-plain')
    expect(body).not.toBeNull()
    expect(body).toHaveTextContent('-const b = 2')
    expect(body).toHaveTextContent('+const c = 4')
    for (const plumbing of ['--- a/file.ts', '+++ b/file.ts', '@@ -1,3 +1,4 @@']) {
      expect(body?.textContent).not.toContain(plumbing)
    }
    expect(row().querySelector('[data-label]')).toHaveTextContent('No highlighting — plain view')
    // Open + Copy, and nothing else: the toggle is not in the row in any form.
    expect(screen.queryByTitle('Unified view')).not.toBeInTheDocument()
    expect(screen.queryByTitle('Split view')).not.toBeInTheDocument()
    expect(container.querySelector('[aria-disabled]')).toBeNull()
    const buttons = [...row().querySelectorAll('button')].map(b => b.getAttribute('title') ?? '')
    expect(buttons).toHaveLength(2)
    expect(buttons[0]).toMatch(OPEN_NAME)
    expect(buttons[1]).toBe('Copy patch')
    // The Copy control is untouched by what the body shows.
    const user = userEvent.setup()
    await user.click(reachableCopy()[0])
    expect(state.copied).toEqual([PATCH])
  })

  /** A rename that keeps every byte has no hunk and no `---`/`+++` pair, and a
   *  binary or mode-only entry has no hunk either. Each is a file the patch
   *  carries, so each gets its own row, named the way Pierre's header names
   *  it — the rename by both files — and the row says in words what Pierre's
   *  change icon said: a binary change, a mode change (never as mode digits).
   *  A card of several files opens with a card-level row: what the card
   *  holds, the whole card's counts, and the controls that act on the whole
   *  card; the file rows beneath it carry their own file and counts only. */
  it('gives a 100% rename, a binary entry and a mode-only entry their own rows, named and noted, under a card row', async () => {
    const DiffBlock = await loadDiffBlock()
    const patch = `diff --git a/src/a.ts b/src/a.ts
index 1111111..2222222 100644
--- a/src/a.ts
+++ b/src/a.ts
@@ -1 +1 @@
-const a = 1
+const a = 2
diff --git a/src/old.ts b/src/new.ts
similarity index 100%
rename from src/old.ts
rename to src/new.ts
diff --git a/assets/logo.png b/assets/logo.png
index 3333333..4444444 100644
Binary files a/assets/logo.png and b/assets/logo.png differ
diff --git a/bin/run.sh b/bin/run.sh
old mode 100644
new mode 100755`
    const { container } = render(<DiffBlock code={patch} complete onFileOpen={vi.fn()} />)
    // Pierre parses a hunk-less entry as one file with no hunks, so it draws a
    // body for every entry; waiting for all four also means the lazy chunk has
    // resolved before this case ends.
    expect(await screen.findAllByTestId('pierre-patch')).toHaveLength(4)
    fireResize()
    await vi.waitFor(() => expect(reachableCopy()).toHaveLength(1))
    const titles = () => visibleHeaders(container).map(row => row.querySelector('[data-title]')?.textContent)
    expect(titles()).toEqual(['4 files', 'a.ts', 'old.ts → new.ts', 'logo.png', 'run.sh'])
    // The card row counts the whole card; the modification is the only file
    // with counts to show; the others say what they are instead.
    const counted = visibleHeaders(container).map(row => row.querySelector('[data-metadata]') != null)
    expect(counted).toEqual([true, true, false, false, false])
    const notes = () => visibleHeaders(container).map(row => row.querySelector('[data-change-note]')?.textContent ?? null)
    expect(notes()).toEqual([null, null, null, 'binary', 'now executable'])
    expect(container.textContent).not.toMatch(/100644|100755/)
    // The controls act on the whole card, so they ride the card row.
    expect(reachableCopy()[0].closest('[data-diffs-header]')).toBe(visibleHeaders(container)[0])

    // Pool dead: the rows stay, the notes with them; the plain bodies print
    // what each entry has to say and nothing the row already says. The 100%
    // rename, the binary change and the chmod have nothing left, so each is
    // the row alone — as it is when Pierre draws it.
    await failHighlightPool()
    expect(titles()).toEqual(['4 files', 'a.ts', 'old.ts → new.ts', 'logo.png', 'run.sh'])
    expect(notes()).toEqual([null, null, null, 'binary', 'now executable'])
    const bodies = [...container.querySelectorAll('pre.pierre-plain')].map(pre => pre.textContent)
    expect(bodies).toEqual(['-const a = 1\n+const a = 2'])
  })

  /** A chmod beside an edit: the row states the mode change in words and the
   *  body — plain or highlighted — is the edit. Neither the mode nor the
   *  content is lost, and no mode digits reach the reader. */
  it('states a mode change in words on the row of an entry that also has content', async () => {
    const DiffBlock = await loadDiffBlock()
    const patch = `diff --git a/run.sh b/run.sh
old mode 100755
new mode 100644
--- a/run.sh
+++ b/run.sh
@@ -1 +1 @@
-echo one
+echo two`
    const { container } = render(<DiffBlock code={patch} complete onFileOpen={vi.fn()} />)
    expect(await screen.findAllByTestId('pierre-patch')).toHaveLength(1)
    fireResize()
    await vi.waitFor(() => expect(reachableCopy()).toHaveLength(1))
    const row = () => visibleHeaders(container)[0]
    expect(row().querySelector('[data-title]')).toHaveTextContent('run.sh')
    expect(row().querySelector('[data-change-note]')).toHaveTextContent('no longer executable')
    expect(row().querySelector('[data-deletions-count]')).toHaveTextContent('-1')
    expect(row().querySelector('[data-additions-count]')).toHaveTextContent('+1')
    expect(container.textContent).not.toMatch(/100644|100755/)

    await failHighlightPool()
    expect(row().querySelector('[data-change-note]')).toHaveTextContent('no longer executable')
    expect(container.querySelector('pre.pierre-plain')).toHaveTextContent('-echo one +echo two')
    expect(container.querySelector('pre.pierre-plain')?.textContent).not.toContain('mode')
  })

  /** An added file and an additions-only edit both read `name +N`; a deleted
   *  file and a file emptied in place both read `name −N`. Pierre's header
   *  told them apart with its change icon; the row says it in a word. A
   *  rename is told by its title: the basenames when they differ, the full
   *  paths when they do not — a move between directories would otherwise read
   *  `a.ts → a.ts`, collapsed to `a.ts`, and say nothing. */
  it('says added or deleted on the row, and lets a rename be told by its title, full paths for a move', async () => {
    const DiffBlock = await loadDiffBlock()
    const patch = `diff --git a/src/new.ts b/src/new.ts
new file mode 100644
--- /dev/null
+++ b/src/new.ts
@@ -0,0 +1 @@
+export const n = 1
diff --git a/src/gone.ts b/src/gone.ts
deleted file mode 100644
--- a/src/gone.ts
+++ /dev/null
@@ -1 +0,0 @@
-export const g = 1
diff --git a/src/before.ts b/src/after.ts
similarity index 90%
rename from src/before.ts
rename to src/after.ts
--- a/src/before.ts
+++ b/src/after.ts
@@ -1 +1 @@
-export const b = 1
+export const b = 2
diff --git a/src/util.ts b/test/util.ts
similarity index 100%
rename from src/util.ts
rename to test/util.ts`
    const { container } = render(<DiffBlock code={patch} complete onFileOpen={vi.fn()} />)
    expect(await screen.findAllByTestId('pierre-patch')).toHaveLength(4)
    fireResize()
    await vi.waitFor(() => expect(reachableCopy()).toHaveLength(1))
    const rows = visibleHeaders(container)
    expect(rows.map(row => row.querySelector('[data-title]')?.textContent)).toEqual(['4 files', 'new.ts', 'gone.ts', 'before.ts → after.ts', 'src/util.ts → test/util.ts'])
    expect(rows.map(row => row.querySelector('[data-change-note]')?.textContent ?? null)).toEqual([null, 'added', 'deleted', null, null])
  })

  /** The row must fit a 320px pane in its fullest state — plain-view label,
   *  a change note, the controls and the counts on one row — so the title
   *  group wraps (the note and the label drop under the name instead of three
   *  items truncating side by side) and every text item in it can shrink and
   *  truncate; a `shrink-0` item there pushes the row past the card instead.
   *  jsdom lays nothing out, so this pins the class contract the repo's other
   *  narrow-viewport tests pin; the PR's 320px frames are the layout evidence. */
  it('lets the header row wrap and every text item of it shrink and truncate for a 320px pane', async () => {
    const DiffBlock = await loadDiffBlock()
    const patch = `diff --git a/scripts/capture-the-whole-transcript.sh b/scripts/capture-the-whole-transcript.sh
old mode 100644
new mode 100755
--- a/scripts/capture-the-whole-transcript.sh
+++ b/scripts/capture-the-whole-transcript.sh
@@ -1 +1 @@
-echo one
+echo two`
    const { container } = render(<DiffBlock code={patch} complete onFileOpen={vi.fn()} />)
    expect(await screen.findAllByTestId('pierre-patch')).toHaveLength(1)
    fireResize()
    await vi.waitFor(() => expect(reachableCopy()).toHaveLength(1))
    await failHighlightPool()
    const row = visibleHeaders(container)[0]
    const group = row.firstElementChild as HTMLElement
    expect(group.className).toContain('min-w-0')
    expect(group.className).toContain('flex-1')
    expect(group.className).toContain('flex-wrap')
    const items = ['[data-title]', '[data-change-note]', '[data-label]'].map(sel => row.querySelector<HTMLElement>(sel))
    for (const item of items) {
      expect(item).not.toBeNull()
      const cls = item!.className
      expect(cls, cls).toContain('truncate')
      expect(cls, cls).toContain('min-w-0')
      expect(cls, cls).not.toContain('shrink-0')
    }
    // Truncated text keeps its whole wording reachable as the item's tooltip.
    expect(row.querySelector('[data-change-note]')).toHaveAttribute('title', 'now executable')
    expect(row.querySelector('[data-label]')).toHaveAttribute('title', 'No highlighting — plain view')
  })

  it('draws the header itself and tells Pierre to draw the body only', async () => {
    const { container } = await renderPainted(vi.fn())
    // One header, the block's own: Pierre receives `disableFileHeader: true`, so
    // there is no second header for the controls to vanish into when Pierre
    // has nothing to draw.
    expect(state.lastFileDiffOptions?.disableFileHeader).toBe(true)
    expect(screen.queryByTestId('pierre-own-header')).not.toBeInTheDocument()
    const headers = visibleHeaders(container)
    expect(headers).toHaveLength(1)
    // The row carries what Pierre's header carries elsewhere: the basename and
    // the exact ± counts read off the patch.
    expect(headers[0].querySelector('[data-title]')).toHaveTextContent('file.ts')
    expect(headers[0].querySelector('[data-deletions-count]')).toHaveTextContent('-1')
    expect(headers[0].querySelector('[data-additions-count]')).toHaveTextContent('+2')
    // The controls sit in that row, not inside Pierre's surface.
    expect(reachableCopy()[0].closest('[data-diffs-header]')).toBe(headers[0])
  })

  /** A patch may carry several files: Pierre's own headers named each one, with
   *  its own counts and a rename's old name. The block's rows must, too — one
   *  per file — under a card-level row that says how many files the card
   *  holds, counts the whole card and carries the controls that act on the
   *  whole card (Open, layout, Copy), so a reader can tell the card's numbers
   *  from one file's. All of it stays through a pool death exactly like the
   *  single-file row, and the card row is the one that says "Plain view". */
  it('gives every file of a multi-file patch its own row under a card row with the totals and the controls, and keeps them through a pool death', async () => {
    const DiffBlock = await loadDiffBlock()
    const { container } = render(<DiffBlock code={MULTI_FILE_PATCH} complete onFileOpen={vi.fn()} />)
    expect(await screen.findAllByTestId('pierre-patch')).toHaveLength(2)
    fireResize()
    await vi.waitFor(() => expect(reachableCopy()).toHaveLength(1))

    const rows = () => visibleHeaders(container)
    const titles = () => rows().map(row => row.querySelector('[data-title]')?.textContent)
    const counts = (row: HTMLElement) => [
      row.querySelector('[data-deletions-count]')?.textContent ?? null,
      row.querySelector('[data-additions-count]')?.textContent ?? null,
    ]
    expect(titles()).toEqual(['2 files', 'a.ts', 'old.ts → new.ts'])
    expect(counts(rows()[0])).toEqual(['-1', '+2'])
    expect(counts(rows()[1])).toEqual(['-1', '+1'])
    expect(counts(rows()[2])).toEqual([null, '+1'])
    // Open / Copy / layout act on the whole patch, so they ride the card row;
    // a file row carries its file and its counts only.
    expect(reachableOpen()).toHaveLength(1)
    expect(reachableCopy()[0].closest('[data-diffs-header]')).toBe(rows()[0])
    expect(reachable(screen.getAllByTitle('Unified view'))[0].closest('[data-diffs-header]')).toBe(rows()[0])
    expect(rows()[1].querySelector('button')).toBeNull()
    expect(rows()[2].querySelector('button')).toBeNull()

    await failHighlightPool()
    expect(titles()).toEqual(['2 files', 'a.ts', 'old.ts → new.ts'])
    expect(reachableCopy()).toHaveLength(1)
    expect(reachableOpen()).toHaveLength(1)
    expect(rows()[0].querySelector('[data-label]')).toHaveTextContent('No highlighting — plain view')
    expect(rows()[1].querySelector('[data-label]')).toBeNull()
    // The toggle is withheld over the plain bodies: the card row is Open + Copy.
    expect(screen.queryByTitle('Unified view')).not.toBeInTheDocument()
    expect(rows()[0].querySelectorAll('button')).toHaveLength(2)
  })
})
