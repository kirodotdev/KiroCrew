/**
 * How long a memory-mode switch may hold the composer locked.
 *
 * The switch locks every composer until its create and switch settle; a
 * stalled gateway would otherwise leave the user unable to type anywhere
 * until a reload. After this long the lock lifts, both sessions are kept and
 * nothing is carried or deleted. A function so a test can shorten it.
 */
export function memorySwitchTimeoutMs(): number {
  return 15_000
}
