/**
 * What a chat slot runs on: the model, effort-level and slash-command
 * catalogs, selection capabilities, and a slot's agent, model, reasoning
 * effort, auto-compact threshold, workspace, project and in-place reload.
 */

import { withDeadline } from '../../lib/withDeadline'
import type { ClientTransport } from './transport'

/** Deadline for `api.slashCommands`. Same value as the `$`-menu's
 *  `SKILLS_TIMEOUT_MS` (`./skills`), for the same reason and against the same
 *  gateway: both menus are composer affordances whose fetch blocks Enter while
 *  it is unsettled, so a divergent bound here would only be a second number to
 *  explain. Rationale in the CR description. */
export const SLASH_COMMANDS_TIMEOUT_MS = 15_000

export function createChatSlotSettingsEndpoints({ post, j, jfetch: fetch }: ClientTransport) {
  const selection = {
    models: () => fetch('/api/models').then(j),
    chatSlotSelectionCapabilities: (slot: string) =>
      fetch('/api/chat/slots/' + encodeURIComponent(slot) + '/selection-capabilities').then(j) as Promise<{
        known: boolean
        backend?: string
        effort_supported?: boolean
        effort_levels?: string[]
        model_effort_pair_ids?: boolean
      }>,
    effortLevels: (slot?: string) =>
      fetch('/api/effort-levels' + (slot ? '?slot=' + encodeURIComponent(slot) : '')).then(j) as Promise<string[]>,
    // Bounded HERE, not per initiator: react-query dedupes on the key, so the
    // weakest initiator would otherwise decide whether the promise is bounded.
    slashCommands: (signal?: AbortSignal) =>
      withDeadline(SLASH_COMMANDS_TIMEOUT_MS, signal, s =>
        fetch('/api/slash-commands', { signal: s }).then(j)),
    /** `kind` names the namespace the user picked from. Omitted, the backend
     *  keeps its legacy name-first resolution; stated, a same-name template and
     *  member are told apart and an unresolvable choice is refused (409) rather
     *  than answered by the default agent. `announce` asks the gateway to
     *  append the "Switched to agent" transcript line, as the typed `/agent`
     *  command does; the picker leaves it off. */
    chatSlotAgent: (slot: string, agent: string, kind?: 'member' | 'template', opts?: { announce?: boolean }) =>
      post('/api/chat/slots/' + encodeURIComponent(slot) + '/agent', {
        agent,
        ...(kind ? { agent_kind: kind } : {}),
        ...(opts?.announce ? { announce: true } : {}),
      }).then(j) as Promise<{ ok?: boolean; agent?: string; agent_kind?: 'member' | 'template' | ''; workspace?: string }>,
    chatSlotModel: (slot: string, model: string) =>
      post('/api/chat/slots/' + encodeURIComponent(slot) + '/model', { model }).then(j) as Promise<{ ok?: boolean; model?: string }>,
    /** This slot's auto-compact threshold override (null = follows the global). */
    chatSlotAutocompact: (slot: string) =>
      fetch('/api/chat/slots/' + encodeURIComponent(slot) + '/autocompact').then(j) as Promise<{ pct: number | null; global_pct: number; min: number; max: number }>,
    /** Set (number) or clear (null) this slot's auto-compact threshold override. */
    setChatSlotAutocompact: (slot: string, pct: number | null) =>
      post('/api/chat/slots/' + encodeURIComponent(slot) + '/autocompact', { pct }).then(j) as Promise<{ ok?: boolean; pct: number | null; global_pct: number }>,
    chatSlotsModel: (model: string, skip_running: boolean) =>
      post('/api/chat/slots/model', { model, skip_running }).then(j) as Promise<{ ok: boolean; model: string; switched: string[]; skipped_running: string[]; unchanged: string[]; failed: string[] }>,
    chatSlotReasoningEffort: (slot: string, reasoning_effort: string) =>
      post('/api/chat/slots/' + encodeURIComponent(slot) + '/reasoning-effort', { reasoning_effort }).then(j) as Promise<{ ok?: boolean; reasoning_effort?: string; model?: string; deferred?: boolean }>,
    chatSlotWorkspace: (slot: string, workspace: string) =>
      post('/api/chat/slots/' + encodeURIComponent(slot) + '/workspace', { workspace }).then(j),
    // Relaunch the slot's agent process in place (fresh agent spec, env, and MCP
    // servers; conversation preserved). 409 while a turn is in flight.
    chatSlotReload: (slot: string) =>
      post('/api/chat/slots/' + encodeURIComponent(slot) + '/reload', {}).then(j) as Promise<{ ok?: boolean; error?: string }>,
    chatSlotProject: (slot: string, project: string) =>
      post('/api/chat/slots/' + encodeURIComponent(slot) + '/project', { project }).then(j) as Promise<{ ok?: boolean; project?: string }>,
    /** A fresh conversation on the SAME slot: the next turn cold-starts instead
     *  of resuming the accumulated one. The slot stays open, its key is
     *  unchanged (channel linkage, schedules and tasks hang off it) and its
     *  transcript stays on disk — the model forgets, the record does not.
     *
     *  `replay: false` is sent EXPLICITLY. The route defaults it to `true`,
     *  which replays the discarded history straight back into the fresh
     *  context: omitting the flag would make the whole call a no-op from the
     *  user's point of view while still answering 200.
     *
     *  409 `turn_in_flight` while a turn is running on the slot or the session,
     *  or while sub-agents are still attached.
     *
     *  `boundary` reports the DURABLE record of where the fresh conversation
     *  begins, which the route waits for: `recorded` landed, `not_owed` is an
     *  ordinary chat slot with no member log, and `failed` means the reset
     *  happened but nothing wrote the line — so the discarded messages are still
     *  on screen with nothing marking them as forgotten, and the caller must say
     *  so rather than report a clean reset. */
    chatSlotResetConversation: (slot: string) =>
      post('/api/chat/slots/' + encodeURIComponent(slot) + '/reset-conversation', { replay: false }).then(j) as Promise<{ slot?: string; reset?: boolean; replay?: boolean; boundary?: 'recorded' | 'not_owed' | 'failed' }>,
  }

  return { selection }
}
