/**
 * SubagentRunCard — a persistent, clickable inline card rendered in the chat
 * message flow for a `spawn_run` tool call. Mirrors WorkflowRunCard: the
 * transient SubagentProgressBar above the composer drops as soon as a wave
 * ends (and only ever covers the slot you are viewing), so nothing in
 * scrollback records that a wave was launched. This card stays anchored to the
 * invocation that launched it.
 *
 * It subscribes to the same Redux slice the chip uses (`chat.subagents` /
 * `chat.subagentQueued`, folded from `subagent_spawn`/`tool`/`done`/`queued` WS
 * frames) for live per-agent status, and counts QUEUED agents explicitly — a
 * wave sitting behind the concurrency cap has no per-agent entry yet, which is
 * exactly the window in which the UI would otherwise look empty. Clicking opens
 * the Subagents side panel.
 */
import { memo } from 'react'
import { Bot, Loader2, CheckCircle2, AlertCircle, Clock, Square } from 'lucide-react'
import { PanelRightSolid } from '../../components/icons/panels'
import { useAppSelector, useAppDispatch } from '../../store'
import { openActivityToTab, selectSubagent } from '../../store/chatSlice'
import { sanitizeLlmOutput } from '../../utils/sanitize'
import type { ChatMessage, SubagentActivity } from '../../types'
import { SPAWN_LAUNCH_MARKER } from './types'

import { i18nT } from '../../i18n/t'
/** The `spawn_run` tool result opens with "Spawned N subagent(s)." followed by
 *  one indented "  <id> (<agent>): <task>" line per accepted agent (see the
 *  spawn_run handler in mcp_core.py). Matching the header identifies the call
 *  as a launch; the per-agent lines carry the ids. Both live in the persisted
 *  `meta.output`, so historical messages render the card too. */
const SPAWN_HEADER_RE = /^Spawned (\d+) subagent\(s\)\./m
/** Agent ids are hex digests from SubagentManager; the agent name is optional
 *  (spawn_run omits the parenthetical when no agent was pinned). Non-hex ids are
 *  skipped, which is what excludes the `q<n>` queue sentinels in scrollback
 *  persisted before SubagentManager pre-assigned queued members their real id. */
const SPAWN_AGENT_LINE_RE = /^ {2}([0-9a-f]{4,32})(?: \(([^)]*)\))?: /gm

export interface SpawnRunLaunch {
  /** Agent ids the gateway accepted. May be shorter than `announced` when some
   *  tasks failed to start. */
  ids: string[]
  /** Count from the "Spawned N subagent(s)." header. */
  announced: number
}

/**
 * Reduce a persisted `meta.output` to the plain text the launch markers live in.
 *
 * Tool output arrives in two shapes. Native/ACP tools persist bare text. Tools
 * served over MCP — `spawn_run` among them — persist the MCP result envelope,
 * `{"content":[{"type":"text","text":"…"}]}`, where the launch header sits
 * mid-line after the JSON preamble and the per-agent lines are escaped `\n`
 * rather than real newlines. Both anchored patterns below therefore fail
 * against the envelope. Concatenating the envelope's text parts restores the
 * shape the patterns were written for.
 */
function toolOutputText(output: string): string {
  const trimmed = output.trimStart()
  if (!trimmed.startsWith('{') && !trimmed.startsWith('[')) return output
  try {
    const parsed: unknown = JSON.parse(trimmed)
    const content = (parsed as { content?: unknown } | null)?.content
    if (!Array.isArray(content)) return output
    const parts: string[] = []
    for (const part of content) {
      const text = (part as { text?: unknown } | null)?.text
      if (typeof text === 'string') parts.push(text)
    }
    return parts.length ? parts.join('\n') : output
  } catch {
    // Not JSON after all (or truncated by the server's size cap) — fall back to
    // scanning the raw string rather than dropping the launch record.
    return output
  }
}

/** Per-message parse cache. `extractSpawnRunLaunch` is called from the render
 *  dispatch AND from TurnBlock's grouping logic, so it re-runs on every chunk
 *  of a streaming turn; `meta.output` can reach the server's 1 MB cap, and
 *  JSON.parse of that on each frame is real jank. Keyed by the message object
 *  and invalidated by output identity, so the growing-output case still
 *  re-parses exactly once per new value. */
const launchCache = new WeakMap<ChatMessage, { output: string; launch: SpawnRunLaunch | null }>()

