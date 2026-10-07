import { useEffect, useId, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent } from 'react'
import { createPortal } from 'react-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, ChevronDown, Copy, GitBranch, Globe, Loader2, Plus, Search } from 'lucide-react'
import { api } from '../api/client'
import type { GitBranchRow } from '../api/client/files'
import ErrorNotice from './ErrorNotice'
import CopyBranchButton from './CopyBranchButton'
import { useListKeyboardNav } from '../hooks/useListKeyboardNav'
import { useImeGuard } from '../hooks/useImeGuard'
import { copyToClipboard } from '../utils/clipboard'
import { gitErrorCode } from '../utils/gitStatusError'
import { findReport } from '../utils/errorReport'
import { errMessage } from '../utils/thunkError'
import { timeAgo } from '../utils/timeAgo'
import { i18nT } from '../i18n/t'
import { isMac } from '../utils/platform'

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
  git_switch_session_busy: 'components.branchSwitcher.error_session_busy',
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

type Flash =
  | { kind: 'switched'; branch: string }
  | { kind: 'copy_failed'; name: string }

/** How long the switch acknowledgment stays beside the trigger. */
const SWITCHED_MS = 4000
/** A failure stays longer, so it can be read and its text selected. */
const COPY_FAILED_MS = 8000

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

/** What a row's ahead/behind arrows count, in words. */
function aheadBehindText(row: GitBranchRow): string {
  return [
    row.ahead ? i18nT('components.branchSwitcher.ahead_count', { count: row.ahead }) : '',
    row.behind ? i18nT('components.branchSwitcher.behind_count', { count: row.behind }) : '',
  ].filter(Boolean).join(', ')
}

