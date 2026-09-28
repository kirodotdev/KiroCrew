/**
 * `PierreEditor` (the public wrapper in `src/pierre/index.tsx`) shows a plain
 * `<pre>` while the heavy editor chunk loads. A caller's `className` is the
 * surface's size cap -- a chat code block caps its editor at 480px -- so it
 * must bound that pre-chunk render as well as the editor that replaces it,
 * or a long snippet paints unbounded for as long as the chunk takes.
 *
 * The lazy chunk is pinned to never resolve, so what renders IS the Suspense
 * fallback -- a property of this test, not of the module runner's timing.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, cleanup } from '@testing-library/react'
import { PierreEditor } from '../pierre'

// Same shape as pierre.warmSwap.slowChunk.test.tsx: a suspending component
// rather than a never-resolving mock factory, so the behaviour does not depend
// on how the module runner treats a pending factory.
vi.mock('../pierre/PierreEditorImpl', () => {
  const pending = new Promise<never>(() => {})
  const StillLoading = () => { throw pending }
  return { PierreEditorImpl: StillLoading }
})

afterEach(cleanup)

describe('PierreEditor Suspense fallback', () => {
  it('carries the caller className onto the plain fallback', () => {
    const { container } = render(
      <PierreEditor
        file={{ name: 'snippet.txt', contents: 'a\nb\nc', cacheKey: 'k' }}
        onChange={() => {}}
        className="max-h-[480px]"
      />,
    )
    const pre = container.querySelector('pre.pierre-plain')
    expect(pre).not.toBeNull()
    expect(pre!.className).toContain('max-h-[480px]')
  })

  it('adds nothing when the caller passes no className', () => {
    const { container } = render(
      <PierreEditor
        file={{ name: 'snippet.txt', contents: 'a', cacheKey: 'k' }}
        onChange={() => {}}
      />,
    )
    const pre = container.querySelector('pre.pierre-plain')
    expect(pre).not.toBeNull()
    expect(pre!.className.trim().endsWith('whitespace-pre')).toBe(true)
  })
})
