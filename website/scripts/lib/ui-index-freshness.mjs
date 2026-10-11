/**
 * What `gen-ui-index.mjs --check` does when the committed find_ui files
 * (the index and `guidePlans.gen.ts`) disagree with the tree.
 *
 * Three modes, because a stale index means different things in different places:
 *
 * - plain `--check` (CI's freshness job, `npm run gen:ui -- --check`): stale is
 *   a failure, exit {@link STALE_EXIT}. The PR freshness job
 *   (`.github/scripts/ui_index_freshness.py`) reads that code to tell a PR that
 *   caused the staleness from one that inherited a stale base.
 * - `--check --warn-if-stale` (`npm run build`, so `make build`, every PR build
 *   lane, main's Build and nightly): stale WARNS and the build goes on. The index
 *   is a merge-time artifact: each PR is fresh at its own head and can still
 *   merge onto a main that moved, so a hard stop here broke every build on main
 *   until someone landed a regen commit.
 * - `KC_UI_INDEX_STRICT=1` (release.yml's build lanes): `--warn-if-stale` is
 *   ignored and stale fails again, so a release never ships an index that
 *   disagrees with its own bundle.
 */

/** Exit code for a stale or missing committed file. 1 stays "the generator found a problem". */
export const STALE_EXIT = 3

/** True when `KC_UI_INDEX_STRICT` turns a warn-only check back into a failure. */
export function strictFromEnv(env) {
  const v = String(env.KC_UI_INDEX_STRICT ?? '').trim().toLowerCase()
  return v !== '' && v !== '0' && v !== 'false'
}

/**
 * @param {{ stale: Array<{ file: string, missing: boolean }>, warnIfStale: boolean, strict: boolean }} opts
 *   `stale` lists the committed files whose bytes differ from the regenerated ones.
 * @returns {{ exitCode: number, level: 'ok' | 'warning' | 'error', lines: string[] }}
 */
export function freshnessVerdict({ stale, warnIfStale, strict }) {
  if (stale.length === 0) return { exitCode: 0, level: 'ok', lines: [] }
  const what = stale.map((s) => `${s.file} is ${s.missing ? 'missing' : 'stale'}`).join('; ')
  const fix = 'Run `npm run gen:ui` in website/ and commit the result.'
  if (warnIfStale && !strict) {
    return {
      exitCode: 0,
      level: 'warning',
      lines: [
        `gen-ui-index: WARNING: ${what}. ${fix}`,
        'gen-ui-index: building anyway. Until it is regenerated, this build\'s find_ui may name paths '
          + 'its dashboard no longer draws, and a stale index makes its auto tier report "unavailable". '
          + 'Release builds (KC_UI_INDEX_STRICT=1) fail here instead.',
      ],
    }
  }
  return {
    exitCode: STALE_EXIT,
    level: 'error',
    lines: [`gen-ui-index: ${what}. ${fix}${strict && warnIfStale ? ' (KC_UI_INDEX_STRICT is set, so a stale index fails this build.)' : ''}`],
  }
}
