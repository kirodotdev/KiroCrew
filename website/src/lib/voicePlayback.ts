import { TAB_ID } from '../api/tabId'

let requestSequence = 0

/** HTTP LAN dashboards may lack randomUUID; tab identity plus a counter still
 * distinguishes each playback attempt without depending on a secure context. */
export function createVoiceRequestId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `${TAB_ID}-voice-${++requestSequence}`
}

/** Schedule short PCM WAV chunks on one audio clock instead of opening an audio
 * element per chunk, which introduces a loading gap at every boundary. */
export class VoicePcmPlayer {
  private context: AudioContext | null = null
  private sources = new Set<AudioBufferSourceNode>()
  private chain: Promise<void> = Promise.resolve()
  private generation = 0
  private nextStart = 0
  private pending = 0

  constructor(private onPlaying: (playing: boolean) => void, private onError: (code: string) => void) {}

  /** Call synchronously inside the user's click/key handler; Safari does not
   * transfer that permission across a synthesis request or a WebSocket frame. */
  unlock(): void {
    try {
      const context = this.context ??= new AudioContext()
      if (context.state === 'suspended') void context.resume().catch(() => {})
    } catch { /* enqueue reports a failure only when speech was requested */ }
  }

  enqueue(bytes: ArrayBuffer): void {
    const generation = this.generation
    this.pending++
    this.onPlaying(true)
    this.chain = this.chain.then(async () => {
      if (generation !== this.generation) return
      const context = this.context ??= new AudioContext()
      if (context.state === 'suspended') {
        // Some browsers leave resume pending until a gesture. Bound that wait
        // so an autoplay restriction becomes an actionable error, not a stuck queue.
        await new Promise<void>((resolve, reject) => {
          const timeout = setTimeout(() => reject(new Error('voice_playback_blocked')), 1500)
          context.resume().then(resolve, () => reject(new Error('voice_playback_blocked')))
            .finally(() => clearTimeout(timeout))
        })
      }
      const buffer = await context.decodeAudioData(bytes)
      if (generation !== this.generation) return
      const source = context.createBufferSource()
      source.buffer = buffer
      source.connect(context.destination)
      this.sources.add(source)
      source.onended = () => {
        source.disconnect()
        if (generation !== this.generation) return
        this.sources.delete(source)
        this.settle()
      }
      const start = Math.max(context.currentTime + 0.03, this.nextStart)
      this.nextStart = start + buffer.duration
      source.start(start)
    }).catch(error => {
      if (generation !== this.generation) return
      this.onError(error instanceof Error && error.message === 'voice_playback_blocked'
        ? 'voice_playback_blocked' : 'voice_playback_failed')
      if (generation === this.generation) this.stop()
    }).finally(() => {
      if (generation !== this.generation) return
      this.pending--
      this.settle()
    })
  }

  private settle(): void {
    if (this.pending === 0 && this.sources.size === 0) this.onPlaying(false)
  }

  stop(): void {
    this.generation++
    for (const source of this.sources) {
      source.onended = null
      try { source.stop() } catch { /* already ended or never started */ }
      try { source.disconnect() } catch { /* already detached */ }
    }
    this.sources.clear()
    this.pending = 0
    this.nextStart = 0
    this.chain = Promise.resolve()
    this.onPlaying(false)
  }

  close(): void {
    this.stop()
    void this.context?.close()
    this.context = null
  }
}

/** Offset after the latest safe speech boundary. Fences stay whole so partial
 * code is never spoken as prose. Clauses cap latency for long unpunctuated text. */
export function voiceBoundary(text: string, consumed: number): number {
  let fenced = false
  let inline = false
  let boundary = consumed
  for (let i = 0; i < text.length; i++) {
    if (text.startsWith('```', i) || text.startsWith('~~~', i)) {
      fenced = !fenced
      i += 2
      continue
    }
    if (fenced) continue
    if (text[i] === '`') { inline = !inline; continue }
    if (inline || i < consumed) continue
    const ch = text[i]
    if (/[。！？!?\n]/u.test(ch) || (ch === '.' && (i + 1 === text.length || /\s/u.test(text[i + 1])))) {
      boundary = i + 1
    } else if (i - boundary >= 60 && /[，、；：,;:\s]/u.test(ch)) {
      boundary = i + 1
    }
  }
  return boundary
}
