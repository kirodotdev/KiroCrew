import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

vi.mock('../components/CodeEditor', () => ({
  CodeEditor: ({ content, lang, lineNums, wordWrap, onChange }: {
    content: string
    lang: string
    lineNums: boolean
    wordWrap: boolean
    onChange: (value: string) => void
  }) => (
    <textarea
      aria-label="editable-code"
      data-language={lang}
      data-line-numbers={String(lineNums)}
      data-word-wrap={String(wordWrap)}
      value={content}
      onChange={event => onChange(event.target.value)}
    />
  ),
}))

vi.mock('../pierre', () => ({
  PierreCode: ({ file, langHint, options }: {
    file: { contents: string }
    langHint: string
    options: { disableLineNumbers: boolean; overflow: string }
  }) => (
    <pre
      data-language={langHint}
      data-line-numbers={String(!options.disableLineNumbers)}
      data-overflow={options.overflow}
    >
      {file.contents}
    </pre>
  ),
}))

import WorkflowSourceCode from '../apps/workflows/WorkflowSourceCode'

const SOURCE = 'async def workflow(ctx):\n    return 1\n'

describe('WorkflowSourceCode', () => {
  it('renders an editable Python surface with line numbers and byte-preserving changes', () => {
    const onChange = vi.fn()
    render(
      <WorkflowSourceCode
        source={SOURCE}
        onChange={onChange}
        ariaLabel="Workflow source"
      />,
    )

    const surface = screen.getByRole('region', { name: 'Workflow source' })
    const editor = screen.getByLabelText('editable-code')
    expect(surface).toHaveAttribute('data-workflow-source-mode', 'editable')
    expect(editor).toHaveAttribute('data-language', 'python')
    expect(editor).toHaveAttribute('data-line-numbers', 'true')
    expect(editor).toHaveAttribute('data-word-wrap', 'false')

    fireEvent.change(editor, { target: { value: SOURCE + '# exact bytes' } })
    expect(onChange).toHaveBeenCalledWith(SOURCE + '# exact bytes')
  })

  it('renders a read-only highlighted Python surface with line numbers', () => {
    render(<WorkflowSourceCode source={SOURCE} ariaLabel="Workflow source" compact />)

    const surface = screen.getByRole('region', { name: 'Workflow source' })
    const code = surface.querySelector('pre')
    expect(surface).toHaveAttribute('data-workflow-source-mode', 'read-only')
    expect(code).toHaveAttribute('data-language', 'python')
    expect(code).toHaveAttribute('data-line-numbers', 'true')
    expect(code).toHaveAttribute('data-overflow', 'scroll')
    expect(code?.textContent).toBe(SOURCE)
  })

  it('formats TaskRunner plans as YAML', () => {
    const source = 'agents:\n  test:\n    prompt: run tests\n'
    render(
      <WorkflowSourceCode
        source={source}
        sourceFormat="task-plan"
        ariaLabel="Workflow source"
      />,
    )

    expect(
      screen.getByRole('region', { name: 'Workflow source' }).querySelector('pre'),
    ).toHaveAttribute('data-language', 'yaml')
  })
})
