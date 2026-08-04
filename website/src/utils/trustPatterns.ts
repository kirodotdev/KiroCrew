/**
 * Trust-grant pattern helpers — the single source of truth for how a trust
 * click is turned into a `pattern` string.
 *
 * Why this is a module and not three inline expressions: the `pattern` sent with
 * `trust_command` / `trust_base` is what decides HOW MUCH a grant widens. Any
 * surface that offers tiered trust (the dashboard's `TrustDropdown`, and any
 * embedded/companion approval UI) has to produce byte-identical patterns for the
 * same click. Two independent copies can drift silently — the button label stays
 * the same while the granted scope changes — so the transform lives here and is
 * imported, never re-derived.
 *
 * Inputs arrive already computed and redacted by the gateway (see chat_runner's
 * `_extract_full_command` / `_extract_base_command`); these functions only shape
 * them for the slot-approve endpoint.
 */

/**
 * Turn the gateway's comma-joined base list into the glob pattern that trusts
 * each of those commands with any arguments.
 *
 *     "cat"     -> "cat *"
 *     "cat,wc"  -> "cat *,wc *"      (a piped/chained command)
 */
export function trustBasePattern(baseCommand: string): string {
  return baseCommand
    .split(',')
    .map(b => b.trim() + ' *')
    .join(',')
}

/**
 * Render the base list for display: `"cat,wc"` -> `"cat, wc"`.
 *
 * Label only. Never pass this to a `pattern` field — the spaces after the commas
 * are cosmetic and would not match.
 */
export function baseCommandLabel(baseCommand: string): string {
  return baseCommand.split(',').join(', ')
}

/**
 * Shorten a command for a BUTTON LABEL only — never for the pattern itself.
 * Truncating a pattern would change the grant; this is display only.
 */
export function truncateCommandLabel(cmd: string, max = 30): string {
  return cmd.length > max ? cmd.slice(0, max) + '…' : cmd
}
