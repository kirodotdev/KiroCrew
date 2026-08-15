/**
 * The pencil swaps the rendered block for a scratch editor over a LOCAL copy of
 * the snippet: edits never reach the message, but the editor's own Copy button
 * must hand out what the user currently sees, not the original text. These
 * cover that contract plus the editor chrome around it (language label,
 * discard-on-close, and which header actions exist while a block is still
 * streaming).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import { setTerminalEnabledFlag } from '../utils/terminalRegistry'

const hoisted = vi.hoisted(() => ({ files: [] as { name: string; contents: string }[] }))

vi.mock('../pierre', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  PierreEditor: ({ file, onChange }: {
    file: { name: string; contents: string }
    onChange?: (v: string) => void
  }) => {
    hoisted.files.push(file)
    return (
      <textarea
        data-testid="pierre-editor"
        aria-label="scratch editor"
        defaultValue={file.contents}
        onChange={e => onChange?.(e.target.value)}
      />
    )
  },
}))

// The rendered block is a separate component with its own tests; here it only
// has to be distinguishable from the editor and to host the header actions.
vi.mock('../components/CodeBlock', () => ({
  CodeBlock: ({ code, lang, headerActions }: {
    code: string; lang?: string; headerActions?: React.ReactNode
  }) => (
    <div data-testid="rendered-block" data-lang={lang ?? ''}>
      <span data-testid="rendered-code">{code}</span>
      {headerActions}
    </div>
  ),
}))

vi.mock('../utils/clipboard', () => ({ copyCode: vi.fn(() => Promise.resolve()) }))

import { copyCode } from '../utils/clipboard'
import EditableCodeBlock from '../components/EditableCodeBlock'

const openEditor = () => fireEvent.click(screen.getByLabelText('Edit code block'))
const editorFile = () => hoisted.files[hoisted.files.length - 1]
/** The editor header holds exactly the close and copy buttons; copy is the one
 *  the catalog does not name, so identify it by elimination rather than pinning
 *  a label. */
const copyButton = () => screen.getAllByRole('button')
  .filter(b => b.getAttribute('aria-label') !== 'Close editor')[0]

beforeEach(() => {
  hoisted.files.length = 0
  vi.mocked(copyCode).mockClear()
})

afterEach(() => {
  cleanup()
  setTerminalEnabledFlag(false)
})

describe('EditableCodeBlock editor chrome', () => {
  it('offers no header actions while the block is still streaming', () => {
    render(<EditableCodeBlock code="const a = 1" lang="ts" complete={false} />)
    expect(screen.getByTestId('rendered-block')).toBeInTheDocument()
    expect(screen.queryByLabelText('Edit code block')).toBeNull()
  })

  it('swaps the rendered block for the editor and back again', () => {
    render(<EditableCodeBlock code="const a = 1" lang="ts" complete />)
    openEditor()
    expect(screen.getByTestId('pierre-editor')).toBeInTheDocument()
    expect(screen.queryByTestId('rendered-block')).toBeNull()
    expect(screen.getByText('ts')).toBeInTheDocument()

    fireEvent.click(screen.getByLabelText('Close editor'))
    expect(screen.getByTestId('rendered-block')).toBeInTheDocument()
    expect(screen.queryByTestId('pierre-editor')).toBeNull()
  })

  it('labels an unlabelled snippet without inventing a language', () => {
    render(<EditableCodeBlock code="plain text" complete />)
    openEditor()
    expect(screen.getByText('code')).toBeInTheDocument()
    expect(editorFile().name).toBe('snippet.txt')
  })

  it('names the editor buffer after the snippet language', () => {
    render(<EditableCodeBlock code="print(1)" lang="python" complete />)
    openEditor()
    expect(editorFile().name).toBe('snippet.python')
  })
})

describe('EditableCodeBlock scratch copy', () => {
  it('copies the untouched snippet when nothing was edited', () => {
    render(<EditableCodeBlock code="const a = 1" lang="ts" complete />)
    openEditor()
    fireEvent.click(copyButton())
    expect(copyCode).toHaveBeenCalledWith('const a = 1')
  })

  it('copies the live edit, not the message’s original text', () => {
    render(<EditableCodeBlock code="const a = 1" lang="ts" complete />)
    openEditor()
    fireEvent.change(screen.getByTestId('pierre-editor'), { target: { value: 'const a = 99' } })
    fireEvent.click(copyButton())
    expect(copyCode).toHaveBeenCalledWith('const a = 99')
    expect(copyCode).not.toHaveBeenCalledWith('const a = 1')
  })

  it('discards the scratch edit when the editor is closed', () => {
    render(<EditableCodeBlock code="const a = 1" lang="ts" complete />)
    openEditor()
    fireEvent.change(screen.getByTestId('pierre-editor'), { target: { value: 'throwaway' } })
    fireEvent.click(screen.getByLabelText('Close editor'))

    openEditor()
    fireEvent.click(copyButton())
    expect(copyCode).toHaveBeenCalledWith('const a = 1')
  })

  it('acknowledges the copy and reverts the button after the flash', async () => {
    render(<EditableCodeBlock code="const a = 1" lang="ts" complete />)
    openEditor()
    const idle = copyButton().getAttribute('aria-label')
    fireEvent.click(copyButton())
    const copied = copyButton().getAttribute('aria-label')
    expect(copied).not.toBe(idle)
    expect(copyButton().getAttribute('title')).toBe(copied)

    await waitFor(() => expect(copyButton().getAttribute('aria-label')).toBe(idle), { timeout: 4000 })
  })
})

describe('EditableCodeBlock run-in-terminal action', () => {
  it('offers the run action on a shell snippet once a terminal is available', () => {
    setTerminalEnabledFlag(true)
    render(<EditableCodeBlock code="ls -la" lang="bash" complete />)
    expect(screen.getByLabelText('Run in terminal')).toBeInTheDocument()
  })

  it('withholds it for a non-shell language', () => {
    setTerminalEnabledFlag(true)
    render(<EditableCodeBlock code="const a = 1" lang="ts" complete />)
    expect(screen.queryByLabelText('Run in terminal')).toBeNull()
    expect(screen.getByLabelText('Edit code block')).toBeInTheDocument()
  })

  it('withholds it for a snippet carrying no language at all', () => {
    setTerminalEnabledFlag(true)
    render(<EditableCodeBlock code="ls -la" complete />)
    expect(screen.queryByLabelText('Run in terminal')).toBeNull()
  })

  it('withholds it when no terminal is available', () => {
    render(<EditableCodeBlock code="ls -la" lang="bash" complete />)
    expect(screen.queryByLabelText('Run in terminal')).toBeNull()
  })
})
