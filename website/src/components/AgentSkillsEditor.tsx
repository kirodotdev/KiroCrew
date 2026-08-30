import { useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { createPortal } from 'react-dom'
import { Brain, ChevronDown, Lock, Plus, X } from 'lucide-react'
import { api } from '../api/client'
import { Btn, Input } from './ui'
import InfoTip from './InfoTip'
import { useFilteredDropdown } from '../hooks/useFilteredDropdown'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'

import { i18nT } from '../i18n/t'
import ErrorNotice from './ErrorNotice'
/** A row from `GET /api/skills` — only the fields this editor needs. */
export interface CatalogSkill {
  key: string
  name: string
  description?: string
  source?: string
}

/**
 * `package/<digest>:<rel>` — the qualifier is what makes one of several colliding
 * copies addressable, and it is the ONLY field that differs between them: two bundles
 * vendoring the same skill share a name AND a description, so neither disambiguates.
 * A catalog row carries no bundle name to show instead.
 */
const QUALIFIED_PACKAGE_KEY = /^package\/([0-9a-f]{8,}):(.+)$/

function splitQualifiedKey(key: string): { digest: string; rel: string } | null {
  const m = QUALIFIED_PACKAGE_KEY.exec(key)
  return m ? { digest: m[1], rel: m[2] } : null
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
  const queryClient = useQueryClient()

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

  // How many catalog rows share each display name. Only a name carried by more than
  // one row needs its qualifier shown, so an ordinary skill stays a plain label.
  const nameCounts = useMemo(() => {
    const m = new Map<string, number>()
    for (const s of catalog) m.set(s.name, (m.get(s.name) ?? 0) + 1)
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
    onError: (e: unknown) => {
      setError(e instanceof Error ? e.message : String(e))
      // The catalog in hand is the stale one that minted the refused key, and it is
      // cached, so without this a retry re-sends exactly the key that was just rejected.
      void queryClient.invalidateQueries({ queryKey: ['skills-catalog'] })
    },
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
        <span className="text-[12px] text-muted font-medium uppercase tracking-wider">{i18nT('components.agentSkillsEditor.skills')}</span>
        <InfoTip text={i18nT('components.agentSkillsEditor.skills_this_agent_template_loads_written_as_skil')} />
      </div>
      <div className="flex flex-wrap items-center gap-1.5">
        {skills.map(key => {
          const skill = byKey.get(key)
          const qualified = splitQualifiedKey(key)
          // An unresolved key has no catalog row to name it, so the raw key would render
          // as a 32-hex digest; its rel half is the readable part.
          const label = skill?.name || qualified?.rel || key
          const ambiguous = skill ? (nameCounts.get(skill.name) ?? 0) > 1 : Boolean(qualified)
          return (
            <span
              key={key}
              className="group inline-flex items-center gap-1 pl-2 pr-1 py-1 rounded-full text-[12px] font-mono bg-accent-subtle border border-accent/30 text-text"
              title={skill?.description ? `${skill.description}\n${key}` : key}
            >
              <Brain className="lucide-inline" />
              {label}
              {ambiguous && qualified && (
                <span className="text-muted text-[10px]">{qualified.digest.slice(0, 8)}</span>
              )}
              <button
                className="text-muted hover:text-danger-fg hover:bg-danger rounded-full px-0.5 transition-colors disabled:opacity-40"
                title={i18nT('components.agentSkillsEditor.remove', { name: label })}
                aria-label={i18nT('components.agentSkillsEditor.remove_skill', { name: label })}
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
            title={i18nT('components.agentSkillsEditor.edit_agent_config_to_change_mapping', { path: uri })}
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
            <Plus className="lucide-inline" /> {i18nT('components.agentSkillsEditor.add_skill')}
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
                  aria-label={i18nT('components.agentSkillsEditor.filter_skills')}
                  placeholder={i18nT('components.agentSkillsEditor.type_to_filter')}
                  value={filter}
                  onChange={e => setFilter(e.target.value)}
                  className="w-full px-2 py-1 text-[13px]"
                />
              </div>
              <div role="listbox" aria-label={i18nT('components.agentSkillsEditor.available_skills')} className="overflow-y-auto flex-1 min-h-0 p-1">
                {filtered.length === 0 ? (
                  <div className="px-2 py-3 text-[12px] text-muted text-center">{i18nT('components.agentSkillsEditor.no_matching_skills')}</div>
                ) : filtered.map(s => {
                  const qualified =
                    (nameCounts.get(s.name) ?? 0) > 1 ? splitQualifiedKey(s.key) : null
                  return (
                    <button
                      key={s.key}
                      role="option"
                      aria-selected={false}
                      tabIndex={-1}
                      title={s.key}
                      className="w-full text-left px-2 py-1.5 rounded-md hover:bg-bg-hover focus-ring transition-colors"
                      onClick={() => add(s.key)}
                    >
                      <span className="block text-[13px] font-mono text-text truncate">{s.name}</span>
                      {s.description && (
                        <span className="block text-[11px] text-muted truncate">{s.description}</span>
                      )}
                      {qualified && (
                        <span className="block text-[10px] font-mono text-muted truncate">
                          {qualified.digest.slice(0, 8)}
                        </span>
                      )}
                    </button>
                  )
                })}
              </div>
            </div>,
            document.body
          )}
        </div>
      </div>
      {skills.length === 0 && unmanaged.length === 0 && (
        <div className="text-[11px] text-muted mt-1.5">
          {i18nT('components.agentSkillsEditor.no_skills_mapped_this_agent_uses_the_default_beh')}
        </div>
      )}
      {/* No hand-off: the notice sits beside unsaved form input, and the button
          navigates away — which would discard what the user typed. */}
      <ErrorNotice message={error} className="mt-1.5" />
    </div>
  )
}
