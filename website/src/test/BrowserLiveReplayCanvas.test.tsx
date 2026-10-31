/**
 * ReplayCanvas: a frame that will not load (the recording was cleared on the
 * gateway, the frame URL answers 404) wipes the picture and says so, instead of
 * leaving the previous frame on screen while the timeline moves on.
 */
import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ReplayCanvas } from '../pages/chat/BrowserLiveReplayStage'
import type { Recording } from '../hooks/useBrowserLive.replay'

/** An `Image` that loads unless its URL contains "gone". */
class FakeImage {
  decoding = ''
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  set src(url: string) {
    setTimeout(() => (url.includes('gone') ? this.onerror?.() : this.onload?.()), 0)
  }
}

function recording(urls: string[]): Recording {
  return {
    turnId: 't1',
    startedTs: 0,
    endedTs: 1,
    live: false,
    width: 100,
    height: 50,
    frames: urls.map((src, i) => ({ i, ts: i * 100, w: 100, h: 50, src })),
    events: [],
  }
}

describe('ReplayCanvas', () => {
  let ctx: { drawImage: ReturnType<typeof vi.fn>; clearRect: ReturnType<typeof vi.fn> }

  beforeEach(() => {
    vi.useFakeTimers()
    vi.stubGlobal('Image', FakeImage)
    ctx = { drawImage: vi.fn(), clearRect: vi.fn() }
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(ctx as unknown as CanvasRenderingContext2D)
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  const flush = async () => { await act(async () => { await vi.runAllTimersAsync() }) }

  it('clears the picture and shows the error when a frame fails to load, and recovers on the next good frame', async () => {
    const rec = recording(['/f/ok-1.jpg', '/f/gone-2.jpg', '/f/ok-3.jpg'])
    const { rerender } = render(<ReplayCanvas rec={rec} index={0} label="Replay" />)
    await flush()
    expect(ctx.drawImage).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId('browser-replay-frame-error')).toBeNull()

    rerender(<ReplayCanvas rec={rec} index={1} label="Replay" />)
    await flush()
    expect(ctx.clearRect).toHaveBeenCalled()
    expect(ctx.drawImage).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('browser-replay-frame-error').textContent).toContain('could not be loaded')
    expect(screen.getByTestId('browser-replay-canvas').getAttribute('data-failed')).toBe('true')

    rerender(<ReplayCanvas rec={rec} index={2} label="Replay" />)
    await flush()
    expect(ctx.drawImage).toHaveBeenCalledTimes(2)
    expect(screen.queryByTestId('browser-replay-frame-error')).toBeNull()
    expect(screen.getByTestId('browser-replay-canvas').getAttribute('data-failed')).toBeNull()
  })

  it('shows the notice only while the failed frame is the current one, and names no picture then', async () => {
    const rec = recording(['/f/ok-1.jpg', '/f/gone-2.jpg', '/f/ok-3.jpg'])
    const { rerender } = render(<ReplayCanvas rec={rec} index={1} label="Replay" />)
    await flush()
    expect(screen.getByTestId('browser-replay-frame-error')).toBeTruthy()
    // Nothing is drawn, so nothing claims to be a picture; the canvas stays hidden.
    expect(screen.queryByRole('img', { name: 'Replay' })).toBeNull()
    expect(screen.getByTestId('browser-replay-canvas').getAttribute('aria-hidden')).toBe('true')

    // Scrubbing on: the next frame has not loaded yet, but the notice is not its.
    rerender(<ReplayCanvas rec={rec} index={2} label="Replay" />)
    expect(screen.queryByTestId('browser-replay-frame-error')).toBeNull()
    expect(screen.getByRole('img', { name: 'Replay' })).toBeTruthy()
    await flush()
    expect(ctx.drawImage).toHaveBeenCalledTimes(1)

    // Back to the failed frame: it is tried again and fails again, for itself.
    rerender(<ReplayCanvas rec={rec} index={1} label="Replay" />)
    await flush()
    expect(screen.getByTestId('browser-replay-frame-error')).toBeTruthy()
    expect(screen.queryByRole('img', { name: 'Replay' })).toBeNull()
  })
})
