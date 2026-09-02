/**
 * Touch escape hatch for hover-revealed action affordances.
 *
 * Chat-surface actions hide behind `opacity-0` + `group-hover/*:opacity-100`,
 * which a touch pointer can never trigger: where `(hover: none)` matches, the
 * actions are permanently invisible and, even when forced visible, sit below
 * the 40px touch-target floor. These clusters force the actions visible and
 * grow every target to 40px (20px icon + 10px padding) under `(hover: none)`,
 * while leaving hover-capable pointers byte-identical to before.
 *
 * Proven on the assistant-message footer (issues #2014/#3584, PRs #1895,
 * #2013, #2016); shared here so the cluster is defined once instead of being
 * hand-copied per component. Tailwind scans this file (`src/**` content glob),
 * so the literal class strings below are what generates the CSS — keep them
 * as plain literals, never build them dynamically.
 *
 * Two shapes exist because the override targets differ:
 *
 * - `HOVER_NONE_ACTIONS_ROW_CLS` goes on a flex ROW of action buttons. The
 *   grown row can exceed a phone's width, so it must also `flex-wrap`, and the
 *   buttons/icons are matched as descendants (`[&_button]`, `[&_svg]`).
 * - `HOVER_NONE_ACTION_BTN_CLS` goes on a SINGLE action button (the element
 *   itself, not an ancestor): `[&_button]` cannot match it, so the padding is
 *   applied directly, and `flex-wrap` is meaningless on one absolutely
 *   positioned button. The direct `p-2.5` wins over the button's own base
 *   padding by Tailwind's variant-after-base ORDERING, not by specificity —
 *   so this shape is only valid on buttons whose padding comes from an
 *   unvariated base utility (`p-1.5`, `px-2`); a padding that itself carries
 *   a variant (`sm:p-1.5`) could sort after the override and silently win.
 */
export const HOVER_NONE_ACTIONS_ROW_CLS =
  '[@media(hover:none)]:opacity-100 [@media(hover:none)]:flex-wrap [@media(hover:none)]:[&_button]:p-2.5 [@media(hover:none)]:[&_svg]:h-5 [@media(hover:none)]:[&_svg]:w-5'

export const HOVER_NONE_ACTION_BTN_CLS =
  '[@media(hover:none)]:opacity-100 [@media(hover:none)]:p-2.5 [@media(hover:none)]:[&_svg]:h-5 [@media(hover:none)]:[&_svg]:w-5'
