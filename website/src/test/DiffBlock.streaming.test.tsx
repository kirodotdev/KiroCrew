import { describe, it, vi, beforeEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import DiffBlock from '../components/DiffBlock'

beforeEach(() => {
  globalThis.fetch = vi.fn(() => Promise.resolve({ ok: true })) as unknown as typeof fetch
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
  await screen.findByTitle('Copy patch')
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
        <DiffBlock code={partial} complete={false} streaming />,
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
})
