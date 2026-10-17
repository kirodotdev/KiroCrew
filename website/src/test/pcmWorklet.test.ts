import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { describe, expect, it } from 'vitest'

const source = readFileSync('public/pcm-worklet.js', 'utf8')

function capture(rate: number, duration = 1, frequency = 1000, signal?: (i: number, total: number) => number) {
  const messages: unknown[] = []
  let Processor!: new () => { process(inputs: Float32Array[][]): boolean; port: { onmessage: (event: unknown) => void } }
  runInNewContext(source, {
    sampleRate: rate,
    AudioWorkletProcessor: class { port = { postMessage: (data: unknown) => messages.push(data) } },
    registerProcessor: (_name: string, processor: typeof Processor) => { Processor = processor },
  })
  const worklet = new Processor()
  const count = Math.round(rate * duration)
  for (let start = 0; start < count; start += 128) {
    const input = Float32Array.from({ length: Math.min(128, count - start) }, (_, i) =>
      signal ? signal(start + i, count) : 0.6 * Math.sin(2 * Math.PI * frequency * (start + i) / rate))
    worklet.process([[input]])
  }
  worklet.port.onmessage({ data: { type: 'flush' } })
  const chunks = messages.slice(0, -1).map(buffer => new Int16Array(buffer as ArrayBuffer))
  return { worklet, messages, chunks, samples: chunks.flatMap(chunk => Array.from(chunk)) }
}

describe('PCM worklet transport', () => {
  it.each([16000, 44100, 48000])('preserves one second as 16000 nominal samples plus the FIR tail from %i Hz', rate => {
    const { samples, chunks } = capture(rate)
    const filterTail = rate > 16000 ? Math.floor(62 * 16000 / rate) : 0
    expect(samples).toHaveLength(16000 + filterTail)
    expect(chunks.slice(0, 10).every(chunk => chunk.length === 1600)).toBe(true)
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
