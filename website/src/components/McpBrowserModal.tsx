/**
 * McpBrowserModal — multi-provider MCP server discovery and installation.
 *
 * Opened by the "Add Server" button on the MCP page. Searches across all
 * available providers (official MCP registry, plus the edition capability
 * registry when one is installed) and displays
 * results in a two-pane layout: results list (left) + detail preview (right).
 * Keyboard-first: arrow keys move the selection, Enter installs.
 *
 * Install-only by design: uninstall stays in the installed-servers table.
 */
import { useState, useCallback, useRef, useEffect, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Download, Check, ExternalLink, Loader2, RefreshCw, AlertTriangle, ArrowLeft, KeyRound, Terminal } from 'lucide-react'
import { api, ApiError } from '../api/client'
import Modal from './Modal'
import { Btn } from './ui'
import MarkdownRenderer from './MarkdownRenderer'
import { DiscoverySearchBar, DiscoveryStates } from './DiscoverySearchBar'
import { safeHttpUrl } from '../lib/safeUrl'
import type { DiscoveredMcpServer, McpDiscoverDetail, McpInstallPlan } from '../types'

import { i18nT } from '../i18n/t'
interface Props {
  open: boolean
  onClose: () => void
}

/** Per-server install lifecycle for UI feedback. */
type InstallPhase =
  | { step: 'installing' }
  | { step: 'done'; name: string; requiredEnv: string[]; enabled: boolean }
  | { step: 'conflict' }
  | { step: 'error'; message: string }

/** Consent gate: installing is allowed only once the server's detail (the
 *  pane with the install-plan preview) has actually loaded — and, for
 *  registry entries, only when a plan exists (capability entries install
 *  through the edition manager and legitimately have a null plan). */
function installReadyFor(
  server: DiscoveredMcpServer,
  detail: McpDiscoverDetail | undefined,
): boolean {
  if (!detail) return false
  return server.provider !== 'official' || detail.install_plan != null
}

