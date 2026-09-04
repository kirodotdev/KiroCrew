/**
 * Trust-grant pattern helpers for the pet's approval card.
 *
 * A trust grant widens what runs without asking again, and the `pattern` string
 * is what decides HOW MUCH it widens — so these transforms have to produce
 * exactly what the dashboard produces for the same click.
 *
 * The transforms shared with the dashboard are RE-EXPORTED from the single
 * source of truth in `website/src/utils/trustPatterns.ts` rather than copied:
 * two hand-synced copies are how a budget or algorithm change lands on one
 * surface and not the other (the drift class #4462 existed to remove). This
 * module stays as the seam so the vendored renderer keeps its original
 * `../shared/trustPatterns` import line untouched.
 *
 * The gateway supplies the inputs already computed and redacted (see
 * the gateway's shared `trust_patterns` module); these
 * functions only shape them for the slot-approve endpoint's `pattern` field.
 */
export { trustBasePattern, truncateCommandLabel } from '../../../../utils/trustPatterns'

/**
 * Is a family grant ("all `npm` commands") meaningfully different from a grant
 * for this exact command?
 *
 * A base differing from the full command means the command carries arguments or
 * is chained. For a plain MCP tool call the two are identical (a single token),
 * so offering the family option would duplicate the command option. Derived from
 * the two values rather than sniffing the title's display prefix, so no surface
 * has to couple to a server-side display string.
 *
 * Lives here (not in the shared module) because only the pet card offers the
 * family row this way — it has exactly one caller and is not duplicated.
 */
export function familyGrantIsDistinct(
  fullCommand: string | undefined,
  baseCommand: string | undefined,
): boolean {
  return Boolean(baseCommand) && baseCommand !== fullCommand
}
