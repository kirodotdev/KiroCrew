import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import DiffPanel from './DiffPanel'
import { copyToClipboard } from '../utils/clipboard'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn() }))
vi.mock('../utils/monacoLocal', () => ({ ensureMonacoLocal: vi.fn().mockResolvedValue(undefined) }))

/** Captures what DiffPanel hands Monaco, and drives the two lifecycle hooks. */
const captured: {
  props?: Record<string, unknown>
  defined: string[]
  revealed: number[]
  disposed: number
  lineChanges: unknown[]
} = { defined: [], revealed: [], disposed: 0, lineChanges: [] }

vi.mock('@monaco-editor/react', () => ({
  DiffEditor: (props: Record<string, unknown>) => {
    captured.props = props
    const monaco = {
      editor: {
        defineTheme: (name: string) => { captured.defined.push(name) },
      },
    }
    ;(props.beforeMount as (m: unknown) => void)(monaco)
    const editor = {
      onDidUpdateDiff: (cb: () => void) => {
        // Fire immediately: the real editor calls back once the diff settles.
        queueMicrotask(cb)
        return { dispose: () => { captured.disposed += 1 } }
      },
      getLineChanges: () => captured.lineChanges,
      getModifiedEditor: () => ({
        revealLineInCenter: (n: number) => { captured.revealed.push(n) },
      }),
    }
    ;(props.onMount as (e: unknown) => void)(editor)
    return <div data-testid="zzq-diff-editor" />
  },
}))

beforeEach(() => {
  vi.mocked(copyToClipboard).mockReset()
  captured.props = undefined
  captured.defined = []
  captured.revealed = []
  captured.disposed = 0
  captured.lineChanges = []
})

describe('DiffPanel', () => {
  it('shows the identical banner instead of an editor when both sides match', () => {
    render(<DiffPanel filePath="zzq/a.ts" original="same" modified="same" />)
    expect(screen.getByText('Contents are identical')).toBeInTheDocument()
    expect(screen.queryByTestId('zzq-diff-editor')).not.toBeInTheDocument()
  })

  it('two empty sides are a new-empty-file case and still mount the editor', async () => {
    render(<DiffPanel filePath="zzq/a.ts" original="" modified="" />)
    expect(await screen.findByTestId('zzq-diff-editor')).toBeInTheDocument()
    expect(screen.queryByText('Contents are identical')).not.toBeInTheDocument()
  })

  it('registers both themes and derives the language from the extension', async () => {
    render(<DiffPanel filePath="zzq/a.ts" original="a" modified="b" />)
    await screen.findByTestId('zzq-diff-editor')
    expect(captured.defined).toEqual(['kirocrew-dark', 'kirocrew-light'])
    expect(captured.props!.language).toBe('typescript')
  })

  it('falls back to plaintext for an extensionless path', async () => {
    render(<DiffPanel filePath="zzq-no-ext" original="a" modified="b" />)
    await screen.findByTestId('zzq-diff-editor')
    expect(captured.props!.language).toBe('plaintext')
  })

  it('forces the side-by-side choice and honours the lineNumbers flag', async () => {
    const { unmount } = render(
      <DiffPanel filePath="zzq/a.ts" original="a" modified="b" sideBySide={false} lineNumbers />,
    )
    await screen.findByTestId('zzq-diff-editor')
    const options = captured.props!.options as Record<string, unknown>
    expect(options.renderSideBySide).toBe(false)
    expect(options.useInlineViewWhenSpaceIsLimited).toBe(false)
    expect(options.lineNumbers).toBe('on')
    expect(options.readOnly).toBe(true)
    unmount()

    render(<DiffPanel filePath="zzq/a.ts" original="a" modified="b" />)
    await screen.findByTestId('zzq-diff-editor')
    const next = captured.props!.options as Record<string, unknown>
    expect(next.renderSideBySide).toBe(true)
    expect(next.lineNumbers).toBe('off')
  })

  it('reveals the first change once and disposes the listener', async () => {
    captured.lineChanges = [{ modifiedStartLineNumber: 42, modifiedEndLineNumber: 44 }]
    render(<DiffPanel filePath="zzq/a.ts" original="a" modified="b" />)
    await screen.findByTestId('zzq-diff-editor')
    await waitFor(() => expect(captured.revealed).toEqual([42]))
    expect(captured.disposed).toBe(1)
  })

  it('falls back to the end line, then to line 1, when the start is absent', async () => {
    captured.lineChanges = [{ modifiedStartLineNumber: 0, modifiedEndLineNumber: 7 }]
    const { unmount } = render(<DiffPanel filePath="zzq/a.ts" original="a" modified="b" />)
    await screen.findByTestId('zzq-diff-editor')
    await waitFor(() => expect(captured.revealed).toEqual([7]))
    unmount()

    captured.revealed = []
    captured.lineChanges = [{ modifiedStartLineNumber: 0, modifiedEndLineNumber: 0 }]
    render(<DiffPanel filePath="zzq/b.ts" original="a" modified="b" />)
    await waitFor(() => expect(captured.revealed).toEqual([1]))
  })

  it('reveals nothing when the diff reports no changes', async () => {
    captured.lineChanges = []
    render(<DiffPanel filePath="zzq/a.ts" original="a" modified="b" />)
    await screen.findByTestId('zzq-diff-editor')
    await waitFor(() => expect(captured.disposed).toBe(1))
    expect(captured.revealed).toEqual([])
  })

  it('the footer copies the file path', () => {
    render(<DiffPanel filePath="zzq/deep/a.ts" original="a" modified="b" />)
    fireEvent.click(screen.getByTitle('Click to copy path'))
    expect(copyToClipboard).toHaveBeenCalledWith('zzq/deep/a.ts')
  })
})
