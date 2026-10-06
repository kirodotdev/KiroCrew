import { useEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent } from 'react'
import { createPortal } from 'react-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, ChevronDown, GitBranch, Globe, Loader2, Plus, Search } from 'lucide-react'
import { api } from '../api/client'
import type { GitBranchRow } from '../api/client/files'
import ErrorNotice from './ErrorNotice'
import CopyBranchButton from './CopyBranchButton'
import { useListKeyboardNav } from '../hooks/useListKeyboardNav'
import { useImeGuard } from '../hooks/useImeGuard'
import { gitErrorCode } from '../utils/gitStatusError'
import { findReport } from '../utils/errorReport'
import { errMessage } from '../utils/thunkError'
import { timeAgo } from '../utils/timeAgo'
import { i18nT } from '../i18n/t'

/** Same grammar the switch route applies to a NEW name (`is_valid_followup_branch`),
 *  so the create row is offered only for a name the server will accept. */
const NEW_BRANCH_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*(?:\/[A-Za-z0-9][A-Za-z0-9._-]*)*$/
const WINDOWS_DEVICE_STEMS = new Set([
  'con', 'prn', 'aux', 'nul',
  'com1', 'com2', 'com3', 'com4', 'com5', 'com6', 'com7', 'com8', 'com9',
  'lpt1', 'lpt2', 'lpt3', 'lpt4', 'lpt5', 'lpt6', 'lpt7', 'lpt8', 'lpt9',
])

export function isValidNewBranchName(name: string): boolean {
  if (!name || name.length > 200 || !NEW_BRANCH_RE.test(name)) return false
  if (name.includes('..') || name === 'HEAD') return false
  return name.split('/').every(part =>
    !!part && !part.endsWith('.') && !part.endsWith('.lock') &&
    !WINDOWS_DEVICE_STEMS.has(part.split('.')[0].toLowerCase()))
}

/** Localized copy for each coded switch failure. The backend's English sentence is
 *  never shown under a localized surface; an uncoded failure falls back to the
 *  generic line with git's own first line as detail. */
const SWITCH_ERROR_KEYS: Record<string, string> = {
  git_switch_dirty: 'components.branchSwitcher.error_dirty',
  git_branch_in_worktree: 'components.branchSwitcher.error_in_worktree',
  git_branch_exists: 'components.branchSwitcher.error_exists',
  git_operation_in_progress: 'components.branchSwitcher.error_in_progress',
  git_branch_not_found: 'components.branchSwitcher.error_not_found',
  invalid_branch: 'components.branchSwitcher.error_invalid',
  git_switch_filter_refused: 'components.branchSwitcher.switch_blocked_filter',
  git_sandbox_unavailable: 'components.branchSwitcher.error_sandbox',
  repo_root_outside_project: 'components.branchSwitcher.outside_project',
  git_timeout: 'components.branchSwitcher.error_timeout',
}

function errorDetail(error: unknown): string | undefined {
  const body = (error as { body?: unknown } | null)?.body
  if (typeof body !== 'string' || !body.trim().startsWith('{')) return undefined
  try {
    const parsed = JSON.parse(body) as { detail?: unknown }
    return typeof parsed.detail === 'string' && parsed.detail ? parsed.detail : undefined
  } catch {
    return undefined
  }
}

type Choice =
  | { kind: 'local'; row: GitBranchRow }
  | { kind: 'remote'; row: GitBranchRow }
  | { kind: 'create'; name: string }

function choiceKey(c: Choice): string {
  return c.kind === 'create' ? `create:${c.name}` : `${c.kind}:${c.row.name}`
}

/** `<remote>/<name>` -> `<name>`: the local branch a tracked remote row becomes. */
function localNameFor(remote: string): string {
  const slash = remote.indexOf('/')
  return slash >= 0 ? remote.slice(slash + 1) : remote
}

