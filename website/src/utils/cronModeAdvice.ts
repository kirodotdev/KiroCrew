/** Which cheaper execution mode a cron job's prompt suggests, if any.
 *
 *  A cron job dispatched to the agent pays for its whole injected context on
 *  every wake, whether or not the wake had anything to do. Two cheaper modes
 *  exist: a script job runs with no agent turn at all, and an agent job with
 *  minimal context keeps the agent but drops memory, lessons, steering, skills
 *  and prior session history. The create form used to offer neither, so a
 *  routine check registered here paid full price for its whole life.
 *
 *  This runs on every keystroke in the form, so it is a plain regex pass with no
 *  round trip.
 */

/** Work a program can settle with no reasoning.
 *
 *  Narrow on purpose. Widening this set produces MORE script suggestions, which
 *  is the direction that costs a user a broken job rather than a missed saving.
 */
const DETERMINISTIC =
  /\b(timestamps?|unchanged|last modified|mtime|newer than|older than|expired|disk|disk space|free space|disk usage|inodes?|file exists|already exists|is present|is missing|http status|status code|response code|ping|ports?|reachable|is up|is down|responding|checksums?|hash|byte size|file size|line count|row count|thresholds?|quota|above \d|below \d|exceeds|greater than|less than|count of|number of files|exit code)\b/i

/** Work that needs an agent. Any hit rules out a script suggestion outright.
 *
 *  Verb stems take a trailing `\w*` on purpose. Suggesting a script for a job
 *  that actually reasons is the failure that hides: the rewritten job stays
 *  green and quietly stops doing its work.
 */
const JUDGEMENT =
  /\b(summari[sz]\w*|summary of|review\w*|draft\w*|compose\w*|write up|write a|decide\w*|decision|judge\w*|assess\w*|evaluat\w*|interpret\w*|explain\w*|describe\w*|analy[sz]\w*|analysis|investigat\w*|diagnos\w*|root cause|triag\w*|recommend\w*|suggest\w*|prioriti[sz]\w*|rank\w*|classif\w*|categori[sz]\w*|brainstorm\w*|plan\w*|propos\w*|refactor\w*|implement\w*|fix the|repl(?:y|ies)|respond to)\b/i

/** Context a minimal-context run does not inject. Any hit rules out both modes. */
const NEEDS_CONTEXT =
  /\b(memor(?:y|ies)|remember|lessons?|preferences?|steering|knowledge base|previous session|past session|prior session|chat history|conversation history|my notes|project context|as we discussed)\b/i

/** `$skill` inline tokens. A minimal-context run injects no skill at all, so a
 *  job naming one this way stops working. Mirrors the core's own token shape. */
const SKILL_TOKEN = /(?<![\w$])\$([a-z0-9][a-z0-9/_-]*)/

/** A skill named in prose, for a prompt that spells it out instead. */
const SKILL_WORD = /\bskill\b/i

/** Below this many characters a prompt is still being typed, and a hint that
 *  appears and disappears on the third keystroke is noise rather than help. */
const MIN_LENGTH = 12

export type CronModeAdvice =
  /** Nothing worth saying. */
  | 'none'
  /** Needs an agent, does not need the full context. */
  | 'minimal-context'
  /** Needs no agent at all. Cheaper than minimal context, and the form cannot
   *  create it, so the hint points at chat or the CLI. */
  | 'script'

/**
 * Decide which cheaper mode to suggest for a job being described.
 *
 * @param message        the prompt the user has typed so far
 * @param minimalContext whether the form's minimal-context control is already on
 */
export function adviseCronMode(message: string, minimalContext: boolean): CronModeAdvice {
  const text = (message || '').trim()
  if (text.length < MIN_LENGTH) return 'none'
  // A job that reads its injected context cannot lose it, so neither mode
  // applies and there is nothing useful to say. Checked first: this is the one
  // signal that rules out both suggestions.
  if (NEEDS_CONTEXT.test(text) || SKILL_TOKEN.test(text) || SKILL_WORD.test(text)) return 'none'
  // Judgement outranks the mechanical signal, so a prompt carrying both is
  // never sent down the script path.
  if (JUDGEMENT.test(text)) return minimalContext ? 'none' : 'minimal-context'
  // Script is cheaper than minimal context, so it is still worth saying even
  // when minimal context is already on.
  if (DETERMINISTIC.test(text)) return 'script'
  return 'none'
}
