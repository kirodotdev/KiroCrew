// PCM downsampler AudioWorklet: converts mic Float32 (usually 48 kHz)
// to 16 kHz mono Int16 PCM and posts ArrayBuffers to the main thread
// via `port.postMessage`. Used by useStreamingStt.ts for live STT.
//
// Kept intentionally small — runs on the realtime audio thread.

const TARGET_RATE = 16000
// AWS Transcribe streaming recommends chunk sizes of 50-200ms for
// optimal latency (smaller chunks = per-frame WebSocket + protocol
// overhead; larger chunks = added perceived lag). 100ms hits the
// middle of the recommended range: at 16 kHz Int16, 1600 samples =
// 3200 bytes per chunk, emitted ~10 times per second.
// See: https://repost.aws/questions/...latency-from-5-7-seconds-to-10-12-seconds-in-transcribe
const BATCH_SAMPLES = TARGET_RATE / 10

class PcmWorklet extends AudioWorkletProcessor {
  constructor () {
    super()
    this._ratio = sampleRate / TARGET_RATE
    this._carry = 0
    // Accumulator for batching. Many audio quanta (~128 samples each at
    // source rate ≈ 2.7ms at 48 kHz) decimate to only ~43 output samples;
    // posting each quantum individually gives Transcribe 3ms chunks — far
    // below the 50ms floor and the cause of observed first-word latency.
    this._batch = new Int16Array(BATCH_SAMPLES)
    this._batchLen = 0
  }

  process (inputs) {
    const input = inputs[0]
    if (!input || input.length === 0) return true
    const channel = input[0]
    if (!channel || channel.length === 0) return true

    // Linear decimation: pick one sample every `_ratio` input samples.
    // Good enough for speech STT at 16 kHz from any browser rate.
    // _carry is the input-sample offset (>= 0) into the current block where
    // the next output sample should be picked. It must never go negative, or
    // channel[negative] → undefined → silent clicks every block.
    const outCount = Math.max(0, Math.floor((channel.length - this._carry) / this._ratio))
    if (outCount === 0) {
      // Whole block skipped; subtract block length and clamp.
      this._carry = Math.max(0, this._carry - channel.length)
      return true
    }
    // Emit samples directly into the batch buffer; flush when full.
    for (let i = 0; i < outCount; i++) {
      const idx = Math.floor(this._carry + i * this._ratio)
      const s = Math.max(-1, Math.min(1, channel[idx] || 0))
      this._batch[this._batchLen++] = s < 0 ? s * 0x8000 : s * 0x7FFF
      if (this._batchLen === BATCH_SAMPLES) {
        // Transferable copy — the worklet retains the pre-allocated batch.
        const out = new Int16Array(this._batch)
        this.port.postMessage(out.buffer, [out.buffer])
        this._batchLen = 0
      }
    }
    // Advance carry into next block. Always non-negative by construction since
    // outCount was chosen so the last pick fits within the current block.
    const nextInputIdx = this._carry + outCount * this._ratio
    this._carry = Math.max(0, nextInputIdx - channel.length)
    return true
  }
}

registerProcessor('pcm-worklet', PcmWorklet)
