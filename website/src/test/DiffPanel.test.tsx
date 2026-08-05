import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'

// Mock Monaco DiffEditor — heavy, lazy-loaded, and unrenderable in jsdom.
// The mock surfaces props as data attributes so the test can verify the
// wrapper passes original/modified/language/options correctly.
interface MockDiffEditorProps {
  original?: string
  modified?: string
  language?: string
  theme?: string
  options?: { renderSideBySide?: boolean; lineNumbers?: string; useInlineViewWhenSpaceIsLimited?: boolean }
}

vi.mock('@monaco-editor/react', () => ({
  DiffEditor: ({ original, modified, language, theme, options }: MockDiffEditorProps) => (
    <div
      data-testid="monaco-diff"
      data-original={original}
      data-modified={modified}
      data-language={language}
      data-theme={theme}
      data-side-by-side={String(options?.renderSideBySide)}
      data-inline-when-narrow={String(options?.useInlineViewWhenSpaceIsLimited)}
      data-line-numbers={String(options?.lineNumbers)}
    />
  ),
  loader: { config: () => {} },
}))

// Mock monaco-editor to avoid loading the real editor in jsdom.
vi.mock('monaco-editor', () => ({}))

// Mock the local Monaco setup utility (depends on mocked modules above).
vi.mock('../utils/monacoLocal', () => ({
  ensureMonacoLocal: async () => {},
}))

// Stub useIsDark via the real module so the wrapper picks light/dark theme.
vi.mock('../components/MonacoCodeBlock', async () => {
  const actual = await vi.importActual<typeof import('../components/MonacoCodeBlock')>(
    '../components/MonacoCodeBlock',
  )
  return { ...actual, useIsDark: () => false }
})

const { default: DiffPanel } = await import('../components/DiffPanel')

beforeEach(() => {
  document.documentElement.removeAttribute('data-theme')
})

describe('DiffPanel', () => {
  it('renders the file path in the footer', async () => {
    render(<DiffPanel filePath="/abs/path/to/code.ts" original="a" modified="b" />)
    expect(await screen.findByText('/abs/path/to/code.ts')).toBeInTheDocument()
  })

  it('infers language from .ts extension', async () => {
    render(<DiffPanel filePath="/x/foo.ts" original="" modified="" />)
    const editor = await screen.findByTestId('monaco-diff')
    expect(editor.getAttribute('data-language')).toBe('typescript')
  })

  it('infers language from .py extension', async () => {
    render(<DiffPanel filePath="/x/foo.py" original="" modified="" />)
    const editor = await screen.findByTestId('monaco-diff')
    expect(editor.getAttribute('data-language')).toBe('python')
  })

  it('passes the raw extension to Monaco for unknown types (Monaco gracefully ignores)', async () => {
    render(<DiffPanel filePath="/x/foo.unknownext" original="" modified="" />)
    const editor = await screen.findByTestId('monaco-diff')
    // monacoLang() returns the raw ext for anything not in its map; Monaco
    // then ignores languages it doesn't know. We just confirm the wrapper
    // doesn't crash on an unfamiliar extension.
    expect(editor.getAttribute('data-language')).toBe('unknownext')
  })

  it('falls back to plaintext when there is no extension', async () => {
    render(<DiffPanel filePath="Makefile" original="" modified="" />)
    const editor = await screen.findByTestId('monaco-diff')
    expect(editor.getAttribute('data-language')).toBe('plaintext')
  })

  it('passes original and modified content to the editor', async () => {
    render(
      <DiffPanel
        filePath="/x/data.json"
        original='{"a":1}'
        modified='{"a":2}'
      />,
    )
    const editor = await screen.findByTestId('monaco-diff')
    expect(editor.getAttribute('data-original')).toBe('{"a":1}')
    expect(editor.getAttribute('data-modified')).toBe('{"a":2}')
  })

  it('defaults to side-by-side rendering', async () => {
    render(<DiffPanel filePath="/x/a.ts" original="" modified="" />)
    const editor = await screen.findByTestId('monaco-diff')
    expect(editor.getAttribute('data-side-by-side')).toBe('true')
  })

  it('honors sideBySide=false (unified mode)', async () => {
    render(<DiffPanel filePath="/x/a.ts" original="" modified="" sideBySide={false} />)
    const editor = await screen.findByTestId('monaco-diff')
    expect(editor.getAttribute('data-side-by-side')).toBe('false')
  })

  // Regression: Monaco's useInlineViewWhenSpaceIsLimited defaults to true and
  // silently forces the inline view below renderSideBySideInlineBreakpoint
  // (900px). The chat side panel is always narrower than that, so the split
  // toggle had no visible effect. renderSideBySide must stay authoritative.
  it('opts out of the narrow-width inline fallback', async () => {
    render(<DiffPanel filePath="/x/a.ts" original="" modified="" />)
    const editor = await screen.findByTestId('monaco-diff')
    expect(editor.getAttribute('data-inline-when-narrow')).toBe('false')
  })

  it('lineNumbers default off', async () => {
    render(<DiffPanel filePath="/x/a.ts" original="" modified="" />)
    const editor = await screen.findByTestId('monaco-diff')
    expect(editor.getAttribute('data-line-numbers')).toBe('off')
  })

  it('lineNumbers prop toggles "on"', async () => {
    render(<DiffPanel filePath="/x/a.ts" original="" modified="" lineNumbers />)
    const editor = await screen.findByTestId('monaco-diff')
    expect(editor.getAttribute('data-line-numbers')).toBe('on')
  })

  it('uses light theme by default (useIsDark stubbed to false)', async () => {
    render(<DiffPanel filePath="/x/a.ts" original="" modified="" />)
    const editor = await screen.findByTestId('monaco-diff')
    expect(editor.getAttribute('data-theme')).toBe('kirocrew-light')
  })

  it('shows "Contents are identical" banner when original === modified', async () => {
    const { queryByTestId } = render(
      <DiffPanel filePath="/x/same.ts" original="foo\nbar" modified="foo\nbar" />,
    )
    // The banner renders instead of the Monaco editor.
    expect(await screen.findByText('Contents are identical')).toBeInTheDocument()
    expect(queryByTestId('monaco-diff')).toBeNull()
  })

  it('does NOT show the identical banner when content differs', async () => {
    render(<DiffPanel filePath="/x/diff.ts" original="a" modified="b" />)
    const editor = await screen.findByTestId('monaco-diff')
    expect(editor).toBeInTheDocument()
    expect(screen.queryByText('Contents are identical')).not.toBeInTheDocument()
  })
})
