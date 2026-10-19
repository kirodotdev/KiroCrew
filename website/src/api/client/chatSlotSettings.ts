/**
 * What a chat slot runs on: the model, effort-level and slash-command
 * catalogs, selection capabilities, and a slot's agent, model, reasoning
 * effort, auto-compact threshold, workspace, project and in-place reload.
 */

import { withDeadline } from '../../lib/withDeadline'
import type { ClientTransport } from './transport'
import type {
  RemoteDirListing,
  RemoteFileContent,
  SessionEnvironmentsCheck,
  SessionEnvironmentWorkspace,
  SlotEnvironment,
} from '../client'

/** Deadline for `api.slashCommands`. Same value as the `$`-menu's
 *  `SKILLS_TIMEOUT_MS` (`./skills`), for the same reason and against the same
 *  gateway: both menus are composer affordances whose fetch blocks Enter while
 *  it is unsettled, so a divergent bound here would only be a second number to
 *  explain. Rationale in the CR description. */
export const SLASH_COMMANDS_TIMEOUT_MS = 15_000

/** A session's turn time limit as `/api/chat/slots/{slot}/turn-timeout` reports it. */
export interface SlotTurnTimeout {
  secs: number | null
  global_secs: number
  min: number
  max: number
}

/** What `GET /api/chat/slots/follow-default` answers. */
export interface FollowDefaultCount {
  count: number
  digest: string
  default_model: string
  models: { model: string; count: number }[]
  titles: string[]
}

