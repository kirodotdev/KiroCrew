/**
 * Path chips and path links in a reply that is still streaming.
 *
 * Only the text still being typed (the open markdown block, while it ends the
 * message) skips the existence probe on render, since a half-typed path is
 * itself a valid candidate. A block the assembler has closed, by a fence or a
 * widget after it, is final text: its chips are probed and open their file
 * while the turn still runs. A path-shaped span in the typed text is probed
 * when the pointer comes to it, and opens its file on a click once the probe
 * has confirmed it. Until then it is a copy chip, and its click copies inside
 * the click itself: WebKit refuses a clipboard write that waits on a probe.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, waitFor } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { __resetPathKindCache } from '../hooks/usePathKind'
import { copyToClipboard } from '../utils/clipboard'
import { kindsRequestCount, probedPaths, stubPathKinds } from './pathKindStub'

// Resolves `true`: a mock that resolved `undefined` would look like a refused
// clipboard write.
vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn(async () => true) }))

const CLOSED = '/home/user/proj/closed.py'
const LINK = '/home/user/proj/notes.md'
const TAIL = '/home/user/proj/tail.py'
const TAIL_LINK = '/home/user/proj/tail-notes.md'
const TAIL_FOUND = '/home/user/proj/found.py'

const CONTENT = [
  `I updated \`${CLOSED}\` and [the notes](${LINK}).`,
  '',
  '```text',
  'ok',
  '```',
  '',
  `Still typing \`${TAIL}\` and [more notes](${TAIL_LINK})`,
].join('\n')

const realFetch = globalThis.fetch

beforeEach(() => {
  __resetPathKindCache()
  vi.mocked(copyToClipboard).mockClear()
})
afterEach(() => { globalThis.fetch = realFetch })

function codeNamed(container: HTMLElement, text: string): HTMLElement {
  const code = [...container.querySelectorAll('code')].find((c) => c.textContent === text)
  expect(code, `code span ${text}`).toBeDefined()
  return code as HTMLElement
}

async function confirmedChip(container: HTMLElement, path: string): Promise<HTMLElement> {
  return waitFor(() => {
    const c = container.querySelector(`code[data-path="${path}"]`)
    expect(c).not.toBeNull()
    return c as HTMLElement
  })
}

describe('MarkdownRenderer path probes while a reply streams', () => {
  it('opens a path in a closed block before the turn ends and leaves the typed text unprobed', async () => {
    const fn = stubPathKinds(() => 'file')
    const onFileOpen = vi.fn()
    const { container, getByRole } = render(
      <MarkdownRenderer content={CONTENT} streaming glow onFileOpen={onFileOpen} />,
    )

    // The chip and the link in the closed block share one probe batch, so
    // once the chip is confirmed the link is too.
    const chip = await confirmedChip(container, CLOSED)
    expect(chip.getAttribute('data-chip-action')).toBe('navigate')
    fireEvent.click(chip)
    expect(onFileOpen).toHaveBeenCalledWith(CLOSED)

    fireEvent.click(getByRole('link', { name: 'the notes' }))
    expect(onFileOpen).toHaveBeenCalledWith(LINK)

    expect(probedPaths(fn)).toEqual(expect.arrayContaining([CLOSED, LINK]))
    expect(probedPaths(fn)).not.toContain(TAIL)
    expect(probedPaths(fn)).not.toContain(TAIL_LINK)
    expect(codeNamed(container, TAIL).getAttribute('data-chip-action')).toBe('copy')
  })

  it.each([
    ['a fence still streaming', `I updated \`${CLOSED}\`.\n\n\`\`\`text\nok\n`],
    ['a closed fence with nothing after it', `I updated \`${CLOSED}\`.\n\n\`\`\`text\nok\n\`\`\``],
  ])('probes the block before %s, which is the stream tail', async (_shape, content) => {
    const fn = stubPathKinds(() => 'file')
    const onFileOpen = vi.fn()
    const { container } = render(<MarkdownRenderer content={content} streaming glow onFileOpen={onFileOpen} />)

    const chip = await confirmedChip(container, CLOSED)
    expect(chip.getAttribute('data-chip-action')).toBe('navigate')
    expect(probedPaths(fn)).toContain(CLOSED)
    fireEvent.click(chip)
    expect(onFileOpen).toHaveBeenCalledWith(CLOSED)
  })

  it('copies a span in the typed text inside its click, with no probe', () => {
    const fn = stubPathKinds(() => 'file')
    const onFileOpen = vi.fn()
    const { container } = render(
      <MarkdownRenderer content={`Still typing \`${TAIL}\` and more`} streaming glow onFileOpen={onFileOpen} />,
    )
    const chip = codeNamed(container, TAIL)
    expect(chip.getAttribute('data-chip-action')).toBe('copy')

    fireEvent.click(chip)
    // Synchronously, before anything is awaited: the write runs inside the
    // click's user activation, which is what WebKit checks.
    expect(copyToClipboard).toHaveBeenCalledWith(TAIL)
    expect(onFileOpen).not.toHaveBeenCalled()
    expect(probedPaths(fn)).toEqual([])
  })

  it('probes a path in the typed text when the pointer comes to it, then opens it on click', async () => {
    const fn = stubPathKinds(() => 'file')
    const onFileOpen = vi.fn()
    const { container } = render(
      <MarkdownRenderer content={`Still typing \`${TAIL}\` and more`} streaming glow onFileOpen={onFileOpen} />,
    )
    fireEvent.pointerEnter(codeNamed(container, TAIL))
    const chip = await confirmedChip(container, TAIL)
    expect(chip.getAttribute('data-chip-action')).toBe('navigate')
    expect(probedPaths(fn)).toEqual([TAIL])

    fireEvent.click(chip)
    expect(onFileOpen).toHaveBeenCalledWith(TAIL)
    expect(copyToClipboard).not.toHaveBeenCalled()
  })

  it('keeps a pointed-at span in the typed text a copy chip when its probe names nothing', async () => {
    const fn = stubPathKinds((p) => (p === TAIL_FOUND ? 'file' : null))
    const onFileOpen = vi.fn()
    const { container } = render(
      <MarkdownRenderer content={`Still typing \`${TAIL}\` and \`${TAIL_FOUND}\``} streaming glow onFileOpen={onFileOpen} />,
    )
    // One commit, so both spans share one probe batch and are answered
    // together: once the found span is a path chip, the other has its verdict.
    act(() => {
      fireEvent.pointerEnter(codeNamed(container, TAIL))
      fireEvent.pointerEnter(codeNamed(container, TAIL_FOUND))
    })
    await confirmedChip(container, TAIL_FOUND)
    expect(kindsRequestCount(fn)).toBe(1)
    expect([...probedPaths(fn)].sort()).toEqual([TAIL, TAIL_FOUND].sort())

    const missing = codeNamed(container, TAIL)
    expect(missing.getAttribute('data-chip-action')).toBe('copy')
    fireEvent.click(missing)
    expect(copyToClipboard).toHaveBeenCalledWith(TAIL)
    expect(onFileOpen).not.toHaveBeenCalled()
  })

  it('probes the typed text once the reply ends', async () => {
    const fn = stubPathKinds(() => 'file')
    const onFileOpen = vi.fn()
    const { container, rerender } = render(
      <MarkdownRenderer content={CONTENT} streaming glow onFileOpen={onFileOpen} />,
    )
    rerender(<MarkdownRenderer content={CONTENT} onFileOpen={onFileOpen} />)
    const tail = await confirmedChip(container, TAIL)
    expect(tail.getAttribute('data-chip-action')).toBe('navigate')
    expect(probedPaths(fn)).toEqual(expect.arrayContaining([TAIL, TAIL_LINK]))
    fireEvent.click(tail)
    expect(onFileOpen).toHaveBeenCalledWith(TAIL)
  })
})
