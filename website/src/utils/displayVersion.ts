/**
 * Mirrors the backend's `_display_version` (handlers/updates.py) for version
 * strings that never cross the gateway (the Electron updater's own reports):
 * a promoted STABLE build keeps its soaked candidate's prerelease stamp in
 * the bytes (promotion never re-stamps), so fold the stamp to its clean base
 * for DISPLAY on the stable channel only. Keys on the FOLLOWED channel, same
 * as the backend, plus the same escape the backend applies via
 * `_channel_move_pending`: bytes the followed lane has never published are not
 * folded. Lenient like the backend's `running_release` rule — any SemVer
 * prerelease label folds onto its numeric base, because release.yml passes
 * every `v1.2.3-<label>` tag's label through unchanged.
 *
 * DISPLAY ONLY. Every functional reader keeps the raw string: the updater's
 * compare gate, `versionLooksPrerelease`, the arm target, and the update
 * popup's per-version snooze/skip keys. Gateway-reported versions should
 * prefer the backend-folded `*_display` sibling fields and use this only as
 * a fallback shape for desktop-local values.
 */
export function foldStableStamp(
  version: string,
  channel: string | null | undefined,
  runningAheadOfChannel?: boolean | null,
): string {
  if (channel !== 'stable') return version
  // These bytes are ahead of everything the stable lane publishes, so stable has
  // never shipped them and folding would invent a release: an insider
  // `0.5.0-insider.2` whose channel preference was flipped to stable rendered as
  // a clean `v0.5.0` that does not exist. UNKNOWN (undefined/null — no check has
  // completed) keeps folding, which is the promoted-stable case the fold exists
  // for and the overwhelmingly common one.
  if (runningAheadOfChannel === true) return version
  const m = /^([0-9]+(?:\.[0-9]+)*)-.+$/.exec(version.trim())
  return m ? m[1] : version
}

/**
 * Do two version strings name the SAME build, across the two spellings the
 * release pipeline emits for it?
 *
 * One release stamps its bytes twice: the desktop shell carries the SemVer
 * form (`0.6.0-insider.4`, `0.6.0-rc.2`, `0.6.0-nightly.20260806t065257`) and
 * the wheel the gateway runs from carries the PEP 440 form of the same tag
 * (`0.6.0rc4`, `0.6.0rc2`, `0.6.0.dev20260806065257`). Read side by side they
 * look like two releases; they are one. So a shell that spawned its own
 * gateway compares its `getInfo().version` against the gateway's `version`
 * and must get "same" on every channel, not just on stable where both sides
 * happen to fold their stamp away.
 *
 * Normalises exactly the pipeline's own aliases and nothing more: separators
 * and dots are dropped, any tagged prerelease label (`insider`, `rc`, `beta`…)
 * is the desktop spelling of the wheel's `rc`, `nightly` is the desktop
 * spelling of `dev` with a `t` inside the timestamp, and a `+local` build
 * segment identifies a build, not a version. A string with no numeric core
 * compares unequal to everything, including itself — "cannot tell" must never
 * read as "same".
 */
export function sameBuildVersion(a: string, b: string): boolean {
  const left = buildKey(a)
  const right = buildKey(b)
  return left !== null && right !== null && left.length === right.length
    && left.every((part, i) => part === right[i])
}

/**
 * One entry per prerelease FAMILY the pipeline emits, matching both of its
 * spellings and capturing only the digits that identify the build. Matched
 * against the tail with its separators and dots already stripped, so `-rc.2`
 * and `rc2` both arrive as `rc2`. The entry's INDEX is the family in the key.
 *
 * - Tagged prerelease: `release.yml` maps ANY `-<label>.N` tag to the wheel
 *   version `rcN` — not only `-insider.N` but `-rc.N`, `-beta.N`,
 *   `-beta-preview.N`, whatever label the tag carried — using only the
 *   TRAILING number. So the desktop spelling is `<anything>N` (the label may
 *   itself contain hyphens or digits) and `rcN` is what the wheel says back.
 * - Nightly: `nightly.yml` stamps `-nightly.<date>t<time>` on the desktop and
 *   `.dev<date><time>` on the wheel.
 *
 * A stable build has an empty tail, which matches neither family and is kept
 * verbatim, so it compares equal only to another empty tail. The nightly
 * family is listed first because its tail also ends in digits.
 */
const PRERELEASE_FAMILIES: readonly RegExp[] = [
  /^(?:nightly|dev)([0-9]{8})t?([0-9]{6})$/,
  /^[a-z][a-z0-9-]*?([0-9]+)$/,
]

/** `[core segments..., family, ...identifying digits]`, or null with no numeric core. */
function buildKey(version: string): (number | string)[] | null {
  const bare = version.trim().replace(/\+.*$/, '')
  const core = /^[0-9]+(?:\.[0-9]+)*/.exec(bare)
  if (!core) return null
  const segments = core[0].split('.').map(Number)
  while (segments.length > 1 && segments[segments.length - 1] === 0) segments.pop()
  const tail = bare.slice(core[0].length).replace(/^[-.]/, '').replace(/\./g, '').toLowerCase()
  const family = PRERELEASE_FAMILIES.findIndex((re) => re.test(tail))
  const ident = family >= 0 ? PRERELEASE_FAMILIES[family].exec(tail)!.slice(1) : [tail]
  return [...segments, family, ...ident]
}