/**
 * Extract the accepted subagent ids from a tool message's persisted output, or
 * null when the message is not a `spawn_run` launch. Pure — no hooks — so it is
 * safe to call from the render dispatch and from TurnBlock's grouping logic.
 */
export function extractSpawnRunLaunch(message: ChatMessage): SpawnRunLaunch | null {
  const output = (message.meta?.output as string | undefined) || ''
  if (!output) return null
  // Cheap reject first: skips the JSON work for every ordinary tool message.
  if (!output.includes(SPAWN_LAUNCH_MARKER)) return null
  const cached = launchCache.get(message)
  if (cached && cached.output === output) return cached.launch
  const text = toolOutputText(output)
  const launch = parseSpawnRunLaunch(text)
  // The cached object is handed to every consumer (ChatPage's render dispatch,
  // TurnBlock's grouping). All of them only read, but an in-place `sort`/`push`
  // added later would poison every subsequent render from this cache entry.
  if (launch) Object.freeze(launch.ids)
  launchCache.set(message, { output, launch })
  return launch
}

/** Pure parse of the already-unwrapped launch text. */
function parseSpawnRunLaunch(text: string): SpawnRunLaunch | null {
  const header = SPAWN_HEADER_RE.exec(text)
  if (!header) return null
  const ids: string[] = []
  // Fresh lastIndex per call: the /g regex is module-scoped and stateful.
  SPAWN_AGENT_LINE_RE.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = SPAWN_AGENT_LINE_RE.exec(text)) !== null) ids.push(m[1])
  const announced = Number(header[1]) || 0
  // A header with no parseable agent lines still means a launch happened —
  // render the card in its neutral state rather than dropping the record.
  return { ids, announced }
}

/** True when a chat message is a `spawn_run` launch that should render as the
 *  inline card (and therefore must NOT be folded into TurnBlock's collapsible
 *  tool-call group). */
export function isSpawnRunTool(message: ChatMessage): boolean {
  return message.role === 'tool' && extractSpawnRunLaunch(message) !== null
}

const EMPTY_SUBAGENTS: Record<string, SubagentActivity> = {}

/** Terminal statuses, tallied across the launch's own ids only. */
function tally(agents: (SubagentActivity | undefined)[]) {
  let running = 0, done = 0, failed = 0, stopped = 0, unknown = 0
  for (const a of agents) {
    if (!a) { unknown++; continue }
    if (a.status === 'running' || a.status === 'tool' || a.status === 'pending') running++
    else if (a.status === 'done') done++
    else if (a.status === 'error') failed++
    else if (a.status === 'stopped') stopped++
    else unknown++
  }
  return { running, done, failed, stopped, unknown }
}

