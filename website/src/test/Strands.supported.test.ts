import { describe, it, expect, vi, afterEach } from 'vitest'
import { strandsSupported } from '../components/Strands'

/**
 * `strandsSupported()` decides whether the dictation panel can draw its shader
 * or falls back to the bar meter. The composer asks it the first time a
 * recording wants the panel, so the three answers are pinned here directly.
 */
describe('strandsSupported', () => {
  afterEach(() => { vi.restoreAllMocks() })

  it('is true when a WebGL2 context is created', () => {
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({} as unknown as RenderingContext)
    expect(strandsSupported()).toBe(true)
  })

  it('is false when the browser has no WebGL2', () => {
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
    expect(strandsSupported()).toBe(false)
  })

  it('is false when creating the context throws', () => {
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(() => { throw new Error('blocked') })
    expect(strandsSupported()).toBe(false)
  })
})
