import { renderHook } from '@testing-library/react'
import { Texture } from 'three'
import { describe, expect, it, vi } from 'vitest'
import { useKeptPicture } from './keptPicture'
import type { PageSource } from './source'

const sourceOf = (texture: Texture) => {
  const letGoPicture = vi.fn()
  const source = { frame: () => ({ texture }), keepPicture: () => texture, letGoPicture } as unknown as PageSource
  return { source, letGoPicture }
}

describe('the kept picture (M59)', () => {
  it('is let go when its panel goes', () => {
    const t = new Texture()
    const { source, letGoPicture } = sourceOf(t)
    const { result, unmount } = renderHook(() => useKeptPicture(source))
    result.current.keep()
    expect(result.current.kept.current).toBe(t)
    unmount()
    expect(letGoPicture).toHaveBeenCalledTimes(1)
  })

  it('is let go at its old source when the panel gets another', () => {
    const a = sourceOf(new Texture())
    const b = sourceOf(new Texture())
    const { result, rerender } = renderHook(({ s }) => useKeptPicture(s), { initialProps: { s: a.source } })
    result.current.keep()
    rerender({ s: b.source })
    expect(a.letGoPicture).toHaveBeenCalledTimes(1)
    expect(b.letGoPicture).not.toHaveBeenCalled()
    expect(result.current.kept.current).toBeNull()
  })

  it('asks the source nothing when nothing is kept', () => {
    const { source, letGoPicture } = sourceOf(new Texture())
    const { unmount } = renderHook(() => useKeptPicture(source))
    unmount()
    expect(letGoPicture).not.toHaveBeenCalled()
  })
})
