/**
 * Hook lifecycle event names, as the backend spells them.
 *
 * These are WIRE VALUES, not copy: the API matches them by value against the
 * backend's own event allowlist, so a translated or reworded one is rejected. They
 * live here, apart from the page, so that boundary is visible in one place and
 * the i18n literal-string lint can be scoped to exactly this file.
 */
export const EVENTS = [
  'AgentSpawn',
  'UserPromptSubmit',
  'PreToolUse',
  'PostToolUse',
  'Stop',
  'SessionLaneChanged',
]

/** Glob metacharacters. Only these four stand in for characters we cannot know. */
const GLOB_WILDCARDS = /[*?[\]]/g

/**
 * Can this `SessionLaneChanged` matcher never match any tag id?
 *
 * The charset is the backend allowlist's own, `_TOKEN_ALLOWED` = `[a-z0-9_-]+`, which every id
 * must satisfy to reach a matcher token at all -- and ids are `uuid4().hex[:12]` or a seeded lane
 * key (`done`, `todo`, `review`, `planned`, `implementation`), so a seeded id is exactly as valid
 * as a generated hex one and must NOT be condemned. Hyphen and underscore are IN the test because
 * that allowlist admits them.
 *
 * ONLY `*?[]` are stripped, and getting that wrong hid dead matchers: this runs for GLOB mode,
 * where the backend is `fnmatch`, so `.+()|^${}` are LITERAL there. Stripping them as if they
 * were wildcards collapsed `d.ne` to `done` and suppressed the warning for a matcher no id can
 * ever match. A character that is literal in glob and absent from the charset now condemns.
 *
 * CASE IS IRRELEVANT, and getting that wrong is what this comment exists to prevent: the
 * backend matches with `fnmatch(context.lower(), matcher.lower())`, lowercasing BOTH sides, so
 * `*added:Done;*` fires exactly as `*added:done;*` does. The literal is therefore folded before
 * the charset test, and only a character no id may hold AT ANY CASE — a space, punctuation —
 * condemns a term.
 *
 * The matcher context spells each id as `added:<id>;` / `removed:<id>;`, so two shapes fire on
 * nothing and both save without complaint: one with no direction-tagged term at all, and one
 * whose term spells something outside the charset. Wildcards stand in for unknown characters,
 * so a term is only condemned on what it spells literally.
 */
export function matcherCannotMatchAnyTagId(matcher: string): boolean {
  const text = matcher.trim()
  if (text === '') return false

  // `:<id>;` with no direction word is the spec's "any movement" selector, so the direction
  // is optional -- but the trailing `;` is NOT required, or a trailing wildcard drops the term.
  const terms = [...text.matchAll(/(?:added|removed)?:([^;]*)/g)]
  // Every context token closes with `;` and matching is whole-string, so a final term stopping
  // at the id leaves that `;` unconsumed; only a trailing `*`/`?` can span it.
  const tail = /(?:added|removed)?:([^;]*)$/.exec(text)
  if (tail && tail[1] !== '' && !/[*?]$/.test(tail[1])) return true
  if (terms.length === 0) {
    // A context token is `added:<id>;` and matching is whole-string, so a no-`:` pattern must
    // span both ends itself: `*done` never reaches the `;`, `done*` never starts at `added:`.
    return !(/^[*?]/.test(text) && /[*?]$/.test(text))
  }

  return terms.some(([, idPart]) => {
    const literal = idPart.replace(GLOB_WILDCARDS, '').toLowerCase()
    return literal !== '' && /[^a-z0-9_-]/.test(literal)
  })
}
