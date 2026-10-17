import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, screen, waitFor, within } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { copyToClipboard } from '../utils/clipboard'
import { __resetPathKindCache } from '../hooks/usePathKind'
import { stubPathKinds } from './pathKindStub'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn(async () => undefined) }))

const realFetch = globalThis.fetch

beforeEach(() => { vi.mocked(copyToClipboard).mockClear() })

/** Dispatch a real cancelable click and report whether the default action
 *  survived. `preventDefault` from ANY listener in propagation cancels an
 *  anchor's navigation, so this is the mechanism under test — not a spy on
 *  window.location, which jsdom does not implement. */
function clickAndReadDefault(el: Element): boolean {
  const ev = new MouseEvent('click', { bubbles: true, cancelable: true })
  el.dispatchEvent(ev)
  return ev.defaultPrevented
}

describe('inline code used as a link label', () => {
  it('keeps the anchor navigable instead of copying', async () => {
    render(<MarkdownRenderer content={'See [`https://example.com/x`](https://example.com/x) now.'} />)
    const code = await screen.findByText('https://example.com/x')

    expect(code.tagName).toBe('CODE')
    expect(code.closest('a')).toHaveAttribute('href', 'https://example.com/x')

    // The defect: the span's copy handler called preventDefault, which cancels
    // the enclosing anchor's navigation from anywhere in propagation.
    expect(clickAndReadDefault(code)).toBe(false)
    expect(copyToClipboard).not.toHaveBeenCalled()

    // Not a control either: the anchor owns the click, so the span must not
    // advertise itself as a button or take the keyboard.
    expect(code).not.toHaveAttribute('role', 'button')
    expect(code).not.toHaveAttribute('tabindex')
  })

  it('still copies a backticked span that is NOT inside a link', async () => {
    render(<MarkdownRenderer content={'Open `https://example.com/x` now.'} />)
    const code = await screen.findByText('https://example.com/x')

    expect(code).toHaveAttribute('role', 'button')
    fireEvent.click(code)
    expect(copyToClipboard).toHaveBeenCalledWith('https://example.com/x')
  })

  it('still copies a backticked command in prose', async () => {
    render(<MarkdownRenderer content={'Run `npm test` please.'} />)
    const code = await screen.findByText('npm test')
    fireEvent.click(code)
    expect(copyToClipboard).toHaveBeenCalledWith('npm test')
  })

  it('leaves a plain-text link label navigable', async () => {
    render(<MarkdownRenderer content={'See [the docs](https://example.com/y) now.'} />)
    const anchor = await screen.findByText('the docs')
    expect(anchor.tagName).toBe('A')
    expect(clickAndReadDefault(anchor)).toBe(false)
  })
})

describe('a real local path used as a link label', () => {
  afterEach(() => {
    globalThis.fetch = realFetch
    __resetPathKindCache()
  })

  // The issue's own shape (a repo file linked to its forge URL) and a generic
  // host, for a file and for a directory.
  const cases = [
    { kind: 'file', path: 'src/kiro_crew/context.py', href: 'https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/context.py' },
    { kind: 'file', path: 'src/app/main.py', href: 'https://example.com/blob/src/app/main.py' },
    { kind: 'dir', path: './src/app/', href: 'https://example.com/tree/src/app' },
  ] as const

  for (const { kind, path, href } of cases) {
    it(`follows the link instead of opening the ${kind} (${href})`, async () => {
      stubPathKinds(() => kind)
      const onFileOpen = vi.fn()
      const onFolderOpen = vi.fn()
      const { container } = render(
        <MarkdownRenderer
          content={`Open \`${path}\` or see [\`${path}\`](${href}) now.`}
          onFileOpen={onFileOpen}
          onFolderOpen={onFolderOpen}
        />,
      )
      // Positive control: the SAME path outside the link becomes a confirmed
      // chip, so the probe stub really answered and the linked span below is
      // judged after the point where it, too, would have turned into a chip.
      await waitFor(() => expect(container.querySelector('code[data-path]')).not.toBeNull())
      expect(container.querySelectorAll('code[data-path]')).toHaveLength(1)

      const anchor = container.querySelector(`a[href="${href}"]`)
      expect(anchor).not.toBeNull()
      const code = anchor!.querySelector('code')!
      expect(code).toHaveTextContent(path)

      // The defect: the chip's handler called preventDefault, which cancelled
      // the anchor's navigation and opened the local file instead.
      expect(clickAndReadDefault(code)).toBe(false)
      expect(onFileOpen).not.toHaveBeenCalled()
      expect(onFolderOpen).not.toHaveBeenCalled()

      // Plain code text, not a control: no button nested inside the link, no
      // path claim, and no space held for a chip glyph that will never arrive.
      expect(code).not.toHaveAttribute('data-path')
      expect(code).not.toHaveAttribute('role', 'button')
      expect(code).not.toHaveAttribute('tabindex')
      expect(within(anchor as HTMLElement).queryByRole('button')).toBeNull()
      expect(code.querySelector('svg')).toBeNull()
    })
  }

  // A link to a LOCAL address keeps today's behaviour: the href is often the
  // same file the label names, and the confirmed chip is what opens it — the
  // anchor itself opens a root-relative path, but not a relative one.
  for (const href of ['/repo/src/app/main.py', 'src/app/main.py']) {
    it(`still opens the file when the link points at a local address (${href})`, async () => {
      stubPathKinds(() => 'file')
      const onFileOpen = vi.fn()
      const { container } = render(
        <MarkdownRenderer content={`See [\`${href}\`](${href}) now.`} onFileOpen={onFileOpen} />,
      )
      const linkedCode = () => container.querySelector(`a[href="${href}"] code`)!
      // Re-queried each try: the span is re-rendered when the probe settles.
      await waitFor(() => {
        fireEvent.click(linkedCode())
        expect(onFileOpen).toHaveBeenCalledWith(href)
      })
      // The label's own chip took the click, as on main — not the anchor's
      // root-relative path handling, which would open the same file.
      expect(linkedCode()).toHaveAttribute('data-path', href)
    })
  }

  it('keeps a non-path label inert inside a link to a local address (#5730)', async () => {
    const { container } = render(<MarkdownRenderer content={'See [`npm test`](docs/testing.md) now.'} />)
    await screen.findByText('npm test')
    const code = container.querySelector('a[href="docs/testing.md"] code')!
    expect(clickAndReadDefault(code)).toBe(false)
    expect(copyToClipboard).not.toHaveBeenCalled()
    expect(code).not.toHaveAttribute('role', 'button')
  })
})
