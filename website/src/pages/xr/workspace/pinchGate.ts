/**
 * When a hand's pinch is a press, and when it is over.
 *
 * A tracker can lose a pinch for a frame while the fingers are still together. The session then says "ended" and,
 * a frame later, "started". Taken at its word, whatever the hand held is let go and taken again a little behind:
 * it slips. So a pinch that opens counts as open only after it has stayed open for `openMs`. One that closes again
 * before that never opened.
 *
 * This is the gate in front of the framework's pointer: `start` and `end` are the session's events, `tick` runs
 * once a frame, and each answers what the pointer is told.
 */
export class PinchGate {
  /** The pinch is held, as the pointer knows it. */
  held = false
  private openAt = 0

  constructor(private readonly openMs: number) {}

  /** The session says the pinch began. `'down'` means: press the pointer. */
  start(): 'down' | null {
    // The same pinch, back after a dropped frame: it goes on as if it had never opened.
    if (this.held && this.openAt) { this.openAt = 0; return null }
    if (this.held) return null
    this.held = true
    return 'down'
  }

  /** The session says the pinch ended, at `now` (ms). `'up'` means: release the pointer at once. */
  end(now: number): 'up' | null {
    if (!this.held) return null
    if (this.openMs <= 0) { this.held = false; return 'up' }
    this.openAt = now
    return null
  }

  /** Once a frame. `'up'` means: the pinch has stayed open long enough, release the pointer. */
  tick(now: number): 'up' | null {
    if (!this.held || !this.openAt || now - this.openAt < this.openMs) return null
    this.held = false
    this.openAt = 0
    return 'up'
  }

  /** The hand is gone. `'cancel'` means: the pointer loses its press, and what it held commits nothing more. */
  drop(): 'cancel' | null {
    if (!this.held) return null
    this.held = false
    this.openAt = 0
    return 'cancel'
  }
}
