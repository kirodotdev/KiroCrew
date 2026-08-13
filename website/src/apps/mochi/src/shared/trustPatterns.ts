/**
 * Trust-grant pattern helpers for the pet's approval card.
 *
 * A trust grant widens what runs without asking again, and the `pattern` string
 * is what decides HOW MUCH it widens — so these transforms have to produce
 * exactly what the dashboard produces for the same click. The dashboard's
 * equivalents are inline in `website/src/components/TrustDropdown.tsx`
 * (`truncated` / `basePattern` there).
 *
 * DUPLICATED ON PURPOSE, FOR NOW. Hoisting this to a shared `utils/` module and
 * pointing TrustDropdown at it would mean editing a core dashboard component from
 * the PR that adds this app — the wrong blast radius for an app change. The
 * de-duplication belongs in its own PR, scoped to core, where a dashboard
 * reviewer sees it.
 *
 * Until then `mochiTrustScopes.test.ts` pins the exact output strings, so a
 * future change to either side that silently widens a grant fails a test rather
 * than shipping.
 *
 * The gateway supplies the inputs already computed and redacted (see
 * chat_runner's `_extract_full_command` / `_extract_base_command`); these
 * functions only shape them for the slot-approve endpoint's `pattern` field.
 */

/**
 * Turn the gateway's comma-joined base list into the glob pattern that trusts
 * each of those commands with any arguments.
 *
 *     "cat"      -> "cat *"
 *     "cat,wc"   -> "cat *,wc *"      (a piped/chained command)
 */
export function trustBasePattern(baseCommand: string): string {
  return baseCommand
    .split(',')
    .map(b => b.trim() + ' *')
    .join(',')
}

/**
 * Shorten a command for a BUTTON LABEL only — never for the pattern itself.
 * Truncating a pattern would change the grant; this is display only.
 *
 * Elides the MIDDLE rather than the tail. This label is the only place INSIDE
 * THE GRANT CONTROL that shows what is about to be trusted (the card renders
 * toolInput above it when the gateway sent one, but the trust row must stand on
 * its own), and cutting the tail is what makes two commands collide: they share a
 * long head (`gh api repos/<owner>/<repo>/contents/…`) and differ at the END, in
 * the filename. Raising the budget alone only moves that cliff, since a longer
 * `owner/repo` pushes the filename past any fixed head budget.
 *
 * Must stay byte-identical to the dashboard copy in
 * `website/src/utils/trustPatterns.ts` — a divergent budget OR a divergent
 * algorithm is this same defect in a new place. Callers pass the untruncated
 * command as a `title` tooltip so a long command stays fully recoverable.
 */
export function truncateCommandLabel(cmd: string, max = 64): string {
  if (cmd.length <= max) return cmd
  const tail = Math.floor((max - 1) / 3)
  const head = max - 1 - tail
  // Both cuts snap off surrogate boundaries: `slice` counts UTF-16 code units, so
  // an unsnapped cut through an astral character leaves a lone surrogate and the
  // label renders as a replacement character.
  const headEnd = isHighSurrogate(cmd.charCodeAt(head - 1)) ? head - 1 : head
  const tailStart = isLowSurrogate(cmd.charCodeAt(cmd.length - tail))
    ? cmd.length - tail + 1
    : cmd.length - tail
  return cmd.slice(0, headEnd) + '…' + cmd.slice(tailStart)
}

function isHighSurrogate(code: number): boolean {
  return code >= 0xd800 && code <= 0xdbff
}

function isLowSurrogate(code: number): boolean {
  return code >= 0xdc00 && code <= 0xdfff
}

/**
 * Is a family grant ("all `npm` commands") meaningfully different from a grant
 * for this exact command?
 *
 * A base differing from the full command means the command carries arguments or
 * is chained. For a plain MCP tool call the two are identical (a single token),
 * so offering the family option would duplicate the command option. Derived from
 * the two values rather than sniffing the title's display prefix, so no surface
 * has to couple to a server-side display string.
 */
export function familyGrantIsDistinct(
  fullCommand: string | undefined,
  baseCommand: string | undefined,
): boolean {
  return Boolean(baseCommand) && baseCommand !== fullCommand
}
