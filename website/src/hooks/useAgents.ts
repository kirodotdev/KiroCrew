import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import type { KiroCrewAgent } from '../components/AgentSelector'

/**
 * Reads the execution-choice catalog (`GET /api/agents/catalog`): configured
 * members AND installed shared templates, each row tagged `selection_kind`.
 *
 * It is a READ. The hook used to POST `/api/agents/sync` once per mount before
 * listing, and that write enrolled every discovered template as a crew member
 * (config row + member memory) just so the picker could offer it. Opening a chat,
 * the schedule form or the channel page therefore grew the Crew Members roster
 * as a side effect. The catalog lists a template without making it a member, so
 * a picker no longer has to mutate the registry to show what can run.
 *
 * @param sessionKey Chat-slot key whose project scope should apply. Omit on
 *   surfaces with no slot context; project-scoped agents are then excluded.
 * @param projectDir The slot's current project directory. The server resolves
 *   project-scoped agents from it, so it is part of this fetch's identity, not
 *   just an input to it: pointing the SAME slot at a different project changes
 *   the roster without changing `sessionKey`. Omit on surfaces with no slot
 *   context (the roster is then global-only and cannot go stale this way).
 *
 * @returns `choices` — the catalog rows a namespace-aware picker (the chat agent
 *   pop-up) renders, keyed by (selection_kind, name). Unless the gateway's
 *   `dashboard.crewmates_in_agent_picker` config is `true` (read from the shared
 *   `['kirocrewConfig']` query, like `useCrewmateThreadsFlag`), a member row is
 *   withheld when a listed template reaches the same binding (see
 *   `withoutCoveredCrewmates`); a crewmate no template covers stays pickable, and
 *   so does the member named as the default agent unless a listed template
 *   shares its name.
 *   With the key on, every member is listed, a member and a template of one name
 *   are two rows here, and picking one sends its kind.
 * @returns `agents` — the same catalog folded to ONE row per name for the
 *   name-only consumers (the schedule form's `agent_id`, the channel and project
 *   pages, the keyboard cycle). A member wins the fold because the backend's
 *   name-first resolution answers a bare name with the alias, so the template
 *   the fold hides is exactly the one a bare name could not reach anyway.
 * @returns `displayAgents` — `agents` as of the last successful load, NOT
 *   emptied on a scope switch. Display-only (the chat sidebar's agent tint and
 *   inherited row colour); never a source for a selection.
 * @returns `error` — the catalog fetch FAILED, as distinct from an install that
 *   genuinely has one agent. The two used to be the same observation: the fetch
 *   swallowed its rejection and left `agents` empty, so every caller rendered a
 *   failed load as a legitimately short list (#5990). Callers that cannot
 *   otherwise recover must surface it and offer `reload`.
 * @returns `reload` — re-run the fetch. `refreshTrigger` cannot serve as the
 *   retry on a surface that passes a constant (the schedule form passes `0`),
 *   because the effect then never runs again for the life of the mount.
 * @returns `reloading` — a `reload` fetch is in flight. Without it a retry that
 *   fails AGAIN is invisible: `setError(true)` over an already-true value bails
 *   out of re-rendering, so the surface is pixel-identical after the click and
 *   the one recovery affordance looks broken during the very outage it exists
 *   for. Callers use it to make the attempt visibly complete.
 */
