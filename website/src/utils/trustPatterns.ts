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
 *
 * Elides the MIDDLE rather than the tail, which is what actually removes the
 * collision class. Commands that differ usually differ at the END — a filename,
 * a trailing path segment, a flag value — while sharing a long head
 * (`gh api repos/<owner>/<repo>/contents/…`). Cutting the tail is therefore
 * precisely what makes two different commands render identically, and raising the
 * budget alone would only move that cliff: a longer `owner/repo` pushes the
 * distinguishing filename past any fixed head budget.
 *
 * Keeping the tail also matters on touch, where the `title` tooltip callers
 * attach never fires, so the label is the whole basis for an exact-string grant.
 *
 * Must stay byte-identical to the mochi copy in
 * `website/src/apps/mochi/src/shared/trustPatterns.ts` — a divergent budget OR a
 * divergent algorithm is this same defect in a new place.
 */

/** Pull a head cut back off a lone high surrogate, so slicing never emits half a
 *  code point. Only ever shrinks, so the budget contract still holds. */
function snapHeadOffSurrogate(cmd: string, end: number): number {
  const code = cmd.charCodeAt(end - 1)
  return code >= 0xd800 && code <= 0xdbff ? end - 1 : end
}

/** Push a tail cut forward off a lone low surrogate, for the same reason. */
function snapTailOffSurrogate(cmd: string, start: number): number {
  const code = cmd.charCodeAt(start)
  return code >= 0xdc00 && code <= 0xdfff ? start + 1 : start
}

export function truncateCommandLabel(cmd: string, max = 64): string {
  if (cmd.length <= max) return cmd
  // One char of the budget goes to the ellipsis; the tail gets a third of the
  // rest, which is enough for a filename plus a short flag without starving the
  // head of the subcommand and repo. Degrades gracefully at any budget, so there
  // is deliberately no small-budget special case: no production caller passes
  // `max` at all, and a second branch in a security-adjacent helper is surface
  // that has to be reasoned about for no gain.
  const tail = Math.floor((max - 1) / 3)
  const head = max - 1 - tail
  // Both cuts are snapped off surrogate boundaries. `slice` counts UTF-16 code
  // units, so an unsnapped cut through an astral character (an emoji in a commit
  // message, a CJK extension-B ideograph in a path) leaves a lone surrogate that
  // renders as a replacement character -- turning a label the user is meant to
  // read for a security decision into mojibake.
  return (
    cmd.slice(0, snapHeadOffSurrogate(cmd, head))
    + '…'
    + cmd.slice(snapTailOffSurrogate(cmd, cmd.length - tail))
  )
}
