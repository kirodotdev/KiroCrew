import { i18nT } from '../i18n/t'

/** The handle fields a roster row carries; the subset every surface that
 *  resolves a stored agent value needs. `KiroCrewAgent` and `MemberRosterRow`
 *  both satisfy it. */
export interface AgentHandleRow {
  /** The crew's display name (alias of `display_name` for one release). */
  name: string
  /** The `config.agents` key: the crew's immutable identity. Optional: older
   *  payloads and project-scope rows predate or lack the field. */
  member_id?: string
  /** The crew's display name. Optional: older payloads predate the field. */
  display_name?: string
}

/** The label a roster surface shows for a crew: its display name when one is
 *  set, otherwise its name. One helper rather than `a.display_name || a.name`
 *  at each site, so every surface applies the same fallback (and the same
 *  trim — a stored label arrives trimmed, but an in-flight edit draft may
 *  not be). */
export function crewDisplayName(a: Pick<AgentHandleRow, 'name' | 'display_name'>): string {
  return a.display_name?.trim() || a.name
}

/** The value a picker STORES for a crew: its identity (`member_id`), never its
 *  label, so a rename cannot strand the slot, job or token that pinned it.
 *  Project-scope rows have no id and keep storing the name. */
export function crewHandle(a: AgentHandleRow): string {
  return a.member_id || a.name
}

/** Whether a stored agent value names this roster row. Older records (and
 *  project rows) hold the name; newer ones hold `member_id`; both match. */
export function isHandleOf(a: AgentHandleRow, handle: string): boolean {
  return handle === a.name || (!!a.member_id && handle === a.member_id)
}

/**
 * The label to render for a STORED agent value — a cron's `agent`, a webhook
 * token's `agent`, a channel role's agent — resolved through the roster the
 * surface already holds. A value pinned as `member_id` (`crew-program-manager`)
 * renders as the crew's display name (`Crew Program Manager`); a value the
 * roster does not list (roster still loading, a template, a deleted crew)
 * renders verbatim, which is the only honest fallback.
 */
export function agentDisplayLabel(agent: string, agents?: readonly AgentHandleRow[]): string {
  const row = agents?.find(a => isHandleOf(a, agent))
  return row ? crewDisplayName(row) : agent
}

/**
 * Display label for an agent field that may be empty (issue #6495).
 *
 * An empty agent means the record resolves the CURRENT default at run time,
 * so the accurate label is the resolved alias — marked `· default` (reusing
 * AgentSelector's badge key) so an inherited default stays distinguishable
 * from an explicit pin. The built-in wire alias `default` and the English marker
 * render as the same word, so that one combination shows it once. A named alias
 * remains marked even when its spelling happens to equal a translated marker:
 * the first word is the agent's identity and the second is its inherited state.
 * Degrades to the literal `default` until the default loads or when none is
 * configured. The inherited-default tooltip also explains the dynamic binding
 * on interactive surfaces. Textual (not a styled badge) because the Worlds scene
 * layers render it as a plain string.
 *
 * A set agent is resolved through `agents` when the surface passes its roster,
 * so a stored `member_id` renders as the crew's display name (see
 * `agentDisplayLabel`). Surfaces without a roster at hand render it verbatim.
 *
 * The one shared spelling for every surface that renders this state; do not
 * inline copies.
 */
export function agentOrDefaultLabel(agent: string | undefined, defaultAgent: string, agents?: readonly AgentHandleRow[]): string {
  if (agent) return agentDisplayLabel(agent, agents)
  if (!defaultAgent) return 'default'
  const defaultLabel = i18nT('components.agentSelector.default')
  return defaultAgent === 'default' && defaultLabel === 'default'
    ? defaultLabel
    : `${defaultAgent} · ${defaultLabel}`
}
