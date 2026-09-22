import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { api } from '../api/client'
import type { KiroCrewAgent } from '../components/AgentSelector'

/**
 * TEMPORARY: keep crewmates out of the chat agent pop-up. Only the picker's
 * `choices` list is affected -- the folded `agents` list (cron, channel, project
 * bindings, the keyboard cycle) still sees every name, so a slot already
 * running a member keeps working and nothing is written. Flip to `false` to
 * restore the two-group pop-up; the only other touch is
 * `test/useAgents.hideCrewmates.test.tsx`, which pins the flag on.
 */
export const HIDE_CREWMATE_CHOICES = true

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
 *   pop-up) renders, keyed by (selection_kind, name). While
 *   `HIDE_CREWMATE_CHOICES` is on, member rows are withheld and the pop-up offers
 *   templates only; a member is still reachable from its DM thread on the Crew
 *   Members page. With the flag off, a member and a template of one name are two
 *   rows here, and picking one sends its kind.
 * @returns `agents` — the same catalog folded to ONE row per name for the
 *   name-only consumers (the schedule form's `agent_id`, the channel and project
 *   pages, the keyboard cycle). A member wins the fold because the backend's
 *   name-first resolution answers a bare name with the alias, so the template
 *   the fold hides is exactly the one a bare name could not reach anyway.
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
  const [defaultAgent, setDefaultAgent] = useState('')
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
  // Filtered AFTER the fold, so hiding a member from the pop-up never changes
  // which row a bare name resolves to for the name-only consumers.
  const pickerChoices = useMemo(
    () => (HIDE_CREWMATE_CHOICES ? choices.filter(c => c.selection_kind !== 'member') : choices),
    [choices],
  )

  return { agents, choices: pickerChoices, defaultAgent, error, reload, reloading }
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
