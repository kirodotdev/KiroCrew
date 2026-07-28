import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  BookOpen,
  Brain,
  CalendarClock,
  Check,
  CheckCircle2,
  CopyCheck,
  FileSearch,
  FolderOpen,
  Import,
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
import { GhostVar1, GhostVar2 } from '../assets/onboarding/GhostIcons'
import { Btn, SendBtn } from './ui'

type Stage = 1 | 2 | 3 | 4

const STAGE_LABELS = ['Sources', 'Categories', 'Review', 'Results']
const SUPPORTED_SOURCE_IDS = new Set([
  'codex',
  'claude_code',
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

export default function AgentImportFlow({
  initialOpen,
  onComplete,
}: {
  initialOpen: boolean
  onComplete: () => void
}) {
  const [open, setOpen] = useState(initialOpen)
  const [stage, setStage] = useState<Stage>(1)
  const [scanGeneration, setScanGeneration] = useState(0)
  const [selectedSources, setSelectedSources] = useState<Set<string>>(new Set())
  const [strategy, setStrategy] = useState<AgentImportConflictStrategy>('skip')
  const [selectedCategories, setSelectedCategories] = useState<Record<string, Set<string>>>({})
  const dialogRef = useRef<HTMLDivElement>(null)
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
    mutationFn: () => api.onboardingImportApply(applyPayload),
  })
  const completionMutation = useMutation({
    mutationFn: () => api.onboardingImportState({ completed: true }),
    onSuccess: () => {
      setOpen(false)
      onComplete()
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
  }, [open, stage, scanQuery.isPending, scanQuery.isError, applyMutation.status])

  const skip = () => {
    if (!completionMutation.isPending && !applyMutation.isPending) {
      completionMutation.mutate()
    }
  }

  useEffect(() => {
    if (!open) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        skip()
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
    // `skip` intentionally follows the current mutation state.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, completionMutation.isPending, applyMutation.isPending])

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
    setStage(4)
    applyMutation.mutate()
  }
  const importAnother = () => resetFlow(true)
  const completionError = completionMutation.error
  const isBusy = completionMutation.isPending || applyMutation.isPending

  const stageContent = (() => {
    if (scanQuery.isPending) {
      return (
        <div className="flex min-h-[360px] flex-col items-center justify-center text-center">
          <Loader2 className="lucide-inline animate-spin text-accent" />
          <h1 ref={headingRef} tabIndex={-1} className="mt-4 text-2xl font-semibold text-text-strong outline-none">
            Scanning for agent setup
          </h1>
          <p className="mt-2 text-sm text-muted">Checking supported tools on this gateway host.</p>
        </div>
      )
    }
    if (scanQuery.isError) {
      return (
        <div className="flex min-h-[360px] flex-col items-center justify-center text-center">
          <AlertTriangle className="lucide-inline text-danger" />
          <h1 ref={headingRef} tabIndex={-1} className="mt-4 text-2xl font-semibold text-text-strong outline-none">
            We could not scan agent setup
          </h1>
          <p className="mt-2 max-w-lg text-sm text-danger" role="alert">
            {errorMessage(scanQuery.error, 'The gateway returned an unexpected error.')}
          </p>
          <SendBtn type="button" className="mt-5" onClick={() => scanQuery.refetch()}>
            <RefreshCw className="lucide-inline" /> Try again
          </SendBtn>
        </div>
      )
    }
    if (sources.length === 0) {
      return (
        <div className="flex min-h-[360px] flex-col items-center justify-center text-center">
          <FileSearch className="lucide-inline text-muted" />
          <h1 ref={headingRef} tabIndex={-1} className="mt-4 text-2xl font-semibold text-text-strong outline-none">
            No supported setup found
          </h1>
          <p className="mt-2 max-w-lg text-sm leading-relaxed text-muted">
            KiroCrew did not find supported setup to import on this gateway host.
          </p>
          <SendBtn
            type="button"
            className="mt-5"
            disabled={isBusy}
            onClick={skip}
          >
            {isBusy && <Loader2 className="lucide-inline animate-spin" />}
            Skip import
          </SendBtn>
          {completionError && (
            <p className="mt-4 text-sm text-danger" role="alert">
              {errorMessage(completionError, 'Could not save onboarding state.')}
            </p>
          )}
        </div>
      )
    }
    if (stage === 1) {
      return (
        <>
          <h1 ref={headingRef} tabIndex={-1} className="text-2xl font-semibold text-text-strong outline-none">
            Choose sources
          </h1>
          <p className="mt-2 text-sm leading-relaxed text-muted">
            KiroCrew found agent setup on this gateway host. Select the sources to review.
          </p>
          {completionError && (
            <p className="mt-4 text-sm text-danger" role="alert">
              {errorMessage(completionError, 'Could not save onboarding state.')}
            </p>
          )}
          <div className="mt-6 space-y-3">
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
                  <span className="shrink-0 text-[13px] font-medium text-muted">{count} found</span>
                </label>
              )
            })}
          </div>
          <div className="mt-7 flex flex-wrap items-center justify-between gap-3 border-t border-border pt-5">
            <Btn type="button" disabled={isBusy} onClick={skip}>Skip for now</Btn>
            <SendBtn
              type="button"
              disabled={selectedSources.size === 0}
              onClick={() => setStage(2)}
            >
              Choose categories <ArrowRight className="lucide-inline" />
            </SendBtn>
          </div>
        </>
      )
    }
    if (stage === 2) {
      const hasSelection = applyPayload.sources.length > 0
      return (
        <>
          <div className="mb-8 flex items-center justify-center gap-5" aria-hidden="true">
            <span className="flex h-16 w-16 items-center justify-center rounded-lg bg-accent text-accent-fg shadow-sm">
              <FolderOpen className="lucide-inline" />
            </span>
            <ArrowRight className="lucide-inline text-muted" />
            <span className="flex h-16 w-16 items-center justify-center rounded-lg border border-border bg-card text-text-strong shadow-sm">
              <CopyCheck className="lucide-inline" />
            </span>
          </div>
          <h1 ref={headingRef} tabIndex={-1} className="text-center text-4xl font-semibold text-text-strong outline-none">
            Select items to import
          </h1>
          <p className="mt-3 text-center text-base leading-relaxed text-muted">
            Import all your supported work or handpick what to bring over.
          </p>
          <div className="mt-10 space-y-5">
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
            Your existing KiroCrew setup will not be affected.
          </p>
          <div className="mt-7 flex items-center justify-between gap-3 border-t border-border pt-5">
            <Btn type="button" onClick={() => setStage(1)}>
              <ArrowLeft className="lucide-inline" /> Back
            </Btn>
            <SendBtn type="button" disabled={!hasSelection} onClick={() => setStage(3)}>
              Review import <ArrowRight className="lucide-inline" />
            </SendBtn>
          </div>
        </>
      )
    }
    if (stage === 3) {
      return (
        <>
          <h1 ref={headingRef} tabIndex={-1} className="text-2xl font-semibold text-text-strong outline-none">
            Review import
          </h1>
          <p className="mt-2 text-sm leading-relaxed text-muted">
            Review the merge before KiroCrew changes local setup.
          </p>
          <section className="mt-6 rounded-lg border border-ok/30 bg-ok-subtle p-4">
            <h2 className="flex items-center gap-2 text-sm font-semibold text-text-strong">
              <ShieldCheck className="lucide-inline text-ok" /> Merge only
            </h2>
            <p className="mt-2 text-sm leading-relaxed text-muted">
              Existing KiroCrew setup is never overwritten. Matching items are deduplicated,
              and conflicts keep the current KiroCrew version.
            </p>
          </section>
          <section className="mt-6">
            <h2 className="text-sm font-semibold text-text-strong">Selected setup</h2>
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
            <p className="mt-2 text-[13px] text-muted">{selectedItemCount} items selected</p>
          </section>
          <section className="mt-6">
            <h2 className="text-sm font-semibold text-text-strong">Not imported</h2>
            <p className="mt-2 text-[13px] leading-relaxed text-muted">
              Credentials remain with their source. Rules, hooks, agents or personas, and
              raw instructions are unsupported and stay unchanged.
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
          <div className="mt-7 flex items-center justify-between gap-3 border-t border-border pt-5">
            <Btn type="button" onClick={() => setStage(2)}>
              <ArrowLeft className="lucide-inline" /> Back
            </Btn>
            <SendBtn type="button" onClick={beginImport}>
              <Import className="lucide-inline" /> Import selected
            </SendBtn>
          </div>
        </>
      )
    }

    if (applyMutation.isPending) {
      return (
        <div className="flex min-h-[360px] flex-col items-center justify-center text-center" aria-live="polite">
          <Loader2 className="lucide-inline animate-spin text-accent" />
          <h1 ref={headingRef} tabIndex={-1} className="mt-4 text-2xl font-semibold text-text-strong outline-none">
            Importing setup
          </h1>
          <p className="mt-2 text-sm text-muted">Merging selected items into KiroCrew.</p>
        </div>
      )
    }
    if (applyMutation.isError) {
      return (
        <div className="flex min-h-[360px] flex-col items-center justify-center text-center">
          <AlertTriangle className="lucide-inline text-danger" />
          <h1 ref={headingRef} tabIndex={-1} className="mt-4 text-2xl font-semibold text-text-strong outline-none">
            Import did not finish
          </h1>
          <p className="mt-3 max-w-lg text-sm text-danger" role="alert">
            {errorMessage(applyMutation.error, 'The gateway returned an unexpected error.')}
          </p>
          <div className="mt-5 flex flex-wrap justify-center gap-2">
            <Btn type="button" onClick={() => setStage(3)}>
              <ArrowLeft className="lucide-inline" /> Back to review
            </Btn>
            <SendBtn type="button" onClick={() => applyMutation.mutate()}>
              <RefreshCw className="lucide-inline" /> Retry import
            </SendBtn>
          </div>
        </div>
      )
    }

    const summary = applyMutation.data?.summary
    return (
      <>
        <div className="text-center">
          <CheckCircle2 className="lucide-inline text-ok" />
          <h1 ref={headingRef} tabIndex={-1} className="mt-4 text-2xl font-semibold text-text-strong outline-none">
            Import complete
          </h1>
          <p className="mt-2 text-sm text-muted">Your selected setup is ready in KiroCrew.</p>
        </div>
        <dl className="mt-7 grid grid-cols-1 divide-y divide-border rounded-xl border border-border bg-bg sm:grid-cols-3 sm:divide-x sm:divide-y-0">
          <div className="p-4 text-center">
            <dt className="text-[13px] text-muted">Imported</dt>
            <dd className="mt-1 text-2xl font-semibold text-text-strong">{summary?.imported ?? 0}</dd>
          </div>
          <div className="p-4 text-center">
            <dt className="text-[13px] text-muted">Deduplicated</dt>
            <dd className="mt-1 text-2xl font-semibold text-text-strong">{summary?.deduplicated ?? 0}</dd>
          </div>
          <div className="p-4 text-center">
            <dt className="text-[13px] text-muted">Skipped</dt>
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
                ? '1 item already exists with different content.'
                : `${summary.resolvable_conflicts} items already exist with different content.`}
            </p>
            <p className="mt-1 text-[13px] text-muted">
              Nothing was changed. Import the new copy alongside yours, or replace yours
              (a restore copy is kept on the gateway).
            </p>
            <div className="mt-3 flex flex-wrap gap-2">
              <Btn
                type="button"
                disabled={isBusy}
                onClick={() => { setStrategy('rename'); applyMutation.mutate() }}
              >
                Keep both
              </Btn>
              <Btn
                type="button"
                disabled={isBusy}
                onClick={() => { setStrategy('overwrite'); applyMutation.mutate() }}
              >
                Replace mine
              </Btn>
            </div>
          </div>
        )}
        {completionError && (
          <div className="mt-5 rounded-lg border border-danger/20 bg-danger/10 p-3 text-sm text-danger" role="alert">
            {errorMessage(completionError, 'Could not save onboarding state.')}
          </div>
        )}
        <div className="mt-7 flex flex-wrap items-center justify-between gap-3 border-t border-border pt-5">
          <Btn type="button" disabled={completionMutation.isPending} onClick={importAnother}>
            <RefreshCw className="lucide-inline" /> Import another
          </Btn>
          <SendBtn
            type="button"
            disabled={completionMutation.isPending}
            onClick={() => completionMutation.mutate()}
          >
            {completionMutation.isPending
              ? <Loader2 className="lucide-inline animate-spin" />
              : <Check className="lucide-inline" />}
            Continue
          </SendBtn>
        </div>
      </>
    )
  })()

  return createPortal(
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-label="Import agent setup"
      className="fixed inset-0 z-[140] flex min-h-0 overflow-y-auto bg-bg-elevated p-0 text-text sm:items-center sm:justify-center sm:p-6"
    >
      <div className="relative flex min-h-screen w-full flex-col overflow-hidden bg-card shadow-xl sm:min-h-[min(760px,calc(100vh-48px))] sm:max-w-6xl sm:flex-row sm:rounded-2xl sm:border sm:border-border">
        <aside className="relative flex min-h-[248px] w-full shrink-0 overflow-hidden bg-accent text-accent-fg sm:min-h-0 sm:w-[36%]">
          <div className="pointer-events-none absolute inset-0 opacity-90" aria-hidden="true">
            <div className="absolute -left-12 top-[-72px]">
              <GhostVar1 width={170} />
            </div>
            <div className="absolute -right-10 top-10">
              <GhostVar2 width={150} />
            </div>
            <div className="absolute -bottom-16 -right-16">
              <GhostVar1 width={190} />
            </div>
            <div className="absolute -bottom-10 -left-14">
              <GhostVar2 width={124} />
            </div>
          </div>
          <div className="relative z-10 flex w-full flex-col p-7 sm:p-10">
            <div className="flex items-center gap-3">
              <GhostVar1 width={32} />
              <span className="text-[15px] font-semibold tracking-wide">Kiro Crew</span>
            </div>
            <div className="mt-auto max-w-[290px]">
              <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-fg/75">
                Import setup
              </p>
              <h1 className="mt-3 text-4xl font-semibold leading-[1.05] tracking-[-0.02em] sm:text-[clamp(2.2rem,4vw,3.5rem)]">
                Bring your crew with you.
              </h1>
              <p className="mt-5 max-w-[270px] text-sm leading-relaxed text-accent-fg/80">
                Move supported instructions, memories, workspaces, MCP servers,
                skills, schedules, and safe settings into KiroCrew.
              </p>
            </div>
            <p className="mt-8 text-[12px] font-medium text-accent-fg/75">
              Merge-only setup · credentials stay where they are
            </p>
          </div>
        </aside>
        <section className="flex min-h-[calc(100vh-248px)] min-w-0 flex-1 flex-col bg-card sm:min-h-0">
          <header className="flex shrink-0 items-start justify-between gap-4 px-6 pb-0 pt-7 sm:px-10 sm:pt-10">
            <div>
              <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-accent">
                Setup <span className="px-1 text-muted">→</span> Import
              </p>
              <p className="mt-2 text-[13px] text-muted">
                Bring supported setup into your KiroCrew workspace.
              </p>
            </div>
            <div className="flex shrink-0 flex-col items-end gap-2">
              <span className="text-[12px] font-medium text-muted">
                {stage} / {STAGE_LABELS.length} · {STAGE_LABELS[stage - 1]}
              </span>
              <Btn
                type="button"
                aria-label="Skip import and close"
                disabled={isBusy}
                onClick={skip}
              >
                Skip
              </Btn>
            </div>
          </header>
          <div className="min-h-0 flex-1 overflow-y-auto">
            <main className="w-full px-6 py-8 sm:px-10 sm:py-10">
              <div className="mx-auto max-w-2xl">{stageContent}</div>
            </main>
          </div>
        </section>
      </div>
    </div>,
    document.body,
  )
}
