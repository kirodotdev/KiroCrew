import { useContext, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import {
  AlertTriangle,
  BookOpen,
  Brain,
  CalendarClock,
  FileSearch,
  FolderOpen,
  Loader2,
  Plug,
  RefreshCw,
  Settings2,
  ShieldCheck,
  Sparkles,
  type LucideIcon,
} from 'lucide-react'
import {
  api,
  type AgentImportApplyRequest,
  type AgentImportConflictStrategy,
  type AgentImportSource,
} from '../api/client'
import { Btn, SendBtn } from './ui'
import OnboardingChapterShell, {
  OnboardingShellContext,
  type ShellAsideCopy,
} from './OnboardingChapterShell'

import { i18nT } from '../i18n/t'
type Stage = 1 | 2 | 3 | 4

/**
 * The accent left panel's copy for the first-run SETUP chapters.
 *
 * Shared with the Privacy chapter that follows this one, rather than duplicated:
 * the panel is mounted ONCE by `OnboardingShellHost`, so identical copy across
 * the hand-off means nothing on the left changes and the floating mascots have
 * no reason to re-render, let alone replay their entrance. Two look-alike copies
 * would drift the moment one is edited.
 *
 * A function (not a const) because `i18nT()` at module load would freeze the
 * boot language; the lookup has to run during render.
 */
export function setupChapterAside(ariaLabel: string): ShellAsideCopy {
  return {
    ariaLabel,
    panelHeadline: i18nT('components.agentImportFlow.bring_your_crew_with_you'),
    panelBody: i18nT('components.agentImportFlow.bring_your_supported_setup_sessions_memories_wor'),
    panelFootnote: i18nT('components.agentImportFlow.merge_only_setup_credentials_stay_where_they_are'),
  }
}

/** The wizard's stages, in order. Only the COUNT is ever displayed (the "2 of 4"
 *  eyebrow) — each stage's on-screen heading and description are separate,
 *  already-localised strings in `stageHeader`. This used to be an array of
 *  English stage NAMES ('Sources', 'Categories', 'Review', 'Results') that
 *  nothing rendered, which read like a label table needing translation and was
 *  not one. Typed as `Stage[]` so an id here has to be a real stage. */
const STAGES: Stage[] = [1, 2, 3, 4]
const SUPPORTED_SOURCE_IDS = new Set([
  'codex',
  'claude_code',
  'gemini',
  'meshclaw',
  'openclaw',
  'hermes',
])
const SUPPORTED_CATEGORY_IDS = new Set([
  'instructions',
  'memories',
  'workspaces',
  'mcp_servers',
  'skills',
  'schedules',
  'settings',
])
const CATEGORY_ICONS: Record<string, LucideIcon> = {
  instructions: BookOpen,
  memories: Brain,
  workspaces: FolderOpen,
  mcp_servers: Plug,
  skills: Sparkles,
  schedules: CalendarClock,
  settings: Settings2,
}

function supportedCategories(source: AgentImportSource) {
  return source.categories.filter(category =>
    category.count > 0
    && SUPPORTED_CATEGORY_IDS.has(category.id),
  )
}

function eligibleSources(sources: AgentImportSource[]): AgentImportSource[] {
  return sources.filter(source =>
    source.detected
    && SUPPORTED_SOURCE_IDS.has(source.id)
    && supportedCategories(source).length > 0,
  )
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback
}

// Animated "Importing…" label — cycles the trailing dots between ".", ".." and
// "..." while an import is in flight. The dots span reserves width so the
// button doesn't jitter as they grow.
function ImportingLabel() {
  const [dots, setDots] = useState('.')
  useEffect(() => {
    const id = setInterval(() => setDots(d => (d.length >= 3 ? '.' : `${d}.`)), 400)
    return () => clearInterval(id)
  }, [])
  return (
    <>
      {i18nT('components.agentImportFlow.importing')}<span className="inline-block w-3 text-left">{dots}</span>
    </>
  )
}

export default function AgentImportFlow({
  initialOpen,
  onComplete,
  onSkipAll,
}: {
  initialOpen: boolean
  onComplete: () => void
  // "Skip all" abandons the whole first-run flow (import + onboarding tour) and
  // drops the user straight into the product. Falls back to onComplete when the
  // host does not provide a distinct skip-all path.
  onSkipAll?: () => void
}) {
  const [open, setOpen] = useState(initialOpen)
  const [stage, setStage] = useState<Stage>(1)
  const [scanGeneration, setScanGeneration] = useState(0)
  const [selectedSources, setSelectedSources] = useState<Set<string>>(new Set())
  const [strategy, setStrategy] = useState<AgentImportConflictStrategy>('skip')
  const [selectedCategories, setSelectedCategories] = useState<Record<string, Set<string>>>({})
  // The focus trap queries the dialog element. Inside a persistent shell host
  // the dialog is host-owned, so use its ref; standalone we own it locally.
  const shellHost = useContext(OnboardingShellContext)
  const localDialogRef = useRef<HTMLDivElement>(null)
  const dialogRef = shellHost?.dialogRef ?? localDialogRef
  const headingRef = useRef<HTMLHeadingElement>(null)
  const previousFocusRef = useRef<HTMLElement | null>(null)
  const previousInitialOpenRef = useRef(initialOpen)
  const initializedScanGenerationRef = useRef<number | null>(null)

  const scanQuery = useQuery({
    queryKey: ['agent-import-scan', scanGeneration],
    queryFn: () => api.onboardingImportScan(),
    enabled: open,
    retry: false,
    refetchOnWindowFocus: false,
  })
  const sources = useMemo(
    () => eligibleSources(scanQuery.data?.sources ?? []),
    [scanQuery.data?.sources],
  )

  const resetFlow = (refresh: boolean) => {
    setStage(1)
    setSelectedSources(new Set())
    setSelectedCategories({})
    setStrategy('skip')
    applyMutation.reset()
    completionMutation.reset()
    if (refresh) setScanGeneration(value => value + 1)
  }

  const applyPayload = useMemo<AgentImportApplyRequest>(() => ({
    sources: sources
      .filter(source => selectedSources.has(source.id))
      .map(source => ({
        id: source.id,
        categories: supportedCategories(source)
          .filter(category => selectedCategories[source.id]?.has(category.id))
          .map(category => category.id),
      }))
      .filter(source => source.categories.length > 0),
    conflict_strategy: strategy,
  }), [selectedCategories, selectedSources, sources, strategy])

  const applyMutation = useMutation({
    // Takes the strategy as an argument so a resolution click cannot race the
    // re-render: `setStrategy(...)` then `mutate()` in one handler would send
    // the payload memoized for the PREVIOUS strategy and silently repeat the
    // unresolved import.
    mutationFn: (override?: AgentImportConflictStrategy) =>
      api.onboardingImportApply(
        override ? { ...applyPayload, conflict_strategy: override } : applyPayload,
      ),
    onSuccess: () => setStage(4),
  })
  const completionMutation = useMutation({
    mutationFn: () => api.onboardingImportState({ completed: true }),
    onSuccess: () => {
      setOpen(false)
      onComplete()
    },
  })
  const skipAllMutation = useMutation({
    mutationFn: () => api.onboardingImportState({ completed: true }),
    onSuccess: () => {
      setOpen(false)
      if (onSkipAll) onSkipAll()
      else onComplete()
    },
  })

  useEffect(() => {
    if (
      !open
      || !scanQuery.data
      || scanQuery.isError
      || sources.length > 0
      || completionMutation.isPending
      || completionMutation.isSuccess
      || completionMutation.isError
    ) return
    completionMutation.mutate()
  }, [
    completionMutation,
    open,
    scanQuery.data,
    scanQuery.isError,
    sources.length,
  ])

  useEffect(() => {
    if (initialOpen && !previousInitialOpenRef.current) {
      resetFlow(true)
      setOpen(true)
    } else if (!initialOpen && previousInitialOpenRef.current) {
      setOpen(false)
    }
    previousInitialOpenRef.current = initialOpen
    // `initialOpen` is an opening signal. Replay is handled independently.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialOpen])

  useEffect(() => {
    if (
      !scanQuery.data
      || initializedScanGenerationRef.current === scanGeneration
    ) return
    const detected = eligibleSources(scanQuery.data.sources)
    setSelectedSources(new Set(detected.map(source => source.id)))
    setSelectedCategories(Object.fromEntries(
      detected.map(source => [
        source.id,
        new Set(supportedCategories(source).map(category => category.id)),
      ]),
    ))
    initializedScanGenerationRef.current = scanGeneration
  }, [scanGeneration, scanQuery.data])

  useEffect(() => {
    if (!open) return
    previousFocusRef.current = document.activeElement as HTMLElement | null
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = previousOverflow
      previousFocusRef.current?.focus()
    }
  }, [open])

  useEffect(() => {
    if (open) headingRef.current?.focus()
    // shellHost?.sectionSlot: in host mode the heading is portaled a pass after
    // open, so re-run once the section slot exists to move initial focus to it.
  }, [open, stage, scanQuery.isPending, scanQuery.isError, applyMutation.status, shellHost?.sectionSlot])

  const skip = () => {
    if (!completionMutation.isPending && !applyMutation.isPending && !skipAllMutation.isPending) {
      completionMutation.mutate()
    }
  }
  const skipAll = () => {
    if (!completionMutation.isPending && !applyMutation.isPending && !skipAllMutation.isPending) {
      skipAllMutation.mutate()
    }
  }

  useEffect(() => {
    if (!open) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        // Escape means SKIP ALL, not "skip import": one keystroke abandons the
        // whole of first run, exactly like the header control. It still cannot
        // bypass the mandatory Privacy chapter — that is the host's job, and
        // both exits route through it (see App.tsx).
        skipAll()
        return
      }
      if (event.key !== 'Tab') return
      const focusable = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
      ) ?? []).filter(element => element.getAttribute('aria-hidden') !== 'true')
      if (focusable.length === 0) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      const activeIndex = focusable.indexOf(document.activeElement as HTMLElement)
      if (event.shiftKey && (activeIndex <= 0)) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && (activeIndex < 0 || activeIndex === focusable.length - 1)) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
    // `skipAll` intentionally follows the current mutation state.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, completionMutation.isPending, applyMutation.isPending, skipAllMutation.isPending])

  if (!open) return null

  const toggleSource = (id: string) => {
    setSelectedSources(current => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }
  const toggleCategory = (sourceId: string, categoryId: string) => {
    setSelectedCategories(current => {
      const categories = new Set(current[sourceId] ?? [])
      if (categories.has(categoryId)) categories.delete(categoryId)
      else categories.add(categoryId)
      return { ...current, [sourceId]: categories }
    })
  }
  const selectedItemCount = applyPayload.sources.reduce(
    (total, selection) => total + selection.categories.reduce((sourceTotal, categoryId) => {
      const source = sources.find(candidate => candidate.id === selection.id)
      const category = source?.categories.find(candidate => candidate.id === categoryId)
      return sourceTotal + (category?.count ?? 0)
    }, 0),
    0,
  )
  const beginImport = () => {
    applyMutation.mutate(undefined)
  }
  const importAnother = () => resetFlow(true)
  // Both mutations PUT the identical completed-onboarding payload to the same
  // endpoint, so one failure message covers them and every existing render site
  // reports either. Without this a rejected write on the "Skip all" path (the
  // header control, or Escape) would leave the dialog open with no feedback at
  // all — the user presses Escape, nothing happens, and nothing says why.
  const completionError = completionMutation.error ?? skipAllMutation.error
  const isBusy = completionMutation.isPending || applyMutation.isPending || skipAllMutation.isPending
  const hasSelection = applyPayload.sources.length > 0

  // Per-stage navigation buttons, rendered in the shared pinned footer bar
  // (right side). The footer's left side shows the stage indicator.
  const stepFooterNav = (() => {
    if (stage === 1) {
      return (
        <>
          <Btn type="button" className="h-9 rounded-lg px-4" disabled={isBusy} onClick={skip}>{i18nT('components.agentImportFlow.skip_import')}</Btn>
          <SendBtn
            type="button"
            disabled={selectedSources.size === 0}
            onClick={() => setStage(2)}
          >
            {i18nT('components.agentImportFlow.continue')}
          </SendBtn>
        </>
      )
    }
    if (stage === 2) {
      return (
        <>
          <Btn type="button" className="h-9 rounded-lg px-4" onClick={() => setStage(1)}>
            {i18nT('components.agentImportFlow.back')}
          </Btn>
          <SendBtn type="button" disabled={!hasSelection} onClick={() => setStage(3)}>
            {i18nT('components.agentImportFlow.continue')}
          </SendBtn>
        </>
      )
    }
    if (stage === 3) {
      return (
        <>
          <Btn type="button" className="h-9 rounded-lg px-4" disabled={applyMutation.isPending} onClick={() => setStage(2)}>
            {i18nT('components.agentImportFlow.back')}
          </Btn>
          <SendBtn type="button" disabled={applyMutation.isPending} onClick={beginImport}>
            {applyMutation.isPending ? <ImportingLabel /> : i18nT('components.agentImportFlow.import_selected')}
          </SendBtn>
        </>
      )
    }
    return (
      <>
        <Btn type="button" className="h-9 rounded-lg px-4" disabled={completionMutation.isPending} onClick={importAnother}>
          <RefreshCw className="lucide-inline" /> {i18nT('components.agentImportFlow.import_another')}
        </Btn>
        <SendBtn
          type="button"
          disabled={completionMutation.isPending}
          onClick={() => completionMutation.mutate()}
        >
          {completionMutation.isPending && <Loader2 className="lucide-inline animate-spin" />}
          {i18nT('components.agentImportFlow.continue')}
        </SendBtn>
      </>
    )
  })()

  // The stepper footer shows for the four real stages, but not for the
  // full-panel scanning / error / empty states or the mid-import spinner.
  const showStepFooter =
    !scanQuery.isPending
    && !scanQuery.isError
    && sources.length > 0

  // Stage title + description, rendered in the FIXED header region (not the
  // scroll body) so they stay put while the stage content scrolls. Returns null
  // for the results stage and the full-panel scanning / error / empty states,
  // which carry their own centered headings in the body.
  const stageHeader = (() => {
    if (!showStepFooter) return null
    const headings: Record<Stage, { title: string; description: string }> = {
      1: {
        title: i18nT('components.agentImportFlow.choose_sources'),
        description: i18nT('components.agentImportFlow.kiro_crew_found_agent_setup_on_this_gateway_host'),
      },
      2: {
        title: i18nT('components.agentImportFlow.select_items_to_import'),
        description: i18nT('components.agentImportFlow.import_all_your_supported_work_or_handpick_what'),
      },
      3: {
        title: i18nT('components.agentImportFlow.review_import'),
        description: i18nT('components.agentImportFlow.review_the_merge_before_kiro_crew_changes_local'),
      },
      4: {
        title: i18nT('components.agentImportFlow.import_complete'),
        description: i18nT('components.agentImportFlow.your_selected_setup_is_ready_in_kiro_crew'),
      },
    }
    const { title, description } = headings[stage]
    return (
      <div className="mt-6">
        <h1 ref={headingRef} tabIndex={-1} className="text-2xl font-semibold text-text-strong outline-none">
          {title}
        </h1>
        <p className="mt-2 text-sm leading-relaxed text-muted">{description}</p>
      </div>
    )
  })()

  const stageContent = (() => {
    if (scanQuery.isPending) {
      return (
        <div className="flex min-h-[360px] flex-col items-center justify-center text-center">
          <Loader2 className="lucide-inline animate-spin text-accent" />
          <h1 ref={headingRef} tabIndex={-1} className="mt-4 text-2xl font-semibold text-text-strong outline-none">
            {i18nT('components.agentImportFlow.scanning_for_agent_setup')}
          </h1>
          <p className="mt-2 text-sm text-muted">{i18nT('components.agentImportFlow.checking_supported_tools_on_this_gateway_host')}</p>
        </div>
      )
    }
    if (scanQuery.isError) {
      return (
        <div className="flex min-h-[360px] flex-col items-center justify-center text-center">
          <AlertTriangle className="lucide-inline text-danger" />
          <h1 ref={headingRef} tabIndex={-1} className="mt-4 text-2xl font-semibold text-text-strong outline-none">
            {i18nT('components.agentImportFlow.we_could_not_scan_agent_setup')}
          </h1>
          <p className="mt-2 max-w-lg text-sm text-danger" role="alert">
            {errorMessage(scanQuery.error, i18nT('components.agentImportFlow.the_gateway_returned_an_unexpected_error'))}
          </p>
          <SendBtn type="button" className="mt-5" onClick={() => scanQuery.refetch()}>
            <RefreshCw className="lucide-inline" /> {i18nT('components.agentImportFlow.try_again')}
          </SendBtn>
        </div>
      )
    }
    if (sources.length === 0) {
      return (
        <div className="flex min-h-[360px] flex-col items-center justify-center text-center">
          <FileSearch className="lucide-inline text-muted" />
          <h1 ref={headingRef} tabIndex={-1} className="mt-4 text-2xl font-semibold text-text-strong outline-none">
            {i18nT('components.agentImportFlow.no_supported_setup_found')}
          </h1>
          <p className="mt-2 max-w-lg text-sm leading-relaxed text-muted">
            {i18nT('components.agentImportFlow.kirocrew_did_not_find_supported_setup_to_import')}
          </p>
          <SendBtn
            type="button"
            className="mt-5"
            disabled={isBusy}
            onClick={skip}
          >
            {isBusy && <Loader2 className="lucide-inline animate-spin" />}
            {i18nT('components.agentImportFlow.skip_import')}
          </SendBtn>
          {completionError && (
            <p className="mt-4 text-sm text-danger" role="alert">
              {errorMessage(completionError, i18nT('components.agentImportFlow.could_not_save_onboarding_state'))}
            </p>
          )}
        </div>
      )
    }
    if (stage === 1) {
      return (
        <>
          {completionError && (
            <p className="mb-4 text-sm text-danger" role="alert">
              {errorMessage(completionError, i18nT('components.agentImportFlow.could_not_save_onboarding_state'))}
            </p>
          )}
          <div className="space-y-3">
            {sources.map(source => {
              const count = supportedCategories(source).reduce((sum, category) => sum + category.count, 0)
              const inputId = `agent-import-source-${source.id}`
              return (
                <label
                  key={source.id}
                  htmlFor={inputId}
                    className={`flex cursor-pointer items-start gap-3 rounded-xl border p-4 transition-colors ${
                      selectedSources.has(source.id)
                        ? 'border-accent bg-accent-subtle shadow-sm'
                        : 'border-border bg-bg hover:border-accent/50'
                    }`}
                  >
                  <input
                    id={inputId}
                    type="checkbox"
                    aria-label={`${source.name}, ${count} items`}
                    className="mt-1 h-4 w-4 accent-[var(--accent)]"
                    checked={selectedSources.has(source.id)}
                    onChange={() => toggleSource(source.id)}
                  />
                  <span className="min-w-0 flex-1">
                    <span className="block font-semibold text-text-strong">{source.name}</span>
                    {source.detail && <span className="mt-1 block break-words text-[13px] text-muted">{source.detail}</span>}
                  </span>
                  <span className="shrink-0 text-[13px] font-medium text-muted">{count} {i18nT('components.agentImportFlow.found')}</span>
                </label>
              )
            })}
          </div>
        </>
      )
    }
    if (stage === 2) {
      return (
        <>
          <div className="space-y-5">
            {sources.filter(source => selectedSources.has(source.id)).map(source => (
              <fieldset key={source.id} aria-label={`${source.name} categories`}>
                <legend className="mb-2 text-sm font-semibold text-text-strong">{source.name}</legend>
                <div className="divide-y divide-border overflow-hidden rounded-xl border border-border bg-bg shadow-sm">
                  {supportedCategories(source).map(category => {
                    const CategoryIcon = CATEGORY_ICONS[category.id] ?? Settings2
                    return (
                      <label
                        key={category.id}
                        htmlFor={`agent-import-category-${source.id}-${category.id}`}
                        className="flex cursor-pointer items-start gap-3 p-4 transition-colors hover:bg-bg-hover"
                      >
                        <CategoryIcon className="lucide-inline mt-0.5 shrink-0 text-muted" />
                        <span className="min-w-0 flex-1">
                          <span className="block font-medium text-text">
                            {category.label} ({category.count})
                          </span>
                          {category.description && (
                            <span className="mt-1 block text-[13px] leading-relaxed text-muted">
                              {category.description}
                            </span>
                          )}
                        </span>
                        <input
                          id={`agent-import-category-${source.id}-${category.id}`}
                          type="checkbox"
                          aria-label={`${category.label}, ${category.count}`}
                          className="mt-1 h-4 w-4 shrink-0 accent-[var(--accent)]"
                          checked={selectedCategories[source.id]?.has(category.id) ?? false}
                          onChange={() => toggleCategory(source.id, category.id)}
                        />
                      </label>
                    )
                  })}
                </div>
              </fieldset>
            ))}
          </div>
          <p className="mt-5 text-center text-[13px] text-muted">
            {i18nT('components.agentImportFlow.your_existing_kirocrew_setup_will_not_be_affecte')}
          </p>
        </>
      )
    }
    if (stage === 3) {
      return (
        <>
          {applyMutation.isError && (
            <p className="mb-4 text-sm text-danger" role="alert">
              {errorMessage(applyMutation.error, i18nT('components.agentImportFlow.the_import_did_not_finish_please_try_again'))}
            </p>
          )}
          <section className="rounded-lg border border-ok/30 bg-ok-subtle p-4">
            <h2 className="flex items-center gap-2 text-sm font-semibold text-text-strong">
              <ShieldCheck className="lucide-inline text-ok" /> {i18nT('components.agentImportFlow.merge_only')}
            </h2>
            <p className="mt-2 text-sm leading-relaxed text-muted">
              {i18nT('components.agentImportFlow.existing_kirocrew_setup_is_never_overwritten_mat')}
            </p>
          </section>
          <section className="mt-6">
            <h2 className="text-sm font-semibold text-text-strong">{i18nT('components.agentImportFlow.selected_setup')}</h2>
            <div className="mt-2 divide-y divide-border rounded-xl border border-border bg-bg">
              {applyPayload.sources.map(selection => {
                const source = sources.find(candidate => candidate.id === selection.id)
                return (
                  <div key={selection.id} className="flex items-start justify-between gap-4 p-4">
                    <div>
                      <p className="font-medium text-text">{source?.name ?? selection.id}</p>
                      <p className="mt-1 text-[13px] text-muted">
                        {selection.categories.map(categoryId =>
                          source?.categories.find(category => category.id === categoryId)?.label ?? categoryId,
                        ).join(', ')}
                      </p>
                    </div>
                    <span className="shrink-0 text-[13px] font-medium text-muted">
                      {selection.categories.length} {selection.categories.length === 1 ? 'category' : 'categories'}
                    </span>
                  </div>
                )
              })}
            </div>
            <p className="mt-2 text-[13px] text-muted">{selectedItemCount} {i18nT('components.agentImportFlow.items_selected')}</p>
          </section>
          <section className="mt-6">
            <h2 className="text-sm font-semibold text-text-strong">{i18nT('components.agentImportFlow.not_imported')}</h2>
            <p className="mt-2 text-[13px] leading-relaxed text-muted">
              {i18nT('components.agentImportFlow.credentials_remain_with_their_source_rules_hooks')}
            </p>
            {(scanQuery.data?.skipped?.length ?? 0) > 0 && (
              <div className="mt-3 divide-y divide-border rounded-xl border border-border bg-bg">
                {scanQuery.data?.skipped?.map((item, index) => (
                  <div key={`${item.source}-${item.category}-${index}`} className="flex items-start justify-between gap-4 p-3">
                    <div>
                      <p className="text-[13px] font-medium text-text">
                        {item.source}: {item.category}
                      </p>
                      <p className="mt-1 text-[13px] text-muted">{item.reason}</p>
                    </div>
                    {item.count !== undefined && <span className="font-mono text-[13px] text-muted">{item.count}</span>}
                  </div>
                ))}
              </div>
            )}
          </section>
        </>
      )
    }

    const summary = applyMutation.data?.summary
    return (
      <>
        <dl className="grid grid-cols-1 divide-y divide-border rounded-xl border border-border bg-bg sm:grid-cols-3 sm:divide-x sm:divide-y-0">
          <div className="p-4 text-center">
            <dt className="text-[13px] text-muted">{i18nT('components.agentImportFlow.imported')}</dt>
            <dd className="mt-1 text-2xl font-semibold text-text-strong">{summary?.imported ?? 0}</dd>
          </div>
          <div className="p-4 text-center">
            <dt className="text-[13px] text-muted">{i18nT('components.agentImportFlow.deduplicated')}</dt>
            <dd className="mt-1 text-2xl font-semibold text-text-strong">{summary?.deduplicated ?? 0}</dd>
          </div>
          <div className="p-4 text-center">
            <dt className="text-[13px] text-muted">{i18nT('components.agentImportFlow.skipped')}</dt>
            <dd className="mt-1 text-2xl font-semibold text-text-strong">{summary?.skipped ?? 0}</dd>
          </div>
        </dl>
        {!!summary?.resolvable_conflicts && (
          <div
            className="mt-5 rounded-lg border border-warn/30 bg-warn/10 p-3 text-sm"
            role="status"
          >
            <p className="font-medium text-text-strong">
              {summary.resolvable_conflicts === 1
                ? i18nT('components.agentImportFlow.one_item_already_exists_with_different_content')
                : `${summary.resolvable_conflicts} ${i18nT(
                  'components.agentImportFlow.items_already_exist_with_different_content',
                )}`}
            </p>
            <p className="mt-1 text-[13px] text-muted">
              {i18nT('components.agentImportFlow.nothing_was_changed_import_the_new_copy_alongsid')}
            </p>
            <div className="mt-3 flex flex-wrap gap-2">
              <Btn
                type="button"
                disabled={isBusy}
                onClick={() => { setStrategy('rename'); applyMutation.mutate('rename') }}
              >
                {i18nT('components.agentImportFlow.keep_both')}
              </Btn>
              <Btn
                type="button"
                disabled={isBusy}
                onClick={() => { setStrategy('overwrite'); applyMutation.mutate('overwrite') }}
              >
                {i18nT('components.agentImportFlow.replace_mine')}
              </Btn>
            </div>
          </div>
        )}
        {completionError && (
          <div className="mt-5 rounded-lg border border-danger/20 bg-danger/10 p-3 text-sm text-danger" role="alert">
            {errorMessage(completionError, i18nT('components.agentImportFlow.could_not_save_onboarding_state'))}
          </div>
        )}
      </>
    )
  })()

  return (
    <OnboardingChapterShell
      dialogRef={dialogRef}
      {...setupChapterAside(i18nT('components.agentImportFlow.import_agent_setup'))}
      eyebrow={
        <>
          {i18nT('components.agentImportFlow.import_setup')}
          {showStepFooter && ` · ${stage} of ${STAGES.length}`}
        </>
      }
      onSkipAll={skipAll}
      skipDisabled={isBusy}
      header={stageHeader}
      footer={showStepFooter ? stepFooterNav : undefined}
    >
      {stageContent}
    </OnboardingChapterShell>
  )
}
