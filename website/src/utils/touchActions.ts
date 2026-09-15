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
 *
 *   Its glyph is one step SMALLER than the single-button shape below (`h-4`
 *   against `h-5`) because a row carries several icons side by side and reads
 *   crowded at the larger size. The padding grows by the same step (`p-3`
 *   against `p-2.5`) so the TAP TARGET is unchanged at 40px: shrinking the
 *   glyph is a density decision, and it must not quietly become a
 *   touch-target regression. 40px is a convention miss against the 44px
 *   convention and well clear of WCAG 2.5.8's 24px floor -- see
 *   `docs/narrow-viewport.md`'s two-tier grading.
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
  '[@media(hover:none)]:opacity-100 [@media(hover:none)]:flex-wrap [@media(hover:none)]:[&_button]:p-3 [@media(hover:none)]:[&_svg]:h-4 [@media(hover:none)]:[&_svg]:w-4'

/**
 * Third shape, for a row of ICON-ONLY buttons (the assistant and user message
 * footers), and the one shape here that also styles the POINTER case. It works
 * the way ChatGPT's response-actions row does: every button is a fixed square
 * cell with its glyph centred, the cells sit flush (`gap-x-0`), a hover paints
 * the whole cell so its extent is visible, and a negative start margin on a
 * leading button pulls its glyph back onto the text column.
 *
 *   pointer: 28x28 cell, 14px glyph, first glyph pulled 7px  (glyph pitch 28)
 *   touch:   36x32 cell, 16px glyph, first glyph pulled 10px (glyph pitch 36)
 *
 * The touch cell is wider than it is tall on purpose, the way ChatGPT's is
 * (`touch:w-10 h-8`). Width is what separates NEIGHBOURS: a finger that lands
 * off-centre sideways hits the next action, so the glyph pitch has to carry
 * the safety margin. Height has no neighbour -- above is the message text,
 * below is whitespace -- and touch browsers already snap a near-miss onto the
 * only tappable element nearby, so extra height buys nothing and costs every
 * completed turn 8px. 32px is above WCAG 2.5.8's 24px floor; the 44px
 * convention (`docs/narrow-viewport.md`) was already a miss at the previous
 * 40px square. 36 wide also keeps six actions plus an en-US timestamp on one
 * line at 390pt, which the 40px square did not.
 *
 * The touch half replaces the padded-and-gapped row shape above, which put
 * 28px of air between glyphs (12+12 padding plus a 4px gap) and pushed the
 * first glyph 12px in from the column. Column gap only: `gap-y` is left alone
 * so a wrapped row still breathes. The descendant `[&_button]` rules beat a
 * button's own `p-0.5`/`rounded` by specificity (class + element), so the
 * buttons keep their base classes untouched. The hover paint is
 * `[&_button:hover]`, with `:hover` INSIDE the arbitrary selector: Tailwind 3
 * stacks variants right-to-left, so `[&_button]:hover:` compiles to
 * `.row:hover button` and paints every cell when the row is hovered.
 *
 * Icon-only is the contract: a fixed width would clip a text label, so a row
 * that carries one keeps `HOVER_NONE_ACTIONS_ROW_CLS`.
 */
export const ICON_ACTION_ROW_CLS =
  'gap-x-0 [&_button]:h-7 [&_button]:w-7 [&_button]:p-0 [&_button]:inline-flex [&_button]:items-center [&_button]:justify-center [&_button]:rounded-md [&_button:hover]:bg-bg-hover [&>button:first-child]:-ms-[7px] [@media(hover:none)]:opacity-100 [@media(hover:none)]:flex-wrap [@media(hover:none)]:[&_button]:h-8 [@media(hover:none)]:[&_button]:w-9 [@media(hover:none)]:[&_svg]:h-4 [@media(hover:none)]:[&_svg]:w-4 [@media(hover:none)]:[&>button:first-child]:-ms-2.5'

export const HOVER_NONE_ACTION_BTN_CLS =
  '[@media(hover:none)]:opacity-100 [@media(hover:none)]:p-2.5 [@media(hover:none)]:[&_svg]:h-5 [@media(hover:none)]:[&_svg]:w-5'
