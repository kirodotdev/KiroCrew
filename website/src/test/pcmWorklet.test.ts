import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { describe, expect, it } from 'vitest'

const source = readFileSync('public/pcm-worklet.js', 'utf8')

type Signal = (i: number, total: number) => number
type Worklet = { process(inputs: Float32Array[][]): boolean; port: { onmessage: (event: unknown) => void } }

/** Load the processor into a fresh context at `rate`; messages collect its posts. */
function load(rate: number) {
  const messages: unknown[] = []
  let Processor!: new () => Worklet
  runInNewContext(source, {
    sampleRate: rate,
    AudioWorkletProcessor: class { port = { postMessage: (data: unknown) => messages.push(data) } },
    registerProcessor: (_name: string, processor: typeof Processor) => { Processor = processor },
  })
  return { worklet: new Processor(), messages }
}

function decode(messages: unknown[]) {
  const chunks = messages.slice(0, -1).map(buffer => new Int16Array(buffer as ArrayBuffer))
  return { chunks, samples: chunks.flatMap(chunk => Array.from(chunk)) }
}

function capture(rate: number, duration = 1, frequency = 1000, signal?: Signal) {
  const { worklet, messages } = load(rate)
  const count = Math.round(rate * duration)
  for (let start = 0; start < count; start += 128) {
    const input = Float32Array.from({ length: Math.min(128, count - start) }, (_, i) =>
      signal ? signal(start + i, count) : 0.6 * Math.sin(2 * Math.PI * frequency * (start + i) / rate))
    worklet.process([[input]])
  }
  worklet.port.onmessage({ data: { type: 'flush' } })
  return { worklet, messages, ...decode(messages) }
}

type Block = [Float32Array | undefined, Float32Array | undefined]

/**
 * Drive the processor as the Meetings app builds it -- `numberOfInputs: 2`,
 * microphone on input 0, system audio on input 1 -- one quantum per entry.
 * An undefined side is an input with nothing connected: the graph hands the
 * processor an empty array for it.
 */
function captureTwo(rate: number, blocks: Block[]) {
  const { worklet, messages } = load(rate)
  for (const [mic, sys] of blocks) worklet.process([mic ? [mic] : [], sys ? [sys] : []])
  worklet.port.onmessage({ data: { type: 'flush' } })
  return { worklet, messages, ...decode(messages) }
}

const constant = (length: number, value: number) => new Float32Array(length).fill(value)

describe('PCM worklet transport', () => {
  it.each([16000, 44100, 48000])('preserves one second as 16000 nominal samples plus the FIR tail from %i Hz', rate => {
    const { samples, chunks } = capture(rate)
    const filterTail = rate > 16000 ? Math.floor(62 * 16000 / rate) : 0
    expect(samples).toHaveLength(16000 + filterTail)
    expect(chunks.slice(0, 10).every(chunk => chunk.length === 1600)).toBe(true)
  })

  it('drains a short recording batch without stopping transcription capture', () => {
    const { worklet, messages } = load(16000)
    expect(worklet.process([[constant(128, 0.25)]])).toBe(true)
    worklet.port.onmessage({ data: { type: 'drain' } })

    expect(new Int16Array(messages[0] as ArrayBuffer)).toHaveLength(128)
    expect(messages[1]).toEqual({ type: 'drained' })
    expect(worklet.process([[constant(128, 0.5)]])).toBe(true)

    worklet.port.onmessage({ data: { type: 'flush' } })
    expect(new Int16Array(messages[2] as ArrayBuffer)).toHaveLength(128)
    expect(messages[3]).toEqual({ type: 'flushed' })
  })

  it('flushes a sub-batch utterance before acknowledging stop, and accepts no later capture', () => {
    const { samples, messages, worklet } = capture(48000, 0.037)
    expect(samples).toHaveLength(592 + 20)
    expect(messages[messages.length - 1]).toEqual({ type: 'flushed' })
    const before = messages.length
    expect(worklet.process([[new Float32Array(4800)]])).toBe(false)
    expect(messages).toHaveLength(before)
  })

  it('rejects ultrasonic aliases while preserving the speech band', () => {
    const rms = (samples: number[]) => Math.sqrt(samples.slice(100).reduce((n, value) => n + value * value, 0) / (samples.length - 100))
    const speech = rms(capture(48000, 1, 1000).samples)
    const alias = rms(capture(48000, 1, 12000).samples)
    expect(speech).toBeGreaterThan(12000)
    expect(alias / speech).toBeLessThan(0.01)
  })

  it.each([44100, 48000])('retains the filtered response to the very last captured sample at %i Hz', rate => {
    const { samples } = capture(rate, 0.1, 0, (i, total) => i === total - 1 ? 1 : 0)
    // Almost the entire impulse response lies after the nominal input duration.
    // Merely flushing the PCM batch leaves a peak of only 10–21 here.
    const tail = samples.slice(1600)
    expect(Math.max(...tail.map(Math.abs))).toBeGreaterThan(7000)
    expect(tail.some(sample => sample < 0)).toBe(true)
  })

  it.each([1000, 4000, 6000])('keeps fractional-rate sample timing clean for a %i Hz tone', frequency => {
    const samples = capture(44100, 1, frequency).samples.slice(200, 15800)
    // Fit amplitude and phase, leaving timing jitter and spurious tones in the
    // residual. Amplitude-only checks cannot detect fractional-clock distortion.
    const sine = samples.map((_, i) => Math.sin(2 * Math.PI * frequency * i / 16000))
    const cosine = samples.map((_, i) => Math.cos(2 * Math.PI * frequency * i / 16000))
    const a = 2 * samples.reduce((sum, value, i) => sum + value * sine[i], 0) / samples.length
    const b = 2 * samples.reduce((sum, value, i) => sum + value * cosine[i], 0) / samples.length
    const noise = samples.reduce((sum, value, i) => sum + (value - a * sine[i] - b * cosine[i]) ** 2, 0) / samples.length
    expect(10 * Math.log10((a * a + b * b) / 2 / noise)).toBeGreaterThan(55)
  })
})

