/**
 * `watchTrackHealth` reports a capture track the browser stopped feeding.
 *
 * WebKit can hand back a track that is muted, or end it, without any error:
 * the level reads 0 and no transcript arrives, which looks exactly like a
 * silent speaker. A fresh track is also muted for a moment before audio flows,
 * so only a mute that outlasts the grace period counts.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { i18nT } from '../i18n/t'
import { TRACK_MUTE_GRACE_MS, watchTrackHealth } from '../hooks/mic'

const MUTED = 'hooks.mic.browser_muted_the_microphone'
const ENDED = 'hooks.mic.microphone_disconnected_by_browser'

class FakeTrack extends EventTarget {
  muted = false
  readyState = 'live'
}

function streamOf(track: FakeTrack) {
  return { getAudioTracks: () => [track] } as unknown as MediaStream
}

function mute(track: FakeTrack) { track.muted = true; track.dispatchEvent(new Event('mute')) }
function unmute(track: FakeTrack) { track.muted = false; track.dispatchEvent(new Event('unmute')) }

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { vi.useRealTimers() })

describe('watchTrackHealth', () => {
  it('reports a mute that outlasts the grace period', () => {
    const track = new FakeTrack()
    const onFailure = vi.fn()
    watchTrackHealth(streamOf(track), onFailure)

    mute(track)
    vi.advanceTimersByTime(TRACK_MUTE_GRACE_MS - 1)
    expect(onFailure).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1)

    expect(onFailure).toHaveBeenCalledTimes(1)
    expect(onFailure).toHaveBeenCalledWith(i18nT(MUTED))
  })

  it('reports a track that is already muted when the watch starts', () => {
    const track = new FakeTrack()
    track.muted = true
    const onFailure = vi.fn()
    watchTrackHealth(streamOf(track), onFailure)

    vi.advanceTimersByTime(TRACK_MUTE_GRACE_MS)

    expect(onFailure).toHaveBeenCalledWith(i18nT(MUTED))
  })

  it('ignores a brief mute that WebKit lifts on its own', () => {
    const track = new FakeTrack()
    const onFailure = vi.fn()
    watchTrackHealth(streamOf(track), onFailure)

    mute(track)
    vi.advanceTimersByTime(TRACK_MUTE_GRACE_MS / 2)
    unmute(track)
    vi.advanceTimersByTime(TRACK_MUTE_GRACE_MS * 4)

    expect(onFailure).not.toHaveBeenCalled()
  })

  it('reports a track the browser ended', () => {
    const track = new FakeTrack()
    const onFailure = vi.fn()
    watchTrackHealth(streamOf(track), onFailure)

    track.dispatchEvent(new Event('ended'))

    expect(onFailure).toHaveBeenCalledTimes(1)
    expect(onFailure).toHaveBeenCalledWith(i18nT(ENDED))
  })

  it('reports at most once per watch', () => {
    const track = new FakeTrack()
    const onFailure = vi.fn()
    watchTrackHealth(streamOf(track), onFailure)

    mute(track)
    vi.advanceTimersByTime(TRACK_MUTE_GRACE_MS)
    track.dispatchEvent(new Event('ended'))

    expect(onFailure).toHaveBeenCalledTimes(1)
  })

  it('calls nothing once detached, including a mute timer already running', () => {
    const track = new FakeTrack()
    const onFailure = vi.fn()
    const detach = watchTrackHealth(streamOf(track), onFailure)

    mute(track)
    detach()
    vi.advanceTimersByTime(TRACK_MUTE_GRACE_MS * 2)
    track.dispatchEvent(new Event('ended'))

    expect(onFailure).not.toHaveBeenCalled()
  })

  it('is a no-op for a stream with no watchable track', () => {
    const onFailure = vi.fn()
    const none = { getAudioTracks: () => [] } as unknown as MediaStream
    const plain = { getAudioTracks: () => [{ muted: true }] } as unknown as MediaStream

    watchTrackHealth(none, onFailure)()
    watchTrackHealth(plain, onFailure)
    vi.advanceTimersByTime(TRACK_MUTE_GRACE_MS * 2)

    expect(onFailure).not.toHaveBeenCalled()
  })

  it('resolves both messages in the English catalog, and they differ', () => {
    expect(i18nT(MUTED)).not.toBe(MUTED)
    expect(i18nT(ENDED)).not.toBe(ENDED)
    expect(i18nT(MUTED)).not.toBe(i18nT(ENDED))
  })
})
