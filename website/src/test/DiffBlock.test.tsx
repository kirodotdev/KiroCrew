import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import DiffBlock from '../components/DiffBlock'

beforeEach(() => {
  globalThis.fetch = vi.fn(() => Promise.resolve({ ok: true })) as unknown as typeof fetch
})

const simpleDiff = `--- a/file.ts
+++ b/file.ts
@@ -1,3 +1,4 @@
 const a = 1
-const b = 2
+const b = 3
+const c = 4
 const d = 5`

describe('DiffBlock', () => {
  it('renders diff header', () => {
    render(<DiffBlock code={simpleDiff} complete={true} />)
    expect(screen.getByText(/diff/)).toBeInTheDocument()
  })

  it('shows added lines with tinted background', () => {
    const { container } = render(<DiffBlock code={simpleDiff} complete={true} />)
    const addLines = container.querySelectorAll('.bg-diff-add')
    expect(addLines.length).toBeGreaterThan(0)
  })

  it('shows deleted lines with tinted background', () => {
    const { container } = render(<DiffBlock code={simpleDiff} complete={true} />)
    const delLines = container.querySelectorAll('.bg-diff-del')
    expect(delLines.length).toBeGreaterThan(0)
  })

  describe('C1 gutter (single colored line number, no sign column)', () => {
    it('renders exactly one line-number gutter per row', () => {
      const { container } = render(<DiffBlock code={simpleDiff} complete={true} />)
      const addRow = container.querySelector('.bg-diff-add')!
      const gutters = Array.from(addRow.querySelectorAll('span'))
        .filter(s => (s as HTMLElement).style.width.endsWith('ch'))
      expect(gutters).toHaveLength(1)
    })

    it('does not render a +/- sign column — row text is number + content only', () => {
      const { container } = render(<DiffBlock code={simpleDiff} complete={true} />)
      const delRow = container.querySelector('.bg-diff-del')!
      // Old line number (2) followed directly by content, no "-" marker.
      expect(delRow.textContent).toBe('2const b = 2')
    })

    it('colors the gutter number by change type', () => {
      const { container } = render(<DiffBlock code={simpleDiff} complete={true} />)
      const addGutter = container.querySelector('.bg-diff-add span')!
      const delGutter = container.querySelector('.bg-diff-del span')!
      expect(addGutter.className).toContain('text-diff-add-text')
      expect(delGutter.className).toContain('text-diff-del-text')
    })

    it('shows the new line number on add rows and the old on del rows', () => {
      const { container } = render(<DiffBlock code={simpleDiff} complete={true} />)
      // del "const b = 2" is old line 2; the first add "const b = 3" is new line 2,
      // the second add "const c = 4" is new line 3.
      const adds = container.querySelectorAll('.bg-diff-add')
      expect(adds[0].textContent).toBe('2const b = 3')
      expect(adds[1].textContent).toBe('3const c = 4')
    })

    it('marks changed rows with the inset edge bar', () => {
      const { container } = render(<DiffBlock code={simpleDiff} complete={true} />)
      const addRow = container.querySelector('.bg-diff-add')!
      const contextRow = Array.from(container.querySelectorAll('.ft-drow'))
        .find(r => r.textContent?.includes('const a = 1'))!
      expect(addRow.className).toContain('shadow-[inset_2px_0_0_var(--diff-add-text)]')
      expect(contextRow.className).not.toContain('shadow-[inset_2px_0_0')
    })

    it('force-wraps long lines instead of horizontal scrolling', () => {
      const { container } = render(<DiffBlock code={simpleDiff} complete={true} />)
      // No scroll container…
      expect(container.querySelector('.overflow-x-auto')).toBeNull()
      // …and every content cell wraps (unified view).
      const content = container.querySelector('.bg-diff-add span:last-child')!
      expect(content.className).toContain('whitespace-pre-wrap')
      expect(content.className).toContain('break-words')
      // Split view wraps too.
      fireEvent.click(screen.getByTitle('Split view'))
      expect(container.querySelector('.overflow-x-auto')).toBeNull()
      const splitContent = container.querySelector('.bg-diff-add span:last-child')!
      expect(splitContent.className).toContain('whitespace-pre-wrap')
    })
  })

  describe('hunk header separator', () => {
    it('does not render the raw @@ header row', () => {
      render(<DiffBlock code={simpleDiff} complete={true} />)
      expect(screen.queryByText(/@@ -1,3 \+1,4 @@/)).not.toBeInTheDocument()
    })

    it('renders no separator for the first hunk', () => {
      render(<DiffBlock code={simpleDiff} complete={true} />)
      expect(screen.queryByText(/unchanged line/)).not.toBeInTheDocument()
    })

    it('renders an unchanged-lines separator between hunks', () => {
      const twoHunks = `--- a/f.ts\n+++ b/f.ts\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n@@ -150,3 +150,3 @@\n d\n-e\n+E\n f`
      const { container } = render(<DiffBlock code={twoHunks} complete={true} />)
      const label = screen.getByText('146 unchanged lines')
      // Pill bubble around the label, tinted with the theme's hunk colors…
      expect(label.className).toContain('rounded-full')
      expect(label.className).toContain('bg-diff-hunk')
      expect(label.className).toContain('text-diff-hunk-text')
      // …flanked by the zigzag mask rules (CSS mask over currentColor —
      // no SVG element in TSX per AUTOSDE use-lucide-icons).
      const row = label.closest('div')!
      expect(row.querySelectorAll('.zigzag-rule')).toHaveLength(2)
    })

    it('uses the singular form for a one-line gap', () => {
      const twoHunks = `@@ -1,2 +1,2 @@\n a\n-b\n+B\n@@ -4,2 +4,2 @@\n c\n-d\n+D`
      render(<DiffBlock code={twoHunks} complete={true} />)
      expect(screen.getByText('1 unchanged line')).toBeInTheDocument()
    })

    it('replaces the @@ row with the separator in split view too', () => {
      const twoHunks = `@@ -1,2 +1,2 @@\n a\n-b\n+B\n@@ -10,2 +10,2 @@\n c\n-d\n+D`
      render(<DiffBlock code={twoHunks} complete={true} />)
      fireEvent.click(screen.getByTitle('Split view'))
      expect(screen.queryByText(/@@ -10,2/)).not.toBeInTheDocument()
      expect(screen.getByText('7 unchanged lines')).toBeInTheDocument()
    })
  })

  it('shows generating indicator when not complete', () => {
    render(<DiffBlock code={simpleDiff} complete={false} />)
    expect(screen.getByText('generating diff…')).toBeInTheDocument()
  })

  it('hides generating indicator when complete', () => {
    render(<DiffBlock code={simpleDiff} complete={true} />)
    expect(screen.queryByText('generating diff…')).not.toBeInTheDocument()
  })

  it('has copy button on hover', () => {
    render(<DiffBlock code={simpleDiff} complete={true} />)
    expect(screen.getByTitle('Copy patch')).toBeInTheDocument()
  })

  it('toggles between unified and split view', () => {
    render(<DiffBlock code={simpleDiff} complete={true} />)
    const toggle = screen.getByTitle('Split view')
    fireEvent.click(toggle)
    expect(screen.getByTitle('Unified view')).toBeInTheDocument()
  })

  it('handles kiro-cli diff format', () => {
    const kiroDiff = `+10:const x = 1\n-5:const y = 2`
    const { container } = render(<DiffBlock code={kiroDiff} complete={true} />)
    expect(container.querySelectorAll('.bg-diff-add').length).toBeGreaterThan(0)
    expect(container.querySelectorAll('.bg-diff-del').length).toBeGreaterThan(0)
  })

  it('shows filename in header when diff has file path', () => {
    render(<DiffBlock code={simpleDiff} complete={true} />)
    expect(screen.getByText(/— file.ts/)).toBeInTheDocument()
  })

  it('shows View file button when onFileOpen is provided', async () => {
    render(<DiffBlock code={simpleDiff} complete={true} onFileOpen={() => {}} />)
    await waitFor(() => expect(screen.getByTitle(/^Open .* in side panel$/)).toBeInTheDocument())
  })

  it('does not show View file button when onFileOpen is not provided', () => {
    render(<DiffBlock code={simpleDiff} complete={true} />)
    expect(screen.queryByTitle(/^Open .* in side panel$/)).not.toBeInTheDocument()
  })

  it('calls onFileOpen with file path when View file is clicked', async () => {
    const onFileOpen = vi.fn()
    render(<DiffBlock code={simpleDiff} complete={true} onFileOpen={onFileOpen} />)
    await waitFor(() => expect(screen.getByTitle(/^Open .* in side panel$/)).toBeInTheDocument())
    fireEvent.click(screen.getByTitle(/^Open .* in side panel$/))
    expect(onFileOpen).toHaveBeenCalledWith('file.ts')
  })

  it('does not show View file for diffs without file paths', () => {
    const noPathDiff = `@@ -1,2 +1,2 @@\n-old\n+new`
    render(<DiffBlock code={noPathDiff} complete={true} onFileOpen={() => {}} />)
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

  it('does not show View file button for paths with traversals', () => {
    const traversalDiff = `--- a/../../etc/passwd\n+++ b/../../etc/passwd\n@@ -1,2 +1,2 @@\n-old\n+new`
    render(<DiffBlock code={traversalDiff} complete={true} onFileOpen={() => {}} />)
    expect(screen.queryByTitle(/^Open .* in side panel$/)).not.toBeInTheDocument()
  })

  it('shows View file button for absolute paths', async () => {
    const absDiff = `--- a//home/user/src/app.ts\n+++ b//home/user/src/app.ts\n@@ -1,2 +1,2 @@\n-old\n+new`
    render(<DiffBlock code={absDiff} complete={true} onFileOpen={() => {}} />)
    await waitFor(() => expect(screen.getByTitle(/^Open .* in side panel$/)).toBeInTheDocument())
  })

  it('does not show View file button for sensitive paths', () => {
    const sensitiveDiff = `--- a/.aws/credentials\n+++ b/.aws/credentials\n@@ -1,2 +1,2 @@\n-old\n+new`
    render(<DiffBlock code={sensitiveDiff} complete={true} onFileOpen={() => {}} />)
    expect(screen.queryByTitle(/^Open .* in side panel$/)).not.toBeInTheDocument()
  })

  it('does not show View file button for .git directory paths', () => {
    const gitDiff = `--- a/.git/config\n+++ b/.git/config\n@@ -1,2 +1,2 @@\n-old\n+new`
    render(<DiffBlock code={gitDiff} complete={true} onFileOpen={() => {}} />)
    expect(screen.queryByTitle(/^Open .* in side panel$/)).not.toBeInTheDocument()
  })

  it('allows paths that merely start with a sensitive name', async () => {
    const envrcDiff = `--- a/.envrc\n+++ b/.envrc\n@@ -1,2 +1,2 @@\n-old\n+new`
    render(<DiffBlock code={envrcDiff} complete={true} onFileOpen={() => {}} />)
    await waitFor(() => expect(screen.getByTitle(/^Open .* in side panel$/)).toBeInTheDocument())
  })

  it('hides View file button when file does not exist', async () => {
    globalThis.fetch = vi.fn(() => Promise.resolve({ ok: false, status: 404 })) as unknown as typeof fetch
    render(<DiffBlock code={simpleDiff} complete={true} onFileOpen={() => {}} />)
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled())
    expect(screen.queryByTitle(/^Open .* in side panel$/)).not.toBeInTheDocument()
  })

  it('Open button is text-only and hover-gated like the other diff actions (round 10)', async () => {
    // All three actions (side-by-side / copy / Open) are hover-gated together.
    // Open uses a plain text label rather than an icon since the diff header
    // already prefixes the file name.
    globalThis.fetch = vi.fn(() => Promise.resolve({ ok: true, status: 200 })) as unknown as typeof fetch
    render(<DiffBlock code={simpleDiff} complete={true} onFileOpen={() => {}} />)
    await waitFor(() => expect(screen.getByText('Open')).toBeInTheDocument())
    // No labeled icon variant.
    expect(screen.queryByText('Open file')).toBeNull()
    // Sits inside the same opacity-0 hover-reveal container as the
    // side-by-side / copy buttons.
    const container = screen.getByText('Open').closest('div')!
    expect(container.className).toMatch(/opacity-0/)
    expect(container.className).toMatch(/group-hover\/diff:opacity-100/)
  })

  it('uses pathHint when diff has no headers (round 9)', async () => {
    // Bare diff with no +++/--- headers — common when a file-mod tool
    // emits "Created /path/to/file:" before the diff content. The
    // surrounding chat renderer extracts the path and passes it as a
    // hint so DiffBlock's Open file button still works.
    globalThis.fetch = vi.fn(() => Promise.resolve({ ok: true, status: 200 })) as unknown as typeof fetch
    const headerlessDiff = '+ # Hello\n+ World\n'
    render(<DiffBlock code={headerlessDiff} complete={true} onFileOpen={() => {}} pathHint="/tmp/hello.md" />)
    await waitFor(() => expect(screen.getByText('Open')).toBeInTheDocument())
    expect(screen.getByTitle(/Open .*\/tmp\/hello\.md.* in side panel/)).toBeInTheDocument()
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

    it('still shows the filename in the header while suppressed or unresolved', () => {
      globalThis.fetch = vi.fn(() => new Promise(() => {})) as unknown as typeof fetch
      render(<DiffBlock code={noIndexDiff} complete={true} onFileOpen={() => {}} />)
      expect(screen.getByText(/— app.ts/)).toBeInTheDocument()
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

  describe('line-number gutter width', () => {
    // Regression: gutters were hardcoded to w-[3.5ch], which fits only 3
    // digits. Diffs at line 1000+ overflowed the column — the old/new
    // numbers visually collided ("10081008") and the column separator was
    // drawn through the digits. The gutter must scale with the widest
    // line number in the diff.
    const gutterSpans = (container: HTMLElement) =>
      Array.from(container.querySelectorAll('span'))
        .filter(s => (s as HTMLElement).style.width.endsWith('ch')) as HTMLElement[]

    it('keeps the compact 3.5ch gutter for small line numbers', () => {
      const { container } = render(<DiffBlock code={simpleDiff} complete={true} />)
      const spans = gutterSpans(container)
      expect(spans.length).toBeGreaterThan(0)
      for (const s of spans) expect(s.style.width).toBe('3.5ch')
    })

    it('widens the gutter to fit 4-digit line numbers', () => {
      const bigDiff = `--- a/file.ts\n+++ b/file.ts\n@@ -1008,4 +1008,3 @@\n context1\n-removed1\n-removed2\n context2`
      const { container } = render(<DiffBlock code={bigDiff} complete={true} />)
      const spans = gutterSpans(container)
      expect(spans.length).toBeGreaterThan(0)
      // 4 digits + 1.5ch padding
      for (const s of spans) expect(s.style.width).toBe('5.5ch')
    })

    it('widens the gutter in side-by-side view too', () => {
      const bigDiff = `--- a/file.ts\n+++ b/file.ts\n@@ -12345,3 +12345,3 @@\n context1\n-old\n+new\n context2`
      const { container } = render(<DiffBlock code={bigDiff} complete={true} />)
      fireEvent.click(screen.getByTitle('Split view'))
      const spans = gutterSpans(container)
      expect(spans.length).toBeGreaterThan(0)
      // 5 digits + 1.5ch padding
      for (const s of spans) expect(s.style.width).toBe('6.5ch')
    })
  })
})