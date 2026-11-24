/**
 * Sound that comes from where something happened.
 *
 * A cue is two short tones made in the browser (no audio files), placed in
 * space at the Bot or panel it belongs to. The scene calls `cue(name, at)`;
 * what a cue sounds like lives only here, so a world can re-voice it.
 *
 * Nothing plays until the person has acted once (browsers require that), and
 * every cue is also kept in a short log, so tests can check what would sound
 * and where without any audio device.
 */
export type XrCue = 'ask' | 'arrive' | 'reply' | 'done' | 'commit' | 'refuse' | 'handoff'

/** Two notes per cue, in Hz, and how long each lasts in seconds. */
const VOICES: Record<XrCue, { notes: [number, number]; len: number }> = {
  ask: { notes: [660, 880], len: 0.14 },
  arrive: { notes: [440, 554], len: 0.1 },
  reply: { notes: [587, 740], len: 0.09 },
  done: { notes: [523, 784], len: 0.16 },
  commit: { notes: [784, 1047], len: 0.12 },
  refuse: { notes: [330, 262], len: 0.12 },
  handoff: { notes: [494, 659], len: 0.1 },
}

export interface CuePlayed { cue: XrCue; at: [number, number, number] }

export class XrSound {
  private ctx: AudioContext | null = null
  /** Sound is off until turned on. The log below fills either way. */
  enabled = false
  readonly log: CuePlayed[] = []

  /** Call from a click or a pinch: browsers only start audio after a gesture. */
  resume(): void {
    if (!this.enabled || typeof AudioContext === 'undefined') return
    this.ctx ??= new AudioContext()
    if (this.ctx.state === 'suspended') void this.ctx.resume().catch(() => undefined)
  }

  /** Where the listener's head is and which way it faces, in the same space the cues use. */
  listen(pos: readonly [number, number, number], forward: readonly [number, number, number]): void {
    const l = this.ctx?.listener
    if (!l || !l.positionX) return
    const t = this.ctx?.currentTime ?? 0
    l.positionX.setValueAtTime(pos[0], t); l.positionY.setValueAtTime(pos[1], t); l.positionZ.setValueAtTime(pos[2], t)
    l.forwardX.setValueAtTime(forward[0], t); l.forwardY.setValueAtTime(forward[1], t); l.forwardZ.setValueAtTime(forward[2], t)
    l.upX.setValueAtTime(0, t); l.upY.setValueAtTime(1, t); l.upZ.setValueAtTime(0, t)
  }

  cue(cue: XrCue, at: readonly [number, number, number]): void {
    this.log.push({ cue, at: [Math.round(at[0] * 100) / 100, Math.round(at[1] * 100) / 100, Math.round(at[2] * 100) / 100] })
    if (this.log.length > 40) this.log.shift()
    const ctx = this.ctx
    if (!this.enabled || !ctx || ctx.state !== 'running') return
    const v = VOICES[cue]
    const pan = new PannerNode(ctx, { panningModel: 'HRTF', distanceModel: 'inverse', refDistance: 0.8, positionX: at[0], positionY: at[1], positionZ: at[2] })
    pan.connect(ctx.destination)
    v.notes.forEach((hz, i) => {
      const t0 = ctx.currentTime + i * v.len
      const osc = new OscillatorNode(ctx, { type: 'sine', frequency: hz })
      const gain = new GainNode(ctx, { gain: 0 })
      gain.gain.setValueAtTime(0, t0)
      gain.gain.linearRampToValueAtTime(0.16, t0 + 0.012)
      gain.gain.exponentialRampToValueAtTime(0.001, t0 + v.len)
      osc.connect(gain).connect(pan)
      osc.start(t0)
      osc.stop(t0 + v.len + 0.02)
    })
  }

  dispose(): void { void this.ctx?.close().catch(() => undefined); this.ctx = null }
}
