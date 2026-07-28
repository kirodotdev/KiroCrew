import { useMemo, useRef, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { createPortal } from 'react-dom'
import { Brain, ChevronDown, Lock, Plus, X } from 'lucide-react'
import { api } from '../api/client'
import { Btn, Input } from './ui'
import InfoTip from './InfoTip'
import { useFilteredDropdown } from '../hooks/useFilteredDropdown'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'

/** A row from `GET /api/skills` — only the fields this editor needs. */
export interface CatalogSkill {
  key: string
  name: string
  description?: string
  source?: string
}

interface Props {
  /** Agent template name (the `{name}` in `/api/agents/detail/{name}`). */
  agentName: string
  /** Catalog keys currently mapped via the agent's `skill://` resources. */
  skills: string[]
  /**
   * `skill://` URIs the catalog cannot express — wildcard patterns and paths
   * outside every known skill root. Shown read-only: the backend preserves them
   * across writes, so listing them here explains why an agent may load more
   * than the editable chips suggest.
   */
  unmanaged?: string[]
  /**
   * Called after a successful save with the agent the save was issued FOR and
   * its new key list. The name is passed back because a slow PATCH can resolve
   * after the user has selected a different agent — the caller must ignore a
   * response that no longer matches what is on screen, or agent A's skills land
   * on agent B and the next edit writes them to B's spec.
   */
  onChange: (agentName: string, skills: string[]) => void
}

/**
 * Add/remove the skills an agent template maps.
 *
 * Writes through `PATCH /api/agents/detail/{name}` with `{ skills: [...] }`,
 * which the backend materializes as kiro-cli-native `skill://` entries in the
 * agent's `resources`. Each edit saves immediately (same interaction model as
 * the model picker on this page) — there is no separate Save button to forget.
 */
export default function AgentSkillsEditor({ agentName, skills, unmanaged = [], onChange }: Props) {
  const [error, setError] = useState('')
  const btnRef = useRef<HTMLButtonElement>(null)

  const { data: catalog = [] } = useQuery<CatalogSkill[]>({
    queryKey: ['skills-catalog'],
    queryFn: async () => {
      const rows = await api.skills()
      return Array.isArray(rows) ? (rows as CatalogSkill[]).filter(s => s?.key) : []
    },
    staleTime: 30_000,
  })

  const byKey = useMemo(() => {
    const m = new Map<string, CatalogSkill>()
    for (const s of catalog) m.set(s.key, s)
    return m
  }, [catalog])

  // Candidates = catalog minus what's already mapped, name-sorted for a stable
  // list regardless of the catalog's source-grouped order.
  const candidates = useMemo(
    () => catalog.filter(s => !skills.includes(s.key)).sort((a, b) => a.name.localeCompare(b.name)),
    [catalog, skills],
  )

  const { open, setOpen, filter, setFilter, dropdownRef, inputRef, filtered } =
    useFilteredDropdown(candidates)

  const save = useMutation({
    // The agent name travels WITH the request so the response can be matched to
    // the agent it was issued for, not to whatever is selected when it lands.
    mutationFn: ({ agent, next }: { agent: string; next: string[] }) =>
      api.agentPatch(agent, { skills: next }),
    onMutate: () => setError(''),
    onSuccess: (res: { skills?: string[] }, { agent, next }) =>
      onChange(agent, res?.skills ?? next),
    onError: (e: unknown) => setError(e instanceof Error ? e.message : String(e)),
  })

  const add = (key: string) => {
    setOpen(false)
    save.mutate({ agent: agentName, next: [...skills, key] })
  }
  const remove = (key: string) =>
    save.mutate({ agent: agentName, next: skills.filter(k => k !== key) })

  const { onListKeyDown } = useListboxKeyboard({
    open,
    dropdownRef,
    inputRef,
    hasFilterInput: true,
    filteredCount: filtered.length,
    onEnterSingleMatch: () => add(filtered[0].key),
    closeToTrigger: () => { setOpen(false); btnRef.current?.focus() },
  })

  return (
    <div className="mb-3">
      <div className="flex items-center gap-2 mb-1.5">
        <span className="text-[12px] text-muted font-medium uppercase tracking-wider">Skills</span>
        <InfoTip text="Skills this agent template loads. Written as skill:// resources in the agent config, so kiro-cli loads them natively when the agent starts. An agent with no skills mapped falls back to the default behavior: the kirocrew agent sees the whole catalog, other agents see none." />
      </div>
      <div className="flex flex-wrap items-center gap-1.5">
        {skills.map(key => {
          const skill = byKey.get(key)
          return (
            <span
              key={key}
              className="group inline-flex items-center gap-1 pl-2 pr-1 py-1 rounded-full text-[12px] font-mono bg-accent-subtle border border-accent/30 text-text"
              title={skill?.description || key}
            >
              <Brain className="lucide-inline" />
              {skill?.name || key}
              <button
                className="text-muted hover:text-danger-fg hover:bg-danger rounded-full px-0.5 transition-colors disabled:opacity-40"
                title={`Remove ${skill?.name || key}`}
                aria-label={`Remove skill ${skill?.name || key}`}
                disabled={save.isPending}
                onClick={() => remove(key)}
              >
                <X className="lucide-inline" />
              </button>
            </span>
          )
        })}
        {unmanaged.map(uri => (
          <span
            key={uri}
            className="inline-flex items-center gap-1 px-2 py-1 rounded-full text-[12px] font-mono bg-bg-elevated border border-border text-muted"
            title={`${uri} — edit the agent config file to change this mapping`}
          >
            <Lock className="lucide-inline" />
            {uri}
          </span>
        ))}
        <div className="relative">
          <Btn
            ref={btnRef}
            className="flex items-center gap-1 px-2 py-1 text-[12px]"
            disabled={save.isPending || candidates.length === 0}
            onClick={() => setOpen(!open)}
          >
            <Plus className="lucide-inline" /> Add skill
            <span className="text-muted text-[10px]"><ChevronDown className="lucide-inline" /></span>
          </Btn>
          {open && btnRef.current && createPortal(
            // Presentational positioning wrapper: interactive semantics live on
            // the inner role="listbox" and its option buttons, so this element
            // only hosts the roving-focus keydown handler (mirrors the model
            // dropdown on this page).
            // eslint-disable-next-line jsx-a11y/no-static-element-interactions
            <div
              ref={dropdownRef}
              tabIndex={-1}
              onKeyDown={onListKeyDown}
              className="fixed z-[9999] bg-card border border-border rounded-lg shadow-lg min-w-[280px] max-w-[380px] max-h-[320px] flex flex-col overflow-hidden animate-slide-up"
              style={(() => {
                const r = btnRef.current!.getBoundingClientRect()
                const dropH = 320
                const top = r.bottom + 4 + dropH > window.innerHeight ? r.top - dropH - 4 : r.bottom + 4
                const left = Math.max(8, Math.min(r.left, window.innerWidth - 388))
                return { top, left }
              })()}
            >
              <div className="p-2 border-b border-border">
                <Input
                  ref={inputRef}
                  type="text"
                  aria-label="Filter skills"
                  placeholder="Type to filter…"
                  value={filter}
                  onChange={e => setFilter(e.target.value)}
                  className="w-full px-2 py-1 text-[13px] font-mono"
                />
              </div>
              <div role="listbox" aria-label="Available skills" className="overflow-y-auto flex-1 min-h-0 p-1">
                {filtered.length === 0 ? (
                  <div className="px-2 py-3 text-[12px] text-muted text-center">No matching skills</div>
                ) : filtered.map(s => (
                  <button
                    key={s.key}
                    role="option"
                    aria-selected={false}
                    tabIndex={-1}
                    className="w-full text-left px-2 py-1.5 rounded-md hover:bg-bg-hover focus-ring transition-colors"
                    onClick={() => add(s.key)}
                  >
                    <span className="block text-[13px] font-mono text-text truncate">{s.name}</span>
                    {s.description && (
                      <span className="block text-[11px] text-muted truncate">{s.description}</span>
                    )}
                  </button>
                ))}
              </div>
            </div>,
            document.body
          )}
        </div>
      </div>
      {skills.length === 0 && unmanaged.length === 0 && (
        <div className="text-[11px] text-muted mt-1.5">
          No skills mapped — this agent uses the default behavior.
        </div>
      )}
      {error && <div className="text-[12px] text-danger mt-1.5">{error}</div>}
    </div>
  )
}
