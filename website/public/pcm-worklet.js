// Audio-thread capture: anti-aliased 16 kHz mono PCM, in 100 ms batches.
//
// TWO INPUTS, ONE STREAM. A meeting needs the microphone *and* what the other
// participants are saying, which arrive as two separate MediaStreams (mic via
// getUserMedia, system audio via getDisplayMedia or an Electron loopback), and
// speech-to-text takes one mono stream. The Meetings app constructs this node
// with `numberOfInputs: 2` and the two sources are SUMMED here, ahead of the
// resampler, so the mix goes through the anti-alias filter exactly once.
//
// A node constructed with the default single input is unaffected: inputs[1] is
// absent and contributes nothing. That backward compatibility is load-bearing:
// useStreamingStt.ts (voice input) shares this file.
//
// Sources are summed and clipped, not averaged. In a meeting only one side is
// usually talking, and averaging would halve the level of whichever side that
// is, which costs transcription accuracy on every utterance. Summing only
// distorts while both talk at once, which is brief and already hard to
// transcribe.
const TARGET_RATE = 16000
const BATCH_SAMPLES = TARGET_RATE / 10
const FILTER_TAPS = 63
const FILTER_PHASES = 256

class PcmWorklet extends AudioWorkletProcessor {
  constructor () {
    super()
    this._phase = 0
    this._history = new Float32Array(FILTER_TAPS)
    this._position = 0
    // Integer ratios need one kernel. Fractional ratios (notably 44.1 kHz)
    // select a precomputed delay phase rather than jittering between input ticks.
    this._phases = sampleRate % TARGET_RATE === 0 ? 1 : FILTER_PHASES
    this._filter = new Float32Array(FILTER_TAPS * this._phases)
    const cutoff = Math.min(0.5, TARGET_RATE / sampleRate * 0.45)
    const middle = (FILTER_TAPS - 1) / 2
    for (let phase = 0; phase < this._phases; phase++) {
      let sum = 0
      const offset = phase * FILTER_TAPS
      for (let i = 0; i < FILTER_TAPS; i++) {
        const x = i - middle - phase / this._phases
        const sinc = x === 0 ? 2 * cutoff : Math.sin(2 * Math.PI * cutoff * x) / (Math.PI * x)
        const window = 0.54 - 0.46 * Math.cos(2 * Math.PI * i / (FILTER_TAPS - 1))
        sum += this._filter[offset + i] = sinc * window
      }
      for (let i = 0; i < FILTER_TAPS; i++) this._filter[offset + i] /= sum
    }
    this._batch = new Int16Array(BATCH_SAMPLES)
    this._batchLen = 0
    // Scratch buffer for the two-source sum; grown on demand, never shrunk.
    this._mix = new Float32Array(128)
    this._stopped = false
    this._hasInput = false
    this.port.onmessage = e => {
      if (this._stopped) return
      if (e.data?.type === 'drain') {
        this._flush()
        this.port.postMessage({ type: 'drained' })
        return
      }
      if (e.data?.type !== 'flush') return
      // A causal FIR still holds the last input's response after capture ends.
      // Drain that history before the PCM batch; otherwise stopping clips the
      // remaining impulse response even when the short frame is preserved.
      if (this._hasInput && sampleRate > TARGET_RATE) {
        this.process([[new Float32Array(FILTER_TAPS - 1)]])
      }
      this._stopped = true
      this._flush()
      // MessagePort ordering puts the last short audio frame ahead of the
      // acknowledgment, so the main thread sends it before the WebSocket stop.
      this.port.postMessage({ type: 'flushed' })
    }
  }

  _flush () {
    if (!this._batchLen) return
    const out = this._batch.slice(0, this._batchLen)
    this.port.postMessage(out.buffer, [out.buffer])
    this._batchLen = 0
  }

  // The block to resample: the first input alone, or the sample-wise sum of
  // both when a second source is connected and currently producing audio. The
  // check is per block rather than latched at construction, because the second
  // source can appear after the node (a screen share granted mid-meeting) and
  // disappear before it (a share the user stopped) -- either way the mic keeps
  // flowing through untouched. A short block on one side (a source that just
  // started or stopped) contributes zeros past its end instead of NaN.
  _mixed (inputs) {
    const first = inputs[0]?.[0]
    const second = inputs[1]?.[0]
    const firstLen = first?.length ?? 0
    const secondLen = second?.length ?? 0
    if (!secondLen) return firstLen ? first : null
    if (!firstLen) return second
    const length = Math.max(firstLen, secondLen)
    if (this._mix.length < length) this._mix = new Float32Array(length)
    const mix = this._mix.subarray(0, length)
    for (let i = 0; i < length; i++) mix[i] = (first[i] || 0) + (second[i] || 0)
    return mix
  }

  process (inputs) {
    if (this._stopped) return false
    const channel = this._mixed(inputs)
    if (!channel) return true
    this._hasInput = true
    for (let i = 0; i < channel.length; i++) {
      this._history[this._position] = channel[i]
      this._phase += TARGET_RATE
      while (this._phase >= sampleRate) {
        this._phase -= sampleRate
        let sample = channel[i]
        if (sampleRate > TARGET_RATE) {
          sample = 0
          const phaseOffset = Math.floor(this._phase / TARGET_RATE * this._phases) * FILTER_TAPS
          for (let j = 0; j < FILTER_TAPS; j++) {
            sample += this._history[(this._position - j + FILTER_TAPS) % FILTER_TAPS] * this._filter[phaseOffset + j]
          }
        }
        sample = Math.max(-1, Math.min(1, sample))
        this._batch[this._batchLen++] = sample < 0 ? sample * 0x8000 : sample * 0x7FFF
        if (this._batchLen === BATCH_SAMPLES) this._flush()
      }
      this._position = (this._position + 1) % FILTER_TAPS
    }
    return true
  }
}

registerProcessor('pcm-worklet', PcmWorklet)
