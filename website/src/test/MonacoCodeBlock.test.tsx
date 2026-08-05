import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, act } from '@testing-library/react'
import { lineCount, monacoLang, useIsDark } from '../components/MonacoCodeBlock'
import { renderHook, act as actHook } from '@testing-library/react'
import { renderWithProviders } from './helpers'

// Mock Monaco editor — it's lazy-loaded and heavy
vi.mock('@monaco-editor/react', () => ({
  default: ({ value, onChange }: { value?: string; onChange?: (value: string) => void }) => (
    <textarea aria-label="monaco-editor" data-testid="monaco-editor" value={value} onChange={e => onChange?.(e.target.value)} />
  ),
  loader: { config: () => {} },
}))

// Mock monaco-editor to avoid loading the real editor in jsdom.
vi.mock('monaco-editor', () => ({}))

vi.mock('../utils/monacoLocal', () => ({
  ensureMonacoLocal: async () => {},
}))

vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn(),
  copyCode: vi.fn(),
}))

// Mock hljs for CodeBlock
vi.mock('../utils/hljs', () => ({
  default: {
    getLanguage: () => null,
    highlight: () => ({ value: '' }),
    highlightAuto: () => ({ value: '' }),
    registerLanguage: vi.fn(),
  },
}))

// Must import after mocks
const { default: MonacoCodeBlock } = await import('../components/MonacoCodeBlock')

beforeEach(() => {
  vi.clearAllMocks()
  document.documentElement.removeAttribute('data-theme')
})

describe('lineCount', () => {
  it('returns minimum 3 for short code', () => {
    expect(lineCount('')).toBe(3)
    expect(lineCount('one line')).toBe(3)
  })

  it('caps at 40 for long code', () => {
    expect(lineCount('x\n'.repeat(50))).toBe(40)
  })

  it('counts actual lines in range', () => {
    expect(lineCount('a\nb\nc\nd\ne\nf\ng\nh\ni\nj')).toBe(10)
  })
})

describe('monacoLang', () => {
  it('maps aliases to Monaco language IDs', () => {
    expect(monacoLang('js')).toBe('javascript')
    expect(monacoLang('py')).toBe('python')
    expect(monacoLang('ts')).toBe('typescript')
    expect(monacoLang('sh')).toBe('bash')
  })

  it('returns plaintext for undefined', () => {
    expect(monacoLang(undefined)).toBe('plaintext')
  })

  it('passes through unknown languages', () => {
    expect(monacoLang('go')).toBe('go')
  })
})

describe('useIsDark', () => {
  it('detects dark theme from data-theme attribute', () => {
    document.documentElement.setAttribute('data-theme', 'dark')
    const { result } = renderHook(() => useIsDark())
    expect(result.current).toBe(true)
  })

  it('detects light theme', () => {
    document.documentElement.setAttribute('data-theme', 'light')
    const { result } = renderHook(() => useIsDark())
    expect(result.current).toBe(false)
  })

  it('reacts to data-theme changes', async () => {
    document.documentElement.setAttribute('data-theme', 'light')
    const { result } = renderHook(() => useIsDark())
    expect(result.current).toBe(false)

    await actHook(async () => {
      document.documentElement.setAttribute('data-theme', 'dark')
    })
    expect(result.current).toBe(true)
  })
})

describe('MonacoCodeBlock', () => {
  const sampleCode = 'const x = 1\nconst y = 2'

  it('renders read-only view by default', () => {
    renderWithProviders(<MonacoCodeBlock code={sampleCode} lang="typescript" complete={true} />)
    expect(screen.queryByTestId('monaco-editor')).not.toBeInTheDocument()
  })

  it('shows Edit button only when complete', () => {
    const { rerender } = renderWithProviders(<MonacoCodeBlock code={sampleCode} complete={false} />)
    expect(screen.queryByLabelText('Edit code block')).not.toBeInTheDocument()

    rerender(<MonacoCodeBlock code={sampleCode} complete={true} />)
    expect(screen.getByLabelText('Edit code block')).toBeInTheDocument()
  })

  it('opens Monaco editor on Edit click', async () => {
    renderWithProviders(<MonacoCodeBlock code={sampleCode} lang="typescript" complete={true} />)
    fireEvent.click(screen.getByLabelText('Edit code block'))
    expect(await screen.findByTestId('monaco-editor')).toBeInTheDocument()
  })

  it('resets value to original code on Close', async () => {
    renderWithProviders(<MonacoCodeBlock code={sampleCode} lang="typescript" complete={true} />)

    // Open editor
    fireEvent.click(screen.getByLabelText('Edit code block'))
    const editor = await screen.findByTestId('monaco-editor')

    fireEvent.change(editor, { target: { value: 'modified code' } })

    fireEvent.click(screen.getByTitle('Close editor'))

    // Re-open — should show original code, not modified
    fireEvent.click(screen.getByLabelText('Edit code block'))
    const reopened = await screen.findByTestId('monaco-editor')
    expect(reopened).toHaveValue(sampleCode)
  })

  it('syncs value when code prop changes while not editing', async () => {
    const { rerender } = renderWithProviders(<MonacoCodeBlock code="initial" lang="typescript" complete={true} />)

    // Update code prop (simulates streaming)
    await act(async () => {
      rerender(<MonacoCodeBlock code="updated" lang="typescript" complete={true} />)
    })

    // Open editor — should show updated code
    fireEvent.click(screen.getByLabelText('Edit code block'))
    const editor = await screen.findByTestId('monaco-editor')
    expect(editor).toHaveValue('updated')
  })

  it('does not overwrite user edits when code prop changes during editing', async () => {
    const { rerender } = renderWithProviders(<MonacoCodeBlock code="initial" lang="typescript" complete={true} />)
    fireEvent.click(screen.getByLabelText('Edit code block'))
    const editor = await screen.findByTestId('monaco-editor')
    fireEvent.change(editor, { target: { value: 'user edits' } })

    await act(async () => {
      rerender(<MonacoCodeBlock code="streamed update" lang="typescript" complete={true} />)
    })

    expect(editor).toHaveValue('user edits')
  })
})