interface BranchSwitcherProps {
  projectDir: string
  /** The branch the status route reports, shown on the trigger while the list loads. */
  branch?: string
  /** `header` is the Git panel's title control and opens downward; `chip` is the
   *  composer shelf's branch segment, which sits at the bottom of the page and
   *  so opens upward. */
  variant?: 'header' | 'chip'
  /** Blocks opening the picker, with the reason as the trigger's tooltip. */
  disabledReason?: string
}

/**
 * The branch control used by the Git panel header and the composer's project
 * chip: shows the checked-out branch and opens a filterable picker to switch to
 * a local branch, check out a remote one as a tracking branch, or create a new
 * branch from HEAD by typing its name.
 *
 * The list is read only while the picker is open. A switch refreshes every view
 * of the working tree (status, log, file tree, the composer's branch chip),
 * since all of them change with the checkout.
 */
export default function BranchSwitcher({ projectDir, branch, variant = 'header', disabledReason }: BranchSwitcherProps) {
  const qc = useQueryClient()
  const ime = useImeGuard()
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [pending, setPending] = useState<string | null>(null)
  const [switchError, setSwitchError] = useState<unknown>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const popRef = useRef<HTMLDivElement>(null)

  const { data, error: listError, isLoading } = useQuery({
    queryKey: ['git-branches', projectDir],
    queryFn: () => api.projectGitBranches(projectDir),
    enabled: open && !!projectDir,
    staleTime: 5_000,
    retry: 1,
  })

  const close = () => {
    setOpen(false)
    setQuery('')
    setSwitchError(null)
    ime.reset()
  }

  // Click outside closes; deferred a tick so the opening click does not.
  useEffect(() => {
    if (!open) return
    let detach = () => {}
    const timer = setTimeout(() => {
      const onDown = (e: MouseEvent) => {
        const t = e.target as Node
        if (popRef.current?.contains(t) || triggerRef.current?.contains(t)) return
        close()
      }
      document.addEventListener('mousedown', onDown)
      detach = () => document.removeEventListener('mousedown', onDown)
    }, 0)
    return () => { clearTimeout(timer); detach() }
    // `close` only resets local state; re-attaching per render would churn the listener.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const blockedCode = data?.switchBlocked === 'filter'
    ? 'git_switch_filter_refused'
    : gitErrorCode(listError) === 'repo_root_outside_project'
      ? 'repo_root_outside_project'
      : undefined
  const q = query.trim()
  const ql = q.toLowerCase()

  const choices = useMemo<Choice[]>(() => {
    if (!data?.repo) return []
    const match = (r: GitBranchRow) => !ql || r.name.toLowerCase().includes(ql)
    const out: Choice[] = [
      ...data.local.filter(match).map(row => ({ kind: 'local' as const, row })),
      ...data.remote.filter(match).map(row => ({ kind: 'remote' as const, row })),
    ]
    const exists = data.local.some(r => r.name === q)
    if (q && !exists && isValidNewBranchName(q)) out.push({ kind: 'create', name: q })
    return out
  }, [data, q, ql])

  const selectable = (c: Choice) =>
    !blockedCode && !pending && (c.kind === 'create' || (c.row.switchable && !(c.kind === 'local' && c.row.current)))

  const choose = async (c: Choice) => {
    if (!selectable(c)) return
    const req = c.kind === 'local'
      ? { path: projectDir, branch: c.row.name }
      : c.kind === 'remote'
        ? { path: projectDir, branch: localNameFor(c.row.name), track: c.row.name }
        : { path: projectDir, branch: c.name, create: true }
    setPending(choiceKey(c))
    setSwitchError(null)
    try {
      await api.projectGitSwitch(req)
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['git-status', projectDir] }),
        qc.invalidateQueries({ queryKey: ['git-log', projectDir] }),
        qc.invalidateQueries({ queryKey: ['git-branches', projectDir] }),
        qc.invalidateQueries({ queryKey: ['project-tree', projectDir] }),
        qc.invalidateQueries({ queryKey: ['project-git'] }),
      ])
      close()
    } catch (err) {
      setSwitchError(err)
    } finally {
      setPending(null)
    }
  }

  const nav = useListKeyboardNav({
    open,
    count: choices.length,
    onChoose: i => { const c = choices[i]; if (c) void choose(c) },
    onClose: close,
  })
  // Start on the first row Enter can act on: the checked-out branch sorts near the
  // top and is never a target, so highlighting it would make Enter a no-op.
  useEffect(() => {
    const first = choices.findIndex(c => c.kind === 'create' || (c.row.switchable && !(c.kind === 'local' && c.row.current)))
    nav.setSelected(Math.max(0, first))
  }, [query, data]) // eslint-disable-line react-hooks/exhaustive-deps

  const label = branch ?? data?.current ?? (data?.detached && data.head
    ? i18nT('components.branchSwitcher.detached', { sha: data.head })
    : '')

  const switchErrorCode = gitErrorCode(switchError)
  const switchErrorKey = switchErrorCode ? SWITCH_ERROR_KEYS[switchErrorCode] : undefined
  const switchErrorDetail = errorDetail(switchError)

  // A running response can disable the trigger while the picker is open; close
  // it then, since a switch would change files under the turn.
  useEffect(() => {
    if (disabledReason && open) close()
  }, [disabledReason]) // eslint-disable-line react-hooks/exhaustive-deps

  const rect = open ? triggerRef.current?.getBoundingClientRect() : undefined
  // 360px, narrowed on a viewport too small to fit it beside the 8px gutters.
  const width = Math.min(360, window.innerWidth - 16)
  const above = variant === 'chip'
  const placement = rect
    ? above
      ? { bottom: window.innerHeight - rect.top + 4, maxHeight: Math.max(220, Math.min(460, rect.top - 12)) }
      : { top: rect.bottom + 4, maxHeight: Math.max(220, Math.min(460, window.innerHeight - rect.bottom - 12)) }
    : undefined

  const renderRow = (c: Choice, i: number) => {
    const active = i === nav.selected
    const isPending = pending === choiceKey(c)
    const enabled = selectable(c)
    const key = choiceKey(c)
    const common = {
      id: `branch-opt-${i}`,
      role: 'option' as const,
      'aria-selected': active,
      'aria-disabled': !enabled || undefined,
      tabIndex: -1,
      ref: (el: HTMLButtonElement | null) => { nav.itemRefs.current[i] = el },
      onMouseEnter: () => nav.setSelected(i),
      onMouseDown: (e: ReactMouseEvent) => { e.preventDefault(); void choose(c) },
      className: `w-full text-left px-3 py-1.5 flex items-start gap-2 border-none transition-colors ${
        active ? 'bg-bg-hover' : 'bg-transparent'} ${enabled ? 'cursor-pointer' : 'cursor-default'}`,
    }
    if (c.kind === 'create') {
      return (
        <button key={key} {...common} data-testid="branch-create">
          {isPending ? <Loader2 size={13} className="mt-0.5 animate-spin text-accent shrink-0" /> : <Plus size={13} className="mt-0.5 text-accent shrink-0" />}
          <span className="text-[12px] text-text truncate">
            {i18nT('components.branchSwitcher.create_branch', { name: c.name })}
          </span>
        </button>
      )
    }
    const row = c.row
    const isCurrent = c.kind === 'local' && row.current
    const when = row.date ? timeAgo(Date.parse(row.date) / 1000) : ''
    return (
      <button
        key={key}
        {...common}
        title={!row.switchable ? i18nT('components.branchSwitcher.not_switchable') : row.name}
        data-testid={`branch-row-${c.kind}`}
      >
        <span className="mt-0.5 w-[13px] shrink-0 flex justify-center">
          {isPending
            ? <Loader2 size={13} className="animate-spin text-accent" />
            : isCurrent
              ? <Check size={13} className="text-accent" />
              : c.kind === 'remote'
                ? <Globe size={12} className="text-muted" />
                : null}
        </span>
        <span className="flex-1 min-w-0">
          <span className="flex items-center gap-1.5">
            <span className={`font-mono text-[12px] truncate ${row.switchable ? 'text-text' : 'text-muted'}`}>{row.name}</span>
            {(row.ahead || row.behind) ? (
              <span className="text-[10px] px-1 rounded bg-bg-hover text-muted font-mono shrink-0">
                {row.ahead ? <>&#x2191;{row.ahead}</> : null}
                {row.ahead && row.behind ? ' ' : null}
                {row.behind ? <>&#x2193;{row.behind}</> : null}
              </span>
            ) : null}
          </span>
          <span className="block text-[11px] text-muted truncate">
            {[row.author, when, row.subject].filter(Boolean).join(' · ')}
          </span>
        </span>
      </button>
    )
  }

  const localCount = choices.filter(c => c.kind === 'local').length
  const remoteStart = localCount
  const remoteCount = choices.filter(c => c.kind === 'remote').length

  return (
    <>
      {variant === 'chip' ? (
        // Composer shelf segment: same muted mono look as the copy chip it
        // replaces. mousedown is cancelled so a click does not pull focus out of
        // the composer before the picker's own input takes it.
        <button
          ref={triggerRef}
          type="button"
          onMouseDown={e => e.preventDefault()}
          onClick={() => (open ? close() : setOpen(true))}
          disabled={!!disabledReason}
          aria-haspopup="listbox"
          aria-expanded={open}
          aria-label={disabledReason ?? i18nT('components.branchSwitcher.switch_branch_current', { branch: label })}
          title={disabledReason ?? i18nT('components.branchSwitcher.switch_branch_current', { branch: label })}
          data-testid="branch-switcher-trigger"
          data-variant={variant}
          className="min-w-0 max-w-[220px] inline-flex items-center gap-1 h-7 px-1 -mx-1 rounded border-none bg-transparent cursor-pointer text-inherit font-mono opacity-70 hover:opacity-100 hover:text-text hover:bg-bg-hover transition-colors disabled:cursor-not-allowed disabled:hover:bg-transparent disabled:hover:opacity-70 disabled:hover:text-inherit"
        >
          <span className="truncate">{label}</span>
          <ChevronDown size={11} className="shrink-0 opacity-70" />
        </button>
      ) : (
        <button
          ref={triggerRef}
          type="button"
          onClick={() => (open ? close() : setOpen(true))}
          disabled={!!disabledReason}
          aria-haspopup="listbox"
          aria-expanded={open}
          aria-label={disabledReason ?? i18nT('components.branchSwitcher.switch_branch_current', { branch: label })}
          title={disabledReason ?? i18nT('components.branchSwitcher.switch_branch')}
          data-testid="branch-switcher-trigger"
          data-variant={variant}
          className="min-w-0 flex items-center gap-1.5 h-[26px] px-1.5 -ml-1.5 rounded-md border-none bg-transparent cursor-pointer text-text hover:bg-bg-hover transition-colors disabled:cursor-not-allowed disabled:hover:bg-transparent"
        >
          <GitBranch size={14} className="text-accent shrink-0" />
          <span className="text-[12px] font-medium truncate">{label || i18nT('components.gitPanel.loading')}</span>
          <ChevronDown size={12} className="text-muted shrink-0" />
        </button>
      )}
      {open && rect && placement && createPortal(
        <div
          ref={popRef}
          role="dialog"
          aria-label={i18nT('components.branchSwitcher.switch_branch')}
          data-testid="branch-switcher"
          data-placement={above ? 'above' : 'below'}
          className="fixed z-[9999] bg-card text-text border border-border rounded-lg shadow-lg flex flex-col overflow-hidden animate-slide-up"
          style={{
            ...placement,
            left: Math.max(8, Math.min(rect.left, window.innerWidth - width - 8)),
            width,
          }}
        >
          <div className="p-2 border-b border-border">
            <div className="relative">
              <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted pointer-events-none" />
              <input
                autoFocus
                type="text"
                role="combobox"
                aria-expanded
                aria-controls="branch-switcher-list"
                aria-activedescendant={choices.length ? `branch-opt-${nav.selected}` : undefined}
                aria-label={i18nT('components.branchSwitcher.search_label')}
                placeholder={i18nT('components.branchSwitcher.search_placeholder')}
                value={query}
                onChange={e => { setQuery(e.target.value); setSwitchError(null) }}
                {...ime.bindComposition()}
                className="w-full bg-bg-elevated border border-border rounded pl-7 pr-3 py-1.5 text-[12px] font-mono text-text placeholder:text-muted placeholder:font-sans focus:outline-hidden focus-visible:border-accent"
              />
            </div>
          </div>

          {(blockedCode || switchError || listError) ? (
            <div className="px-2 pt-2">
              {/* No hand-off: the hand-off navigates away and unmounts this
                  picker, discarding the branch name typed in its search field
                  (the draft "Create branch" acts on). */}
              {blockedCode ? (
                <ErrorNotice
                  variant="inline"
                  className="whitespace-normal"
                  message={i18nT(SWITCH_ERROR_KEYS[blockedCode])}
                  testId="branch-switcher-blocked"
                />
              ) : switchError ? (
                <ErrorNotice
                  variant="inline"
                  className="whitespace-normal"
                  title={switchErrorKey ? undefined : i18nT('components.branchSwitcher.error_generic')}
                  message={switchErrorKey ? i18nT(switchErrorKey) : (switchErrorDetail ?? errMessage(switchError))}
                  report={findReport(errMessage(switchError))}
                  testId="branch-switcher-error"
                />
              ) : (
                <ErrorNotice
                  variant="inline"
                  className="whitespace-normal"
                  message={i18nT('components.branchSwitcher.load_failed')}
                  report={findReport(errMessage(listError))}
                  testId="branch-switcher-load-error"
                />
              )}
            </div>
          ) : null}

          <div id="branch-switcher-list" role="listbox" aria-label={i18nT('components.branchSwitcher.branches')} className="overflow-y-auto flex-1 min-h-0 py-1">
            {isLoading ? (
              <div className="px-3 py-4 text-[12px] text-muted text-center flex items-center justify-center gap-2">
                <Loader2 size={12} className="animate-spin" />
                {i18nT('components.branchSwitcher.loading')}
              </div>
            ) : data && !data.repo ? (
              <div className="px-3 py-4 text-[12px] text-muted text-center">{i18nT('components.gitPanel.not_a_repository')}</div>
            ) : data && choices.length === 0 ? (
              <div className="px-3 py-4 text-[12px] text-muted text-center">
                {q && !isValidNewBranchName(q)
                  ? i18nT('components.branchSwitcher.invalid_new_name')
                  : i18nT('components.branchSwitcher.no_matches')}
              </div>
            ) : (
              <>
                {localCount > 0 && (
                  <div role="presentation" className="px-3 pt-1 pb-0.5 text-[10px] font-semibold uppercase tracking-wider text-muted">
                    {i18nT('components.branchSwitcher.local_branches')}
                  </div>
                )}
                {choices.slice(0, localCount).map((c, i) => renderRow(c, i))}
                {remoteCount > 0 && (
                  <div role="presentation" className="px-3 pt-2 pb-0.5 text-[10px] font-semibold uppercase tracking-wider text-muted">
                    {i18nT('components.branchSwitcher.remote_branches')}
                  </div>
                )}
                {choices.slice(remoteStart, remoteStart + remoteCount).map((c, i) => renderRow(c, remoteStart + i))}
                {choices.slice(remoteStart + remoteCount).map((c, i) => renderRow(c, remoteStart + remoteCount + i))}
              </>
            )}
          </div>

          {(data?.current || data?.truncated) && (
            <div className="px-3 py-1.5 border-t border-border flex items-center gap-2 text-[11px] text-muted">
              {data.current && (
                <span className="min-w-0 flex items-center gap-1">
                  {i18nT('components.branchSwitcher.current_branch')}
                  <CopyBranchButton branch={data.current} className="font-mono text-text" />
                </span>
              )}
              <span className="flex-1" />
              {data.truncated && (
                <span role="status" className="shrink-0">
                  {i18nT('components.branchSwitcher.truncated', { limit: 200 })}
                </span>
              )}
            </div>
          )}
        </div>,
        document.body,
      )}
    </>
  )
}
