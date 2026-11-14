import { describe, expect, it } from 'vitest'

import chatReducer, { setVoicePaused, setVoicePlaying, setVoicePreparing } from './chatSlice'

const initial = () => chatReducer(undefined, { type: '@@INIT' })

describe('voicePaused', () => {
  it('starts false and is set by setVoicePaused', () => {
    expect(initial().voicePaused).toBe(false)
    const paused = chatReducer(chatReducer(initial(), setVoicePlaying(true)), setVoicePaused(true))
    expect(paused.voicePaused).toBe(true)
    expect(chatReducer(paused, setVoicePaused(false)).voicePaused).toBe(false)
  })

  describe('clears when nothing is left to resume', () => {
    const paused = (flags: { playing?: boolean; preparing?: boolean }) => {
      let state = initial()
      state = chatReducer(state, setVoicePlaying(!!flags.playing))
      state = chatReducer(state, setVoicePreparing(!!flags.preparing))
      return chatReducer(state, setVoicePaused(true))
    }

    it('when playback ends while nothing is preparing', () => {
      expect(chatReducer(paused({ playing: true }), setVoicePlaying(false)).voicePaused).toBe(false)
    })

    it('when preparing ends while nothing is playing', () => {
      expect(chatReducer(paused({ preparing: true }), setVoicePreparing(false)).voicePaused).toBe(false)
    })

    it('stays while the other flag still holds a reading', () => {
      expect(chatReducer(paused({ playing: true, preparing: true }), setVoicePlaying(false)).voicePaused).toBe(true)
      expect(chatReducer(paused({ playing: true, preparing: true }), setVoicePreparing(false)).voicePaused).toBe(true)
    })

    it('stays when audio arrives for a request that was preparing', () => {
      let state = paused({ preparing: true })
      state = chatReducer(state, setVoicePlaying(true))
      state = chatReducer(state, setVoicePreparing(false))
      expect(state.voicePaused).toBe(true)
    })
  })
})
