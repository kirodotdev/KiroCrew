/**
 * Whether dispatching a string verbatim would RUN something rather than say something.
 *
 * Quick-send turns one click on a follow-up chip into a sent message, and an option
 * reaches that chip from content an agent read. So a label spelling `/clear` ran on one
 * click and erased the transcript. The reachable chain is opt-in quick send, an
 * agent-authored option, and one ordinary user click.
 *
 * Scoped to the forms that EXECUTE: a dispatch sigil, an embedded `$skill` token, a
 * paste token. Provenance openers are deliberately absent -- they mislabel a message's
 * origin rather than running anything, and such a label is the producer's own visible
 * text that the user reads before clicking.
 *
 * The `$skill` fence is held to the expander's own pattern by
 * `test_dispatch_fence_parity.py`.
 */

import { SKILL_TOKEN_RE } from '../../utils/skillToken'


// Labels opening with one of these dispatch as a harness command or a prompt mention.
// `!` is absent: only the Slack and Discord handlers read it, and the composer's own
// `!` terminal mode reviews the command before it runs.
const RESERVED_DISPATCH_SIGILS = ['/', '@'] as const

// A `$name` token loads a skill body from ANYWHERE in the dispatched string, so this is
// the one dispatch form a leading-sigil test cannot see. Digit-only tokens count too: the
// resolver loads a skill named `40` as readily as `deploy`, so `Approve $40 spend` is
// staged for review rather than sent on one click.
function hasSkillToken(text: string): boolean {
  return SKILL_TOKEN_RE.test(text)
}

// The send path trims before dispatching, and its trim drops characters `\s` does not
// cover, so a guard reading the untrimmed string tests a value the dispatch never sees.
// Each entry is an inclusive [first, last] code-point range.
const DISPATCH_TRIMMED_RANGES: ReadonlyArray<readonly [number, number]> = [
  [0x0009, 0x000d],
  [0x001c, 0x0020],
  [0x0085, 0x0085],
  [0x00a0, 0x00a0],
  [0x1680, 0x1680],
  [0x2000, 0x200d],
  [0x2028, 0x2029],
  [0x202f, 0x202f],
  [0x205f, 0x2060],
  [0x3000, 0x3000],
  [0xfeff, 0xfeff],
]

function isDispatchTrimmed(cp: number): boolean {
  return DISPATCH_TRIMMED_RANGES.some(([first, last]) => cp >= first && cp <= last)
}

// Every trimmed code point is in the BMP, so one UTF-16 unit is one code point here.
function stripDispatchLeading(text: string): string {
  let i = 0
  while (i < text.length && isDispatchTrimmed(text.charCodeAt(i))) i++
  return text.slice(i)
}

/**
 * Report whether *text* dispatched as-is would execute rather than read as prose.
 *
 * Prefix-only for the sigils, deliberately: any of them INSIDE a label ("Run the a/b
 * test", "ping @ 5pm") is ordinary prose, and a substring test would refuse unrelated
 * chips. The embedded-token check is the one exception, because the skill expander
 * resolves `$name` at any offset.
 */
export function dispatchIsCommandShaped(text: string): boolean {
  const dispatched = stripDispatchLeading(text)
  if (!dispatched) return false
  if (RESERVED_DISPATCH_SIGILS.some(sigil => dispatched.startsWith(sigil))) return true
  if (hasSkillToken(dispatched)) return true
  // The send path expands a paste token into the staged block it names, so the label
  // the user read is not the text that goes out. Every token opens with this fixed
  // text (see `formatToken` in `utils/pasteTokens.ts`); a false positive only puts the
  // label in the draft for review, which is the safe direction.
  if (dispatched.includes('[ Paste #')) return true
  return false
}
