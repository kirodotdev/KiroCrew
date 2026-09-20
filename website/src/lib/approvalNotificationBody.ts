import { i18nT } from '../i18n/t'

// The markdown pieces, named rather than inlined into a template literal. The
// i18n gate validates template literals at `mode: 'all'`, which reports a
// template with ANY static text — whitespace included — so a punctuation-only
// quasi cannot be spelled inline. Held as constants, each literal is inspected
// on its content (the strict rule in `eslint-rules/i18n-strict.js` makes the
// ALL-CAPS name irrelevant) and passes as punctuation rather than copy.
const BOLD = '**'
const NEWLINE = '\n'
const BLANK_LINE = '\n\n'
/** Fallback when an approval arrives without a source system. Not copy. */
const DEFAULT_SOURCE = 'agent'
/** Fence info string for an approval's command. Shared with `CodeBlock`, which
 *  soft-wraps this tag, so a producer and the renderer cannot drift apart. */
export const APPROVAL_COMMAND_TAG = 'approval-command'

/**
 * Build the markdown body shared by live and reconciled approval notifications.
 *
 * The command is emitted as FENCED CODE, which is what keeps it honest: this
 * body reaches three plain-text-ish surfaces (the OS banner, the feed excerpt,
 * the detail panel), and `tool_input` is raw shell text whose `*`, `_`, `~` and
 * backticks would otherwise read as markdown — displaying `rm -rf *cache*` as
 * `rm -rf cache`, a narrower command than the one being authorized. Inside a
 * fence there is nothing to guess: `stripMd` keeps the contents verbatim.
 */
export function approvalNotificationBody(source?: string, toolInput?: string, purpose?: string): string {
  let command = ''
  if (toolInput) {
    // Longer than any backtick run in the command, so it cannot close its own
    // fence (CommonMark); `stripMd` closes on the opening run's length to match.
    const longestRun = (toolInput.match(/`+/g) || []).reduce((longest, run) => Math.max(longest, run.length), 0)
    const fence = '`'.repeat(Math.max(3, longestRun + 1))
    // The info string is the dashboard's own tag (like `error-report`):
    // CodeBlock soft-wraps it, so a long command line is entirely on screen
    // next to the control that authorizes it instead of scrolling off the edge.
    command = [fence + APPROVAL_COMMAND_TAG, toolInput, fence].join(NEWLINE)
  }
  // The detail panel's own `source` key: one word, already present in every
  // catalog. The body is the ONLY place that names the requesting system --
  // the panel's metadata row prints the note's kind under a "Kind" label, so
  // the two labels never read as one field.
  const label = i18nT('components.notifications.notificationDetailPanel.source')
  const head = BOLD + label + BOLD + ' ' + (source || DEFAULT_SOURCE)
  return [head, command, purpose || ''].join(BLANK_LINE).trim()
}
