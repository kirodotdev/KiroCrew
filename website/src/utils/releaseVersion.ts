/**
 * Order two Kiro Crew version strings, so the shell can tell which changelog
 * sections belong to the build the reader is actually running.
 *
 * ## Why the dashboard needs this at all
 *
 * The post-upgrade changelog modal used to slice `CHANGELOG.md` from the TOP of
 * the file down to the heading equal to the last-seen version. Both halves of
 * that rule fail on the shape `main` actually has:
 *
 *   * there was no UPPER bound, so the newest section in the file was shown
 *     whether or not the running build contains it; and
 *   * the lower bound was raw string equality against the last-seen version, so
 *     a last-seen version with no section of its own (which is every build
 *     between two releases) matched nothing and the loop ran to end-of-file.
 *
 * Together they produced the reported defect: a build running 0.6.0 opened a
 * modal titled `v0.6.0` whose "What's new" body was headed `[0.4.0]` — the last
 * RELEASED line — and invited the reader to update to it. `main` is bumped a
 * minor ahead of the released line on purpose, and a release's notes are written
 * when that release ships, so "the newest section in the file" is routinely
 * older than the running build and is never a safe thing to show.
 *
 * ## Why not reuse `changelog.py`'s folding
 *
 * `base_version()` folds every prerelease onto its release: `0.2.0rc8` and
 * `0.2.0rc9` are both `0.2.0`. That is right for the Releases ARCHIVE, which
 * lists one row per release, and wrong here, where an insider build stepping
 * from rc8 to rc9 is exactly a version change with notes to show. So this
 * compares versions WHOLE — release core first, then the prerelease tail — and
 * folds nothing.
 *
 * ## Shapes it has to order
 *
 * Every spelling the release pipeline emits, and nothing invented here:
 *
 *   0.6.0                          stable tag and wheel
 *   0.6.0-rc.2 / -insider.4        prerelease tag, desktop build
 *   0.6.0-nightly.20260806t065257  nightly desktop stamp
 *   0.6.0rc4                       any prerelease tag's WHEEL version
 *   0.6.0.dev20260806065257        nightly wheel
 *   ...+local                      a build/local segment on any of the above
 */

/** A version split into its numeric release core and its prerelease tail. */
type Parsed = {
  /** `[0, 6, 0]` for every spelling of 0.6.0. */
  core: number[]
  /** The prerelease tail, normalised: `-rc.2`, `rc2` and `.rc2` all give `rc2`. */
  tail: string
}

/** Leading numeric core: `0.6.0` out of `0.6.0-rc.2`, `0.6.0rc2`, `0.6.0.dev1`. */
const CORE_RE = /^[0-9]+(?:\.[0-9]+)*/

/**
 * Parse *version*, or return `null` when it carries no numeric core at all.
 *
 * `null` is the caller's signal that the two sides cannot be ordered, and every
 * caller here treats that as "do not show this section" rather than guessing —
 * showing notes we cannot place is the defect being fixed.
 */
function parse(version: string): Parsed | null {
  // A build/local segment identifies a build, not a version, and SemVer says it
  // takes no part in ordering. Dropped before anything else so `0.6.0+abc` and
  // `0.6.0` compare EQUAL instead of the local segment reading as a tail.
  const bare = version.trim().replace(/\+.*$/, '')
  const core = CORE_RE.exec(bare)
  if (!core) return null
  return {
    core: core[0].split('.').map(Number),
    // The separator is not information: the same prerelease is spelled `-rc.2`
    // by the tag and `rc2` by the wheel. Strip the leading separator and the
    // dots so both normalise to `rc2` and never order against each other.
    tail: bare.slice(core[0].length).replace(/^[-.]/, '').replace(/\./g, ''),
  }
}

/** Split a tail into digit / non-digit runs, for a natural-order comparison. */
const chunks = (tail: string): string[] => tail.match(/[0-9]+|[^0-9]+/g) ?? []

/**
 * Compare two prerelease tails so `rc9 < rc10` (a plain string compare says the
 * opposite, and an insider line reaching double digits is not hypothetical).
 *
 * Digit runs compare numerically, letter runs lexically. A tail that is a strict
 * prefix of the other sorts lower, which puts `rc` below `rc2`.
 */
function compareTails(a: string, b: string): number {
  const left = chunks(a)
  const right = chunks(b)
  for (let i = 0; i < Math.max(left.length, right.length); i++) {
    const l = left[i]
    const r = right[i]
    if (l === undefined) return -1
    if (r === undefined) return 1
    const numeric = /^[0-9]/.test(l) && /^[0-9]/.test(r)
    if (numeric) {
      if (Number(l) !== Number(r)) return Number(l) < Number(r) ? -1 : 1
    } else if (l !== r) {
      return l < r ? -1 : 1
    }
  }
  return 0
}

/**
 * Return `-1 | 0 | 1` for `a` against `b`, or `null` when either is unorderable.
 *
 * Cores are compared segment by segment with the shorter side zero-padded, so
 * `0.6` and `0.6.0` are EQUAL while `0.10.0` outranks `0.9.0`. On an equal core a
 * release outranks every prerelease of itself, per SemVer.
 */
export function compareVersions(a: string, b: string): number | null {
  const left = parse(a)
  const right = parse(b)
  if (!left || !right) return null
  const width = Math.max(left.core.length, right.core.length)
  for (let i = 0; i < width; i++) {
    const l = left.core[i] ?? 0
    const r = right.core[i] ?? 0
    if (l !== r) return l < r ? -1 : 1
  }
  if (left.tail === right.tail) return 0
  // An absent tail is the release itself, which ships after any draft of it.
  if (!left.tail) return 1
  if (!right.tail) return -1
  return compareTails(left.tail, right.tail)
}

/**
 * True when a changelog section for *section* describes something this build has
 * and the reader has not been shown: `lastSeen < section <= running`.
 *
 * Both bounds matter and for different reasons. The upper one keeps a release
 * newer than the running build out — that is the reported defect, where `main`'s
 * dev build was shown the last released line's notes. The lower one is what makes
 * the modal a diff rather than an archive.
 *
 * Returns false whenever a comparison is unorderable, so a version spelling
 * nobody anticipated shows NO notes instead of the wrong ones.
 */
export function isNewSection(section: string, lastSeen: string, running: string): boolean {
  const withinBuild = compareVersions(section, running)
  if (withinBuild === null || withinBuild > 0) return false
  const afterSeen = compareVersions(section, lastSeen)
  return afterSeen !== null && afterSeen > 0
}
