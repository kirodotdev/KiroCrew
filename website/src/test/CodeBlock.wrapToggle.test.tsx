// Feature: code blocks offer a line-wrap toggle for long lines (issue #17979).
//
// A reader can toggle a code block between horizontal scroll (overflow-x-auto)
// and soft-wrapping (whitespace-pre-wrap break-words) without having to pop out
// into an external view.
//
// 1. In the fallback branch (pre-mount or streaming), the plain <pre> toggles
//    classes between `overflow-x-auto` and `whitespace-pre-wrap break-words`.
// 2. In the highlighted branch, PierreCode receives `options={{ overflow: 'wrap' }}`
//    when wrap is enabled, and undefined when scroll is active.
// 3. Tall code blocks repeat the toggle in the footer action row, keeping the
//    two buttons synchronized.
// 4. Prose blocks wrap by default, but can be unwrapped if desired.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

import { CodeBlock } from '../components/CodeBlock'
import { __resetStagingForTests } from '../components/pierreStaging'

const pierreCalls: Array<{ options?: { overflow?: string } }> = []
vi.mock('../pierre', () => ({
  PierreCode: ({ file, options }: { file: { contents: string }; options?: { overflow?: string } }) => {
    pierreCalls.push({ options })
    return <div data-testid="pierre-mounted">{file.contents}</div>
  },
}))

vi.mock('../utils/clipboard', () => ({ copyCode: vi.fn(() => Promise.resolve(true)) }))
import { copyCode } from '../utils/clipboard'

const LONG_CODE = 'const someVeryLongIdentifier = "This is a very long line that would usually force horizontal scrolling in the code block regions";'

const originalGetBoundingClientRect = HTMLElement.prototype.getBoundingClientRect

function stubPierreSurfaceHeight(height: number) {
  HTMLElement.prototype.getBoundingClientRect = function (this: HTMLElement) {
    const h = this.classList.contains('pierre-surface') ? height : 0
    return { height: h, width: 0, top: 0, left: 0, right: 0, bottom: h, x: 0, y: 0, toJSON() {} } as DOMRect
  }
}