// ─── two inputs, one stream (the Meetings capture graph) ────────────────────
//
// 16 kHz in, 16 kHz out: no resampling, so a block's output IS its input and
// the mixing arithmetic can be read off the samples directly. The resampling
// path shares the same mixed block, which the last test checks at 48 kHz.

describe('PCM worklet two-input mixing', () => {
  // Exactly the worklet's quantisation: scale, then Int16Array's truncation toward zero.
  const level = (v: number) => Math.trunc(v < 0 ? v * 0x8000 : v * 0x7FFF)
  const blocks = (mic: number | undefined, sys: number | undefined, n = 20): Block[] =>
    Array.from({ length: n }, () =>
      [mic === undefined ? undefined : constant(128, mic), sys === undefined ? undefined : constant(128, sys)])

  it('sums the two inputs rather than averaging them', () => {
    // Averaging would halve the level of whichever side is talking -- in a meeting
    // that is almost always exactly one side -- and cost accuracy on every utterance.
    const { samples } = captureTwo(16000, blocks(0.25, 0.25))
    expect(samples).toHaveLength(20 * 128)
    expect(samples.every(s => s === level(0.5))).toBe(true)
  })

  it('clips the sum instead of wrapping when both sides are loud at once', () => {
    expect(captureTwo(16000, blocks(0.8, 0.8)).samples.every(s => s === 0x7FFF)).toBe(true)
    expect(captureTwo(16000, blocks(-0.8, -0.8)).samples.every(s => s === -0x8000)).toBe(true)
  })

  it('passes the microphone through untouched when input 1 has nothing connected', () => {
    // A node built with two inputs but no system audio yet (or a share the user
    // stopped) must produce exactly what the single-input dictation node does.
    const two = captureTwo(16000, blocks(0.25, undefined)).samples
    const one = capture(16000, 20 * 128 / 16000, 0, () => 0.25).samples
    expect(two).toEqual(one)
    expect(two.every(s => s === level(0.25))).toBe(true)
  })

  it('keeps the far side when the microphone input delivers nothing', () => {
    // Input 0 with no block (mic muted at the OS level) must not silence the
    // remote participants.
    const { samples } = captureTwo(16000, blocks(undefined, 0.25))
    expect(samples).toHaveLength(20 * 128)
    expect(samples.every(s => s === level(0.25))).toBe(true)
  })

  it('reads the second input per block, so a share can join and leave mid-capture', () => {
    const { samples } = captureTwo(16000, [
      ...blocks(0.25, undefined, 5), ...blocks(0.25, 0.25, 5), ...blocks(0.25, undefined, 5),
    ])
    expect(samples.slice(0, 5 * 128).every(s => s === level(0.25))).toBe(true)
    expect(samples.slice(5 * 128, 10 * 128).every(s => s === level(0.5))).toBe(true)
    expect(samples.slice(10 * 128).every(s => s === level(0.25))).toBe(true)
  })

  it('treats a short block on one side as zeros past its end, never NaN', () => {
    // A source that just started or stopped can hand over a short quantum.
    const { samples } = captureTwo(16000, [[constant(128, 0.25), constant(64, 0.25)]])
    expect(samples).toHaveLength(128)
    expect(samples.slice(0, 64).every(s => s === level(0.5))).toBe(true)
    expect(samples.slice(64).every(s => s === level(0.25))).toBe(true)
    expect(samples.some(Number.isNaN)).toBe(false)
  })

  it('mixes ahead of the anti-alias filter on a resampling rate', () => {
    // Two 0.3-amplitude 1 kHz tones on the two inputs at 48 kHz must come out as
    // the single 0.6-amplitude tone fed to input 0 alone: same filter, same batches.
    const tone = (amp: number) => (i: number) => amp * Math.sin(2 * Math.PI * 1000 * i / 48000)
    const twoIn: Block[] = []
    for (let start = 0; start < 48000; start += 128) {
      const a = Float32Array.from({ length: 128 }, (_, i) => tone(0.3)(start + i))
      twoIn.push([a, Float32Array.from(a)])
    }
    const mixed = captureTwo(48000, twoIn).samples
    const single = capture(48000, 1, 1000, tone(0.6)).samples
    expect(mixed).toHaveLength(single.length)
    const maxDiff = Math.max(...mixed.map((s, i) => Math.abs(s - single[i])))
    // Float32 rounding of `0.3 + 0.3` vs `0.6` before the FIR: at most a couple of
    // LSBs of the 16-bit output apart, identical for every practical purpose.
    expect(maxDiff).toBeLessThanOrEqual(2)
  })
})
