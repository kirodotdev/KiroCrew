/**
 * Run detection + summary for the per-run tool grouping (DFL-71 phase 1).
 *
 * A `run` is a contiguous sequence of ordinary tool-role singles in a turn —
 * the SAME contiguous run TurnBlock's splitSegments already derives. Pure
 * functions only, no hooks, so TurnBlock (and tests) can call them from render
 * and from grouping logic without a store.
 *
 * The header verb is the run's DOMINANT action kind, classified with the SAME
 * rules a single row uses (`classifyToolCall`), so a run of reads reads
 * `Explored …` and a run of greps reads `Searched …` — never a second spelling
 * of the per-row verbs. Dominance is the kind's frequency among the
 * classifiable calls; a run nothing classifies falls back to `Worked through`.
 */

import { i18nT } from '../../i18n/t'
import { classifyToolCall } from '../../utils/toolCallTitle'
import type { ToolCallTitleInput } from '../../utils/toolCallTitle'
import type { ChatMessage } from '../../types'

/** ToolAction kinds that read as EXPLORATION when they dominate a run. */
const READISH_KINDS = new Set(['read', 'list_files', 'view_image'])
/** Kinds that read as SEARCH. */
const SEARCHISH_KINDS = new Set(['search', 'find_files', 'search_web'])
/** Shell-derived kinds. A shell call the classifier parsed reports one of the
 *  action kinds above (a `cat` is a read); these are the run-level verbs for
 *  the shell shapes nothing refines further. */
const GITISH_KINDS = new Set(['git', 'github', 'github_api'])
const BUILDISH_KINDS = new Set(['install', 'test', 'script', 'build', 'lint'])
const PRINTISH_KINDS = new Set(['print'])

/** The dominant VERB the header renders, in the current locale. */
export function dominantVerbLabel(kindCounts: Map<string, number>): string {
  let best = ''
  let bestN = 0
  for (const [kind, n] of kindCounts) {
    if (n > bestN) { best = kind; bestN = n }
  }
  if (READISH_KINDS.has(best)) return i18nT('pages.chat.toolRunGroup.verb_explored')
  if (SEARCHISH_KINDS.has(best)) return i18nT('pages.chat.toolRunGroup.verb_searched')
  if (best === 'edit' || best === 'create') return i18nT('pages.chat.toolRunGroup.verb_edited')
  if (best === 'execute') return i18nT('pages.chat.toolRunGroup.verb_ran')
  if (best === 'fetch') return i18nT('pages.chat.toolRunGroup.verb_fetched')
  if (best === 'mcp') return i18nT('pages.chat.toolRunGroup.verb_used_mcp')
  if (GITISH_KINDS.has(best)) return i18nT('pages.chat.toolRunGroup.verb_gitted')
  if (BUILDISH_KINDS.has(best)) return i18nT('pages.chat.toolRunGroup.verb_built')
  if (PRINTISH_KINDS.has(best)) return i18nT('pages.chat.toolRunGroup.verb_printed')
  return i18nT('pages.chat.toolRunGroup.verb_worked')
}

/**
 * One call's classification input, read the way ToolCallLine reads it: the
 * 🔧-stripped content as the transport title, the persisted meta as the rest.
 */
function titleInputOf(m: ChatMessage): ToolCallTitleInput {
  const meta = (m.meta ?? {}) as Record<string, unknown>
  const str = (v: unknown) => (typeof v === 'string' ? v : '')
  return {
    title: m.content.replace(/^🔧\s*/, ''),
    kind: str(meta.kind),
    rawInput: meta.input,
    isShell: meta.is_shell === true || meta.is_shell === '1',
    toolName: str(meta.tool_name),
    mcpServer: str(meta.mcp_server),
  }
}

/**
 * Summarize one contiguous run of tool messages.
 *
 * `count` is the number of 🔧 request rows (the same count the turn toggle
 * reports — completion rows never enter a run; TurnBlock filters them);
 * `duration` is deliberately `null` — ToolActivity carries no duration field
 * today, so the header shows verb + count only. The field is kept so phase 2
 * can light it up without this summary's shape changing.
 */
export function summarizeToolRun(messages: ChatMessage[]): ToolRunSummary {
  const kindCounts = new Map<string, number>()
  for (const m of messages) {
    const c = classifyToolCall(titleInputOf(m))
    const kind = c ? c.kind : 'unknown'
    kindCounts.set(kind, (kindCounts.get(kind) ?? 0) + 1)
  }
  return {
    title: dominantVerbLabel(kindCounts),
    count: messages.length,
    duration: null,
  }
}

export interface ToolRunSummary {
  /** The dominant-verb header label, in the current locale. */
  title: string
  /** Number of distinct calls in the run (the 🔧 request rows). */
  count: number
  /** Total run duration. Always `null` in phase 1 — ToolActivity has no
   *  duration; phase 2 fills it from the per-row log entries. */
  duration: string | null
}
