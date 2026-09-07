// Feature: a code block taller than TALL_CODE_BLOCK_PX repeats its action row
// (copy, plus any caller-supplied headerActions) at the bottom, so copying or
// editing a long block never costs a scroll back to its top (issue #8227).
//
// happy-dom does not lay out real content, so `getBoundingClientRect` on every
// element returns zeros by default. The measured height CodeBlock reacts to
// comes from `useMeasuredHeight`'s ref callback, which reads the height
// synchronously on mount -- so the stub below has to be in place before
// render, keyed off the `.pierre-surface` class (the node the ref is bound
// to) rather than per-instance the way `sidePanelReserve.test.ts` does it,
// since there is no post-render hook to reach for before that first read.

import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { render, screen, within, fireEvent, waitFor } from '@testing-library/react'

import { CodeBlock } from '../components/CodeBlock'

vi.mock('../pierre', () => ({
  PierreCode: ({ file }: { file: { contents: string } }) => (
    <div data-testid="pierre-mounted">{file.contents}</div>
  ),
}))

vi.mock('../utils/clipboard', () => ({ copyCode: vi.fn() }))
import { copyCode } from '../utils/clipboard'

const originalGetBoundingClientRect = HTMLElement.prototype.getBoundingClientRect

function stubPierreSurfaceHeight(height: number) {
  HTMLElement.prototype.getBoundingClientRect = function (this: HTMLElement) {
    const h = this.classList.contains('pierre-surface') ? height : 0
    return { height: h, width: 0, top: 0, left: 0, right: 0, bottom: h, x: 0, y: 0, toJSON() {} } as DOMRect
  }
}

describe('CodeBlock: tall blocks repeat their action row at the bottom', () => {
  afterEach(() => {
    HTMLElement.prototype.getBoundingClientRect = originalGetBoundingClientRect
  })

  it('does not add a footer row for a short block', () => {
    stubPierreSurfaceHeight(120)
    render(<CodeBlock code="const x = 1" lang="ts" complete />)
    expect(screen.getAllByLabelText('Copy')).toHaveLength(1)
    expect(screen.queryByTestId('code-block-footer')).toBeNull()
  })

  it('adds a second copy action at the bottom once the block exceeds the threshold', () => {
    stubPierreSurfaceHeight(600)
    render(<CodeBlock code="const x = 1" lang="ts" complete />)
    expect(screen.getAllByLabelText('Copy')).toHaveLength(2)
  })

  it('repeats headerActions (e.g. the edit button) in the footer too', () => {
    stubPierreSurfaceHeight(600)
    render(
      <CodeBlock
        code="const x = 1"
        lang="ts"
        complete
        headerActions={<button aria-label="Edit code">edit</button>}
      />,
    )
    expect(screen.getAllByLabelText('Edit code')).toHaveLength(2)
  })

  it('renders footerActions instead of headerActions when the two differ', () => {
    // max-two-buttons-per-row (AUTOSDE, blocking): a caller whose header
    // already carries 2+ actions passes a trimmed footerActions rather than
    // letting the footer inherit all of them and grow past the cap.
    stubPierreSurfaceHeight(600)
    render(
      <CodeBlock
        code="const x = 1"
        lang="ts"
        complete
        headerActions={<button aria-label="Run">run</button>}
        footerActions={<button aria-label="Edit code">edit</button>}
      />,
    )
    const footer = within(screen.getByTestId('code-block-footer'))
    expect(footer.queryByLabelText('Run')).toBeNull()
    expect(footer.getByLabelText('Edit code')).toBeInTheDocument()
    // The header is untouched.
    expect(screen.getByLabelText('Run')).toBeInTheDocument()
  })

  it('still adds the footer copy action on a tall block that is still streaming', () => {
    // The header's own copy button is available while streaming (a partial
    // snippet is still worth copying), so the footer duplicate follows the
    // same rule rather than waiting for `complete` -- otherwise the footer
    // would pop in mid-stream the instant the block finishes, which reads as
    // more layout shift than just being there from the start.
    stubPierreSurfaceHeight(600)
    render(<CodeBlock code="const x = 1" lang="ts" complete={false} />)
    expect(screen.getAllByLabelText('Copy')).toHaveLength(2)
  })
})

describe('CodeBlock: the copy button never confirms a copy that failed', () => {
  beforeEach(() => {
    vi.mocked(copyCode).mockReset()
  })
  afterEach(() => {
    HTMLElement.prototype.getBoundingClientRect = originalGetBoundingClientRect
  })

  it('flips to the Copied tick on a successful copy', async () => {
    vi.mocked(copyCode).mockResolvedValue(true)
    render(<CodeBlock code="const x = 1" lang="ts" complete />)
    fireEvent.click(screen.getByLabelText('Copy'))
    await waitFor(() => expect(screen.getByLabelText('Copied!')).toBeInTheDocument())
  })

  it('stays on the idle icon when copyCode resolves false', async () => {
    vi.mocked(copyCode).mockResolvedValue(false)
    render(<CodeBlock code="const x = 1" lang="ts" complete />)
    fireEvent.click(screen.getByLabelText('Copy'))
    await waitFor(() => expect(copyCode).toHaveBeenCalled())
    expect(screen.queryByLabelText('Copied!')).toBeNull()
    expect(screen.getByLabelText('Copy')).toBeInTheDocument()
  })

  it('stays on the idle icon when copyCode rejects', async () => {
    vi.mocked(copyCode).mockRejectedValue(new Error('clipboard denied'))
    render(<CodeBlock code="const x = 1" lang="ts" complete />)
    fireEvent.click(screen.getByLabelText('Copy'))
    await waitFor(() => expect(copyCode).toHaveBeenCalled())
    expect(screen.queryByLabelText('Copied!')).toBeNull()
    expect(screen.getByLabelText('Copy')).toBeInTheDocument()
  })
})
