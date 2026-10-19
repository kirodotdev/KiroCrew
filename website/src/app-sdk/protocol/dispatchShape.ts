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
 * The `$skill` fence is held to the expander's own pattern and letter rule by
 * `test_dispatch_fence_parity.py`.
 */

import { PASTE_TOKEN_REGEX } from '../../utils/pasteTokens'
import { SKILL_TOKEN_RE } from '../../utils/skillToken'

const PASTE_TOKEN_RE = new RegExp(PASTE_TOKEN_REGEX.source)

// Labels opening with one of these dispatch as a harness command or a prompt mention.
const RESERVED_DISPATCH_SIGILS = ['/', '@', '!'] as const

// A `$name` token loads a skill body from ANYWHERE in the dispatched string, so this is
// the one dispatch form a leading-sigil test cannot see.
const EMBEDDED_SKILL_TOKEN_RE = new RegExp(SKILL_TOKEN_RE.source, 'g')

// The backend's `has_dollar_candidate` rule: a token with no letter is a price, not a
// skill attempt, so `Approve $40 spend` keeps one-click send.
const SKILL_TOKEN_LETTER_RE = /[a-z]/

function hasSkillToken(text: string): boolean {
  return Array.from(text.matchAll(EMBEDDED_SKILL_TOKEN_RE), m => m[0]).some(t => SKILL_TOKEN_LETTER_RE.test(t))
}

// The send path trims before dispatching, and its trim drops characters `\s` does not
// cover, so a guard reading the untrimmed string tests a value the dispatch never sees.
const DISPATCH_LEADING_RE =
  /^[\u0009-\u000D\u001C-\u0020\u0085\u00A0\u1680\u200B-\u200D\u2000-\u200A\u2028\u2029\u202F\u205F\u2060\u3000\uFEFF]+/

/**
 * Report whether *text* dispatched as-is would execute rather than read as prose.
 *
 * Prefix-only for the sigils, deliberately: any of them INSIDE a label ("Run the a/b
 * test", "ping @ 5pm") is ordinary prose, and a substring test would refuse unrelated
 * chips. The embedded-token check is the one exception, because the skill expander
 * resolves `$name` at any offset.
 */
export function dispatchIsCommandShaped(text: string): boolean {
  const dispatched = text.replace(DISPATCH_LEADING_RE, '')
  if (!dispatched) return false
  if (RESERVED_DISPATCH_SIGILS.some(sigil => dispatched.startsWith(sigil))) return true
  if (hasSkillToken(dispatched)) return true
  // The send path expands a paste token into the staged block it names, so the label
  // the user read is not the text that goes out.
  if (PASTE_TOKEN_RE.test(dispatched)) return true
  return false
}
