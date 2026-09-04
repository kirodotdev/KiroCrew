/**
 * toolDiff — detection for the inline diff-card promotion on tool-call rows.
 *
 * kiro-cli's edit tools carry a structured diff on the ACP tool_call event;
 * the backend renders it to a unified-diff string and stores it as the tool's
 * `input` (live toolLog entry and persisted message `meta.input` alike, see
 * `_tool_meta` in chat_runner.py). A row whose input IS such a diff renders a
 * first-class presentation in the transcript instead of hiding the change
 * inside the collapsed details panel — the structured counterpart of the
 * model-authored ```diff block, which the prompt no longer mandates for
 * tool-made edits on the dashboard (see rfc-tool-derived-diff-cards.md).
 *
 * Promotion is gated on the ACP tool kind being exactly "edit": a shell
 * command whose input happens to contain diff-shaped text (git apply, a
 * heredoc patch) must never promote, and `isDiffText` alone cannot tell those
 * apart. Kind-first ordering also keeps the check cheap — only edit rows pay
 * for the line scan.
 *
 * EVERY edit diff gets a visible trace. Small diffs render a DiffBlock card,
 * folded to its chip until the reader opens it (see ToolCallLine's
 * `openedDiffCards`); diffs over the size cap degrade to a summary chip
 * (filename, −N +M,
 * expands the details panel) rather than to nothing — under the relaxed
 * prompt the model no longer restates tool edits, so a silently-dropped card
 * would leave a large edit with zero transcript trace.
 */
import { isDiffText } from '../../utils/diffUtils'
import { countDiffStats } from '../../utils/diffLineCounts'
import { extractFilePath } from '../../components/DiffBlock'
import type { ChatMessage } from '../../types'

/**
 * Upper bound (in newline count) for a full inline card. A whole-file create
 * arrives as one giant all-additions diff; rendering thousands of rows inline
 * would dominate the transcript and stall the virtualizer's row measurement.
 * Beyond the cap the row shows the summary chip instead.
 */
const DIFF_CARD_MAX_LINES = 400

/** How an edit-tool row presents its diff in the transcript. */
export type ToolDiffView =
  | { mode: 'card'; code: string }
  | { mode: 'summary'; path: string | null; added: number; removed: number; truncated: boolean }

/**
 * The transport's truncation annotation (see DIFF_TRUNCATION_MARK in
 * acp/_dispatch.py). Diff renderers skip `\`-lines by convention, so without
 * an explicit check a cut diff looks complete and its counts silently
 * understate the change. A regex, not a string constant: this is a WIRE
 * pattern matched against backend output, never user-visible copy — the i18n
 * gate rightly flags English string literals in this file, and a pattern
 * literal states the protocol role precisely.
 */
const TRUNCATION_RE = /\n\\ diff truncated$/

/**
 * Classify a tool row's diff presentation, or null when the row is not an
 * edit-tool diff (wrong kind, no input, not a diff). `kind`/`input` come from
 * the live toolLog entry when one backs the row, else from the persisted
 * message meta — both carry the same values.
 */
export function presentToolDiff(
  kind: string | undefined,
  input: string | undefined,
): ToolDiffView | null {
  if (kind !== 'edit' || !input) return null
  // Bare-JSON edit payloads are covered UPSTREAM: acp/_dispatch.py's
  // derive_edit_diff turns strReplace / create / insert args into a unified
  // diff before the input reaches this module. A non-diff input here is a
  // genuinely unrecognizable shape; its fold-proof trace is FileChangeChips
  // (the file_changes snapshot on the assistant message), not this module.
  if (!isDiffText(input)) return null
  // A transport-truncated diff must never render as a complete-looking card:
  // it can be under the card line cap (64 KiB of long lines) while missing
  // most of the change. Always the summary chip, flagged so the chip shows a
  // visible truncation note and the counts read as lower bounds.
  const truncated = TRUNCATION_RE.test(input)
  let newlines = 0
  for (let i = 0; i < input.length; i++) {
    if (input.charCodeAt(i) === 10) newlines++
  }
  if (!truncated && newlines <= DIFF_CARD_MAX_LINES) return { mode: 'card', code: input }
  const { added, removed } = countDiffStats(input)
  return { mode: 'summary', path: extractFilePath(input)?.path ?? null, added, removed, truncated }
}

/**
 * Store-free predicate for grouping logic (TurnBlock): does this persisted
 * tool message carry an edit diff (card OR summary)? Reads only the message
 * itself, so a store-less host (app-sdk ChatMessageList) can call it. Rows
 * persisted before `meta.kind` existed simply never promote — fail-safe to
 * the collapsed-details rendering.
 */
export function isDiffToolMessage(m: ChatMessage): boolean {
  if (m.role !== 'tool' || !m.content?.startsWith('🔧')) return false
  const meta = m.meta as Record<string, unknown> | undefined
  return presentToolDiff(
    typeof meta?.kind === 'string' ? meta.kind : undefined,
    typeof meta?.input === 'string' ? meta.input : undefined,
  ) != null
}