const SubagentRunCard = memo(function SubagentRunCard({
  launch,
  slot,
}: {
  launch: SpawnRunLaunch
  slot: string
}) {
  const dispatch = useAppDispatch()
  const subagents = useAppSelector(s =>
    slot === s.chat.activeSlot ? s.chat.subagents : s.chat.slotActivity[slot]?.subagents ?? EMPTY_SUBAGENTS,
  )
  // Agents accepted but not yet started (behind the concurrency cap / stagger
  // gate) have no per-agent entry — without this the card reads as idle during
  // the exact window the user is most likely to be looking at it.
  const queued = useAppSelector(s => s.chat.subagentQueued?.[slot] ?? 0)

  const mine = launch.ids.map(id => subagents[id])
  const counts = tally(mine)
  // `unknown` = ids the live slice no longer holds (history reload, or dismissed
  // from the panel). Treat them as neither running nor terminal.
  //
  // The header count is authoritative for the wave size, not `ids.length`:
  // spawn_run lists one line per accepted agent, and in LEGACY scrollback the
  // members that were queued behind the concurrency cap / spawn stagger are
  // listed under a sentinel id (`q1`, `q2`) that the hex-id pattern does not
  // match. Taking the total from `ids` would make a 2-agent wave read "1 agent".
  // Tasks that failed to start are reported in a separate section and never
  // reach the header, so this does not over-count them.
  const total = launch.announced || launch.ids.length
  const settled = counts.done + counts.failed + counts.stopped
  // True only when every announced member is observable through `launch.ids`.
  // Waves launched by a current backend always are: SubagentManager pre-assigns
  // each queued member's real id at accept time and the drained spawn starts
  // under it, so every announced member appears in the launch text. Two cases
  // still fall short and must not be claimed on — messages persisted by an
  // older backend (queued members recorded as `q1`/`q2`, with the agent that
  // ran carrying an id found nowhere in the text), and ids the live slice has
  // since dropped (history reload, "Dismiss done").
  const fullyObservable = launch.ids.length >= total && counts.unknown === 0

  const label = counts.running > 0
    ? `${counts.running} agent${counts.running === 1 ? '' : 's'} running`
    // `chat.subagentQueued` is keyed by SLOT, not by launch, so the queued
    // branch must sit BELOW settled: otherwise a second wave queueing behind
    // the cap makes this (already finished) card report the other wave's queue.
    : settled > 0
      // Only claim the WAVE finished when the whole wave is observable.
      // Otherwise state the launch, which is a fact from the header, and let
      // the status chips report the members actually observed — a ratio like
      // "1 of 3 agents finished" would pin a permanently false statement in
      // scrollback, since the unobservable members can never be tallied.
      ? settled >= total && fullyObservable
        ? `${total} agent${total === 1 ? '' : 's'} finished`
        : `${total} agent${total === 1 ? '' : 's'} launched`
      : queued > 0
        // Whole wave still behind the cap: "0 agents running" is technically
        // true and useless — name what is actually happening.
        ? `${queued} agent${queued === 1 ? '' : 's'} queued`
        : `${total} agent${total === 1 ? '' : 's'}`

  const open = () => {
    // Deep-link to the first agent of THIS wave so the panel lands on the
    // transcript the card refers to, not whatever was last selected.
    const first = launch.ids.find(id => subagents[id])
    if (first) dispatch(selectSubagent(first))
    dispatch(openActivityToTab('subagents'))
  }

  const idPreview = sanitizeLlmOutput(launch.ids.slice(0, 4).join(' · '))

  return (
    <div className="px-5 mx-auto w-full py-0.5" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      <button
        type="button"
        onClick={open}
        title={i18nT('pages.chat.subagentRunCard.open_in_the_subagents_panel')}
        data-testid="subagent-run-card"
        className="pi-morph group w-full text-left rounded-md bg-accent/10 border border-accent/20 hover:bg-accent/15 hover:border-accent/40 transition-colors px-3 py-2 flex items-start gap-2"
      >
        <span className="shrink-0 mt-0.5">
          {counts.running > 0
            ? <Loader2 size={15} className="text-accent animate-spin" />
            : counts.failed > 0
              ? <AlertCircle size={15} className="text-danger" />
              : settled > 0
                ? <CheckCircle2 size={15} className="text-green-500" />
                : queued > 0
                  ? <Clock size={15} className="text-muted" />
                  : <Bot size={15} className="text-accent/70" />}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 flex-wrap">
            <Bot size={12} className="text-accent/70 shrink-0" aria-hidden />
            <span className="truncate text-[13px] font-medium text-text-strong">{label}</span>
            {queued > 0 && settled === 0 && (
              <span
                className="shrink-0 inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded bg-muted/15 border border-border text-muted"
                data-testid="subagent-card-queued"
                title={i18nT('pages.chat.subagentRunCard.waiting_to_start_queued_behind_the_concurrency_l')}
              >
                <Clock size={10} aria-hidden /> {queued} {i18nT('pages.chat.subagentRunCard.waiting')}
              </span>
            )}
            {counts.done > 0 && (
              <span className="shrink-0 inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded bg-ok-subtle border border-ok/20 text-ok">
                <CheckCircle2 size={10} aria-hidden /> {counts.done}
              </span>
            )}
            {counts.failed > 0 && (
              <span className="shrink-0 inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded bg-danger-subtle border border-danger/20 text-danger">
                <AlertCircle size={10} aria-hidden /> {counts.failed}
              </span>
            )}
            {counts.stopped > 0 && (
              <span className="shrink-0 inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded bg-muted/15 border border-border text-muted">
                <Square size={10} aria-hidden /> {counts.stopped}
              </span>
            )}
          </div>
          <div className="text-[10px] text-muted font-mono truncate mt-0.5">
            {idPreview ? `${idPreview}${launch.ids.length > 4 ? ` +${launch.ids.length - 4}` : ''} · ` : ''}
            {i18nT('pages.chat.subagentRunCard.open_subagents_panel')}
          </div>
        </div>
        <PanelRightSolid
          size={14}
          className="text-muted shrink-0 mt-0.5 opacity-60 group-hover:opacity-100 transition-opacity"
        />
      </button>
    </div>
  )
})

export default SubagentRunCard