interface BranchSwitcherProps {
  projectDir: string
  /** The branch the status route reports, shown on the trigger while the list loads. */
  branch?: string
  /** `header` is the Git panel's title control and opens downward; `chip` is the
   *  composer shelf's branch segment, which sits at the bottom of the page and
   *  so opens upward. */
  variant?: 'header' | 'chip'
  /** True while this chat's response runs. Switching is blocked then, since a
   *  checkout would change files under the turn, but reading the branch name is
   *  harmless: the trigger stays enabled and copies the name instead of opening
   *  the picker, and its tooltip says how to switch. */
  switchBlocked?: boolean
  /** `branch` is a short commit, not a branch: HEAD is detached. Only changes the
   *  noun the copy control announces. */
  detached?: boolean
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
export default function BranchSwitcher({ projectDir, branch, variant = 'header', switchBlocked = false, detached = false }: BranchSwitcherProps) {
  const qc = useQueryClient()
  const ime = useImeGuard()
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [pending, setPending] = useState<string | null>(null)
  /** The failed switch, with the branch it targeted so the notice can name it. */
  const [switchError, setSwitchError] = useState<{ error: unknown; target: string; create: boolean } | null>(null)
  const [copied, setCopied] = useState(false)
  const copiedTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  /** A transient note anchored to the trigger: a switch that landed, or a copy
   *  the browser refused. The picker is closed in both cases, so it cannot
   *  carry them. */
  const [flash, setFlash] = useState<Flash | null>(null)
  const flashTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  /** Pointer over, or keyboard focus on, the trigger. Only read while switching
   *  is blocked, when the trigger shows why in a visible hint. */
  const [hinting, setHinting] = useState(false)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const popRef = useRef<HTMLDivElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  /** Rows sit below the list's scroll edge. A section heading can land right on
   *  that edge with its rows out of view, which reads as an empty section, so
   *  the edge fades out to say the list goes on. */
  const [moreBelow, setMoreBelow] = useState(false)
  const measureList = () => {
    const el = listRef.current
    setMoreBelow(!!el && el.scrollTop + el.clientHeight < el.scrollHeight - 1)
  }
  const reasonId = useId()
  useEffect(() => () => {
    if (copiedTimer.current) clearTimeout(copiedTimer.current)
    if (flashTimer.current) clearTimeout(flashTimer.current)
  }, [])
  const showFlash = (next: Flash) => {
    if (flashTimer.current) clearTimeout(flashTimer.current)
    setFlash(next)
    flashTimer.current = setTimeout(() => setFlash(null), next.kind === 'copy_failed' ? COPY_FAILED_MS : SWITCHED_MS)
  }

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
    // A filter-blocked repository refuses a create like any other checkout, so
    // the create row is not offered there at all rather than shown inert.
    if (q && !exists && isValidNewBranchName(q) && data.switchBlocked !== 'filter') out.push({ kind: 'create', name: q })
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
    const target = req.branch
    try {
      const result = await api.projectGitSwitch(req)
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['git-status', projectDir] }),
        qc.invalidateQueries({ queryKey: ['git-log', projectDir] }),
        qc.invalidateQueries({ queryKey: ['git-branches', projectDir] }),
        qc.invalidateQueries({ queryKey: ['project-tree', projectDir] }),
        qc.invalidateQueries({ queryKey: ['project-git'] }),
      ])
      close()
      showFlash({ kind: 'switched', branch: result?.branch || target })
    } catch (err) {
      setSwitchError({ error: err, target, create: c.kind === 'create' })
    } finally {
      setPending(null)
    }
  }

  const nav = useListKeyboardNav({
    open,
    count: choices.length,
    // Enter and Tab switch to an existing branch, but never create one: a
    // search that matches nothing would otherwise turn a typo into a new
    // branch plus a checkout. Creating takes ⌘/Ctrl+Enter or a click.
    onChoose: (i, withModifier) => {
      const c = choices[i]
      if (!c || (c.kind === 'create' && !withModifier)) return
      void choose(c)
    },
    onClose: close,
  })
  // Start on the first existing branch Enter can act on: the checked-out
  // branch sorts near the top and is never a target, and the create row needs
  // its own shortcut. Only a list with nothing else starts on the create row.
  useEffect(() => {
    const first = choices.findIndex(c => c.kind !== 'create' && c.row.switchable && !(c.kind === 'local' && c.row.current))
    const create = choices.findIndex(c => c.kind === 'create')
    nav.setSelected(Math.max(0, first >= 0 ? first : create))
  }, [query, data]) // eslint-disable-line react-hooks/exhaustive-deps
  // Re-read the scroll edge whenever the rows change under it, and whenever
  // the rows' own height does (web fonts landing after the first paint grow
  // every row, which can push a heading onto the edge with no state change).
  useEffect(() => {
    if (!open) return
    measureList()
    const el = listRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => measureList())
    ro.observe(el)
    for (const child of Array.from(el.children)) ro.observe(child)
    return () => ro.disconnect()
  }, [open, choices, isLoading])

  const label = branch ?? data?.current ?? (data?.detached && data.head
    ? i18nT('components.branchSwitcher.detached', { sha: data.head })
    : '')

  const switchErrorCode = gitErrorCode(switchError?.error)
  const switchErrorKey = switchErrorCode ? SWITCH_ERROR_KEYS[switchErrorCode] : undefined
  const switchErrorDetail = errorDetail(switchError?.error)

  // A response starting while the picker is open closes it, since a switch
  // would change files under the turn.
  useEffect(() => {
    if (switchBlocked && open) close()
  }, [switchBlocked]) // eslint-disable-line react-hooks/exhaustive-deps

  const copyLabel = async () => {
    if (!label) return
    if (!(await copyToClipboard(label))) {
      setCopied(false)
      showFlash({ kind: 'copy_failed', name: label })
      return
    }
    setFlash(null)
    setCopied(true)
    if (copiedTimer.current) clearTimeout(copiedTimer.current)
    copiedTimer.current = setTimeout(() => setCopied(false), 2500)
  }
  const onTrigger = () => {
    if (switchBlocked) void copyLabel()
    else if (open) close()
    else setOpen(true)
  }
  // While switching is blocked the trigger is a copy control: its name says
  // what a click does, and its description adds how to switch. That reason is
  // also drawn as a visible note on hover, on focus and after a copy, so it
  // does not depend on a delayed native tooltip (which keyboard focus and touch
  // never show). A note beside the trigger fits the narrow composer shelf,
  // where an always-on line of text would not.
  const triggerName = switchBlocked
    ? detached
      ? i18nT(copied ? 'components.branchSwitcher.copied_commit' : 'components.branchSwitcher.copy_commit', { sha: label })
      : i18nT(copied ? 'components.branchSwitcher.copied_branch_name' : 'components.branchSwitcher.copy_branch_name', { branch: label })
    : i18nT('components.branchSwitcher.switch_branch_current', { branch: label })
  const triggerTitle = switchBlocked
    ? i18nT(detached ? 'components.branchSwitcher.copy_commit_while_running' : 'components.branchSwitcher.copy_branch_while_running')
    : undefined
  const TriggerIcon = switchBlocked ? (copied ? Check : Copy) : ChevronDown
  // While switching is blocked the trigger itself looks busy (muted text, a
  // spinner in place of the branch glyph, a copy cursor), so the change of job
  // shows before any hover note does. A just-confirmed copy keeps its check.
  const busyLook = switchBlocked && !copied
  const hintHandlers = {
    onMouseEnter: () => setHinting(true),
    onMouseLeave: () => setHinting(false),
    onFocus: () => setHinting(true),
    onBlur: () => setHinting(false),
  }
  const switchedText = flash?.kind === 'switched'
    ? i18nT('components.branchSwitcher.switched_to', { branch: flash.branch })
    : ''
  const busyHint = switchBlocked && !open && (hinting || copied)
    ? (copied ? i18nT('components.branchSwitcher.copied_while_running') : triggerTitle)
    : undefined
  const showNote = !open && (!!flash || !!busyHint)
  const anchor = showNote ? triggerRef.current?.getBoundingClientRect() : undefined
  const noteWidth = Math.min(280, window.innerWidth - 16)

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
    // A filter-blocked repository takes every row out of play, so the rows say
    // so visually too: dimmed, and no highlight under the pointer or keyboard.
    const muted = !!blockedCode
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
      'data-muted': muted || undefined,
      className: `w-full text-left px-3 py-1.5 flex items-start gap-2 border-none transition-colors ${
        active && !muted ? 'bg-bg-hover' : 'bg-transparent'} ${enabled ? 'cursor-pointer' : 'cursor-default'} ${muted ? 'opacity-50' : ''}`,
    }
    if (c.kind === 'create') {
      // Set apart from the branch rows, and labelled with the one key that
      // acts on it, so it does not read as "the first result".
      return (
        <button key={key} {...common} data-testid="branch-create" className={`${common.className.replace('items-start', 'items-center')} mt-1 border-t border-solid border-border`}>
          {isPending ? <Loader2 size={13} className="animate-spin text-accent shrink-0" /> : <Plus size={13} className="text-accent shrink-0" />}
          <span className="flex-1 min-w-0 text-[12px] text-text truncate">
            {i18nT('components.branchSwitcher.create_branch', { name: c.name })}
          </span>
          <kbd data-testid="branch-create-shortcut" className="shrink-0 font-mono text-[10px] px-1 rounded bg-bg border border-border text-muted">
            {i18nT(isMac ? 'components.branchSwitcher.create_shortcut_mac' : 'components.branchSwitcher.create_shortcut_other')}
          </kbd>
        </button>
      )
    }
    const row = c.row
    const isCurrent = c.kind === 'local' && row.current
    const when = row.date ? timeAgo(Date.parse(row.date) / 1000) : ''
    // A remote row's click creates a local branch, which its name alone does
    // not say, so the row states it on a visible line of its own.
    const remoteAction = c.kind === 'remote' && row.switchable
      ? i18nT('components.branchSwitcher.remote_row_action', { name: localNameFor(row.name) })
      : ''
    return (
      <button
        key={key}
        {...common}
        title={!row.switchable ? i18nT('components.branchSwitcher.not_switchable') : remoteAction || row.name}
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
              // The arrows are shorthand; the tooltip and the screen-reader
              // text say what they count.
              <span title={aheadBehindText(row)} data-testid="branch-ahead-behind" className="text-[10px] px-1 rounded bg-bg-hover text-muted font-mono shrink-0">
                <span aria-hidden="true">
                  {row.ahead ? <>&#x2191;{row.ahead}</> : null}
                  {row.ahead && row.behind ? ' ' : null}
                  {row.behind ? <>&#x2193;{row.behind}</> : null}
                </span>
                <span className="sr-only">{aheadBehindText(row)}</span>
              </span>
            ) : null}
          </span>
          {remoteAction && (
            <span className="block text-[11px] text-text truncate" data-testid="branch-remote-action">
              {remoteAction}
            </span>
          )}
          <span className="block text-[11px] text-muted truncate">
            {[row.author, when, row.subject].filter(Boolean).join(' · ')}
          </span>
        </span>
      </button>
    )
  }

  const localCount = choices.filter(c => c.kind === 'local').length
  const showArrowLegend = choices.some(c => c.kind !== 'create' && (!!c.row.ahead || !!c.row.behind))
  const remoteStart = localCount
  const remoteCount = choices.filter(c => c.kind === 'remote').length

  return (
    <>
      {variant === 'chip' ? (
        // Composer shelf segment, styled like the folder segment beside it:
        // full-strength muted text that brightens with a hover fill, so it reads
        // as a control. mousedown is cancelled so a click does not pull focus out
        // of the composer before the picker's own input takes it.
        <button
          ref={triggerRef}
          type="button"
          onMouseDown={e => e.preventDefault()}
          onClick={onTrigger}
          aria-haspopup={switchBlocked ? undefined : 'listbox'}
          aria-expanded={switchBlocked ? undefined : open}
          aria-label={triggerName}
          aria-describedby={switchBlocked ? reasonId : undefined}
          title={switchBlocked ? undefined : triggerName}
          {...hintHandlers}
          data-testid="branch-switcher-trigger"
          data-variant={variant}
          data-mode={switchBlocked ? 'copy' : 'switch'}
          className={`min-w-0 max-w-[220px] inline-flex items-center gap-1 h-7 px-1.5 -mx-1 rounded-md border-none bg-transparent font-mono hover:text-text hover:bg-[color-mix(in_srgb,var(--bg-elevated)_84%,var(--text))] transition-colors ${
            busyLook ? 'cursor-copy text-muted' : 'cursor-pointer text-inherit'}`}
        >
          {busyLook && <Loader2 size={10} aria-hidden="true" data-testid="branch-switcher-busy" className="shrink-0 animate-spin motion-reduce:animate-none" />}
          <span className="truncate">{label}</span>
          <TriggerIcon size={11} className={`shrink-0 ${copied ? 'text-ok' : 'opacity-70'}`} />
        </button>
      ) : (
        <button
          ref={triggerRef}
          type="button"
          onClick={onTrigger}
          aria-haspopup={switchBlocked ? undefined : 'listbox'}
          aria-expanded={switchBlocked ? undefined : open}
          aria-label={triggerName}
          aria-describedby={switchBlocked ? reasonId : undefined}
          title={switchBlocked ? undefined : i18nT('components.branchSwitcher.switch_branch')}
          {...hintHandlers}
          data-testid="branch-switcher-trigger"
          data-variant={variant}
          data-mode={switchBlocked ? 'copy' : 'switch'}
          className={`min-w-0 flex items-center gap-1.5 h-[26px] px-1.5 -ml-1.5 rounded-md border-none bg-transparent hover:bg-bg-hover transition-colors ${
            busyLook ? 'cursor-copy text-muted' : 'cursor-pointer text-text'}`}
        >
          {busyLook
            ? <Loader2 size={14} aria-hidden="true" data-testid="branch-switcher-busy" className="text-muted shrink-0 animate-spin motion-reduce:animate-none" />
            : <GitBranch size={14} className="text-accent shrink-0" />}
          <span className="text-[12px] font-medium truncate">{label || i18nT('components.gitPanel.loading')}</span>
          <TriggerIcon size={12} className={`shrink-0 ${copied ? 'text-ok' : 'text-muted'}`} />
        </button>
      )}
      {/* The copy-mode reason, read as the trigger's description. */}
      <span id={reasonId} className="sr-only">{switchBlocked ? triggerTitle : ''}</span>
      {/* Mounted before it has anything to say, so a landed switch is
          announced politely when its text arrives. */}
      <span role="status" className="sr-only" data-testid="branch-switcher-status">{switchedText}</span>
      {showNote && anchor && createPortal(
        <div
          data-testid="branch-switcher-note"
          data-note={flash?.kind ?? 'busy'}
          className={`fixed z-[9999] bg-card text-text border border-border rounded-md shadow-lg px-2 py-1 text-[11px] leading-snug ${
            flash?.kind === 'copy_failed' ? '' : 'pointer-events-none'}`}
          style={{
            ...(variant === 'chip'
              ? { bottom: window.innerHeight - anchor.top + 4 }
              : { top: anchor.bottom + 4 }),
            left: Math.max(8, Math.min(anchor.left, window.innerWidth - noteWidth - 8)),
            maxWidth: noteWidth,
          }}
        >
          {flash?.kind === 'copy_failed' ? (
            /* No hand-off: this control sits in the composer's shelf and the
               chat's Git panel, and the hand-off moves the chat to a new
               session, away from a composer draft that is not sent yet (the
               composer's own notices leave it off for the same reason). The
               notice names the text, so it can be selected and copied by hand. */
            <ErrorNotice
              variant="inline"
              className="whitespace-normal"
              message={i18nT('components.branchSwitcher.copy_failed', { name: flash.name })}
              testId="branch-switcher-copy-error"
            />
          ) : flash?.kind === 'switched' ? (
            // The live region above announces it; this is the visible copy.
            <span aria-hidden="true" className="inline-flex items-center gap-1">
              <Check size={12} className="text-ok shrink-0" />
              {switchedText}
            </span>
          ) : (
            <span aria-hidden="true">{busyHint}</span>
          )}
        </div>,
        document.body,
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
                // Every row is inert when switching is off for the repository,
                // so the search field is too: typing would filter rows that
                // cannot be picked.
                disabled={!!blockedCode}
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
                  title={i18nT(
                    switchError.create ? 'components.branchSwitcher.error_create' : 'components.branchSwitcher.error_switch_to',
                    { branch: switchError.target },
                  )}
                  messagePlacement="below"
                  message={switchErrorKey ? i18nT(switchErrorKey) : (switchErrorDetail ?? errMessage(switchError.error))}
                  report={findReport(errMessage(switchError.error))}
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

          {/* Says what a switch does, then the guarantee the switch route
              gives (git refuses any checkout that would overwrite local work),
              so a row reads as safe to click. Left out beside a refusal, which
              says what happened instead, and in a filter-blocked repository,
              which shows its own notice. */}
          {!blockedCode && !switchError && !(data && !data.repo) && (
            <p className="m-0 px-3 pt-2 text-[12px] leading-snug text-text" data-testid="branch-switcher-safety">
              {i18nT('components.branchSwitcher.safe_switch_hint')}
            </p>
          )}

          <div className="relative flex-1 min-h-0 flex flex-col">
          <div ref={listRef} onScroll={measureList} id="branch-switcher-list" role="listbox" aria-label={i18nT('components.branchSwitcher.branches')} className="overflow-y-auto flex-1 min-h-0 py-1">
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
                  // The count says the section has rows even when they sit
                  // below the list's scroll edge.
                  <div role="presentation" data-testid="branch-remote-heading" className="px-3 pt-2 pb-0.5 text-[10px] font-semibold uppercase tracking-wider text-muted">
                    {i18nT('components.branchSwitcher.remote_branches')} ({remoteCount})
                  </div>
                )}
                {choices.slice(remoteStart, remoteStart + remoteCount).map((c, i) => renderRow(c, remoteStart + i))}
                {choices.slice(remoteStart + remoteCount).map((c, i) => renderRow(c, remoteStart + remoteCount + i))}
              </>
            )}
          </div>
          {moreBelow && (
            // A strip of its own below the list, not an overlay: a fade drawn
            // over the last visible row made that branch look disabled.
            <div
              aria-hidden="true"
              data-testid="branch-switcher-more-below"
              className="pointer-events-none shrink-0 h-3.5 flex items-center justify-center"
            >
              <ChevronDown size={12} className="text-muted" />
            </div>
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
              {/* The row pills are bare arrows, so the footer says once what
                  they count. Left out beside the truncation note (both would
                  not fit) and when no row has a pill. */}
              {showArrowLegend && !data.truncated && (
                <span data-testid="branch-ahead-behind-legend" className="shrink-0">
                  {i18nT('components.branchSwitcher.ahead_behind_legend')}
                </span>
              )}
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