export function createChatSlotSettingsEndpoints({ get, post, put, j, jfetch: fetch }: ClientTransport) {
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
    /** `deferred: true` when a response was running: the pick is committed and
     *  applies from the next message; the running reply keeps its model. */
    chatSlotModel: (slot: string, model: string) =>
      post('/api/chat/slots/' + encodeURIComponent(slot) + '/model', { model }).then(j) as Promise<{ ok?: boolean; model?: string; deferred?: boolean }>,
    /** This slot's auto-compact threshold override (null = follows a default:
     *  `agent_pct` when the session's agent declares one, else `global_pct`). */
    chatSlotAutocompact: (slot: string) =>
      fetch('/api/chat/slots/' + encodeURIComponent(slot) + '/autocompact').then(j) as Promise<{ pct: number | null; global_pct: number; agent_pct?: number | null; agent?: string | null; min: number; max: number }>,
    /** Set (number) or clear (null) this slot's auto-compact threshold override. */
    setChatSlotAutocompact: (slot: string, pct: number | null) =>
      post('/api/chat/slots/' + encodeURIComponent(slot) + '/autocompact', { pct }).then(j) as Promise<{ ok?: boolean; pct: number | null; global_pct: number; agent_pct?: number | null; agent?: string | null }>,
    /** This slot's own turn time limit in seconds (null = follows the global
     *  `agent.chat_turn_timeout_secs`, reported as `global_secs`). */
    chatSlotTurnTimeout: (slot: string) =>
      fetch('/api/chat/slots/' + encodeURIComponent(slot) + '/turn-timeout').then(j) as Promise<SlotTurnTimeout>,
    /** Give this slot its own turn time limit (whole seconds), or null to follow the global. */
    setChatSlotTurnTimeout: (slot: string, secs: number | null) =>
      post('/api/chat/slots/' + encodeURIComponent(slot) + '/turn-timeout', { secs }).then(j) as Promise<SlotTurnTimeout & { ok?: boolean }>,
    chatSlotsModel: (model: string, skip_running: boolean) =>
      post('/api/chat/slots/model', { model, skip_running }).then(j) as Promise<{ ok: boolean; model: string; switched: string[]; skipped_running: string[]; unchanged: string[]; failed: string[] }>,
    /** How many chats a person started keep a model of their own, and a digest of
     *  exactly that set for `chatSlotsFollowDefault` to confirm. Changes nothing.
     *  Also what the confirm names: the saved Default Model (`''` on Auto), how
     *  many counted chats keep each model (most first), and the redacted titles of
     *  the first five counted chats (`''` when untitled). */
    chatSlotsFollowDefaultCount: () =>
      fetch('/api/chat/slots/follow-default').then(j) as Promise<FollowDefaultCount>,
    /** Clear the kept model on the chats the count's `digest` names, so each
     *  follows the default again. 409 `chats_changed` (nothing changed) when that
     *  set has moved since the count. */
    chatSlotsFollowDefault: (digest: string) =>
      post('/api/chat/slots/follow-default', { digest }).then(j) as Promise<{ ok: boolean; changed: number }>,
    chatSlotReasoningEffort: (slot: string, reasoning_effort: string) =>
      post('/api/chat/slots/' + encodeURIComponent(slot) + '/reasoning-effort', { reasoning_effort }).then(j) as Promise<{ ok?: boolean; reasoning_effort?: string; model?: string; deferred?: boolean }>,
    /** Which Session Environment this slot's session runs on (`profile: null` =
     *  this machine) plus the configured profiles to choose from. Owner-only. */
    chatSlotEnvironment: (slot: string) =>
      get('/api/chat/slots/' + encodeURIComponent(slot) + '/environment').then(j) as Promise<SlotEnvironment>,
    /** Read-only view of a REMOTE session's workspace: list a directory or read a
     *  file, relative to the profile's working directory. Owner-only. */
    chatSlotEnvironmentList: (slot: string, path: string) =>
      get('/api/chat/slots/' + encodeURIComponent(slot) + '/environment/browse?op=list&path=' + encodeURIComponent(path)).then(j) as Promise<RemoteDirListing>,
    chatSlotEnvironmentRead: (slot: string, path: string) =>
      get('/api/chat/slots/' + encodeURIComponent(slot) + '/environment/browse?op=read&path=' + encodeURIComponent(path)).then(j) as Promise<RemoteFileContent>,
    /** Read-only readiness check: Coder sign-in and each profile's template. */
    sessionEnvironmentsCheck: () =>
      post('/api/session-environments/check').then(j) as Promise<SessionEnvironmentsCheck>,
    sessionEnvironmentsWorkspaces: () =>
      get('/api/session-environments/workspaces').then(j) as Promise<{ workspaces: SessionEnvironmentWorkspace[] }>,
    /** Templates the saved Coder deployment offers; `error` when unreadable. Owner-only. */
    sessionEnvironmentsTemplates: () =>
      get('/api/session-environments/templates').then(j) as Promise<{ templates: string[]; error?: string }>,
    /** Save a remote file. `expected` is the text the editor opened; a file that
     *  changed since answers 409 `file_changed` instead of being overwritten.
     *  `workdir` is the profile's working folder the edit was made under; once the
     *  profile points elsewhere the save answers 409 `root_changed`. */
    chatSlotEnvironmentWrite: (slot: string, path: string, content: string, expected?: string, workdir?: string) =>
      put('/api/chat/slots/' + encodeURIComponent(slot) + '/environment/file', { path, content, ...(expected !== undefined ? { expected } : {}), ...(workdir !== undefined ? { workdir } : {}) }).then(j) as Promise<{ ok: boolean; path: string }>,
    /** Bind (or, with `null`, unbind) the slot's session to a profile. Applies at
     *  the session's next start; an idle runtime is reset so it cannot linger. */
    setChatSlotEnvironment: (slot: string, profile: string | null) =>
      put('/api/chat/slots/' + encodeURIComponent(slot) + '/environment', { profile }).then(j) as Promise<{ profile: string | null; applies: string }>,
    chatSlotWorkspace: (slot: string, workspace: string) =>
      post('/api/chat/slots/' + encodeURIComponent(slot) + '/workspace', { workspace }).then(j),
    // Relaunch the slot's agent process in place (fresh agent spec, env, and MCP
    // servers; conversation preserved). 409 while a turn is in flight.
    chatSlotReload: (slot: string) =>
      post('/api/chat/slots/' + encodeURIComponent(slot) + '/reload', {}).then(j) as Promise<{ ok?: boolean; error?: string }>,
    chatSlotProject: (slot: string, project: string) =>
      post('/api/chat/slots/' + encodeURIComponent(slot) + '/project', { project }).then(j) as Promise<{ ok?: boolean; project?: string }>,
  }

  return { selection }
}