export function useAgents(refreshTrigger: number, sessionKey?: string, projectDir?: string) {
  const [choices, setChoices] = useState<KiroCrewAgent[]>([])
  // The last roster that actually LOADED, kept across a scope switch. `choices`
  // is emptied on a switch so the picker cannot offer the old scope's agents,
  // but surfaces that only DESCRIBE agents (the sidebar's agent-label tint, the
  // row's inherited session colour) must not go blank for that window: every row
  // briefly lost its agent colour on each session switch and flashed back once
  // the new fetch resolved. Never use this to pick or send an agent.
  const [displayChoices, setDisplayChoices] = useState<KiroCrewAgent[]>([])
  const [defaultAgent, setDefaultAgent] = useState('')
  // Closed until the config says otherwise: a missing or failed read keeps
  // the templates-only pop-up the gateway ships with.
  const memberChoicesQuery = useQuery<{ dashboard?: { crewmates_in_agent_picker?: boolean } }, Error, boolean>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    select: (c) => c?.dashboard?.crewmates_in_agent_picker === true,
  })
  const memberChoices = memberChoicesQuery.data === true
  const [error, setError] = useState(false)
  const [reloading, setReloading] = useState(false)
  const [reloadTick, setReloadTick] = useState(0)
  const reload = useCallback(() => {
    setReloading(true)
    setReloadTick(t => t + 1)
  }, [])
  // The scope this roster belongs to, held as two refs rather than one joined
  // key: comparing the parts needs no delimiter, so no directory name can forge
  // a scope boundary.
  const lastKey = useRef<string | undefined>(undefined)
  const lastProject = useRef<string | undefined>(undefined)

  useEffect(() => {
    let cancelled = false
    // A scope switch must not leave the PREVIOUS scope's roster selectable while
    // the new scope's fetch is in flight: a stale project agent picked in that
    // window would be stored against the new slot and reset its project.
    // Cleared only on scope change — a same-scope refresh keeps the current list
    // to avoid flicker. The scope is (slot, project) because re-pointing one
    // slot at another project makes the old project's agents just as stale as a
    // slot switch does.
    if (lastKey.current !== sessionKey || lastProject.current !== projectDir) {
      lastKey.current = sessionKey
      lastProject.current = projectDir
      setChoices([])
      // The previous scope's verdict says nothing about this one.
      setError(false)
    }
    api.agentCatalog(sessionKey).then(d => {
      if (cancelled) return
      setChoices(d.agents || [])
      setDisplayChoices(d.agents || [])
      setDefaultAgent(d.default_agent || '')
      setError(false)
      setReloading(false)
    }).catch(() => {
      // Still swallowed as far as throwing goes — a rejected catalog fetch must
      // not break the surface that asked for it — but no longer silent: the
      // list is left as-is (a failed REFRESH keeps the roster it already had)
      // and the failure becomes readable state.
      if (cancelled) return
      setError(true)
      // Cleared on the failing path too, so a retry that fails again still
      // resolves visibly instead of leaving the caller pinned in "trying".
      setReloading(false)
    })
    return () => { cancelled = true }
  }, [refreshTrigger, sessionKey, projectDir, reloadTick])

  const agents = useMemo(() => foldByName(choices), [choices])
  const displayAgents = useMemo(() => foldByName(displayChoices), [displayChoices])
  // Filtered AFTER the fold, so hiding a member from the pop-up never changes
  // which row a bare name resolves to for the name-only consumers.
  const pickerChoices = useMemo(
    () => (memberChoices ? choices : withoutCoveredCrewmates(choices, defaultAgent)),
    [choices, memberChoices, defaultAgent],
  )

  return { agents, displayAgents, choices: pickerChoices, defaultAgent, error, reload, reloading }
}

/**
 * The pop-up's rows while `dashboard.crewmates_in_agent_picker` is off (the
 * default). A crewmate is withheld
 * only when a listed template already reaches the same thing:
 *
 * - a template of the SAME name is listed, or
 * - the crewmate has no memory of its own (`memory_store === 'default'`) and runs
 *   a listed template -- picking that template is the identical binding.
 *
 * Every other crewmate stays: one made by hand under its own name with its own
 * memory, and one whose agent is its own private copy (the catalog never lists a
 * private copy as a template). Withholding those left no way to pick them from a
 * chat at all.
 *
 * The member named as `defaultAgent` stays even when covered, unless a listed
 * template shares its name. New chats start on the default, and the composer's
 * chip and the default badge show its MEMBER name, so a user who switched away
 * searches for that name and the pop-up has to answer with a row. A same-named
 * template is that row (the identical binding under the name searched for, so
 * listing the member too would show one agent twice: the alias "set as default"
 * enrols for a template is this case). A template of another name is not: the
 * built-in `default` crew runs the listed `kirocrew` template, and a search for
 * `default` found nothing (#18239).
 */
export function withoutCoveredCrewmates(choices: KiroCrewAgent[], defaultAgent: string): KiroCrewAgent[] {
  const templates = new Set(
    choices.filter(c => c.selection_kind === 'template').map(c => c.name),
  )
  const covered = (c: KiroCrewAgent) =>
    templates.has(c.name) || (c.memory_store === 'default' && templates.has(c.kiro_agent))
  const namesTheDefault = (c: KiroCrewAgent) => c.name === defaultAgent && !templates.has(c.name)
  return choices.filter(c => c.selection_kind !== 'member' || namesTheDefault(c) || !covered(c))
}

/** One row per name, member first — see `agents` in the hook's docs. */
function foldByName(choices: KiroCrewAgent[]): KiroCrewAgent[] {
  const byName = new Map<string, KiroCrewAgent>()
  for (const row of choices) {
    const held = byName.get(row.name)
    if (!held || (held.selection_kind === 'template' && row.selection_kind === 'member')) {
      byName.set(row.name, row)
    }
  }
  return [...byName.values()]
}
