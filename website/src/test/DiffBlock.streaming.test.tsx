import { describe, it, vi, beforeEach, expect } from 'vitest'
import { render, screen, act, within } from '@testing-library/react'
import type { ComponentProps, ReactElement } from 'react'

/** A stand-in `PierrePatch` the header-ownership cases drive by hand. `null`
 *  leaves the REAL one in place, which every other case here needs: they
 *  exercise Pierre's own parser on partial frames, and a mock would prove
 *  nothing. */
const pierreDouble = vi.hoisted(() => ({
  render: null as null | ((props: { patch: string; options?: { disableFileHeader?: boolean } }) => ReactElement),
}))
vi.mock('../pierre', async importOriginal => {
  const actual = await importOriginal<typeof import('../pierre')>()
  const Real = actual.PierrePatch
  return {
    ...actual,
    PierrePatch: (props: ComponentProps<typeof Real>) => (pierreDouble.render ? pierreDouble.render(props) : <Real {...props} />),
  }
})

import DiffBlock from '../components/DiffBlock'
import { i18nT } from '../i18n/t'

beforeEach(() => {
  globalThis.fetch = vi.fn(() => Promise.resolve({ ok: true })) as unknown as typeof fetch
  pierreDouble.render = null
})

const fullPatch = `--- /home/user/example/src/greet.py
+++ /home/user/example/src/greet.py
@@ -1,5 +1,7 @@
 def greet(name):
-    print("Hello " + name)
+    if not name:
+        raise ValueError("name is required")
+    print(f"Hello {name}")
 
 
-greet("world")
+greet("Krish")
`

/** Every streaming prefix of a chat diff block must render without throwing.
 *  Pierre's PatchDiff itself asserts exactly-one-file-diff and THROWS on the
 *  partial frames a streaming fence produces (bare header lines, no hunk yet)
 *  — the wrapper must absorb those states rather than crash-looping the
 *  per-message error boundary.
 *
 *  Reaching that throw requires the lazy chunk to be RESOLVED: a render that
 *  is unmounted in the same tick only ever shows the Suspense fallback, so
 *  Pierre's parser is never entered and the suite proves nothing about the
 *  claim above. `warmPierre()` resolves the chunk once (module registry keeps
 *  it resolved for the rest of the file), and each prefix then flushes so
 *  PatchImpl actually mounts and parses before being torn down. */
async function warmPierre() {
  const { unmount } = render(<DiffBlock code={fullPatch} complete />)
  // The lazy `import('./PierreImpl')` chunk (src/pierre/index.tsx) races the
  // default findBy timeout (1000ms) under a loaded, concurrent run -- widen
  // it rather than assume the dynamic import always resolves within 1s
  // (same class as touchHoverActions.test.tsx).
  await screen.findByTitle('Copy patch', {}, { timeout: 5000 })
  unmount()
}

/** One macrotask: long enough for the resolved lazy child to commit and for
 *  Pierre's synchronous parse to run during that commit. */
const flush = () => act(() => new Promise<void>(r => setTimeout(r, 0)))

