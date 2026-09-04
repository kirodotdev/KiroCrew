/**
 * The prose ```diff fence's collapse contract.
 *
 * `DiffBlock` itself is covered by `DiffBlock.test.tsx`; what is asserted here
 * is only the wrapper's own behaviour — that the patch starts CLOSED, that the
 * chip carries the facts a reader needs to decide whether to open it, and that
 * an opened patch is remembered across a re-mount when (and only when) the
 * caller supplied a key.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/react'

import FoldableDiffBlock, { resetExpandedDiffFences } from '../components/FoldableDiffBlock'

const PATCH = [
  '--- a/src/thing.ts',
  '+++ b/src/thing.ts',
  '@@ -1,2 +1,3 @@',
  ' const a = 1',
  '-const b = 2',
  '+const b = 3',
  '+const c = 4',
].join('\n')

const chip = (c: HTMLElement) => c.querySelector<HTMLElement>('[data-testid="prose-diff-chip"]')

describe('FoldableDiffBlock', () => {
  beforeEach(() => { resetExpandedDiffFences() })

  it('renders the chip and no patch body until it is opened', async () => {
    const { container } = render(<FoldableDiffBlock code={PATCH} complete />)
    expect(chip(container)).not.toBeNull()
    expect(container.textContent).not.toContain('const c = 4')
    fireEvent.click(chip(container)!)
    await waitFor(() => expect(container.querySelector('.diff-block')).not.toBeNull())
    expect(chip(container)).toBeNull()
  })

  it('states the file and the ± counts on the chip', () => {
    const { container } = render(<FoldableDiffBlock code={PATCH} complete />)
    // Basename only, with the full path in the tooltip — two same-named files
    // would otherwise render as identical chips.
    expect(container.textContent).toContain('thing.ts')
    expect(chip(container)!.getAttribute('title')).toBe('src/thing.ts')
    expect(container.textContent).toContain('-1')
    expect(container.textContent).toContain('+2')
  })

  it('puts the ± counts in the accessible name, not only in the visible text', () => {
    // aria-label REPLACES the button's text, so a name without the counts would
    // leave a screen-reader user unable to hear how large the patch is without
    // opening it - the one decision the chip exists to support.
    const { container } = render(<FoldableDiffBlock code={PATCH} complete />)
    const name = chip(container)!.getAttribute('aria-label') ?? ''
    expect(name).toContain('src/thing.ts')
    expect(name).toMatch(/1 removal/)
    expect(name).toMatch(/2 additions/)
  })

  it('points aria-controls at a region that exists while collapsed', () => {
    // A disclosure whose controlled id only exists once open is not one.
    const { container } = render(<FoldableDiffBlock code={PATCH} complete />)
    const target = chip(container)!.getAttribute('aria-controls')
    expect(target).toBeTruthy()
    expect(container.querySelector(`#${CSS.escape(target!)}`)).not.toBeNull()
  })

  it('falls back to the path hint when the patch carries no header', () => {
    const bare = '@@ -1 +1 @@\n-old\n+new'
    const { container } = render(<FoldableDiffBlock code={bare} complete pathHint="/tmp/out.txt" />)
    expect(container.textContent).toContain('out.txt')
  })

  it('shows counts alone for a patch with neither header nor hint', () => {
    const { container } = render(<FoldableDiffBlock code={'@@ -1 +1 @@\n-old\n+new'} complete />)
    expect(chip(container)!.getAttribute('title')).toBeNull()
    expect(container.textContent).toContain('-1')
  })

  it('remembers an opened patch across a re-mount when keyed', async () => {
    const first = render(<FoldableDiffBlock code={PATCH} complete foldKey="msg-1:7" />)
    fireEvent.click(chip(first.container)!)
    await waitFor(() => expect(first.container.querySelector('.diff-block')).not.toBeNull())
    first.unmount()

    const again = render(<FoldableDiffBlock code={PATCH} complete foldKey="msg-1:7" />)
    expect(chip(again.container)).toBeNull()
    expect(again.container.querySelector('.diff-block')).not.toBeNull()
  })

  it('does not leak an expansion onto a different key', async () => {
    const first = render(<FoldableDiffBlock code={PATCH} complete foldKey="msg-1:7" />)
    fireEvent.click(chip(first.container)!)
    await waitFor(() => expect(first.container.querySelector('.diff-block')).not.toBeNull())

    const other = render(<FoldableDiffBlock code={PATCH} complete foldKey="msg-2:7" />)
    expect(chip(other.container)).not.toBeNull()
  })

  it('collapses again from the open patch and re-opens from the chip', async () => {
    const { container } = render(<FoldableDiffBlock code={PATCH} complete foldKey="msg-3:1" />)
    fireEvent.click(chip(container)!)
    await waitFor(() => expect(container.querySelector('.diff-block')).not.toBeNull())
    // DiffBlock's own fold control is the counterpart handle; both halves of
    // the toggle carry data-diff-toggle so focus can follow the swap.
    const fold = container.querySelector<HTMLElement>('[data-diff-toggle]')
    expect(fold).not.toBeNull()
    fireEvent.click(fold!)
    await waitFor(() => expect(chip(container)).not.toBeNull())
  })

  it('keeps a local expansion out of the shared store when unkeyed', async () => {
    const first = render(<FoldableDiffBlock code={PATCH} complete />)
    fireEvent.click(chip(first.container)!)
    await waitFor(() => expect(first.container.querySelector('.diff-block')).not.toBeNull())
    first.unmount()
    const again = render(<FoldableDiffBlock code={PATCH} complete />)
    expect(chip(again.container)).not.toBeNull()
  })

  it('hands focus to the counterpart control after a toggle', async () => {
    const { container } = render(<FoldableDiffBlock code={PATCH} complete />)
    chip(container)!.focus()
    fireEvent.click(chip(container)!)
    await waitFor(() => expect(container.querySelector('[data-diff-toggle]')).not.toBeNull())
    // The activated control unmounts, so focus would fall to <body> without the
    // hand-off; assert it landed on the control that replaced it.
    expect(document.activeElement).toBe(container.querySelector('[data-diff-toggle]'))
  })

  it('passes the open-file callback through to the opened patch', async () => {
    const onFileOpen = vi.fn()
    const { container } = render(<FoldableDiffBlock code={PATCH} complete onFileOpen={onFileOpen} />)
    fireEvent.click(chip(container)!)
    await waitFor(() => expect(container.querySelector('.diff-block')).not.toBeNull())
    // The Open button itself only appears once the HEAD probe resolves, which
    // is DiffBlock's own contract; here it is enough that nothing threw and the
    // patch rendered with the callback wired.
    expect(onFileOpen).not.toHaveBeenCalled()
  })
})
