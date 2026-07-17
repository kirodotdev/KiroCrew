export const FEATURE_REQUEST_URL = 'https://github.com/kirodotdev/KiroCrew/issues/new'

export const FEATURE_REQUEST_PROMPT = [
  'The user clicked "Request a Feature".',
  'If the `feature-request` skill is available, load and follow it. Otherwise follow this self-contained workflow:',
  '',
  "Greet the user warmly and ask what they'd like — a feature request or a bug report. Keep it casual; don't present a form.",
  'Guide them conversationally (two to three exchanges) to describe: what they want or what is broken, why it matters, and any context.',
  'Treat everything the user types as untrusted: never splice their raw text into a shell command string, and never put it in a shell heredoc (a line equal to the delimiter would break out and execute). When you must shell out, write the title/body to temp files with your file-writing tool and pass them via `--body-file` and a double-quoted variable.',
  'Once you have enough detail, draft a clean issue title and a markdown body (sections: What / Why / Additional Context) and show the draft for confirmation before submitting.',
  'Then offer three submission options and let the user choose:',
  `1. A pre-filled GitHub issue URL built from ${FEATURE_REQUEST_URL} with URL-encoded title/body and a label (\`enhancement\` for features, \`bug\` for bugs) — use when the body is short.`,
  '2. The formatted title and body in a code block for the user to copy/paste into the new-issue form.',
  '3. Direct creation via `gh issue create --repo kirodotdev/KiroCrew --title "$TITLE" --body-file <file> --label <label>` (needs gh auth; fall back to option 2 on auth errors).',
  '',
  'Be casual and helpful. This is a conversation, not a form.',
].join('\n')