describe('DiffBlock streaming', () => {
  it('renders every streamed prefix without throwing', async () => {
    await warmPierre()
    for (let end = 1; end <= fullPatch.length; end += 7) {
      const partial = fullPatch.slice(0, end)
      const { unmount } = render(
        <DiffBlock code={partial} complete={false} />,
      )
      await flush()
      unmount()
    }
  })

  it('renders a multi-file patch without throwing', async () => {
    await warmPierre()
    const multi = fullPatch + '\n' + fullPatch.replace(/greet\.py/g, 'other.py')
    render(<DiffBlock code={multi} complete />)
    await flush()
  })

  it('renders empty and header-only content without throwing', async () => {
    await warmPierre()
    render(<DiffBlock code="" complete={false} />)
    await flush()
    render(<DiffBlock code={'--- /a/b.py\n+++ /a/b.py'} complete={false} />)
    await flush()
  })

  it('omits the layout toggle and keeps at most two header actions while streaming', async () => {
    const { container } = render(<DiffBlock code={fullPatch} complete={false} onFileOpen={vi.fn()} />)
    await flush()
    const open = within(container).getByRole('button', {
      name: i18nT('components.diffBlock.open_in_side_panel', { path: '/home/user/example/src/greet.py' }),
    })
    const actions = within(open.parentElement!)
    expect(actions.queryByRole('button', { name: i18nT('components.diffBlock.switch_to_split_view') })).toBeNull()
    expect(actions.queryByRole('button', { name: i18nT('components.diffBlock.switch_to_unified_view') })).toBeNull()
    expect(actions.getByRole('button', { name: i18nT('components.diffBlock.copy_patch') })).toBeTruthy()
    expect(actions.getAllByRole('button')).toHaveLength(2)
  })
  /** The GATE: an unfinished block must not reach Pierre at all. Pierre re-parses
   *  and re-tokenizes the WHOLE patch per frame (its cache key is content-derived,
   *  so every frame misses), which a streamed diff would otherwise pay once per
   *  chunk. `pre.pierre-plain` is the app-owned stand-in. Under the block's own
   *  row it prints the hunks' content only — the row names the file, so the
   *  `---`/`+++` lines would say it twice, as they did under the finished plain
   *  body — and the Open control carries the untouched full path, which doubles
   *  as proof that nothing shortened it on the way. */
  it('renders a streaming patch as plain text under its row, hunks only, the full path on the Open control', async () => {
    await warmPierre()
    const { container } = render(<DiffBlock code={fullPatch} complete={false} streaming onFileOpen={vi.fn()} />)
    await flush()
    // Scoped to this render: the cases above deliberately leave their trees
    // mounted, so a document-wide query can read THEIR stand-in instead.
    const plain = container.querySelector('pre.pierre-plain')
    expect(plain).not.toBeNull()
    expect(plain?.textContent).toContain('-    print("Hello " + name)')
    expect(plain?.textContent).toContain('+    print(f"Hello {name}")')
    expect(plain?.textContent).not.toContain('--- /home/user/example/src/greet.py')
    expect(plain?.textContent).not.toContain('+++ /home/user/example/src/greet.py')
    expect(plain?.textContent).not.toContain('@@ -1,5 +1,7 @@')
    // The full path survives, on the row's control: a basename-shortened path would not open.
    expect(within(container).getByRole('button', {
      name: i18nT('components.diffBlock.open_in_side_panel', { path: '/home/user/example/src/greet.py' }),
    })).toBeTruthy()
  })

  /** The gate, read at the seam: the double stands in for `PierrePatch`, so
   *  its absence while frames arrive and its presence once the block is
   *  complete is exactly whether the patch was handed to Pierre. Deleting the
   *  gate flips the first assertion. */
  it('hands the patch to Pierre only once the block is complete', async () => {
    pierreDouble.render = ({ patch }) => <div data-testid="pierre"><pre>{patch}</pre></div>
    const { container, rerender } = render(<DiffBlock code={fullPatch} complete={false} streaming />)
    await flush()
    expect(container.querySelector('[data-testid="pierre"]')).toBeNull()
    expect(container.querySelector('pre.pierre-plain')?.textContent).toContain('+    if not name:')
    rerender(<DiffBlock code={fullPatch} complete />)
    await flush()
    expect(container.querySelector('[data-testid="pierre"]')).not.toBeNull()
    expect(container.querySelector('pre.pierre-plain')).toBeNull()
  })

  /** The header row is the block's own for the block's whole life: the same
   *  row, with the same controls, on both sides of the `complete` flip — no
   *  band leaves the layout mid-read and nothing waits on Pierre drawing a
   *  header. The double below is Pierre reduced to a body with no header at
   *  all, the state its real surface is in while its chunk loads, while the
   *  worker pool starts or recovers, and for a patch it cannot parse. Pierre is
   *  told so (`disableFileHeader`), so there is never a second header for the
   *  controls to move into or vanish with. */
  it('keeps one header row with the controls across the complete flip, and lets Pierre draw the body only', async () => {
    let pierreOptions: { disableFileHeader?: boolean } | undefined
    pierreDouble.render = ({ options }) => {
      pierreOptions = options
      return <div data-testid="pierre"><pre>{'pierre body'}</pre></div>
    }
    const copyName = i18nT('components.diffBlock.copy_patch')
    const { container, rerender } = render(<DiffBlock code={fullPatch} complete={false} />)
    await flush()
    const headers = () => container.querySelectorAll('[data-diffs-header]')
    const copies = () => within(container).queryAllByRole('button', { name: copyName })
    expect(headers()).toHaveLength(1)
    expect(copies()).toHaveLength(1)
    expect(copies()[0].closest('[data-diffs-header]')).toBe(headers()[0])
    const streamingHeader = headers()[0]

    // Flip to complete: Pierre mounts cold and draws nothing but a body.
    rerender(<DiffBlock code={fullPatch} complete />)
    await flush()
    expect(container.querySelector('[data-testid="pierre"]')).not.toBeNull()
    expect(pierreOptions?.disableFileHeader).toBe(true)
    // Same element, not a replacement: the row never left the layout.
    expect(headers()).toHaveLength(1)
    expect(headers()[0]).toBe(streamingHeader)
    expect(copies()).toHaveLength(1)
    expect(copies()[0].closest('[data-diffs-header]')).toBe(streamingHeader)
    // The controls exist exactly once: Copy plus the layout toggle, which joins
    // the row once the block is final, and nothing inside Pierre's surface.
    expect(within(container).getAllByRole('button')).toHaveLength(2)
    expect(container.querySelectorAll('[data-testid="pierre"] button')).toHaveLength(0)
  })

  /** The row carries what Pierre's header would have: the basename and the
   *  ± counts read off the patch itself, while frames still arrive and after. */
  it('titles its own row with the basename and the patch’s ± counts', async () => {
    pierreDouble.render = () => <div data-testid="pierre" />
    const { container, rerender } = render(<DiffBlock code={fullPatch} complete={false} />)
    await flush()
    const header = () => container.querySelector('[data-diffs-header]')!
    expect(header().querySelector('[data-title]')).toHaveTextContent('greet.py')
    expect(header().querySelector('[data-deletions-count]')).toHaveTextContent('-2')
    expect(header().querySelector('[data-additions-count]')).toHaveTextContent('+4')
    rerender(<DiffBlock code={fullPatch} complete />)
    await flush()
    expect(header().querySelector('[data-title]')).toHaveTextContent('greet.py')
    expect(header().querySelector('[data-deletions-count]')).toHaveTextContent('-2')
    expect(header().querySelector('[data-additions-count]')).toHaveTextContent('+4')
  })
})
