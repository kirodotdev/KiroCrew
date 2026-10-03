// System-audio capture for meetings — the track-selection logic and its outcomes.
//
// The real `getDisplayMedia` is not available in this environment (and there is no
// audio device on CI), so the module keeps that call behind an injectable dep and
// everything else pure. This exercises the pure part.

import { describe, it, expect, vi } from 'vitest'

import {
  extractSystemAudio,
  isSystemAudioSupported,
  requestSystemAudio,
  stopStream,
  systemAudioConstraints,
  type StreamLike,
  type TrackLike,
} from '../apps/meetings/audio/systemAudio'

interface FakeTrack extends TrackLike {
  stopped: boolean
}

function track(kind: 'audio' | 'video'): FakeTrack {
  const t: FakeTrack = {
    kind,
    stopped: false,
    stop: () => {
      t.stopped = true
    },
  }
  return t
}

class FakeStream implements StreamLike<FakeTrack> {
  tracks: FakeTrack[]
  constructor(tracks: FakeTrack[]) {
    this.tracks = tracks
  }
  getTracks() {
    return [...this.tracks]
  }
  getAudioTracks() {
    return this.tracks.filter(t => t.kind === 'audio')
  }
  getVideoTracks() {
    return this.tracks.filter(t => t.kind === 'video')
  }
  removeTrack(t: FakeTrack) {
    this.tracks = this.tracks.filter(x => x !== t)
  }
}

describe('systemAudioConstraints', () => {
  // The load-bearing regression guard for this module. `getDisplayMedia` REQUIRES a
  // video track: omitting `video` or passing `video: false` rejects with a
  // TypeError, so the obvious-looking `{ audio: true }` fails in every browser and
  // system audio would silently never work. This pins that video is always asked for.
  it('always requests a video track, because audio-only display capture rejects', () => {
    const opts = systemAudioConstraints()
    expect(opts.video).toBeTruthy()
    expect(opts.video).not.toBe(false)
    expect(opts.audio).toBe(true)
  })

  it('asks for the cheapest possible video, since the track is dropped immediately', () => {
    const video = systemAudioConstraints().video as MediaTrackConstraints
    expect(video.frameRate).toEqual({ max: 1 })
  })

  it('does not prefer the current tab — the meeting is somewhere else', () => {
    expect(systemAudioConstraints()).not.toHaveProperty('preferCurrentTab')
  })
})

describe('isSystemAudioSupported', () => {
  it('is true when getDisplayMedia is a function', () => {
    expect(isSystemAudioSupported({ mediaDevices: { getDisplayMedia: () => {} } })).toBe(true)
  })

  it('is false when getDisplayMedia is missing, and when there is no navigator', () => {
    expect(isSystemAudioSupported({ mediaDevices: {} })).toBe(false)
    expect(isSystemAudioSupported(undefined)).toBe(false)
  })
})

describe('extractSystemAudio', () => {
  it('stops AND removes the video track, keeping the audio one', () => {
    const video = track('video')
    const audio = track('audio')
    const stream = new FakeStream([video, audio])

    expect(extractSystemAudio(stream)).toBe(stream)

    expect(video.stopped).toBe(true)
    // Removed as well as stopped: a source node built from this stream must not be
    // able to see a dead track at all.
    expect(stream.getTracks()).toEqual([audio])
    expect(audio.stopped).toBe(false)
  })

  it('returns null and releases everything when the surface offers no audio', () => {
    // What actually happens when the user shares a whole monitor on a platform that
    // does not offer monitor audio. Leaving the capture live would strand the
    // browser's "sharing" indicator for a stream we cannot use.
    const video = track('video')
    const stream = new FakeStream([video])

    expect(extractSystemAudio(stream)).toBeNull()
    expect(video.stopped).toBe(true)
  })

  it('survives a track whose stop() throws', () => {
    const video = track('video')
    video.stop = () => {
      throw new Error('already ended')
    }
    const audio = track('audio')
    const stream = new FakeStream([video, audio])
    expect(() => extractSystemAudio(stream)).not.toThrow()
    expect(stream.getAudioTracks()).toEqual([audio])
  })
})

describe('stopStream', () => {
  it('stops every track and tolerates null', () => {
    const a = track('audio')
    const v = track('video')
    stopStream(new FakeStream([a, v]))
    expect([a.stopped, v.stopped]).toEqual([true, true])
    expect(() => stopStream(null)).not.toThrow()
    expect(() => stopStream(undefined)).not.toThrow()
  })
})

describe('requestSystemAudio', () => {
  it('reports the audio-only stream on success', async () => {
    const stream = new FakeStream([track('video'), track('audio')])
    const getDisplayMedia = vi.fn().mockResolvedValue(stream)

    const outcome = await requestSystemAudio({ getDisplayMedia })

    expect(outcome).toEqual({ ok: true, stream })
    expect(getDisplayMedia).toHaveBeenCalledWith(
      expect.objectContaining({ audio: true }),
    )
  })

  it('reports "cancelled" when the picker rejects, and never throws', async () => {
    const getDisplayMedia = vi
      .fn()
      .mockRejectedValue(new DOMException('denied', 'NotAllowedError'))

    await expect(requestSystemAudio({ getDisplayMedia })).resolves.toEqual({
      ok: false,
      reason: 'cancelled',
    })
  })

  it('reports "no-audio" separately from a cancel', async () => {
    // Distinct on purpose: the user DID share something, they just shared a surface
    // with no audio, and the fix is to pick the meeting's tab — not to try again.
    const getDisplayMedia = vi.fn().mockResolvedValue(new FakeStream([track('video')]))

    await expect(requestSystemAudio({ getDisplayMedia })).resolves.toEqual({
      ok: false,
      reason: 'no-audio',
    })
  })

  it('reports "unsupported" without prompting', async () => {
    const getDisplayMedia = vi.fn()

    await expect(
      requestSystemAudio({ getDisplayMedia, supported: () => false }),
    ).resolves.toEqual({ ok: false, reason: 'unsupported' })
    expect(getDisplayMedia).not.toHaveBeenCalled()
  })
})
