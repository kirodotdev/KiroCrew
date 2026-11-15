import { afterEach, describe, expect, it, vi } from 'vitest'
import { VoicePcmPlayer, voiceBoundary, createVoiceRequestId } from '../lib/voicePlayback'

afterEach(() => vi.unstubAllGlobals())

describe('speech boundaries', () => {
  it('generates distinct request ids on an HTTP dashboard without randomUUID', () => {
    vi.stubGlobal('crypto', {})
    const first = createVoiceRequestId()
    expect(createVoiceRequestId()).not.toBe(first)
  })
  it('streams Chinese sentences without waiting for whitespace', () => {
    expect(voiceBoundary('你好。请继续！还有尾巴', 0)).toBe(7)
  })
  it('does not split decimal numbers or fenced code', () => {
    const text = 'Value 3.14 stays intact.\n```js\nconst x = 2.0;\n```\nReady!'
    expect(voiceBoundary(text.slice(0, text.indexOf('```\nReady')), 0)).toBe(25)
    expect(voiceBoundary(text, 0)).toBe(text.length)
  })
  it('bounds the wait for a long clause without chopping a word', () => {
    const text = '这是需要尽快播放的内容'.repeat(7) + '，接下来'
    expect(voiceBoundary(text, 0)).toBe(text.indexOf('，') + 1)
  })
})

