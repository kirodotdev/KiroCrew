/**
 * SkillBrowserModal — multi-provider skill discovery and installation.
 *
 * Opened by the "Add Skill" button on the Skills page. Searches across
 * all configured providers (skills.sh) and displays results
 * in a two-pane layout: results list (left) + detail preview (right).
 * Keyboard-first: arrow keys move the selection, Enter installs.
 */
import { useState, useCallback, useRef, useEffect, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Download, Check, ExternalLink, Loader2, RefreshCw, FileText, AlertTriangle, ArrowLeft } from 'lucide-react'
import { api, ApiError } from '../api/client'
import Modal from './Modal'
import { Btn } from './ui'
import MarkdownRenderer from './MarkdownRenderer'
import { DiscoverySearchBar, DiscoveryStates } from './DiscoverySearchBar'
import { SkillMetaStrip } from './SkillDirectoryBrowser'
import { parseFrontmatter } from './SkillForm'
import type { DiscoveredSkill } from '../types'

import { i18nT } from '../i18n/t'
interface Props {
  open: boolean
  onClose: () => void
}

/** Compact human format for install counts: 557834 -> "557.8k". */
export function formatInstalls(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, '')}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1).replace(/\.0$/, '')}k`
  return String(n)
}

/** Per-skill install lifecycle for UI feedback. */
type InstallPhase =
  | { step: 'installing' }
  | { step: 'done'; fileCount: number; kind: string }
  | { step: 'conflict'; key: string }
  | { step: 'error'; message: string }

export default function SkillBrowserModal({ open, onClose }: Props) {
  const queryClient = useQueryClient()
  const [query, setQuery] = useState('')
  const [debouncedQuery, setDebouncedQuery] = useState('')
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // Install lifecycle per skill key (`provider:id`).
  const [installPhases, setInstallPhases] = useState<Record<string, InstallPhase>>({})
  // Locally-installed override so rows flip to Installed without a refetch.
  const [installedOverride, setInstalledOverride] = useState<Set<string>>(new Set())
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  // Narrow-viewport mode: the detail pane replaces the list (single-pane).
  // Only set by explicit row clicks so keyboard arrow-selection doesn't
  // yank the list away on small screens.
  const [mobileDetail, setMobileDetail] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)

  const handleQueryChange = useCallback((value: string) => {
    setQuery(value)
    if (debounceRef.current) clearTimeout(debounceRef.current)
    debounceRef.current = setTimeout(() => setDebouncedQuery(value), 300)
  }, [])

  // Clear any pending debounce timer on unmount.
  useEffect(() => () => {
    if (debounceRef.current) clearTimeout(debounceRef.current)
  }, [])

  // Installed state has no store of its own -- the backend derives it from
  // the skills directory on every search. Reset the session-local optimistic
  // state and refetch on each open so deletions made while the modal was
  // closed are reflected.
  useEffect(() => {
    if (open) {
      setInstalledOverride(new Set())
      setInstallPhases({})
      queryClient.invalidateQueries({ queryKey: ['discover-skills'] })
    }
  }, [open, queryClient])

  const clearQuery = useCallback(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current)
    setQuery('')
    setDebouncedQuery('')
    setSelectedKey(null)
    setMobileDetail(false)
    inputRef.current?.focus()
  }, [])

  const { data, isLoading, isFetching } = useQuery({
    queryKey: ['discover-skills', debouncedQuery],
    queryFn: () => api.discoverSkills(debouncedQuery),
    enabled: open && debouncedQuery.length >= 2,
    retry: false,
    staleTime: 30_000,
  })

  const results = useMemo(() => data?.results ?? [], [data])
  const providers = data?.providers ?? []

  const skillKey = (s: DiscoveredSkill) => `${s.provider}:${s.id}`
  const selectedSkill = results.find(s => skillKey(s) === selectedKey) ?? null
  const isInstalled = (s: DiscoveredSkill) => s.installed || installedOverride.has(skillKey(s))

  // Reset selection when the result set changes (new search). `data` is
  // referentially stable between renders (react-query cache), unlike the
  // derived `results` array which would churn this effect every render.
  // mobileDetail needs no reset here: the detail pane only renders when a
  // selected skill actually exists in the current results.
  useEffect(() => {
    const next = data?.results ?? []
    setSelectedKey(prev => (prev && next.some(s => skillKey(s) === prev) ? prev : null))
  }, [data])

  const setPhase = useCallback((key: string, phase: InstallPhase | null) => {
    setInstallPhases(prev => {
      const next = { ...prev }
      if (phase === null) delete next[key]
      else next[key] = phase
      return next
    })
  }, [])

  const installMutation = useMutation({
    mutationFn: ({ skill, overwrite }: { skill: DiscoveredSkill; overwrite?: boolean }) =>
      api.installDiscoveredSkill(skill.provider, skill.id, { overwrite }),
    onMutate: ({ skill }) => setPhase(skillKey(skill), { step: 'installing' }),
    onSuccess: (result, { skill }) => {
      const key = skillKey(skill)
      setPhase(key, { step: 'done', fileCount: result.file_count, kind: result.kind })
      setInstalledOverride(prev => new Set(prev).add(key))
      queryClient.invalidateQueries({ queryKey: ['skills'] })
    },
    onError: (err, { skill }) => {
      const key = skillKey(skill)
      if (err instanceof ApiError && err.status === 409) {
        setPhase(key, { step: 'conflict', key })
      } else {
        setPhase(key, { step: 'error', message: err instanceof Error ? err.message : String(err) })
      }
    },
  })

  const handleInstall = useCallback((skill: DiscoveredSkill, overwrite?: boolean) => {
    installMutation.mutate({ skill, overwrite })
  }, [installMutation])

  // Keyboard navigation: ArrowDown/ArrowUp move selection, Enter installs
  // the selected skill. Works from the search input and the list itself.
  const moveSelection = useCallback((delta: number) => {
    if (results.length === 0) return
    const idx = results.findIndex(s => skillKey(s) === selectedKey)
    const next = idx === -1
      ? (delta > 0 ? 0 : results.length - 1)
      : Math.min(Math.max(idx + delta, 0), results.length - 1)
    const key = skillKey(results[next])
    setSelectedKey(key)
    // Keep the active row visible in the scrollable list (guarded: jsdom
    // does not implement scrollIntoView).
    const el = listRef.current?.querySelector(`[data-skill-key="${CSS.escape(key)}"]`)
    el?.scrollIntoView?.({ block: 'nearest' })
  }, [results, selectedKey])

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); moveSelection(1) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); moveSelection(-1) }
    else if (e.key === 'Enter' && selectedSkill && !(selectedSkill.installed || installedOverride.has(skillKey(selectedSkill)))) {
      e.preventDefault()
      const phase = installPhases[skillKey(selectedSkill)]
      if (!phase || phase.step === 'error') handleInstall(selectedSkill)
    }
  }, [moveSelection, selectedSkill, installedOverride, installPhases, handleInstall])

  return (
    <Modal open={open} onClose={onClose} title={i18nT('components.skillBrowserModal.add_skill')} maxWidth={1100} height="85vh"
      headerActions={
        isFetching ? <RefreshCw size={14} className="text-muted animate-spin" /> : undefined
      }
    >
      <div className="flex flex-col h-full min-h-0">
        <DiscoverySearchBar
          ref={inputRef}
          idPrefix="skill"
          subject="skills"
          query={query}
          debouncedQuery={debouncedQuery}
          providers={providers}
          resultCount={results.length}
          isLoading={isLoading}
          hasResults={results.length > 0}
          activeDescendant={selectedKey}
          onQueryChange={handleQueryChange}
          onKeyDown={handleKeyDown}
          onClear={clearQuery}
        />

        <DiscoveryStates debouncedQuery={debouncedQuery} isLoading={isLoading} resultCount={results.length} noun="skills" />

        {/* Two-pane on md+: results list (left) + detail preview (right).
            Single-pane below md: the list fills the modal; clicking a row
            swaps to the detail view with a Back button. */}
        {results.length > 0 && (
          <div className="flex gap-3 flex-1 min-h-0">
            <div
              ref={listRef}
              id="skill-results-list"
              role="listbox"
              aria-label={i18nT('components.skillBrowserModal.skill_search_results')}
              className={`${selectedSkill && mobileDetail ? 'hidden md:block' : 'block'} w-full md:w-[40%] md:shrink-0 overflow-y-auto scrollbar-overlay space-y-1.5 pr-1`}
            >
              {results.map(skill => {
                const key = skillKey(skill)
                const active = key === selectedKey
                const phase = installPhases[key]
                return (
                  <div
                    key={key}
                    id={`skill-opt-${key}`}
                    data-skill-key={key}
                    role="option"
                    aria-selected={active}
                    aria-label={skill.name}
                    tabIndex={active ? 0 : -1}
                    onKeyDown={handleKeyDown}
                    className={`px-3 py-2.5 rounded-lg cursor-pointer transition-colors border ${
                      active
                        ? 'bg-bg-hover border-accent/50'
                        : 'border-transparent hover:bg-bg-hover hover:border-border'
                    }`}
                    onClick={() => { setSelectedKey(key); setMobileDetail(true) }}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2">
                          <span className="text-sm font-medium text-text-strong truncate">
                            {skill.name}
                          </span>
                          <span className="shrink-0 text-[10px] px-1.5 py-0.5 rounded-full bg-accent/15 text-accent font-medium">
                            {skill.display_provider}
                          </span>
                        </div>
                        <div className="mt-0.5 flex items-center gap-2 text-xs text-muted">
                          {(skill.installs ?? 0) > 0 && (
                            <span className="flex items-center gap-1 shrink-0">
                              <Download size={11} aria-hidden="true" />
                              {formatInstalls(skill.installs!)} {i18nT('components.skillBrowserModal.installs')}
                            </span>
                          )}
                          {skill.description && (
                            <span className="truncate">{skill.description}</span>
                          )}
                        </div>
                        {phase?.step === 'error' && (
                          <p className="mt-1 text-xs text-red-400">{phase.message}</p>
                        )}
                      </div>
                      <div className="shrink-0 mt-0.5">
                        <InstallStatus
                          skill={skill}
                          installed={isInstalled(skill)}
                          phase={phase}
                          onInstall={handleInstall}
                        />
                      </div>
                    </div>
                  </div>
                )
              })}
            </div>

            <div className={`${selectedSkill && mobileDetail ? 'flex' : 'hidden md:flex'} flex-col flex-1 min-w-0 min-h-0 md:border-l border-border md:pl-3`}>
              {selectedSkill ? (
                <>
                  {/* Back button stays fixed above the scrollable detail content */}
                  <div className="md:hidden mb-2 shrink-0">
                    <Btn onClick={() => setMobileDetail(false)}>
                      <ArrowLeft size={14} aria-hidden="true" /> {i18nT('components.skillBrowserModal.back_to_results')}
                    </Btn>
                  </div>
                  <div className="flex-1 min-h-0 overflow-y-auto scrollbar-overlay">
                    <SkillDetailPanel
                      skill={selectedSkill}
                      installed={isInstalled(selectedSkill)}
                      phase={installPhases[skillKey(selectedSkill)]}
                      onInstall={handleInstall}
                    />
                  </div>
                </>
              ) : (
                <div className="flex items-center justify-center h-full text-muted text-sm">
                  {i18nT('components.skillBrowserModal.select_a_skill_to_preview')}
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </Modal>
  )
}

/** Install button / status indicator, shared by list rows and detail pane. */
function InstallStatus({
  skill,
  installed,
  phase,
  onInstall,
  large,
}: {
  skill: DiscoveredSkill
  installed: boolean
  phase: InstallPhase | undefined
  onInstall: (skill: DiscoveredSkill, overwrite?: boolean) => void
  large?: boolean
}) {
  const iconSize = large ? 14 : 12

  if (phase?.step === 'installing') {
    return (
      <span className="flex items-center gap-1.5 text-xs text-muted" role="status">
        <Loader2 size={iconSize} className="animate-spin" aria-hidden="true" />
        {i18nT('components.skillBrowserModal.installing')}
      </span>
    )
  }
  if (phase?.step === 'done') {
    return (
      <span className="flex items-center gap-1 text-xs text-green-400" role="status">
        <Check size={iconSize} aria-hidden="true" />
        {phase.fileCount > 1 ? `Installed ${phase.fileCount} files` : 'Installed'}
      </span>
    )
  }
  if (phase?.step === 'conflict') {
    return (
      <span className="flex items-center gap-1.5 text-xs">
        <span className="flex items-center gap-1 text-amber-400">
          <AlertTriangle size={iconSize} aria-hidden="true" /> {i18nT('components.skillBrowserModal.exists')}
        </span>
        <Btn onClick={(e: React.MouseEvent) => { e.stopPropagation(); onInstall(skill, true) }}>
          {i18nT('components.skillBrowserModal.overwrite')}
        </Btn>
      </span>
    )
  }
  if (installed) {
    return (
      <span className="flex items-center gap-1 text-xs text-green-400">
        <Check size={iconSize} aria-hidden="true" /> {i18nT('components.skillBrowserModal.installed')}
      </span>
    )
  }
  return (
    <Btn
      primary={large}
      onClick={(e: React.MouseEvent) => { e.stopPropagation(); onInstall(skill) }}
    >
      <Download size={iconSize} aria-hidden="true" />
      {i18nT('components.skillBrowserModal.install')}{large ? ' Skill' : ''}
    </Btn>
  )
}

/** Detail pane: full SKILL.md preview + bundle manifest, fetched lazily. */
function SkillDetailPanel({
  skill,
  installed,
  phase,
  onInstall,
}: {
  skill: DiscoveredSkill
  installed: boolean
  phase: InstallPhase | undefined
  onInstall: (skill: DiscoveredSkill, overwrite?: boolean) => void
}) {
  const { data: preview, isLoading: previewLoading } = useQuery({
    queryKey: ['skill-preview', skill.provider, skill.id],
    queryFn: () => api.previewDiscoveredSkill(skill.provider, skill.id),
    staleTime: 60_000,
  })

  // Same presentation as the installed-skill viewer (SkillDirectoryBrowser):
  // frontmatter parsed into the labeled meta strip, body rendered without
  // the YAML block (CommonMark would garble ---key: value--- into a heading).
  const parsed = useMemo(
    () => parseFrontmatter(preview?.content ?? ''),
    [preview?.content],
  )
  const stripDescription = parsed.meta.description || preview?.description || skill.description || ''
  const stripTriggers = parsed.meta.triggers ?? ''
  const stripTags = parsed.meta.tags || (skill.tags && skill.tags.length > 0 ? skill.tags.join(', ') : '')

  return (
    <div className="pb-2">
      <div className="flex items-start justify-between gap-2 mb-1">
        <div className="flex items-center gap-2 min-w-0">
          <h3 className="text-base font-semibold text-text-strong truncate">{skill.name}</h3>
          <span className="shrink-0 text-[10px] px-1.5 py-0.5 rounded-full bg-accent/15 text-accent font-medium">
            {skill.display_provider}
          </span>
        </div>
        <div className="shrink-0">
          <InstallStatus skill={skill} installed={installed} phase={phase} onInstall={onInstall} large />
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted mb-3">
        {(preview?.author || skill.author) && <span>{i18nT('components.skillBrowserModal.by')} {preview?.author || skill.author}</span>}
        {(skill.installs ?? 0) > 0 && (
          <span className="flex items-center gap-1">
            <Download size={11} aria-hidden="true" /> {formatInstalls(skill.installs!)} {i18nT('components.skillBrowserModal.installs')}
          </span>
        )}
        {preview?.license && <span>{i18nT('components.skillBrowserModal.license')} {preview.license}</span>}
        {(preview?.file_count ?? 0) > 0 && (
          <span className="flex items-center gap-1">
            <FileText size={11} aria-hidden="true" /> {i18nT('components.skillBrowserModal.file', { count: preview!.file_count })}
          </span>
        )}
        {skill.repo_url && (
          <a
            href={skill.repo_url}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-accent hover:underline"
          >
            <ExternalLink size={11} aria-hidden="true" /> {i18nT('components.skillBrowserModal.source')}
          </a>
        )}
      </div>

      {phase?.step === 'error' && (
        <div className="mb-3 p-2 rounded bg-danger-subtle border border-danger/30 text-xs text-danger">
          {phase.message}
        </div>
      )}

      {previewLoading ? (
        <div className="flex items-center gap-2 text-xs text-muted" role="status">
          <Loader2 size={12} className="animate-spin" aria-hidden="true" /> {i18nT('components.skillBrowserModal.loading_preview')}
        </div>
      ) : preview?.content ? (
        <>
          <SkillMetaStrip
            description={stripDescription}
            triggers={stripTriggers}
            tags={stripTags}
          />
          <div className="text-sm leading-relaxed">
            <MarkdownRenderer content={parsed.body} />
          </div>
        </>
      ) : (
        <p className="text-sm text-muted">
          {stripDescription || 'No preview available.'}
        </p>
      )}

      {(preview?.files?.length ?? 0) > 1 && (
        <details className="mt-4">
          <summary className="text-xs text-muted cursor-pointer hover:text-text">
            {i18nT('components.skillBrowserModal.bundle_contents')}{preview!.file_count} {i18nT('components.skillBrowserModal.files')}
          </summary>
          <ul className="mt-1.5 text-xs text-muted font-mono space-y-0.5 max-h-40 overflow-y-auto scrollbar-overlay">
            {preview!.files!.map(f => <li key={f}>{f}</li>)}
          </ul>
        </details>
      )}
    </div>
  )
}
