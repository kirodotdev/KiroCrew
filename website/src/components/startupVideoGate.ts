/**
 * Startup sequencing policy for the feature-intro video — the ONE question
 * `App` asks before it opens `StartupVideoModal` at all.
 *
 * The video is the lowest-priority thing that may interrupt a launch. Anything
 * else with a claim on the first screen (release notes, an update found, an
 * update staged and waiting to install, first-run onboarding) outranks it, and
 * the rule is deliberately blunt: if ANY of them appeared this launch, the video
 * waits for the NEXT one. It is never queued behind them.
 *
 * Why yielding costs a whole launch rather than a delay: a queue produces exactly
 * the thing this policy exists to prevent — the user dismisses release notes and
 * a second dialog takes its place, which reads as the app arguing with them. A
 * time-based deferral is the same failure with a pause in front of it. Waiting
 * loses nothing, because the backend keeps offering the clip until a verdict
 * retires it.
 *
 * This is its own eagerly-imported module rather than part of the modal because
 * the modal sits behind a `lazy()` boundary: `App` must be able to evaluate the
 * policy WITHOUT pulling the modal's chunk (and with it a video element and the
 * share-card graph) into the app-core bundle. Importing the policy from the
 * component it gates would defeat the code split.
 *
 * Note what is NOT here: whether a video exists, and whether the feature is
 * enabled at all. Those are properties of the DATA, they arrive with
 * `GET /api/feature-videos/next`, and the modal renders nothing when they say so.
 * This module answers only "may anything open right now".
 */

/** What `canShowStartupVideo` needs to know about this launch. */
export interface StartupVideoGateInput {
  /**
   * Has any higher-priority startup interruption been shown at ANY point this
   * launch? Latched by the caller, not sampled: a changelog the user has since
   * closed still spends the launch, otherwise closing it would hand straight over
   * to the video — the back-to-back pair the policy forbids.
   */
  interruptionShown: boolean
  /**
   * Have all of those interruptions actually DECIDED yet?
   *
   * Load-bearing, and the subtle half of this gate. The changelog resolves
   * asynchronously (read the version, then fetch the notes), so for the first
   * moments of a launch `interruptionShown` is false because nothing has decided
   * yet — not because nothing will. Opening on that reading races the changelog
   * and shows both.
   */
  settled: boolean
  /**
   * `memory_mode` of the active session. Incognito and temporary sessions keep
   * nothing, and a verdict here is a durable per-user write — so the video is
   * skipped outright rather than shown with a promise it cannot keep.
   */
  memoryMode?: 'persistent' | 'incognito' | 'temporary'
  /** Already opened once this launch — see the guard below. */
  handledThisLaunch: boolean
}

/**
 * The whole policy, as one pure predicate so it can be tested without mounting
 * the dashboard. Every clause is a veto; none of them is a preference.
 */
export function canShowStartupVideo({
  interruptionShown,
  settled,
  memoryMode,
  handledThisLaunch,
}: StartupVideoGateInput): boolean {
  if (!settled) return false
  if (interruptionShown) return false
  if (handledThisLaunch) return false
  if (memoryMode === 'incognito' || memoryMode === 'temporary') return false
  return true
}

/**
 * Per-launch guard.
 *
 * Module state, which is exactly the intended scope: a page load IS a launch, so
 * this resets when and only when a new launch begins. It is what stops the same
 * clip reopening after the modal closes — the verdict POST is the durable half,
 * but it is still in flight (or has failed) at the moment the modal unmounts, so
 * the gate cannot rely on the server having recorded it yet.
 */
let handled = false

/** Has the video already been opened this launch? */
export function startupVideoHandledThisLaunch(): boolean {
  return handled
}

/** Claim the launch. Idempotent; called when the modal is opened. */
export function markStartupVideoHandled(): void {
  handled = true
}

/**
 * Test-only: drop the launch claim.
 *
 * Exported because module state outlives an individual test the way it outlives
 * a component — the correct production behaviour and the wrong test behaviour.
 * Production code must never call this: a "launch" that can be reset on demand
 * is not a launch.
 */
export function resetStartupVideoLaunchGuardForTests(): void {
  handled = false
}
