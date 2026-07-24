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

  it('shows added lines with + prefix', () => {
    const { container } = render(<DiffBlock code={simpleDiff} complete={true} />)
    const addLines = container.querySelectorAll('.bg-diff-add')
    expect(addLines.length).toBeGreaterThan(0)
  })

  it('shows deleted lines with - prefix', () => {
    const { container } = render(<DiffBlock code={simpleDiff} complete={true} />)
    const delLines = container.querySelectorAll('.bg-diff-del')
    expect(delLines.length).toBeGreaterThan(0)
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
    // Round 10: revert the round-7 always-visible variant — users found
    // it asymmetric with the side-by-side / copy buttons. Now all three
    // are hover-gated together, and Open drops the icon for a plain
    // text label since the diff header already prefixes the file name.
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