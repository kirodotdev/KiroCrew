/**
 * Breathing exercise timeline — pure, so the phase maths is testable without a
 * clock or a DOM.
 *
 * 4-7-8 breathing (Dr Andrew Weil): inhale 4s, hold 7s, exhale 8s, four cycles.
 * Four cycles is Weil's own prescribed dose, not an arbitrary number.
 *
 * Chosen over cyclic sighing on SIMPLICITY. Cyclic sighing has slightly better
 * trial evidence for mood (Balban et al. 2023), but its double inhale means three
 * distinct actions per breath, which read as fiddly in use. 4-7-8 keeps the thing
 * that does the work — an exhale twice the length of the inhale — with only one
 * action per phase.
 *
 * Honest scope: 4-7-8 is popular and long-taught, but has less direct RCT support
 * than cyclic sighing or coherent breathing. The extended-exhale mechanism it relies
 * on is well established; the specific ratio is convention rather than a measured
 * optimum.
 *
 * Ported unchanged from the desktop app's `src/shared/breathing.ts`. The phase
 * labels are KEY NAMES here on purpose: this module is clock-only logic that the
 * tests assert against, so translation happens in the view and the timeline's tests
 * stay free of locale.
 */

export interface BreathPhase {
  /** What the user is told to do. */
  /**
   * Catalogue key under `apps.crewCompanion.breathe.`, never prose. The same rule
   * the backend follows for break nudges: emit a key, let the renderer translate.
   * Holding English here would ship an untranslated exercise in every language.
   */
  labelKey:
    | 'apps.crewCompanion.breathe.ready'
    | 'apps.crewCompanion.breathe.inhale'
    | 'apps.crewCompanion.breathe.hold'
    | 'apps.crewCompanion.breathe.exhale'
  ms: number
  /** Companion scale to reach during this phase — stands in for lung volume. */
  scale: number
}

/**
 * A lead-in before the first breath, so the exercise does not start mid-thought.
 * Without it the first "Inhale" arrives while the user is still reading the screen,
 * and the whole first cycle is spent catching up.
 */
export const READY_MS = 3000
export const READY_PHASE: BreathPhase = { labelKey: 'apps.crewCompanion.breathe.ready', ms: READY_MS, scale: 0.85 }

/** Inhale 4s, hold 7s, exhale 8s. The 1:2 inhale:exhale ratio is the active part. */
export const BREATH_PHASES: readonly BreathPhase[] = [
  { labelKey: 'apps.crewCompanion.breathe.inhale', ms: 4000, scale: 1.18 },
  { labelKey: 'apps.crewCompanion.breathe.hold', ms: 7000, scale: 1.18 },
  { labelKey: 'apps.crewCompanion.breathe.exhale', ms: 8000, scale: 0.58 },
]

/** Weil's prescribed dose: four cycles. 4 x 19s = 76s, plus the 3s lead-in. */
export const BREATH_CYCLES = 4

export const CYCLE_MS = BREATH_PHASES.reduce((sum, p) => sum + p.ms, 0)
export const TOTAL_MS = READY_MS + CYCLE_MS * BREATH_CYCLES

export interface BreathState {
  phase: BreathPhase
  /** Index within BREATH_PHASES, or -1 during the lead-in. */
  phaseIndex: number
  /** 1-based cycle for display; 0 during the lead-in. */
  cycle: number
  /** True during the "Get ready" lead-in. */
  ready: boolean
  /** True once the whole exercise has elapsed. */
  done: boolean
  /**
   * Whole seconds left in THIS phase, counting down to 1.
   *
   * A per-phase count is a rhythm guide, not a clock — it gives the eye one thing
   * to follow. A total-time countdown would do the opposite and invite
   * clock-watching, which is why only the phase gets one.
   */
  secondsLeft: number
}

const secondsLeftIn = (phaseMs: number, within: number): number =>
  Math.max(1, Math.ceil((phaseMs - within) / 1000))

/**
 * Where the exercise is at `elapsedMs`.
 *
 * Derived from elapsed time rather than advanced by a chain of timeouts, so a
 * dropped or delayed frame cannot desynchronise the label from the animation —
 * both read from the same function on the same frame.
 */
export function breathStateAt(elapsedMs: number): BreathState {
  const clamped = Math.max(0, elapsedMs)

  if (clamped < READY_MS) {
    return {
      phase: READY_PHASE, phaseIndex: -1, cycle: 0, ready: true, done: false,
      secondsLeft: secondsLeftIn(READY_MS, clamped),
    }
  }

  const afterReady = clamped - READY_MS
  if (afterReady >= CYCLE_MS * BREATH_CYCLES) {
    const last = BREATH_PHASES.length - 1
    return {
      phase: BREATH_PHASES[last], phaseIndex: last,
      cycle: BREATH_CYCLES, ready: false, done: true, secondsLeft: 0,
    }
  }

  const cycle = Math.floor(afterReady / CYCLE_MS)
  let within = afterReady - cycle * CYCLE_MS

  for (let i = 0; i < BREATH_PHASES.length; i++) {
    if (within < BREATH_PHASES[i].ms) {
      return {
        phase: BREATH_PHASES[i], phaseIndex: i, cycle: cycle + 1,
        ready: false, done: false,
        secondsLeft: secondsLeftIn(BREATH_PHASES[i].ms, within),
      }
    }
    within -= BREATH_PHASES[i].ms
  }

  // Unreachable while CYCLE_MS is the sum of the phases, but keeps the return
  // type honest rather than relying on that invariant holding forever.
  const last = BREATH_PHASES.length - 1
  return {
    phase: BREATH_PHASES[last], phaseIndex: last, cycle: cycle + 1,
    ready: false, done: false, secondsLeft: 1,
  }
}