export default function McpBrowserModal({ open, onClose }: Props) {
  const queryClient = useQueryClient()
  const [query, setQuery] = useState('')
  const [debouncedQuery, setDebouncedQuery] = useState('')
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // Install lifecycle per server key (`provider:id`).
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

  // The installed flag is derived server-side from KiroCrew's configured
  // servers on every search. Reset the session-local optimistic state and
  // refetch on each open so uninstalls made while the modal was closed are
  // reflected in both the results and the installed-servers table.
  useEffect(() => {
    if (open) {
      setInstalledOverride(new Set())
      setInstallPhases({})
      queryClient.invalidateQueries({ queryKey: ['mcp-discover'] })
      queryClient.invalidateQueries({ queryKey: ['mcp-servers'] })
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
    queryKey: ['mcp-discover', debouncedQuery],
    queryFn: () => api.mcpDiscover(debouncedQuery),
    enabled: open && debouncedQuery.length >= 2,
    retry: false,
    staleTime: 30_000,
  })

  const results = useMemo(() => data?.results ?? [], [data])
  const providers = data?.providers ?? []

  const serverKey = (s: DiscoveredMcpServer) => `${s.provider}:${s.id}`
  const selectedServer = results.find(s => serverKey(s) === selectedKey) ?? null
  const isInstalled = (s: DiscoveredMcpServer) => s.installed || installedOverride.has(serverKey(s))

  // Reset selection when the result set changes (new search). `data` is
  // referentially stable between renders (react-query cache), unlike the
  // derived `results` array which would churn this effect every render.
  useEffect(() => {
    const next = data?.results ?? []
    setSelectedKey(prev => (prev && next.some(s => serverKey(s) === prev) ? prev : null))
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
    mutationFn: (server: DiscoveredMcpServer) =>
      api.mcpDiscoverInstall(server.provider, server.id),
    onMutate: (server) => setPhase(serverKey(server), { step: 'installing' }),
    onSuccess: (result, server) => {
      const key = serverKey(server)
      setPhase(key, {
        step: 'done',
        name: result.name,
        requiredEnv: result.required_env ?? [],
        // Absent (capability installs) means enabled; official installs
        // with required env land disabled until the user configures them.
        enabled: result.enabled !== false,
      })
      setInstalledOverride(prev => new Set(prev).add(key))
      queryClient.invalidateQueries({ queryKey: ['mcp-servers'] })
      queryClient.invalidateQueries({ queryKey: ['mcp-discover'] })
    },
    onError: (err, server) => {
      const key = serverKey(server)
      if (err instanceof ApiError && err.status === 409) {
        // Name collision with a different spec — no overwrite path in v1;
        // the user resolves it from the installed-servers table.
        setPhase(key, { step: 'conflict' })
      } else {
        setPhase(key, { step: 'error', message: err instanceof Error ? err.message : String(err) })
      }
    },
  })

  const handleInstall = useCallback((server: DiscoveredMcpServer) => {
    installMutation.mutate(server)
  }, [installMutation])

  // Keyboard navigation: ArrowDown/ArrowUp move selection; Enter installs
  // the selected server only once its detail (with the install-plan
  // preview) has loaded in the pane — installing a publisher-controlled
  // spec must never happen before the preview was available to read.
  const moveSelection = useCallback((delta: number) => {
    if (results.length === 0) return
    const idx = results.findIndex(s => serverKey(s) === selectedKey)
    const next = idx === -1
      ? (delta > 0 ? 0 : results.length - 1)
      : Math.min(Math.max(idx + delta, 0), results.length - 1)
    const key = serverKey(results[next])
    setSelectedKey(key)
    // Keep the active row visible in the scrollable list (guarded: jsdom
    // does not implement scrollIntoView).
    const el = listRef.current?.querySelector(`[data-server-key="${CSS.escape(key)}"]`)
    el?.scrollIntoView?.({ block: 'nearest' })
  }, [results, selectedKey])

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); moveSelection(1) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); moveSelection(-1) }
    else if (e.key === 'Enter' && selectedServer && !(selectedServer.installed || installedOverride.has(serverKey(selectedServer)))) {
      e.preventDefault()
      // Consent gate: only install once the detail query for the selected
      // server has resolved — i.e. the pane showing the install plan and
      // required env is actually rendered, not still loading.
      const detail = queryClient.getQueryData<McpDiscoverDetail>(
        ['mcp-discover-detail', selectedServer.provider, selectedServer.id],
      )
      if (!installReadyFor(selectedServer, detail)) return
      const phase = installPhases[serverKey(selectedServer)]
      if (!phase || phase.step === 'error') handleInstall(selectedServer)
    }
  }, [moveSelection, selectedServer, installedOverride, installPhases, handleInstall, queryClient])

  return (
    <Modal open={open} onClose={onClose} title={i18nT('components.mcpBrowserModal.add_server')} maxWidth={1100} height="85vh"
      headerActions={
        isFetching ? <RefreshCw size={14} className="text-muted animate-spin" /> : undefined
      }
    >
      <div className="flex flex-col h-full min-h-0">
        <DiscoverySearchBar
          ref={inputRef}
          idPrefix="mcp"
          subject="MCP servers"
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

        <DiscoveryStates debouncedQuery={debouncedQuery} isLoading={isLoading} resultCount={results.length} noun="servers" />

        {/* Two-pane on md+: results list (left) + detail preview (right).
            Single-pane below md: the list fills the modal; clicking a row
            swaps to the detail view with a Back button. */}
        {results.length > 0 && (
          <div className="flex gap-3 flex-1 min-h-0">
            <div
              ref={listRef}
              id="mcp-results-list"
              role="listbox"
              aria-label={i18nT('components.mcpBrowserModal.mcp_server_search_results')}
              className={`${selectedServer && mobileDetail ? 'hidden md:block' : 'block'} w-full md:w-[40%] md:shrink-0 overflow-y-auto scrollbar-overlay space-y-1.5 pr-1`}
            >
              {results.map(server => {
                const key = serverKey(server)
                const active = key === selectedKey
                const phase = installPhases[key]
                return (
                  <div
                    key={key}
                    id={`mcp-opt-${key}`}
                    data-server-key={key}
                    role="option"
                    aria-selected={active}
                    aria-label={server.name}
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
                            {server.title || server.name}
                          </span>
                          <span className="shrink-0 text-[10px] px-1.5 py-0.5 rounded-full bg-accent/15 text-accent font-medium">
                            {server.display_provider}
                          </span>
                          {server.deprecated && (
                            <span className="shrink-0 text-[10px] px-1.5 py-0.5 rounded-full bg-warn-subtle text-[var(--warn)] font-medium">
                              {i18nT('components.mcpBrowserModal.deprecated')}
                            </span>
                          )}
                        </div>
                        <div className="mt-0.5 flex items-center gap-2 text-xs text-muted">
                          <span className="font-mono shrink-0 truncate max-w-[45%]">{server.id}</span>
                          {server.description && (
                            <span className="truncate">{server.description}</span>
                          )}
                        </div>
                        {phase?.step === 'error' && (
                          <p className="mt-1 text-xs text-red-400">{phase.message}</p>
                        )}
                      </div>
                      <div className="shrink-0 mt-0.5">
                        {/* Status only — the Install action lives in the
                            detail pane so the install-plan preview is always
                            on screen before a spec can be written. */}
                        <InstallStatus
                          server={server}
                          installed={isInstalled(server)}
                          phase={phase}
                          onInstall={handleInstall}
                          readOnly
                        />
                      </div>
                    </div>
                  </div>
                )
              })}
            </div>

            <div className={`${selectedServer && mobileDetail ? 'flex' : 'hidden md:flex'} flex-col flex-1 min-w-0 min-h-0 md:border-l border-border md:pl-3`}>
              {selectedServer ? (
                <>
                  {/* Back button stays fixed above the scrollable detail content */}
                  <div className="md:hidden mb-2 shrink-0">
                    <Btn onClick={() => setMobileDetail(false)}>
                      <ArrowLeft size={14} aria-hidden="true" /> {i18nT('components.mcpBrowserModal.back_to_results')}
                    </Btn>
                  </div>
                  <div className="flex-1 min-h-0 overflow-y-auto scrollbar-overlay">
                    <ServerDetailPanel
                      server={selectedServer}
                      installed={isInstalled(selectedServer)}
                      phase={installPhases[serverKey(selectedServer)]}
                      onInstall={handleInstall}
                    />
                  </div>
                </>
              ) : (
                <div className="flex items-center justify-center h-full text-muted text-sm">
                  {i18nT('components.mcpBrowserModal.select_a_server_to_preview')}
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
  server,
  installed,
  phase,
  onInstall,
  large,
  readOnly,
  installReady = true,
}: {
  server: DiscoveredMcpServer
  installed: boolean
  phase: InstallPhase | undefined
  onInstall: (server: DiscoveredMcpServer) => void
  large?: boolean
  /** Status-only rendering (list rows): never shows the Install button. */
  readOnly?: boolean
  /** Consent gate: false disables the button until the plan preview loaded. */
  installReady?: boolean
}) {
  const iconSize = large ? 14 : 12

  if (phase?.step === 'installing') {
    return (
      <span className="flex items-center gap-1.5 text-xs text-muted" role="status">
        <Loader2 size={iconSize} className="animate-spin" aria-hidden="true" />
        {i18nT('components.mcpBrowserModal.installing')}
      </span>
    )
  }
  if (phase?.step === 'done') {
    // A disabled install (required env unset) is surfaced distinctly: the
    // user still has a config step before the server can run.
    return phase.enabled ? (
      <span className="flex items-center gap-1 text-xs text-green-400" role="status">
        <Check size={iconSize} aria-hidden="true" />
        {i18nT('components.mcpBrowserModal.installed')}
      </span>
    ) : (
      <span className="flex items-center gap-1 text-xs text-amber-400" role="status">
        <Check size={iconSize} aria-hidden="true" />
        {i18nT('components.mcpBrowserModal.installed_disabled')}
      </span>
    )
  }
  if (phase?.step === 'conflict') {
    // 409: a different server already uses this name. There is no overwrite
    // path here — the existing entry is managed from the installed table.
    return (
      <span className="flex items-center gap-1 text-xs text-amber-400" role="status">
        <AlertTriangle size={iconSize} aria-hidden="true" /> {i18nT('components.mcpBrowserModal.name_in_use')}
      </span>
    )
  }
  if (installed) {
    return (
      <span className="flex items-center gap-1 text-xs text-green-400">
        <Check size={iconSize} aria-hidden="true" /> {i18nT('components.mcpBrowserModal.installed')}
      </span>
    )
  }
  if (readOnly) return null
  return (
    <Btn
      primary={large}
      disabled={!installReady}
      title={installReady ? undefined : i18nT('components.mcpBrowserModal.waiting_for_the_install_plan_to_load')}
      onClick={(e: React.MouseEvent) => { e.stopPropagation(); onInstall(server) }}
    >
      <Download size={iconSize} aria-hidden="true" />
      {i18nT('components.mcpBrowserModal.install')}{large ? ' Server' : ''}
    </Btn>
  )
}

/** Human-readable one-line preview of what Install will write to mcp.json. */
function installPlanCommand(plan: McpInstallPlan): string {
  if (plan.spec.url) return plan.spec.url
  const parts = [plan.spec.command, ...(plan.spec.args ?? [])].filter(Boolean)
  return parts.join(' ')
}

/** Detail pane: full description + install plan preview, fetched lazily. */
function ServerDetailPanel({
  server,
  installed,
  phase,
  onInstall,
}: {
  server: DiscoveredMcpServer
  installed: boolean
  phase: InstallPhase | undefined
  onInstall: (server: DiscoveredMcpServer) => void
}) {
  const { data: detail, isLoading: detailLoading } = useQuery({
    queryKey: ['mcp-discover-detail', server.provider, server.id],
    queryFn: () => api.mcpDiscoverDetail(server.provider, server.id),
    staleTime: 60_000,
  })

  const requiredEnv = detail?.required_env ?? []
  const description = detail?.description || server.description || ''
  // The Install action stays disabled until the install-plan preview is on
  // screen (loaded detail; official entries also need a plan). Covers the
  // pending fetch AND the fetch-error case — never an active Install button
  // for a spec the user could not have reviewed.
  const installReady = installReadyFor(server, detail)

  return (
    <div className="pb-2">
      <div className="flex items-start justify-between gap-2 mb-1">
        <div className="flex items-center gap-2 min-w-0">
          <h3 className="text-base font-semibold text-text-strong truncate">{server.title || server.name}</h3>
          <span className="shrink-0 text-[10px] px-1.5 py-0.5 rounded-full bg-accent/15 text-accent font-medium">
            {server.display_provider}
          </span>
          {server.deprecated && (
            <span className="shrink-0 text-[10px] px-1.5 py-0.5 rounded-full bg-warn-subtle text-[var(--warn)] font-medium">
              {i18nT('components.mcpBrowserModal.deprecated')}
            </span>
          )}
        </div>
        <div className="shrink-0">
          <InstallStatus server={server} installed={installed} phase={phase} onInstall={onInstall} large installReady={installReady} />
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted mb-3">
        <span className="font-mono">{server.id}</span>
        {server.version && <span>{i18nT('components.mcpBrowserModal.v')}{server.version}</span>}
        {/* repo_url is publisher-controlled registry data: gate the scheme
          * through safeHttpUrl so a javascript:/data: URL can never become a
          * clickable href (redaction upstream scrubs content, not schemes). */}
        {safeHttpUrl(server.repo_url) && (
          <a
            href={safeHttpUrl(server.repo_url)!}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-accent hover:underline"
          >
            <ExternalLink size={11} aria-hidden="true" /> {i18nT('components.mcpBrowserModal.source')}
          </a>
        )}
      </div>

      {phase?.step === 'done' && !phase.enabled && (
        <div className="mb-3 p-2 rounded bg-warn-subtle border border-[var(--warn)]/30 text-xs text-[var(--warn)]" data-testid="installed-disabled-note">
          {i18nT('components.mcpBrowserModal.installed_disabled_review_the_entry_in_the_serve')}
          {phase.requiredEnv.length > 0 ? ', set its environment variables,' : ''} {i18nT('components.mcpBrowserModal.and_enable_it_there_to_start_using_it')}
        </div>
      )}
      {phase?.step === 'error' && (
        <div className="mb-3 p-2 rounded bg-danger-subtle border border-danger/30 text-xs text-danger">
          {phase.message}
        </div>
      )}
      {phase?.step === 'conflict' && (
        <div className="mb-3 p-2 rounded bg-warn-subtle border border-[var(--warn)]/30 text-xs text-[var(--warn)]">
          {i18nT('components.mcpBrowserModal.a_server_named')}{server.name}{i18nT('components.mcpBrowserModal.already_exists_with_a_different_configuration_ma')}
        </div>
      )}

      {detailLoading ? (
        <div className="flex items-center gap-2 text-xs text-muted" role="status">
          <Loader2 size={12} className="animate-spin" aria-hidden="true" /> {i18nT('components.mcpBrowserModal.loading_details')}
        </div>
      ) : (
        <>
          {/* Install-plan preview: exactly what Install writes to mcp.json.
              null plan (capability) — the edition manager owns the spec, nothing to preview. */}
          {detail?.install_plan && (
            <div className="mb-3 p-2.5 rounded-md border border-border bg-bg-elevated" data-testid="install-plan">
              <div className="flex items-center gap-1.5 text-xs font-medium text-text-strong mb-1">
                <Terminal size={12} aria-hidden="true" />
                {i18nT('components.mcpBrowserModal.install_plan')}
                <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-bg text-muted border border-border font-mono">
                  {detail.install_plan.method}
                </span>
              </div>
              <code className="block text-xs text-muted font-mono break-all">
                {installPlanCommand(detail.install_plan)}
              </code>
              <p className="mt-1 text-[11px] text-muted">
                {i18nT('components.mcpBrowserModal.installs_disabled_you_review_and_enable_it_from')}
              </p>
            </div>
          )}

          {/* Env vars the user must fill in after install. */}
          {requiredEnv.length > 0 && (
            <div className="mb-3 p-2.5 rounded-md border border-[var(--warn)]/30 bg-warn-subtle" data-testid="required-env">
              <div className="flex items-center gap-1.5 text-xs font-medium text-[var(--warn)] mb-1">
                <KeyRound size={12} aria-hidden="true" />
                {i18nT('components.mcpBrowserModal.requires_environment_variables')}
              </div>
              <div className="flex flex-wrap gap-1.5 mb-1">
                {requiredEnv.map(v => (
                  <code key={v} className="text-[11px] px-1.5 py-0.5 rounded bg-bg border border-border font-mono">{v}</code>
                ))}
              </div>
              <p className="text-[11px] text-muted">
                {i18nT('components.mcpBrowserModal.set_these_in_the_server_config_before_enabling_i')}
              </p>
            </div>
          )}

          {description ? (
            <div className="text-sm leading-relaxed">
              {/* MarkdownRenderer sanitizes — provider strings are untrusted. */}
              <MarkdownRenderer content={description} />
            </div>
          ) : (
            <p className="text-sm text-muted">{i18nT('components.mcpBrowserModal.no_description_available')}</p>
          )}
        </>
      )}
    </div>
  )
}
