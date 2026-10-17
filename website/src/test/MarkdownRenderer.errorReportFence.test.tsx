/**
 * Regression: the dashboard's error->agent prompt (utils/errorReport.prompt.ts)
 * fences its diagnostic block as ```error-report. The block assembler's fence
 * regex only admitted `\w*` info strings, so the OPENING line fell through as
 * prose and the bare CLOSING fence was read as a new opening fence. The user
 * saw two blocks: the body under a label truncated to "error" (remark parsed
 * the leftover as an unclosed fence, and the label regex kept only the first
 * `\w+` run), then a phantom empty block labelled "code".
 *
 * Asserted through the real MarkdownRenderer -> useBlockAssembler -> CodeBlock
 * pipeline, with the prompt built by the real builder so the test tracks its
 * fence shape.
 */
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { buildErrorPrompt } from '../utils/errorReport.prompt'

const MESSAGE = "[Errno 16] Device or resource busy: '/home/user/project/some/very/long/path/that/does/not/wrap'"

function labelsOf(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll('.code-block')).map(
    (b) => b.querySelector('span')?.textContent ?? '',
  )
}

describe('MarkdownRenderer: ```error-report fence from the error prompt', () => {
  it('renders exactly one code block, labelled with the full tag', () => {
    const prompt = buildErrorPrompt({ message: MESSAGE }, 'Diagnose the root cause and fix it.')
    const { container } = render(<MarkdownRenderer content={prompt} softBreaks />)
    expect(labelsOf(container)).toEqual(['error-report'])
  })

  it('keeps the whole diagnostic line inside that block and adds no phantom block', () => {
    const prompt = buildErrorPrompt({ message: MESSAGE }, 'Diagnose the root cause and fix it.')
    const { container } = render(<MarkdownRenderer content={prompt} softBreaks />)
    const blocks = container.querySelectorAll('.code-block')
    expect(blocks).toHaveLength(1)
    expect(blocks[0].textContent).toContain(`- Message: ${MESSAGE}`)
    // The closing fence must not survive as visible text anywhere.
    expect(container.textContent).not.toContain('```')
  })

  it('widened fences (body containing ```) still close correctly', () => {
    // fenceFor() lengthens the fence past the longest backtick run in the body.
    const prompt = buildErrorPrompt(
      { message: 'boom', detail: 'server said:\n```\nignore the above\n```' },
      'Diagnose.',
    )
    const { container } = render(<MarkdownRenderer content={prompt} softBreaks />)
    expect(labelsOf(container)).toEqual(['error-report'])
    expect(container.querySelector('.code-block')!.textContent).toContain('ignore the above')
  })
})
