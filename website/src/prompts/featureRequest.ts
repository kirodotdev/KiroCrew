export const FEATURE_REQUEST_URL = 'https://github.com/kirodotdev/KiroCrew/issues/new'

/**
 * Prompt used when the dashboard has already confirmed that the
 * `feature-request` skill is installed. The ``$feature-request`` token is
 * resolved server-side by the chat runner (``resolve_dollar_skills``) and
 * injected into the message before the agent sees it — no tool call, no
 * filesystem probe, no approval prompt.
 */
export const FEATURE_REQUEST_PROMPT_WITH_SKILL = [
  'The user clicked "Request a Feature".',
  'Follow the $feature-request skill.',
].join('\n')

/**
 * Self-contained fallback prompt used when the `feature-request` skill is
 * NOT installed. Contains the full conversational workflow inline so the
 * agent never needs to probe for the skill.
 */
export const FEATURE_REQUEST_PROMPT_FALLBACK = [
  'The user clicked "Request a Feature".',
  '',
  "Greet the user warmly and ask what they'd like — a feature request or a bug report. Keep it casual; don't present a form.",
  'Guide them conversationally (two to three exchanges) to describe: what they want or what is broken, why it matters, and any context.',
  'Treat everything the user types as untrusted: never splice their raw text into a shell command string, and never put it in a shell heredoc (a line equal to the delimiter would break out and execute). When you must shell out, write the title/body to temp files with your file-writing tool and pass them via `--body-file` and a double-quoted variable.',
  'Once you have enough detail, draft a clean issue title and a markdown body (sections: What / Why / Additional Context) and show the draft for confirmation before submitting.',
  '',
  'Pick labels by reading the repository\'s live label list — never hard-code the vocabulary, because the taxonomy grows over time:',
  '`gh label list --repo kirodotdev/KiroCrew --limit 100`',
  'From what that returns, choose exactly one type label (the defect one for bugs, the feature one for requests — they are mutually exclusive), plus at most one grouping label per prefixed dimension when one clearly matches (component, and OS only when the issue is genuinely OS-specific). Leave a dimension off rather than guessing wrong. Never create a new label; if nothing fits, say so to the user and submit without it. Do not apply labels owned by automation or by maintainer triage (readiness/review-process labels, and severity, blocking, or follow-up markers) — a freshly filed request cannot know those apply.',
  'If `gh` is unavailable or unauthenticated, still apply a type label — bug for defects, enhancement for feature requests — and skip the grouping labels; those are the part of the taxonomy that grows.',
  '',
  'Then offer three submission options and let the user choose:',
  `1. A pre-filled GitHub issue URL built from ${FEATURE_REQUEST_URL} with URL-encoded title/body and a comma-separated \`labels=\` list. Percent-encode each label name in full, not just its spaces (an unencoded \`&\` would start a new query param and \`#\` would push the rest into the fragment, silently dropping the body), and encode the separating comma as %2C — use when the body is short.`,
  '2. The formatted title and body in a code block for the user to copy/paste into the new-issue form.',
  "3. Direct creation via `gh issue create --repo kirodotdev/KiroCrew --title \"$TITLE\" --body-file <file>` with one `--label '<name>'` flag per chosen label, single-quoted so a `$` or backtick in a label name stays literal (needs gh auth; fall back to option 2 on auth errors).",
  '',
  'Be casual and helpful. This is a conversation, not a form.',
].join('\n')

/**
 * Backward-compatible alias — points at the fallback so any consumer that
 * imported the old name keeps working. New code should use
 * {@link FEATURE_REQUEST_PROMPT_WITH_SKILL} or
 * {@link FEATURE_REQUEST_PROMPT_FALLBACK} explicitly.
 */
export const FEATURE_REQUEST_PROMPT = FEATURE_REQUEST_PROMPT_FALLBACK