describe('PCM streaming playback', () => {
  function setup(decode = vi.fn().mockResolvedValue({ duration: 0.2 })) {
    const nodes: { start: ReturnType<typeof vi.fn>; stop: ReturnType<typeof vi.fn>; onended: (() => void) | null }[] = []
    class Context {
      state = 'running'
      currentTime = 1
      destination = {}
      decodeAudioData = decode
      close = vi.fn().mockResolvedValue(undefined)
      createBufferSource() {
        const node = { start: vi.fn(), stop: vi.fn(), connect: vi.fn(), disconnect: vi.fn(), onended: null }
        nodes.push(node)
        return node
      }
    }
    vi.stubGlobal('AudioContext', Context)
    const playing = vi.fn()
    const error = vi.fn()
    const listener = { paused: false }
    return { nodes, playing, error, listener, player: new VoicePcmPlayer(playing, error, () => listener.paused) }
  }

  it('schedules adjacent chunks without a media-element loading gap', async () => {
    const { player, nodes, playing, error } = setup()
    player.enqueue(new ArrayBuffer(2))
    player.enqueue(new ArrayBuffer(4))
    await vi.waitFor(() => expect(nodes).toHaveLength(2))
    expect(nodes[0].start).toHaveBeenCalledWith(1.03)
    expect(nodes[1].start).toHaveBeenCalledWith(1.23)
    nodes[0].onended?.()
    expect(playing).not.toHaveBeenLastCalledWith(false)
    nodes[1].onended?.()
    expect(playing).toHaveBeenLastCalledWith(false)
    expect(error).not.toHaveBeenCalled()
    player.close()
  })

  it('discards a decode that finishes after cancellation and lets new audio start', async () => {
    let finish!: (buffer: { duration: number }) => void
    const decode = vi.fn().mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
      .mockResolvedValue({ duration: 0.2 })
    const { player, nodes } = setup(decode)
    player.enqueue(new ArrayBuffer(2))
    await vi.waitFor(() => expect(decode).toHaveBeenCalledTimes(1))
    player.stop()
    player.enqueue(new ArrayBuffer(4))
    await vi.waitFor(() => expect(nodes).toHaveLength(1))
    finish({ duration: 4 })
    await Promise.resolve()
    await Promise.resolve()
    expect(nodes).toHaveLength(1)
    player.close()
    expect(nodes[0].stop).toHaveBeenCalledOnce()
  })

  it('resumes the context synchronously in the user gesture before network audio arrives', async () => {
    let gesture = true
    const resume = vi.fn()
    const { player, nodes } = setup()
    const RunningContext = globalThis.AudioContext
    class GestureContext extends RunningContext {
      state = 'suspended' as AudioContextState
      resume = () => {
        resume(gesture)
        if (!gesture) return Promise.reject(new Error('blocked'))
        this.state = 'running'
        return Promise.resolve()
      }
    }
    vi.stubGlobal('AudioContext', GestureContext)
    player.unlock()
    expect(resume).toHaveBeenCalledWith(true)
    gesture = false
    player.enqueue(new ArrayBuffer(2))
    await vi.waitFor(() => expect(nodes).toHaveLength(1))
    expect(resume).toHaveBeenCalledOnce()
    player.close()
  })

  describe('pause and resume', () => {
    /** A context whose state follows suspend() and resume(), as a real one does. */
    function stubStatefulContext(initial: 'running' | 'suspended' = 'running') {
      const contexts: StatefulContext[] = []
      class StatefulContext {
        state: AudioContextState = initial
        currentTime = 1
        destination = {}
        decodeAudioData = vi.fn().mockResolvedValue({ duration: 0.2 })
        close = vi.fn().mockResolvedValue(undefined)
        suspend = vi.fn(() => { this.state = 'suspended'; return Promise.resolve() })
        resume = vi.fn(() => { this.state = 'running'; return Promise.resolve() })
        createBufferSource() {
          return { start: vi.fn(), stop: vi.fn(), connect: vi.fn(), disconnect: vi.fn(), onended: null }
        }
        constructor() { contexts.push(this) }
      }
      vi.stubGlobal('AudioContext', StatefulContext)
      return contexts
    }

    function newPlayer() {
      const listener = { paused: false }
      const player = new VoicePcmPlayer(vi.fn(), vi.fn(), () => listener.paused)
      return { player, listener }
    }

    it('suspends the audio clock on pause and resumes it on resume', async () => {
      const contexts = stubStatefulContext()
      const { player } = newPlayer()
      player.enqueue(new ArrayBuffer(2))
      await vi.waitFor(() => expect(contexts).toHaveLength(1))
      await vi.waitFor(() => expect(contexts[0].decodeAudioData).toHaveBeenCalled())

      player.pause()
      expect(contexts[0].suspend).toHaveBeenCalledOnce()
      expect(contexts[0].state).toBe('suspended')
      player.resume()
      expect(contexts[0].resume).toHaveBeenCalledOnce()
      expect(contexts[0].state).toBe('running')
      player.close()
    })

    it('does nothing when no audio has ever been queued', () => {
      const contexts = stubStatefulContext()
      const { player } = newPlayer()
      player.pause()
      player.resume()
      expect(contexts).toHaveLength(0)
    })

    it('keeps audio that arrives while paused silent, without the autoplay resume, until resume', async () => {
      const contexts = stubStatefulContext()
      const { player, listener } = newPlayer()
      listener.paused = true
      player.enqueue(new ArrayBuffer(2))
      await vi.waitFor(() => expect(contexts[0]?.decodeAudioData).toHaveBeenCalled())

      expect(contexts[0].suspend).toHaveBeenCalledOnce()
      expect(contexts[0].resume).not.toHaveBeenCalled()
      expect(contexts[0].state).toBe('suspended')

      listener.paused = false
      player.resume()
      expect(contexts[0].state).toBe('running')
      player.close()
    })

    it('does not use the autoplay resume for a context that is already suspended while paused', async () => {
      const contexts = stubStatefulContext('suspended')
      const { player, listener } = newPlayer()
      listener.paused = true
      player.enqueue(new ArrayBuffer(2))
      await vi.waitFor(() => expect(contexts[0]?.decodeAudioData).toHaveBeenCalled())
      expect(contexts[0].resume).not.toHaveBeenCalled()
      expect(contexts[0].suspend).not.toHaveBeenCalled()
      player.close()
    })

    it('does not unlock a suspended context while paused', () => {
      const contexts = stubStatefulContext('suspended')
      const { player, listener } = newPlayer()
      listener.paused = true
      player.unlock()
      expect(contexts[0].resume).not.toHaveBeenCalled()
      player.close()
    })

    it('plays new audio after a stop that happened while paused', async () => {
      const contexts = stubStatefulContext()
      const { player, listener } = newPlayer()
      player.enqueue(new ArrayBuffer(2))
      await vi.waitFor(() => expect(contexts[0]?.decodeAudioData).toHaveBeenCalled())
      listener.paused = true
      player.pause()
      expect(contexts[0].state).toBe('suspended')

      player.stop()
      listener.paused = false
      player.enqueue(new ArrayBuffer(4))
      await vi.waitFor(() => expect(contexts[0].decodeAudioData).toHaveBeenCalledTimes(2))
      expect(contexts[0].resume).toHaveBeenCalledOnce()
      expect(contexts[0].state).toBe('running')
      player.close()
    })
  })

  it('clears queued chunks after one decode failure instead of repeatedly reporting it', async () => {
    const decode = vi.fn().mockRejectedValue(new Error('bad audio'))
    const { player, error, playing, nodes } = setup(decode)
    for (let i = 0; i < 10; i++) player.enqueue(new ArrayBuffer(2))
    await vi.waitFor(() => expect(error).toHaveBeenCalledOnce())
    expect(decode).toHaveBeenCalledOnce()
    expect(playing).toHaveBeenLastCalledWith(false)
    expect(nodes).toHaveLength(0)
    player.close()
  })
})
