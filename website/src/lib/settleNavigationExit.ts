/**
 * Claimed in-app exit, as `useClaimNavigationExitAttempt` hands it out. Lives
 * here, beside the one settle rule, so router-free modules (`utils/navIntent`,
 * `utils/errorReport`) can settle an exit without importing React components.
 */
export type ClaimedNavigationExitAttempt = {
  isSourceCurrent: () => boolean
  end: () => void
}

/** The page's answer to "may I leave?", or the leave check that produces it. */
export type LeaveAnswer = boolean | Promise<boolean> | (() => boolean | Promise<boolean>)

/**
 * Finish a claimed exit once the page's answer is known: in the same task when
 * the answer is a plain boolean, after the confirmation otherwise. The attempt
 * is released (and `onSettled` run) before `perform`, and `perform` runs only
 * when the user agreed and the page that asked is still the one on screen.
 *
 * Pass the leave check itself rather than its result, so a check that throws
 * or rejects counts as "stay" and still releases the attempt. A `null` attempt
 * claimed nothing. `refused` is returned when `perform` does not run.
 */
export function settleNavigationExit(
  attempt: ClaimedNavigationExitAttempt | null,
  answer: LeaveAnswer,
  perform: () => void,
  onSettled?: () => void,
): void
export function settleNavigationExit<T>(
  attempt: ClaimedNavigationExitAttempt | null,
  answer: LeaveAnswer,
  perform: () => T,
  onSettled: (() => void) | undefined,
  refused: T,
): T | Promise<T>
export function settleNavigationExit<T>(
  attempt: ClaimedNavigationExitAttempt | null,
  answer: LeaveAnswer,
  perform: () => T,
  onSettled?: () => void,
  refused?: T,
): T | undefined | Promise<T | undefined> {
  const settle = (allowed: boolean): T | undefined => {
    const ok = allowed && (attempt === null || attempt.isSourceCurrent())
    attempt?.end()
    onSettled?.()
    return ok ? perform() : refused
  }
  let resolved: boolean | Promise<boolean>
  try { resolved = typeof answer === 'function' ? answer() : answer } catch { resolved = false }
  if (typeof resolved === 'boolean') return settle(resolved)
  return resolved.then(settle, () => settle(false))
}
