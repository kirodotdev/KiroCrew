import { describe, it, expect, vi, beforeAll, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import DiffBlock, { extractFilePath } from '../components/DiffBlock'

// The block's controls live in its own header row, but Pierre's lazy chunk still
// mounts beneath that row in every highlighted-mode case below. Warm it once so
// a saturated full-suite worker does not pay the module load inside a test.
beforeAll(() => import('../pierre/PierreImpl'))

beforeEach(() => {
  globalThis.fetch = vi.fn(() => Promise.resolve({ ok: true })) as unknown as typeof fetch
  // The split/unified layout persists app-wide (`mc-diff-split`); start each
  // test from the unseeded default so no test inherits another's toggle.
  localStorage.clear()
})

const simpleDiff = `--- a/file.ts
+++ b/file.ts
@@ -1,3 +1,4 @@
 const a = 1
-const b = 2
+const b = 3
+const c = 4
 const d = 5`

/* Pierre owns the diff BODY: the rows, the gutters and the hunk folding are
 * painted inside a shadow root, behind a lazy chunk that only resolves once a
 * test awaits. The block's title row — filename, ± counts, and the Open /
 * layout / Copy controls — is DiffBlock's own light-DOM row, present from the
 * first render whatever Pierre is doing, so it is what these assertions read;
 * appearance assertions belong in Playwright instead of here.
 *
 * A NEGATIVE assertion about the Open control waits for the header row first
 * and then asserts the guard's own observable: with the `isSafePath(probePath)`
 * term removed from the effect, fetch is called for the unsafe path and the
 * button appears — a bare `queryByTitle(...)).not.toBeInTheDocument()` alone
 * would also pass for a path that was merely never probed. Each one below
 * waits for `headerMounted()` and then asserts that no existence probe fired,
 * which is the effect's early return made visible. */

/** Resolves once the block's header row is live: `Copy patch` is rendered
 *  unconditionally, so its arrival means the Open slot is real and empty
 *  rather than merely unrendered. */
const headerMounted = () => screen.findByTitle('Copy patch')

describe('DiffBlock', () => {
  it('shows generating indicator when not complete', () => {
    render(<DiffBlock code={simpleDiff} complete={false} />)
    expect(screen.getByText('generating diff…')).toBeInTheDocument()
  })

  it('hides generating indicator when complete', () => {
    render(<DiffBlock code={simpleDiff} complete={true} />)
    expect(screen.queryByText('generating diff…')).not.toBeInTheDocument()
  })

  it('has copy button on hover', async () => {
    render(<DiffBlock code={simpleDiff} complete={true} />)
    expect(await screen.findByTitle('Copy patch')).toBeInTheDocument()
  })

  it('toggles between unified and split view and persists the choice', async () => {
    render(<DiffBlock code={simpleDiff} complete={true} />)
    // Unseeded default is split — the shared `mc-diff-split` preference's
    // default — so the button offers the way back to unified.
    fireEvent.click(await screen.findByTitle('Unified view'))
    expect(await screen.findByTitle('Split view')).toBeInTheDocument()
    // The choice lands in the shared preference (#6024), not per-block state.
    expect(localStorage.getItem('mc-diff-split')).toBe('0')
  })

  it('seeds the layout from the shared mc-diff-split preference', async () => {
    localStorage.setItem('mc-diff-split', '0')
    render(<DiffBlock code={simpleDiff} complete={true} />)
    // Persisted unified → the button offers split.
    expect(await screen.findByTitle('Split view')).toBeInTheDocument()
  })

  it('shows View file button when onFileOpen is provided', async () => {
    render(<DiffBlock code={simpleDiff} complete={true} onFileOpen={() => {}} />)
    await waitFor(() => expect(screen.getByTitle(/^Open .* in side panel$/)).toBeInTheDocument())
  })

  it('does not show View file button when onFileOpen is not provided', async () => {
    render(<DiffBlock code={simpleDiff} complete={true} />)
    await headerMounted()
    expect(globalThis.fetch).not.toHaveBeenCalled()
    expect(screen.queryByTitle(/^Open .* in side panel$/)).not.toBeInTheDocument()
  })

  it('calls onFileOpen with file path when View file is clicked', async () => {
    const onFileOpen = vi.fn()
    render(<DiffBlock code={simpleDiff} complete={true} onFileOpen={onFileOpen} />)
    await waitFor(() => expect(screen.getByTitle(/^Open .* in side panel$/)).toBeInTheDocument())
    fireEvent.click(screen.getByTitle(/^Open .* in side panel$/))
    expect(onFileOpen).toHaveBeenCalledWith('file.ts')
  })

  /* Pierre builds its file title from the `---`/`+++` lines; hunks alone give
     it nothing to name. The block's own header does not depend on that: Copy
     is there for a headerless patch too, and the filename is simply empty.
     Open is the one control that needs a path — with none extracted there is
     nothing to probe and nothing to open, and asserting that the probe never
     fired is the guard's own observable. */
  it('keeps Copy but offers no Open for a headerless patch', async () => {
    const noPathDiff = `@@ -1,2 +1,2 @@\n-old\n+new`
    render(<DiffBlock code={noPathDiff} complete={true} onFileOpen={() => {}} />)
    expect(await headerMounted()).toBeInTheDocument()
    expect(globalThis.fetch).not.toHaveBeenCalled()
    expect(screen.queryByTitle(/^Open .* in side panel$/)).not.toBeInTheDocument()
  })

  it('does not probe or offer View file when the headers name only /dev/null', async () => {
    // A pure add/delete names /dev/null on one side; both sides here, so no
    // real path survives extraction even though the header itself renders.
    const devNullDiff = `--- /dev/null\n+++ /dev/null\n@@ -1,2 +1,2 @@\n-old\n+new`
    render(<DiffBlock code={devNullDiff} complete={true} onFileOpen={() => {}} />)
    await headerMounted()
    expect(globalThis.fetch).not.toHaveBeenCalled()
    expect(screen.queryByTitle(/^Open .* in side panel$/)).not.toBeInTheDocument()
  })

  it('extracts file path from diff --git header when +++ line is absent', async () => {
    const gitHeaderDiff = `diff --git a/foo.ts b/foo.ts\n@@ -1,2 +1,2 @@\n-old\n+new`
    const onFileOpen = vi.fn()
    render(<DiffBlock code={gitHeaderDiff} complete={true} onFileOpen={onFileOpen} />)
    await waitFor(() => expect(screen.getByTitle(/^Open .* in side panel$/)).toBeInTheDocument())
    fireEvent.click(screen.getByTitle(/^Open .* in side panel$/))
    expect(onFileOpen).toHaveBeenCalledWith('foo.ts')
  })

  /* The three shapes `isSafePath` rejects. Each renders a real header, so the
     guard's observable is that the existence probe never fires: with the
     `isSafePath(probePath)` term removed from the effect, fetch is called for
     the unsafe path and the Open button appears — both assertions below flip. */
  const unsafeHeaders: Array<[string, string]> = [
    ['a parent traversal', '../../etc/passwd'],
    ['a credentials directory', '.aws/credentials'],
    ['the .git directory', '.git/config'],
  ]
  for (const [label, unsafePath] of unsafeHeaders) {
    it(`does not probe or offer View file for ${label}`, async () => {
      const diff = `--- a/${unsafePath}\n+++ b/${unsafePath}\n@@ -1,2 +1,2 @@\n-old\n+new`
      render(<DiffBlock code={diff} complete={true} onFileOpen={() => {}} />)
      await headerMounted()
      expect(globalThis.fetch).not.toHaveBeenCalled()
      expect(screen.queryByTitle(/^Open .* in side panel$/)).not.toBeInTheDocument()
    })
  }

  it('shows View file button for absolute paths', async () => {
    const absDiff = `--- a//home/user/src/app.ts\n+++ b//home/user/src/app.ts\n@@ -1,2 +1,2 @@\n-old\n+new`
    render(<DiffBlock code={absDiff} complete={true} onFileOpen={() => {}} />)
    await waitFor(() => expect(screen.getByTitle(/^Open .* in side panel$/)).toBeInTheDocument())
  })

  it('allows paths that merely start with a sensitive name', async () => {
    const envrcDiff = `--- a/.envrc\n+++ b/.envrc\n@@ -1,2 +1,2 @@\n-old\n+new`
    render(<DiffBlock code={envrcDiff} complete={true} onFileOpen={() => {}} />)
    await waitFor(() => expect(screen.getByTitle(/^Open .* in side panel$/)).toBeInTheDocument())
  })

  it('keeps spaces in header paths instead of truncating to a sibling file', () => {
    // Spaces are legal path characters; the unified-diff header terminator is
    // a TAB (timestamp separator) or end of line. A cut at the first space
    // would resolve "/work/report final.md" to the SIBLING "/work/report",
    // and the open affordance would read and save the wrong file.
    const spaced = `--- /work/report final.md\n+++ /work/report final.md\n@@ -1,1 +1,1 @@\n-old\n+new`
    expect(extractFilePath(spaced)?.path).toBe('/work/report final.md')
    const gitSpaced = `--- a/docs/my notes.md\n+++ b/docs/my notes.md\n@@ -1,1 +1,1 @@\n-old\n+new`
    expect(extractFilePath(gitSpaced)).toEqual({ path: 'docs/my notes.md', prefixStripped: true })
  })

  it('strips a TAB-separated timestamp from header paths', () => {
    const stamped = `--- /work/a.txt\t2026-01-01 00:00:00\n+++ /work/a.txt\t2026-01-02 00:00:00\n@@ -1,1 +1,1 @@\n-old\n+new`
    expect(extractFilePath(stamped)?.path).toBe('/work/a.txt')
  })

  it('hides View file button when file does not exist', async () => {
    globalThis.fetch = vi.fn(() => Promise.resolve({ ok: false, status: 404 })) as unknown as typeof fetch
    render(<DiffBlock code={simpleDiff} complete={true} onFileOpen={() => {}} />)
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled())
    expect(screen.queryByTitle(/^Open .* in side panel$/)).not.toBeInTheDocument()
  })

  it('Open button is text-only and hover-gated like the other diff actions', async () => {
    // All three actions (side-by-side / copy / Open) are hover-gated together.
    // Open uses a plain text label rather than an icon since the diff header
    // already prefixes the file name.
    globalThis.fetch = vi.fn(() => Promise.resolve({ ok: true, status: 200 })) as unknown as typeof fetch
    render(<DiffBlock code={simpleDiff} complete={true} onFileOpen={() => {}} />)
    await waitFor(() => expect(screen.getByText('Open')).toBeInTheDocument())
    // No labeled icon variant.
    expect(screen.queryByText('Open file')).toBeNull()
    // Sits inside the same opacity-0 hover-reveal container as the
    // side-by-side / copy buttons: the <span> DiffBlock renders as the actions
    // cluster of its own header row, which is the element that carries the gate.
    const actions = screen.getByText('Open').closest('span')!
    expect(actions.className).toMatch(/opacity-0/)
    expect(actions.className).toMatch(/group-hover\/diff:opacity-100/)
  })

  it('headers in diff content win over pathHint', async () => {
    globalThis.fetch = vi.fn(() => Promise.resolve({ ok: true, status: 200 })) as unknown as typeof fetch
    // simpleDiff has a real +++ b/<path> header — that should win.
    render(<DiffBlock code={simpleDiff} complete={true} onFileOpen={() => {}} pathHint="/wrong/path" />)
    await waitFor(() => expect(screen.getByText('Open')).toBeInTheDocument())
    expect(screen.queryByTitle(/Open .*\/wrong\/path.*in side panel/)).toBeNull()
  })

  describe('prefix-stripped absolute paths (issue #2493)', () => {
    // `git diff --no-index /tmp/a /tmp/b` joins git's `b/` prefix onto the
    // absolute path, collapsing the leading slash: the header reads
    // `+++ b/tmp/b`. Naive prefix-stripping then yielded `tmp/b` — a rootless
    // spelling of an absolute path — and probing it as a relative path was the
    // captured `path=home/<user>/…&resolve=1` → 400 from the issue. Such a
    // header is now treated as ambiguous: probed ONLY as the rooted spelling,
    // and only when the surrounding chat text corroborates it (pathHint);
    // uncorroborated it gets no probe and no affordance. Existence probing
    // cannot arbitrate the ambiguity — with no project dir configured the
    // backend 400s every relative path, so absence is not evidence.
    const noIndexDiff = `diff --git a/home/user/src/app.ts b/home/user/src/app.ts\n--- a/home/user/src/app.ts\n+++ b/home/user/src/app.ts\n@@ -1,2 +1,2 @@\n-old\n+new`

    const probedPaths = (mock: ReturnType<typeof vi.fn>) =>
      mock.mock.calls.map(c => decodeURIComponent(String(c[0]).match(/path=([^&]*)/)?.[1] ?? ''))

    it('suppresses the probe entirely for an uncorroborated ambiguous header', async () => {
      // THE captured bug: no pathHint, `+++ b/home/user/…` header. The old
      // code fired `path=home/user/…&resolve=1` (the 400); the fix sends
      // nothing at all and offers no button.
      const fetchMock = vi.fn(() => Promise.resolve({ ok: true }))
      globalThis.fetch = fetchMock as unknown as typeof fetch
      render(<DiffBlock code={noIndexDiff} complete={true} onFileOpen={() => {}} />)
      await new Promise(r => setTimeout(r, 20))
      expect(fetchMock).not.toHaveBeenCalled()
      expect(screen.queryByTitle(/^Open .* in side panel$/)).not.toBeInTheDocument()
    })

    it('probes only the rooted spelling when the chat text corroborates it, and opens it', async () => {
      const fetchMock = vi.fn(() => Promise.resolve({ ok: true }))
      globalThis.fetch = fetchMock as unknown as typeof fetch
      const onFileOpen = vi.fn()
      render(<DiffBlock code={noIndexDiff} complete={true} onFileOpen={onFileOpen} pathHint="/home/user/src/app.ts" />)
      await waitFor(() => expect(screen.getByTitle(/^Open .* in side panel$/)).toBeInTheDocument())
      // Exactly one request, for the rooted spelling, with no resolve=1.
      expect(probedPaths(fetchMock)).toEqual(['/home/user/src/app.ts'])
      expect(String(fetchMock.mock.calls[0][0])).not.toContain('resolve=1')
      fireEvent.click(screen.getByTitle(/^Open .* in side panel$/))
      expect(onFileOpen).toHaveBeenCalledWith('/home/user/src/app.ts')
    })

    it('shows no button when the corroborated rooted spelling does not exist', async () => {
      const fetchMock = vi.fn(() => Promise.resolve({ ok: false, status: 404 }))
      globalThis.fetch = fetchMock as unknown as typeof fetch
      render(<DiffBlock code={noIndexDiff} complete={true} onFileOpen={() => {}} pathHint="/home/user/src/app.ts" />)
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
      expect(screen.queryByTitle(/^Open .* in side panel$/)).not.toBeInTheDocument()
    })

    it('a pathHint naming a DIFFERENT file does not corroborate — header stays suppressed', async () => {
      const fetchMock = vi.fn(() => Promise.resolve({ ok: true }))
      globalThis.fetch = fetchMock as unknown as typeof fetch
      render(<DiffBlock code={noIndexDiff} complete={true} onFileOpen={() => {}} pathHint="/somewhere/else.ts" />)
      await new Promise(r => setTimeout(r, 20))
      expect(fetchMock).not.toHaveBeenCalled()
      expect(screen.queryByTitle(/^Open .* in side panel$/)).not.toBeInTheDocument()
    })

    it('does not treat an ordinary repo-relative header as ambiguous', async () => {
      const fetchMock = vi.fn(() => Promise.resolve({ ok: true }))
      globalThis.fetch = fetchMock as unknown as typeof fetch
      render(<DiffBlock code={simpleDiff} complete={true} onFileOpen={() => {}} />)
      await waitFor(() => expect(fetchMock).toHaveBeenCalled())
      expect(probedPaths(fetchMock)).toEqual(['file.ts'])
    })

    it('does not treat a plain-diff header without a git prefix as ambiguous', async () => {
      // `+++ home/user/x` with NO `b/` prefix carries no evidence of a join —
      // treating it as absolute would be a guess, so it stays relative.
      const plainDiff = `--- home/user/notes.md\n+++ home/user/notes.md\n@@ -1,2 +1,2 @@\n-old\n+new`
      const fetchMock = vi.fn(() => Promise.resolve({ ok: true }))
      globalThis.fetch = fetchMock as unknown as typeof fetch
      render(<DiffBlock code={plainDiff} complete={true} onFileOpen={() => {}} />)
      await waitFor(() => expect(fetchMock).toHaveBeenCalled())
      expect(probedPaths(fetchMock)).toEqual(['home/user/notes.md'])
    })

    it('a header change drops the previous verdict — Open never carries a stale path', async () => {
      // Review finding: a probe that settled before abort() must not leave the
      // Open button targeting the OLD header's path once the diff content
      // (e.g. a streaming header) changes. The resolved state is keyed to the
      // header it was measured for; a mismatch renders no button.
      const fetchMock = vi.fn((url: string) =>
        Promise.resolve({ ok: String(url).includes(encodeURIComponent('/home/user/src/app.ts')) }))
      globalThis.fetch = fetchMock as unknown as typeof fetch
      const onFileOpen = vi.fn()
      const { rerender } = render(<DiffBlock code={noIndexDiff} complete={true} onFileOpen={onFileOpen} pathHint="/home/user/src/app.ts" />)
      await waitFor(() => expect(screen.getByTitle(/^Open .* in side panel$/)).toBeInTheDocument())
      // Header changes to a different (never-existing) file.
      const changedDiff = `--- a/other/place/thing.ts\n+++ b/other/place/thing.ts\n@@ -1,2 +1,2 @@\n-old\n+new`
      rerender(<DiffBlock code={changedDiff} complete={true} onFileOpen={onFileOpen} />)
      // The old verdict is keyed to the old header — button gone immediately
      // and it never comes back for the missing new path.
      expect(screen.queryByTitle(/^Open .* in side panel$/)).not.toBeInTheDocument()
      await new Promise(r => setTimeout(r, 10))
      expect(screen.queryByTitle(/^Open .* in side panel$/)).not.toBeInTheDocument()
    })
  })

  /* Plain mode (Settings → Chat → Messages → Plain diffs) replaces the whole
   * Pierre surface with a `<pre>`, so unlike every other case above these
   * assertions are SYNCHRONOUS on purpose: nothing here waits on the lazy
   * chunk, because in this mode the chunk is never requested. */
  describe('plain-diff preference', () => {
    it('renders the raw patch text and keeps Copy reachable without Pierre’s header', () => {
      localStorage.setItem('mc-diff-plain', '1')
      render(<DiffBlock code={simpleDiff} complete={true} />)
      // The patch text is in the light DOM (Pierre would have put its rows in a
      // shadow root), and the block's own header row is the same row as in
      // highlighted mode — so the filename and Copy survive the switch.
      expect(screen.getByText(/-const b = 2/)).toBeInTheDocument()
      expect(screen.getByText('file.ts')).toBeInTheDocument()
      expect(screen.getByTitle('Copy patch')).toBeInTheDocument()
    })

    /* The plain render prints the patch VERBATIM: a shortened copy would put
       `a/file.ts` where the reader — and anyone copying the patch out of the
       page to apply it — expects the original path. The header row shows the
       basename by shortening `headerPath`, never the patch body. */
    it('keeps the original header paths and shows the basename in its own row', () => {
      localStorage.setItem('mc-diff-plain', '1')
      const deep = `--- a/src/deep/nested/file.ts\n+++ b/src/deep/nested/file.ts\n@@ -1,1 +1,1 @@\n-old\n+new`
      render(<DiffBlock code={deep} complete={true} />)
      expect(screen.getByText(/--- a\/src\/deep\/nested\/file\.ts/)).toBeInTheDocument()
      expect(screen.getByText(/\+\+\+ b\/src\/deep\/nested\/file\.ts/)).toBeInTheDocument()
      expect(screen.getByText('file.ts')).toBeInTheDocument()
    })

    it('drops the split/unified control, which only means something to Pierre', () => {
      localStorage.setItem('mc-diff-plain', '1')
      render(<DiffBlock code={simpleDiff} complete={true} />)
      // Copy being present proves the header row rendered, so these absences are
      // the guard's doing rather than an unrendered header.
      expect(screen.getByTitle('Copy patch')).toBeInTheDocument()
      expect(screen.queryByTitle('Unified view')).not.toBeInTheDocument()
      expect(screen.queryByTitle('Split view')).not.toBeInTheDocument()
    })

    it('is off unless the preference is set — the highlighted diff stays the default', async () => {
      render(<DiffBlock code={simpleDiff} complete={true} />)
      // The layout toggle renders only while colour is on, so its presence is
      // the block reading the preference as off.
      expect(await headerMounted()).toBeInTheDocument()
      expect(await screen.findByTitle('Unified view')).toBeInTheDocument()
    })
  })
})
