/**
 * The i18n lint with the ALL-CAPS declarator hole closed — the ONE config
 * `scripts/check-i18n-strings.mjs` runs over the tree.
 *
 * ## Why this is a second config and not a change to the first
 *
 * `eslint-plugin-i18next` exempts every literal under an ALL-CAPS variable
 * declarator, which is where this dashboard keeps its module-level UI-copy tables
 * (`SESSION_FILTERS`, `SORT_OPTIONS`, `EFFORT_DISPLAY`). Closing that hole makes
 * ~1000 pre-existing, never-counted strings visible at once. Feeding them into
 * `src/i18n/untranslated-baseline.json` would mean one `--update` re-snapshot,
 * and `website/AGENTS.md` ("A ratchet may only be upward-only if a DIFF-SCOPED
 * gate covers the same defect") is explicit that the ledger is not re-snapshotted:
 * a bulk `--update` is precisely the bypass that made the old bidirectional gate
 * decorative — commit 195904c shipped 113 untranslated strings while moving
 * `_total` from 1747 to 1860, green.
 *
 * So the two populations are separated by MARKING rather than by re-measuring.
 * `eslint-rules/i18n-strict.js` appends `ALL_CAPS_MARKER` to every finding it
 * recovers, and one pass then serves both consumers:
 *
 *   - findings WITHOUT the marker → the per-file ceilings in
 *     `untranslated-baseline.json`. That set is provably identical to what the
 *     upstream rule reports (asserted in `src/test/i18nStrictRule.test.ts`, and
 *     1814 == 1814 across 265 files at the time of writing), so the committed
 *     ledger keeps measuring the population it was snapshotted from and nobody
 *     has to re-snapshot anything.
 *   - findings WITH the marker → one aggregate in
 *     `untranslated-strict-baseline.json`, upward-only, goal zero. A separate file
 *     because no other branch touches one that does not yet exist.
 *   - every finding, marked or not → `[added-lines]` and `[vs-base]`, which have
 *     no ledger to buy past. A new ALL-CAPS label table, or a new label added to
 *     an existing one, fails at zero tolerance.
 *
 * Net effect: the hidden class becomes un-addable without inflating a ledger that
 * four open branches share, and it still has a number to drive to zero.
 *
 * The config is DERIVED from the base array rather than copied, so the two can
 * only ever differ in which rule implementation runs — never in the exemptions.
 */

import base from './eslint.i18n.config.js'
import i18nStrict from './eslint-rules/i18n-strict.js'

const UPSTREAM_RULE = 'i18next/no-literal-string'
const STRICT_RULE = 'i18n-strict/no-literal-string'

/**
 * `'off'` (or `0`), in bare or array form. A block that DISABLES the rule for
 * some path is an exemption, not a configuration of it.
 */
const disables = (options) => {
  const severity = Array.isArray(options) ? options[0] : options
  return severity === 'off' || severity === 0
}

let enabling = 0
const strict = base.map((block) => {
  const options = block.rules?.[UPSTREAM_RULE]
  if (!options) return block
  // Every block is swapped into the strict namespace, including the `off`
  // overrides: leaving one behind would delete an exemption rather than move it,
  // because the base rule is not registered here and the strict rule — which IS —
  // would then keep reporting a path the base config had deliberately released.
  if (!disables(options)) enabling += 1
  const rules = { ...block.rules }
  delete rules[UPSTREAM_RULE]
  return {
    ...block,
    plugins: { ...block.plugins, 'i18n-strict': i18nStrict },
    rules: { ...rules, [STRICT_RULE]: options },
  }
})

if (enabling !== 1) {
  // A silent 0 here would mean the strict gate runs no i18n rule at all and
  // reports every diff as clean. Only rule-ENABLING blocks are counted: per-file
  // `off` exemptions are expected to accrue in the base config over time (see
  // `src/lib/commitProfiler.tsx`), and each one is carried across above, so
  // counting them would fail this gate every time an unrelated PR adds one.
  throw new Error(
    `expected exactly one enabling \`${UPSTREAM_RULE}\` block in eslint.i18n.config.js, found ${enabling}. `
    + 'Rework eslint.i18n.strict.config.js rather than shipping a diff gate that checks nothing.',
  )
}

export default strict
