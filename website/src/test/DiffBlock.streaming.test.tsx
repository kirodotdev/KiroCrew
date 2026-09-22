import { describe, it, vi, beforeEach, expect } from 'vitest'
import { render, screen, act, within } from '@testing-library/react'
import type { ComponentProps, ReactElement, ReactNode } from 'react'

/** A stand-in `PierrePatch` the header-hold case drives by hand. `null` leaves
 *  the REAL one in place, which every other case here needs: they exercise
 *  Pierre's own parser on partial frames, and a mock would prove nothing. */
const pierreDouble = vi.hoisted(() => ({
  render: null as null | ((props: { onVisible?: () => void; renderHeaderMetadata?: () => ReactNode }) => ReactElement),
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
   *  chunk. `pre.pierre-plain` is the app-owned stand-in, and the header path it
   *  prints is the untouched one -- the Pierre path shortens headers, so the full
   *  path doubles as proof of WHICH path rendered. */
  it('renders a streaming patch as plain text, keeping the original header paths', async () => {
    await warmPierre()
    const { container } = render(<DiffBlock code={fullPatch} complete={false} streaming />)
    await flush()
    // Scoped to this render: the cases above deliberately leave their trees
    // mounted, so a document-wide query can read THEIR stand-in instead.
    const plain = container.querySelector('pre.pierre-plain')
    expect(plain).not.toBeNull()
    // The full path survives: a basename-shortened header would not apply if copied.
    expect(plain?.textContent).toContain('--- /home/user/example/src/greet.py')
  })

  /** jsdom has no `Worker`, so Pierre's real surface never mounts here -- the pool
   *  reports `unsupported` and Pierre's own wrapper falls back to the SAME plain
   *  stand-in. So "the stand-in is gone" cannot tell the two paths apart. The full
   *  header path can: only the Pierre path shortens headers
   *  (`basenamePatchHeaders`), so the untouched path is present while frames arrive
   *  and absent once the block is handed to Pierre. Holds either way, and deleting
   *  the gate flips the first assertion. */
  it('hands the patch to Pierre only once the block is complete', async () => {
    await warmPierre()
    const { container, rerender } = render(<DiffBlock code={fullPatch} complete={false} streaming />)
    await flush()
    expect(container.textContent).toContain('--- /home/user/example/src/greet.py')
    rerender(<DiffBlock code={fullPatch} complete />)
    await flush()
    expect(container.textContent).not.toContain('--- /home/user/example/src/greet.py')
  })

  /** The header band must never leave the layout mid-read. A cold `PierrePatch`
   *  (never mounted while streaming, so no cached height) shows a HEADER-LESS
   *  plain fallback until its impl paints; if the stand-in header left at the
   *  `complete` flip, the patch body would lift by the band's height and drop
   *  back when Pierre's header landed. The double below is Pierre reduced to its
   *  two signals -- WarmSwap's reveal (`onVisible`) and the header slot being
   *  drawn (`renderHeaderMetadata` invoked) -- and the stand-in row, identified
   *  by the Copy control OUTSIDE Pierre's slot, must survive until BOTH have
   *  fired, with the controls on exactly one visible header throughout. */
  it('keeps the stand-in header until Pierre has revealed and drawn its own header', async () => {
    let reveal: (() => void) | undefined
    let headerDrawn = false
    pierreDouble.render = ({ onVisible, renderHeaderMetadata }) => {
      reveal = onVisible
      return (
        <div data-testid="pierre">
          {headerDrawn && <div data-testid="pierre-header">{renderHeaderMetadata?.()}</div>}
          <pre>{'pierre body'}</pre>
        </div>
      )
    }
    const copyName = i18nT('components.diffBlock.copy_patch')
    const { container, rerender } = render(<DiffBlock code={fullPatch} complete={false} />)
    await flush()
    const standInCopy = () => within(container).queryAllByRole('button', { name: copyName })
      .filter(b => !b.closest('[data-testid="pierre"]'))
    const pierreCopy = () => container.querySelectorAll('[data-testid="pierre-header"] button[aria-label="' + copyName + '"]')
    expect(standInCopy()).toHaveLength(1)

    // Flip to complete: Pierre mounts cold, neither signal has fired.
    rerender(<DiffBlock code={fullPatch} complete />)
    await flush()
    expect(container.querySelector('[data-testid="pierre"]')).not.toBeNull()
    expect(standInCopy()).toHaveLength(1)
    expect(pierreCopy()).toHaveLength(0)

    // WarmSwap reveals first (its box only needs height, not a header): the
    // header is still not drawn, so the stand-in must stay.
    act(() => reveal?.())
    expect(standInCopy()).toHaveLength(1)
    expect(pierreCopy()).toHaveLength(0)

    // Pierre draws its header: the stand-in leaves and the controls move over.
    // `DiffBlock` is memoised, so the double needs a prop change to re-render;
    // `pathHint` is inert here (the patch's own header wins the path).
    headerDrawn = true
    rerender(<DiffBlock code={fullPatch} complete pathHint="/ignored" />)
    await flush()
    expect(standInCopy()).toHaveLength(0)
    expect(pierreCopy()).toHaveLength(1)
    // The controls exist exactly once: Copy plus the layout toggle, which only
    // ever appears in Pierre's header, and nothing left behind in the stand-in.
    expect(within(container).getAllByRole('button')).toHaveLength(2)
  })

  /** The reverse order: header drawn while WarmSwap still hides its box. The
   *  stand-in stays until the reveal, else the band would vanish for the tick
   *  between Pierre drawing into the hidden box and the box becoming visible. */
  it('keeps the stand-in header when Pierre draws its header before the reveal', async () => {
    let reveal: (() => void) | undefined
    pierreDouble.render = ({ onVisible, renderHeaderMetadata }) => {
      reveal = onVisible
      return <div data-testid="pierre"><div data-testid="pierre-header">{renderHeaderMetadata?.()}</div></div>
    }
    const copyName = i18nT('components.diffBlock.copy_patch')
    const { container, rerender } = render(<DiffBlock code={fullPatch} complete={false} />)
    await flush()
    rerender(<DiffBlock code={fullPatch} complete />)
    await flush()
    const standInCopy = () => within(container).queryAllByRole('button', { name: copyName })
      .filter(b => !b.closest('[data-testid="pierre"]'))
    expect(standInCopy()).toHaveLength(1)
    expect(container.querySelectorAll('[data-testid="pierre-header"] button')).toHaveLength(0)
    act(() => reveal?.())
    expect(standInCopy()).toHaveLength(0)
    expect(container.querySelectorAll('[data-testid="pierre-header"] button[aria-label="' + copyName + '"]')).toHaveLength(1)
  })
})
