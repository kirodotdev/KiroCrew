/**
 * The ONE rule for "did the lane this install FOLLOWS publish the bytes it is
 * RUNNING" — the exemption that lets a promoted stable release stop being
 * mislabelled as a prerelease.
 *
 * WHY IT EXISTS. Promotion re-points the soaked candidate's bytes at the stable
 * channel without re-stamping them, so the stable lane's current release is
 * literally `0.4.1rc1` / `0.4.1-insider.1`. Every surface that read the version
 * string to answer "which lane am I on" therefore answered `insider` for the
 * whole promoted-stable population. The feed's own answer is the only honest
 * input, and this predicate is the single place it is interpreted — three
 * hand-rolled copies (header chip, gateway panel, desktop panel) had already
 * started to drift apart on what counts as a completed comparison.
 *
 * WHAT `laneAnswered` MUST BE. Proof that a comparison COMPLETED, taken from the
 * same source as `runningAheadOfLane`. Pairing a verdict with a
 * different-provenance "did we check" flag is how the header and the panel came
 * to disagree: a local `useState` set by a manual check does not license a
 * verdict that only ever arrives on the status frame.
 *
 * ACCEPTED RESIDUAL, and why it is the right trade. The feed reports ONE latest
 * version, so "not ahead of the lane" cannot separate two cases: bytes that ARE
 * the lane's release, and prerelease bytes strictly BEHIND it (an insider build
 * older than what stable now publishes). The second folds and loses its
 * prerelease ask until the offered update lands. There is no signal that
 * separates them — the alternative is to fold nothing, which returns every
 * promoted-stable user to the raw `rc` stamp this exists to hide. A
 * FAILED/absent check is deliberately NOT exempt for the same reason the rest of
 * this change treats unknown as unknown: never claim these bytes are the stable
 * release without evidence. The cost is a promoted-stable install on a
 * feed-unreachable host keeping its prerelease affordances — a nuisance, where
 * the opposite error is a false claim about which version is running.
 */
export function bytesAreTheStableRelease({
  followedChannel,
  laneAnswered,
  runningAheadOfLane,
}: {
  /** The channel this install FOLLOWS (not the lane its version string reads as). */
  followedChannel: string | null | undefined
  /** Did a comparison against that lane's feed actually complete? */
  laneAnswered: boolean
  /** ...and did it find the running build ahead of what that lane publishes? */
  runningAheadOfLane: boolean
}): boolean {
  return followedChannel === 'stable' && laneAnswered && !runningAheadOfLane
}
