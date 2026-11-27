import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, act } from '@testing-library/react'
import LevelBars from '../components/LevelBars'
import { createAudioSample } from '../hooks/mic'

afterEach(() => { vi.unstubAllGlobals() })

function stubFrames() {
  const frames: FrameRequestCallback[] = []
  const cancel = vi.fn()
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { frames.push(cb); return frames.length })
  vi.stubGlobal('cancelAnimationFrame', cancel)
  let now = 0
  const step = (delta = 50) => act(() => { now += delta; frames.splice(0).forEach(cb => cb(now)) })
  return { step, cancel }
}

const heights = (root: HTMLElement) =>
  Array.from(root.children).map(c => Number(/([\d.e-]+)%/.exec((c as HTMLElement).style.height)?.[1]))

describe('LevelBars', () => {
  it('is decorative: hidden from assistive tech', () => {
    stubFrames()
    const { getByTestId } = render(<LevelBars sampleRef={{ current: createAudioSample() }} />)
    expect(getByTestId('level-bars').getAttribute('aria-hidden')).toBe('true')
  })

  it('records the live level as a new bar at the right edge, with no re-render', () => {
    const { step } = stubFrames()
    const sampleRef = { current: createAudioSample() }
    const { getByTestId } = render(<LevelBars sampleRef={sampleRef} />)
    const root = getByTestId('level-bars')
    step()
    const silent = heights(root)
    sampleRef.current.level = 0.2
    step()
    const loud = heights(root)
    expect(loud.at(-1)).toBeGreaterThan((silent.at(-1) ?? 0) * 3)
    expect(loud[0]).toBe(silent[0])
  })

  it('scrolls older levels left and eventually removes them', () => {
    const { step } = stubFrames()
    const sampleRef = { current: createAudioSample() }
    const { getByTestId } = render(<LevelBars sampleRef={sampleRef} />)
    const root = getByTestId('level-bars')
    sampleRef.current.level = 0.2
    step()
    const spike = heights(root).at(-1)
    sampleRef.current.level = 0
    step()
    const after = heights(root)
    expect(after.at(-2)).toBe(spike)
    expect(after.at(-1)).toBeLessThan(spike ?? 0)
    Array.from({ length: root.children.length }, () => step())
    expect(Math.max(...heights(root))).toBe(8)
  })

  it('does not consume a new audio sample before the 40ms cadence', () => {
    const { step } = stubFrames()
    const sampleRef = { current: createAudioSample() }
    const { getByTestId } = render(<LevelBars sampleRef={sampleRef} />)
    const root = getByTestId('level-bars')
    step()
    sampleRef.current.level = 0.2
    step(20)
    expect(Math.max(...heights(root))).toBe(8)
    step(20)
    expect(Math.max(...heights(root))).toBeGreaterThan(50)
  })

  it('does not add a microphone or audio context of its own', () => {
    const { step } = stubFrames()
    const AudioContext = vi.fn()
    const getUserMedia = vi.fn()
    vi.stubGlobal('AudioContext', AudioContext)
    vi.stubGlobal('navigator', { mediaDevices: { getUserMedia } })
    render(<LevelBars sampleRef={{ current: createAudioSample() }} />)
    step()
    expect(AudioContext).not.toHaveBeenCalled()
    expect(getUserMedia).not.toHaveBeenCalled()
  })

  it('keeps newest samples at the visible edge on narrow composers', () => {
    stubFrames()
    const { getByTestId } = render(<LevelBars sampleRef={{ current: createAudioSample() }} />)
    const root = getByTestId('level-bars')
    expect(root.className).toContain('justify-end')
    expect(root.className).toContain('overflow-hidden')
    expect(root.lastElementChild).toBeTruthy()
  })

  it('fills the available width with fixed-width bars', () => {
    stubFrames()
    let resize: ((entries: Array<{ contentRect: { width: number } }>) => void) | undefined
    class StubResizeObserver {
      constructor(callback: (entries: Array<{ contentRect: { width: number } }>) => void) { resize = callback }
      observe() {}
      disconnect() {}
    }
    vi.stubGlobal('ResizeObserver', StubResizeObserver)
    const { getByTestId } = render(<LevelBars sampleRef={{ current: createAudioSample() }} />)
    const root = getByTestId('level-bars')
    act(() => { resize?.([{ contentRect: { width: 400 } }]) })
    expect(root.children).toHaveLength(100)
    act(() => { resize?.([{ contentRect: { width: 240 } }]) })
    expect(root.children).toHaveLength(60)
  })

  it('stops its frame loop on unmount', () => {
    const { cancel } = stubFrames()
    const { unmount } = render(<LevelBars sampleRef={{ current: createAudioSample() }} />)
    unmount()
    expect(cancel).toHaveBeenCalled()
  })
})