describe('CodeBlock: line-wrap toggle', () => {
  beforeEach(() => {
    __resetStagingForTests()
    pierreCalls.length = 0
    vi.mocked(copyCode).mockClear()
  })

  afterEach(() => {
    HTMLElement.prototype.getBoundingClientRect = originalGetBoundingClientRect
  })

  describe('fallback branch (plain <pre> stand-in)', () => {
    it('code blocks start unwrapped and can be toggled to wrapped and back', () => {
      const { container } = render(<CodeBlock code={LONG_CODE} lang="ts" complete={false} />)
      const pre = container.querySelector('pre')
      expect(pre).not.toBeNull()

      // Initially unwrapped (overflow-x-auto)
      expect(pre!.className).toContain('overflow-x-auto')
      expect(pre!.className).not.toContain('whitespace-pre-wrap')

      const toggleBtn = screen.getByTestId('code-block-wrap-toggle')
      expect(toggleBtn).toHaveAttribute('aria-pressed', 'false')
      expect(toggleBtn).toHaveAttribute('aria-label', 'Wrap lines')

      // Click to wrap
      fireEvent.click(toggleBtn)
      expect(pre!.className).toContain('whitespace-pre-wrap')
      expect(pre!.className).toContain('break-words')
      expect(pre!.className).not.toContain('overflow-x-auto')
      expect(toggleBtn).toHaveAttribute('aria-pressed', 'true')
      expect(toggleBtn).toHaveAttribute('aria-label', 'Unwrap lines')

      // Click again to unwrap
      fireEvent.click(toggleBtn)
      expect(pre!.className).toContain('overflow-x-auto')
      expect(pre!.className).not.toContain('whitespace-pre-wrap')
      expect(toggleBtn).toHaveAttribute('aria-pressed', 'false')
      expect(toggleBtn).toHaveAttribute('aria-label', 'Wrap lines')
    })

    it('prose blocks start wrapped and can be toggled to unwrapped', () => {
      const { container } = render(<CodeBlock code={LONG_CODE} lang="markdown" complete={false} />)
      const pre = container.querySelector('pre')
      expect(pre).not.toBeNull()

      // Initially wrapped by default for prose
      expect(pre!.className).toContain('whitespace-pre-wrap')
      expect(pre!.className).toContain('break-words')
      expect(pre!.className).not.toContain('overflow-x-auto')

      const toggleBtn = screen.getByTestId('code-block-wrap-toggle')
      expect(toggleBtn).toHaveAttribute('aria-pressed', 'true')
      expect(toggleBtn).toHaveAttribute('aria-label', 'Unwrap lines')

      // Click to unwrap prose
      fireEvent.click(toggleBtn)
      expect(pre!.className).toContain('overflow-x-auto')
      expect(pre!.className).not.toContain('whitespace-pre-wrap')
      expect(toggleBtn).toHaveAttribute('aria-pressed', 'false')
      expect(toggleBtn).toHaveAttribute('aria-label', 'Wrap lines')
    })
  })

  describe('highlighted branch (PierreCode options)', () => {
    it('passes overflow wrap option to PierreCode when wrap is enabled', () => {
      render(<CodeBlock code={LONG_CODE} lang="ts" complete />)
      expect(pierreCalls.length).toBeGreaterThan(0)
      // Initially undefined (scroll by default)
      expect(pierreCalls.at(-1)?.options).toBeUndefined()

      const toggleBtn = screen.getByTestId('code-block-wrap-toggle')
      fireEvent.click(toggleBtn)

      // Passes wrap option
      expect(pierreCalls.at(-1)?.options).toEqual({ overflow: 'wrap' })

      // Click again to return to scroll
      fireEvent.click(toggleBtn)
      expect(pierreCalls.at(-1)?.options).toBeUndefined()
    })
  })

  describe('tall blocks with repeated footer actions', () => {
    it('footer wrap button stays in sync with header button', () => {
      stubPierreSurfaceHeight(600)
      const { container } = render(<CodeBlock code={LONG_CODE} lang="ts" complete={false} />)
      const pre = container.querySelector('pre')

      const toggleButtons = screen.getAllByTestId('code-block-wrap-toggle')
      expect(toggleButtons).toHaveLength(2)

      const [headerToggle, footerToggle] = toggleButtons
      expect(headerToggle).toHaveAttribute('aria-pressed', 'false')
      expect(footerToggle).toHaveAttribute('aria-pressed', 'false')

      // Clicking footer toggle toggles the whole block
      fireEvent.click(footerToggle)
      expect(pre!.className).toContain('whitespace-pre-wrap')
      expect(headerToggle).toHaveAttribute('aria-pressed', 'true')
      expect(footerToggle).toHaveAttribute('aria-pressed', 'true')

      // Clicking header toggle toggles it back
      fireEvent.click(headerToggle)
      expect(pre!.className).toContain('overflow-x-auto')
      expect(headerToggle).toHaveAttribute('aria-pressed', 'false')
      expect(footerToggle).toHaveAttribute('aria-pressed', 'false')
    })
  })

  describe('copy functionality remains unaffected', () => {
    it('copying works in both wrapped and unwrapped states', async () => {
      render(<CodeBlock code={LONG_CODE} lang="ts" complete={false} />)
      const toggleBtn = screen.getByTestId('code-block-wrap-toggle')

      // Copy in unwrapped state
      fireEvent.click(screen.getByLabelText('Copy'))
      await waitFor(() => expect(copyCode).toHaveBeenCalledWith(LONG_CODE))
      await waitFor(() => expect(screen.getByLabelText('Copied!')).toBeInTheDocument())

      // Toggle wrap
      fireEvent.click(toggleBtn)

      // Copy in wrapped state
      fireEvent.click(screen.getByLabelText('Copied!'))
      await waitFor(() => expect(copyCode).toHaveBeenCalledTimes(2))
    })
  })
})
