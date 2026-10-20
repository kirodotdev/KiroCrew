// Design Tweak — dashboard builtin page (compiled React, SPA-native).
// Ported from the federated-ESM app (window.__kirocrew_modules) to a compiled
// builtin: real ESM imports, SPA-native api/router/chat wiring, and strict
// types. Layout, styling and behavior are preserved from the original; the
// authoring style is now plain JSX (the original `createElement` form hid every
// className/style/`d` value from the shared i18n lint's attribute exemptions).
//
// Design source: Figma "Michelle Playground" frame 232:2123 (see design/).
// Two-panel layout inside Kiro Crew's content area: resizable left rail + preview.
//
// This module is the page's import path and its layout: the left rail, the
// preview panel and the request rows (`RequestGroup`, `CommentRow`). The page's
// state and effects, and every action but `deleteReq`, live in
// `useDesignTweakPageState.ts`, which never imports this module.
import { useCallback } from 'react'
import type React from 'react'
import {
  RefreshCw, ChevronDown, ChevronRight, Folder, FolderOpen,
  Send, Plus, Monitor, Eye, Pencil, History as HistoryIcon, X,
  MessageSquare, Archive, Trash2, MoreHorizontal,
} from 'lucide-react'
import Clickable from '../../components/Clickable'
import ErrorNotice from '../../components/ErrorNotice'
import { FolderBody } from '../../components/FolderBody'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem,
} from '../../components/ui/dropdown-menu'
import { i18nT } from '../../i18n/t'
import { timeAgo as relativeTime } from '../../utils/timeAgo'
import { deleteRequest } from './api'
import type { Request, Comment, ThreadEntry } from './types'
import { errMsg, useDesignTweakPageState } from './useDesignTweakPageState'

const DIMS: Record<string, string> = { desktop: '100%', tablet: '768px', mobile: '390px' }

// Static key map rather than a computed key: `check-i18n-keys.mjs` resolves an
// `as const` map to the union of its values, so every label still gets verified.
const DIM_LABEL_KEY = {
  desktop: 'apps.designTweak.preview.dim_desktop',
  tablet: 'apps.designTweak.preview.dim_tablet',
  mobile: 'apps.designTweak.preview.dim_mobile',
} as const

function dimLabel(k: string): string {
  return i18nT(DIM_LABEL_KEY[k as keyof typeof DIM_LABEL_KEY] ?? DIM_LABEL_KEY.desktop)
}

// The menu spells out the width for every preset except desktop, which has none.
function dimOptionLabel(k: string): string {
  return k === 'desktop' ? dimLabel(k) : `${dimLabel(k)} (${DIMS[k]})`
}

// Row actions must stay MOUNTED to be reachable: a hover-only conditional
// render (or `visibility: hidden`) never enters the tab order at all, so a
// keyboard user could not open, archive, or delete anything. Reveal is pure CSS
// instead — `group-hover` for the mouse, `group-focus-within`/`focus-within` for
// the keyboard (each row is itself focusable, so tabbing onto a row shows what
// can be done to it), matching the Sessions sidebar's own row-action pattern.
// A touch screen (`hover: none`) gets no hover at all, so there the cluster is
// simply always expanded.
//
// The cluster collapses to zero size at rest and each call site cancels its
// parent's flex gap with a matching negative margin, so it contributes nothing
// to the row's resting geometry — the status chip lands where it always did and
// the row keeps its old height. Both axes collapse on purpose: the icon buttons
// are taller than the chip beside them, so a width-only collapse would still
// have grown the row. Only focus behaviour is added.
const REVEAL_CLUSTER = 'shrink-0 w-0 h-0 overflow-hidden opacity-0 transition-opacity '
  + 'group-hover:w-auto group-hover:h-auto group-hover:ml-0 group-hover:opacity-100 '
  + 'group-focus-within:w-auto group-focus-within:h-auto group-focus-within:ml-0 group-focus-within:opacity-100 '
  + 'focus-within:w-auto focus-within:h-auto focus-within:ml-0 focus-within:opacity-100 '
  + '[@media(hover:none)]:w-auto [@media(hover:none)]:h-auto [@media(hover:none)]:ml-0 [@media(hover:none)]:opacity-100'

/** Relative age of an ISO timestamp, through the locale-aware shared helper. */
function ago(iso?: string): string {
  if (!iso) return ''
  const ms = Date.parse(iso)
  if (!Number.isFinite(ms)) return ''
  return relativeTime(ms / 1000)
}

interface Chip { label: string; fg: string; bg: string }

// Request-level chip. A request's status is derived from its comments, so the
// chip carries the counts too — it is the only place the request row states how
// many comments it holds ("2 done", "1 in progress, 1 done").
function reqChip(req: Request): Chip {
  const comments = req.comments || []
  const n = comments.length
  const done = comments.filter((c) => c.status === 'done').length
  const prog = comments.filter((c) => c.status === 'sent').length
  const queued = n - done - prog

  if (req.status === 'draft') {
    return {
      label: i18nT('apps.designTweak.requests.chip_not_sent', { n }),
      fg: 'var(--muted)',
      bg: 'var(--bg-elevated)',
    }
  }
  if (req.status === 'done') {
    return {
      label: i18nT('apps.designTweak.requests.chip_done', { n }),
      fg: 'var(--ok)',
      bg: 'var(--ok-subtle)',
    }
  }
  // In flight: name only the groups that actually have members, so a uniform
  // batch reads "2 in progress" rather than "2 in progress, 0 done".
  const parts: string[] = []
  if (prog) parts.push(i18nT('apps.designTweak.requests.chip_in_progress', { n: prog }))
  if (done) parts.push(i18nT('apps.designTweak.requests.chip_done', { n: done }))
  // `queued` should be empty — sending flips every comment to sent — but a
  // comment left at `new` in a sent request would otherwise vanish from the count.
  if (queued) parts.push(i18nT('apps.designTweak.requests.chip_queued', { n: queued }))
  return {
    label: parts.join(', ') || i18nT('apps.designTweak.requests.chip_in_progress', { n }),
    fg: 'var(--accent)',
    bg: 'var(--accent-subtle)',
  }
}

// Per-comment status dot — the Option B "status-forward" cue.
const DOT: Record<string, string> = {
  new:  'var(--warn)',
  sent: 'var(--accent)',
  done: 'var(--ok)',
}

// Sessions' collapse mechanic, shared with the chat sidebar rather than copied:
// the local copy of this component kept reserving layout height for its closed
// rows after the sidebar's copy was fixed, which is the whole reason it moved.

interface CommentRowProps {
  req: Request
  c: Comment
  onFocus?: (c: Comment) => void
  onDelete?: (req: Request, c: Comment) => void
  done: boolean
}

// One comment = one sub-item under a request. Geometry matches a Sessions
// session row (items-start gap-2.5 px-4 py-2 rounded-md).
function CommentRow({ req, c, onFocus, onDelete, done }: CommentRowProps) {
  const thread: ThreadEntry[] = Array.isArray(c.thread) ? c.thread : []
  const lastAgent = thread.slice().reverse().find((m) => m && m.role !== 'user')
  const label = `${req.number}.${c.index}`
  const canDelete = !done && req.state === 'draft'
  const removeLabel = i18nT('apps.designTweak.comments.remove_from_draft')

  return (
    <Clickable
      className="group flex items-start gap-2.5 px-4 py-2 rounded-md cursor-pointer transition-all hover:bg-bg-hover"
      onClick={() => onFocus && onFocus(c)}
      title={i18nT('apps.designTweak.comments.open_in_preview')}
    >
      <span
        className="rounded-full shrink-0"
        style={{ width: '7px', height: '7px', marginTop: '5px', background: DOT[c.status] || DOT.new }}
      />
      <div className="flex-1 min-w-0">
        <div
          className="text-[13px] font-semibold leading-snug text-text"
          style={{ display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}
        >
          {c.comment}
        </div>
        <div className="text-[12px] text-muted leading-snug truncate mt-0.5">
          {i18nT('apps.designTweak.comments.meta', {
            label,
            element: c.element || i18nT('apps.designTweak.comments.element_count', { n: c.count ?? 0 }),
            ago: ago(c.createdAt),
          })}
        </div>
        {c.followUpTo && (
          <div className="text-[11px] text-muted truncate mt-0.5">
            {i18nT('apps.designTweak.comments.follow_up_to', {
              label: c.followUpLabel || i18nT('apps.designTweak.comments.an_earlier_comment'),
            })}
          </div>
        )}
        {lastAgent && (
          <div
            className="text-[12px] text-muted mt-1 pl-2"
            style={{
              borderLeft: '2px solid var(--border)',
              display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden',
            }}
          >
            {lastAgent.text}
          </div>
        )}
      </div>
      {/* Stays MOUNTED so it is in the tab order — a hover-only conditional
          render is unreachable by keyboard. Reveal is pure CSS (see the note on
          the request row's action cluster); the wrapper, not the button, carries
          the collapse so the button's own padding cannot leak width at rest. */}
      {canDelete && (
        <div className={REVEAL_CLUSTER + ' -ml-2.5'}>
          <button
            title={removeLabel}
            aria-label={removeLabel}
            onClick={(e: React.MouseEvent) => { e.stopPropagation(); onDelete?.(req, c) }}
            className="p-1 rounded-md text-muted hover:text-text hover:bg-bg-elevated cursor-pointer shrink-0"
          >
            <Trash2 size={13} />
          </button>
        </div>
      )}
    </Clickable>
  )
}

interface RequestGroupProps {
  req: Request
  open: boolean
  onToggle: (id: string) => void
  onSend?: (req: Request) => void
  onResend?: (req: Request) => void
  /** Proven absent from the session by `verifyDelivery`. NOT 'unconfirmed':
   *  a request whose delivery has not been checked yet, or whose check could
   *  not run, must show NO send control — the batch may already be in hand. */
  sendMissing?: boolean
  sending?: boolean
  onFocusComment: (c: Comment) => void
  onDeleteComment?: (req: Request, c: Comment) => void
  onOpenChat?: (req: Request) => void
  onArchive?: (req: Request) => void
  onDelete?: (req: Request) => void
  done: boolean
}

// One request = a collapsible group. Row geometry matches a Sessions folder
// header (gap-2 pr-2 py-1.5 pl-13px rounded-md, Folder/FolderOpen glyph swap).
function RequestGroup({
  req, open, onToggle, onSend, onResend, sendMissing, sending,
  onFocusComment, onDeleteComment,
  onOpenChat, onArchive, onDelete, done,
}: RequestGroupProps) {
  const comments = req.comments || []
  const chip = reqChip(req)
  const isDraft = req.status === 'draft'
  const FolderGlyph = open ? FolderOpen : Folder

  // Icon-only, so the label has to be reachable by name as well as by tooltip.
  const iconBtn = (label: string, icon: React.ReactNode, handler?: (req: Request) => void) => (
    <button
      title={label}
      aria-label={label}
      onClick={(e: React.MouseEvent) => { e.stopPropagation(); handler?.(req) }}
      className="p-1 rounded-md text-muted hover:text-text hover:bg-bg-elevated cursor-pointer"
    >
      {icon}
    </button>
  )

  return (
    <div className="mb-0.5">
      {/* ---- request row (the "folder") ---- */}
      <Clickable
        className="group relative flex items-center gap-2 pr-2 py-1.5 rounded-md text-sm text-muted hover:text-text hover:bg-bg-hover transition-all cursor-pointer"
        style={{ paddingLeft: '13px' }}
        onClick={() => onToggle(req.id)}
      >
        <FolderGlyph size={14} className="text-muted shrink-0" />
        <span className="flex-1 text-[13px] font-medium text-text truncate text-left">
          {i18nT('apps.designTweak.requests.request_number', { number: req.number })}
        </span>
        <span
          className="text-[11px] px-2 py-[2px] rounded-full font-medium shrink-0"
          style={{ color: chip.fg, background: chip.bg }}
        >
          {chip.label}
        </span>
        {!done && !isDraft && (
          <div className={'flex items-center gap-0.5 ' + REVEAL_CLUSTER + ' -ml-2'}>
            {iconBtn(i18nT('apps.designTweak.requests.open_in_chat'), <MessageSquare size={13} />, onOpenChat)}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button
                  title={i18nT('apps.designTweak.requests.more_actions', { number: req.number })}
                  aria-label={i18nT('apps.designTweak.requests.more_actions', { number: req.number })}
                  onClick={(e: React.MouseEvent) => e.stopPropagation()}
                  className="p-1 rounded-md text-muted hover:text-text hover:bg-bg-elevated cursor-pointer"
                >
                  <MoreHorizontal size={13} />
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" onClick={(e) => e.stopPropagation()}>
                <DropdownMenuItem onSelect={() => onArchive?.(req)}>
                  <Archive size={13} className="shrink-0" />
                  <span>{i18nT('apps.designTweak.requests.archive_to_history')}</span>
                </DropdownMenuItem>
                <DropdownMenuItem onSelect={() => onDelete?.(req)} className="text-danger">
                  <Trash2 size={13} className="shrink-0" />
                  <span>{i18nT('apps.designTweak.requests.delete_request')}</span>
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        )}
      </Clickable>

      {/* ---- children: 12px indent + 1px guide rail (Sessions renderFolderBlock) ---- */}
      <FolderBody open={open}>
        <div className="border-l border-border mb-1 ml-3 pl-1 rounded-bl-md">
          {comments.length === 0
            ? (
              <div className="px-4 py-2 text-[12px] text-muted">
                {i18nT('apps.designTweak.requests.no_comments')}
              </div>
            )
            : comments.map((c) => (
              <CommentRow
                key={c.cid}
                req={req}
                c={c}
                done={done}
                onFocus={onFocusComment}
                onDelete={onDeleteComment}
              />
            ))}

          {/* ---- send bar: full-width, inside the rail (Option B) ---- */}
          {isDraft && comments.length > 0 && (
            <div>
              <div className="mx-3 mt-1 border-b border-border" />
              <button
                onClick={(e: React.MouseEvent) => { e.stopPropagation(); onSend?.(req) }}
                disabled={!!sending}
                className="w-full flex items-center justify-center gap-2 px-4 py-2 rounded-md text-[12px] font-semibold cursor-pointer mt-0.5 disabled:cursor-wait"
                style={{ background: 'var(--accent)', color: 'var(--accent-fg)', opacity: sending ? 0.7 : 1, border: 0 }}
              >
                <Send size={13} />
                {sending
                  ? i18nT('apps.designTweak.requests.sending')
                  : i18nT('apps.designTweak.requests.send_request', { number: req.number })}
              </button>
            </div>
          )}

          {/* ---- send bar for a sealed request the SESSION does not have.
                 `/send` seals server-side and the panel dispatches afterwards, so
                 a crash or a lost ack between the two leaves a sealed request
                 whose fate the panel cannot infer. `verifyDelivery` asks the
                 session; this only renders once the batch is provably absent, so
                 pressing it cannot duplicate work the agent already holds. Drawn
                 in the danger colour because it means a step did not complete,
                 not that something is ready to go. ---- */}
          {!done && !isDraft && sendMissing && comments.length > 0 && (
            <div>
              <div className="mx-3 mt-1 border-b border-border" />
              <button
                onClick={(e: React.MouseEvent) => { e.stopPropagation(); onResend?.(req) }}
                disabled={!!sending}
                className="w-full flex items-center justify-center gap-2 px-4 py-2 rounded-md text-[12px] font-semibold cursor-pointer mt-0.5 disabled:cursor-wait"
                style={{ background: 'var(--danger)', color: '#fff', opacity: sending ? 0.7 : 1, border: 0 }}
              >
                <Send size={13} />
                {sending
                  ? i18nT('apps.designTweak.requests.sending')
                  : i18nT('apps.designTweak.requests.send_missing_request', { number: req.number })}
              </button>
            </div>
          )}
        </div>
      </FolderBody>
    </div>
  )
}


export default function DesignTweak() {
  // The page's state, effects and actions (`useDesignTweakPageState.ts`), except
  // `deleteReq` below: its native `confirm()` keeps it in this module, which
  // `eslint.config.js` allows a native dialog in.
  const {
    ime, selectedId, ddOpen, setDdOpen, ddIn, ddTriggerRef, ddPanelRef, adding, setAdding, newPath,
    setNewPath, detecting, devOpen, setDevOpen, devDraft, setDevDraft, starting, devError, histOpen,
    setHistOpen, previewId, setPreviewNonce, dimsOpen, setDimsOpen, dims, setDimsFor, mode, status,
    setStatus, bridgeError, setBridgeError, reqOpen, sendingId, iframeRef, projects, booting, refresh,
    withFollowUpLabels, toggleReq, postToOverlay, railW, onDragStart, selected, connected, switchTo,
    connect, disconnect, removeProject, addProject, pickFolder, sendRequest, missingIds, resendRequest,
    previewProject, myPending, myHistory, previewSrc, previewState, previewNote, isDevServer,
    markFramed, pinItems, focusComment, openInChat, archiveReq, deleteComment, setDevServer,
    useDetectedDevServer, startDevServer, setEditMode,
  } = useDesignTweakPageState()

  // Delete → permanently remove the request, its comments, and their pins.
  // It sits one icon away from Archive but is irreversible, so it takes the same
  // `confirm()` guard the Sessions sidebar uses for its destructive deletes.
  // Archive stays un-confirmed on purpose: the request is still in History.
  const deleteReq = useCallback(async (req: Request) => {
    if (!confirm(i18nT('apps.designTweak.requests.delete_confirm', { number: req.number }))) return
    try { await deleteRequest(req.id); refresh() }
    catch (err) { setStatus(i18nT('apps.designTweak.status.delete_failed', { error: errMsg(err) })) }
  }, [refresh, setStatus])

  const HistChevron = histOpen ? ChevronDown : ChevronRight

  // ---------- render ----------
  return (
    <div className="flex h-full min-h-0" style={{ padding: '8px 12px' }}>

      {/* ================= LEFT RAIL (resizable, bordered container) ================= */}
      <div
        className="shrink-0 flex flex-col min-h-0"
        style={{
          width: `${railW}px`,
          border: '1px solid var(--border)',
          borderRadius: '16px',
          overflow: 'hidden',
        }}
      >

        {/* header */}
        <div className="flex items-start gap-3 px-5 pt-4 pb-2">
          <div className="flex-1 min-w-0">
            <div className="text-[20px] font-bold text-text-strong leading-tight">
              {i18nT('apps.designTweak.page.title')}
            </div>
            <div className="text-[12px] text-muted mt-0.5">
              {i18nT('apps.designTweak.page.tagline')}
            </div>
          </div>
        </div>

        {/* project dropdown + connect */}
        <div className="flex items-center gap-3 px-5 py-2">
          {/* Trigger + panel share a positioned wrapper. The panel is inset to
              left:0/right:0 of THIS wrapper, so its width is structurally identical
              to the trigger's — no measurement, and content can never widen it. */}
          <div className="flex-1 min-w-0" style={{ position: 'relative' }}>
            <button
              ref={ddTriggerRef}
              onClick={() => { setDdOpen(!ddOpen); setAdding(false) }}
              className="w-full flex items-center gap-2 h-10 px-3 rounded-xl bg-bg-elevated border border-border text-[13px] font-bold text-text cursor-pointer"
            >
              <Folder size={16} className="shrink-0 text-muted" />
              <span className="truncate">
                {selected ? selected.name : i18nT('apps.designTweak.projects.select_app')}
              </span>
              <ChevronDown size={16} className="ml-auto shrink-0 text-muted" />
            </button>

            {/* dropdown panel — drops DOWNWARD from the trigger's bottom edge.

                Geometry lives in inline styles on purpose: this app has no build
                step and borrows the host's compiled Tailwind, so any class Kiro Crew
                does not itself use was purged. `left-5`, `right-5` and `top-[52px]`
                are all absent from the host bundle, which left top/left/right at
                `auto` — the panel then sat at its static position, vertically
                centred in this items-center row and sized by its own content. */}
            {ddOpen && (
              <div
                ref={ddPanelRef}
                className="rounded-xl border border-border bg-card shadow-lg overflow-hidden"
                style={{
                  position: 'absolute',
                  top: 'calc(100% + 4px)',   // just below the trigger
                  left: 0,
                  right: 0,                  // == trigger width
                  zIndex: 20,
                  transformOrigin: 'top center',
                  transform: ddIn ? 'translateY(0)' : 'translateY(-4px)',
                  opacity: ddIn ? 1 : 0,
                  transition: 'transform 130ms cubic-bezier(.4,0,.2,1), opacity 110ms ease-out',
                }}
              >
                {/* scrollable list: 4.5 items visible (item ≈ 40px → 180px) */}
                <div style={{ maxHeight: '180px', overflowY: 'auto' }}>
                  {booting && projects.length === 0
                    ? (
                      <div className="px-3 py-3 flex items-center gap-2 text-[13px] text-muted">
                        <RefreshCw size={13} className="animate-spin" />
                        {i18nT('apps.designTweak.projects.loading')}
                      </div>
                    )
                    : projects.length === 0
                      ? (
                        <div className="px-3 py-3 text-[13px] text-muted">
                          {i18nT('apps.designTweak.projects.none_loaded')}
                        </div>
                      )
                      : projects.map((p) => (
                        <Clickable
                          key={p.id}
                          onClick={() => { switchTo(p); setDdOpen(false) }}
                          className={`group w-full flex items-center gap-2 h-10 px-3 text-left text-[13px] cursor-pointer hover:bg-bg-elevated ${p.id === selectedId ? 'text-text font-bold' : 'text-muted'}`}
                        >
                          <Folder size={14} className="shrink-0" />
                          <span className="truncate flex-1">{p.name}</span>
                          {/* Tag framework projects: they cannot preview from disk, so the
                              tag is what tells you a dev server is part of the deal —
                              shown whether or not one is currently running. */}
                          {(p.needsDevServer || p.previewUrl) && (
                            <span
                              title={p.previewUrl
                                ? i18nT('apps.designTweak.projects.previewing_from', { url: p.previewUrl })
                                : i18nT('apps.designTweak.projects.needs_dev_server', {
                                    command: p.devCommand || i18nT('apps.designTweak.projects.no_dev_script'),
                                  })}
                              className="shrink-0 text-[10px] px-1.5 rounded-full"
                              style={{
                                paddingTop: '1px',
                                paddingBottom: '1px',
                                color: p.previewUrl ? 'var(--accent)' : 'var(--muted)',
                                background: p.previewUrl ? 'var(--accent-subtle)' : 'var(--bg-elevated)',
                              }}
                            >
                              {i18nT('apps.designTweak.projects.dev_badge')}
                            </span>
                          )}
                          {/* Always occupies its 22px, only the opacity toggles — a
                              conditionally-rendered button changed the row's content
                              width on hover, which shifted the name beside it. Opacity
                              rather than `visibility: hidden`, because a hidden element
                              is skipped by the tab order and a keyboard user could then
                              never remove a project. Reveal is CSS, off the row's
                              `group`, so the resting appearance is unchanged. */}
                          <button
                            title={i18nT('apps.designTweak.projects.remove_from_list')}
                            aria-label={i18nT('apps.designTweak.projects.remove_from_list')}
                            onClick={(e: React.MouseEvent) => { e.stopPropagation(); removeProject(p) }}
                            className="flex items-center justify-center text-muted hover:text-text hover:bg-bg-elevated cursor-pointer opacity-0 transition-opacity group-hover:opacity-100 [@media(hover:none)]:opacity-100 group-focus-within:opacity-100 focus-visible:opacity-100"
                            style={{
                              width: '22px', height: '22px', borderRadius: '6px', flex: '0 0 auto',
                            }}
                          >
                            <X size={14} />
                          </button>
                        </Clickable>
                      ))}
                </div>
                {/* pinned below the scroll area */}
                <div className="border-t border-border">
                  {adding
                    ? (
                      <div className="flex items-center gap-2 p-2">
                        <input
                          value={newPath}
                          autoFocus
                          onChange={(e: React.ChangeEvent<HTMLInputElement>) => setNewPath(e.target.value)}
                          {...ime.bindEnter({ onEnter: () => addProject() })}
                          placeholder={i18nT('apps.designTweak.projects.path_placeholder')}
                          className="flex-1 h-8 px-2 rounded-md bg-bg-elevated border border-border text-[12px] text-text"
                        />
                        <button
                          onClick={() => addProject()}
                          className="h-8 px-3 rounded-md bg-accent text-accent-fg text-[12px] font-bold cursor-pointer"
                        >
                          {i18nT('apps.designTweak.projects.add')}
                        </button>
                      </div>
                    )
                    : (
                      <button
                        onClick={pickFolder}
                        title={i18nT('apps.designTweak.projects.browse_or_type_path')}
                        className="w-full flex items-center gap-2 h-10 px-3 text-[13px] text-muted hover:text-text hover:bg-bg-elevated cursor-pointer"
                      >
                        <Plus size={14} />
                        {i18nT('apps.designTweak.projects.load_new_app')}
                      </button>
                    )}
                </div>
              </div>
            )}
          </div>

          {connected
            ? (
              <button
                onClick={disconnect}
                title={i18nT('apps.designTweak.projects.click_again_to_disconnect')}
                className="shrink-0 h-10 px-4 text-[13px] font-bold text-text cursor-pointer hover:bg-bg-elevated"
                style={{ background: 'transparent', border: '1px solid var(--border)', borderRadius: '12px' }}
              >
                {i18nT('apps.designTweak.projects.connected')}
              </button>
            )
            : (
              <button
                onClick={connect}
                disabled={!selected}
                className="shrink-0 h-10 px-4 bg-accent text-accent-fg text-[13px] font-bold cursor-pointer disabled:opacity-40"
                style={{ borderRadius: '12px' }}
              >
                {i18nT('apps.designTweak.projects.connect')}
              </button>
            )}
        </div>

        {status && <div className="px-5 py-1 text-[11px] text-muted truncate">{status}</div>}
        {/* No hand-off: the refused comment is sitting restored in the preview
            overlay's composer (see `create_failed` / `dispatch_failed`), and the
            navigation would unmount the iframe that holds it. */}
        <ErrorNotice message={bridgeError} onDismiss={() => setBridgeError('')} className="mx-5 my-1" />

        {/* request tree + history (nesting mirrors the Sessions folder view) */}
        <div className="flex-1 min-h-0 flex flex-col">
          {/* request groups, newest first (scrolls independently) */}
          <div className="flex-1 min-h-0 overflow-y-auto px-2">
            {booting
              ? (
                <div className="py-6 px-3 flex items-center gap-2 text-[13px] text-muted">
                  <RefreshCw size={14} className="animate-spin" />
                  {i18nT('apps.designTweak.projects.loading_your_apps')}
                </div>
              )
              : myPending.length === 0
                ? (
                  <div className="py-6 px-3 text-[13px] text-muted">
                    {!previewId
                      ? i18nT('apps.designTweak.requests.empty_no_app')
                      : i18nT('apps.designTweak.requests.empty_for_app', {
                          name: previewProject?.name || i18nT('apps.designTweak.requests.this_app'),
                        })}
                  </div>
                )
                : withFollowUpLabels(myPending).slice().reverse().map((req) => (
                  <RequestGroup
                    key={req.id}
                    req={req}
                    done={false}
                    open={reqOpen[req.id] !== false}   /* expanded by default */
                    onToggle={toggleReq}
                    onSend={sendRequest}
                    onResend={resendRequest}
                    sendMissing={missingIds.has(req.id)}
                    sending={sendingId === req.id}
                    onFocusComment={focusComment}
                    onDeleteComment={deleteComment}
                    onOpenChat={openInChat}
                    onArchive={archiveReq}
                    onDelete={deleteReq}
                  />
                ))}
          </div>

          {/* History — pinned to the bottom, expands UPWARD when opened */}
          <div className="shrink-0 border-t border-border">
            {histOpen && myHistory.length > 0 && (
              <div
                className="overflow-y-auto px-2 border-b border-border/60"
                style={{ maxHeight: '38vh' }}
              >
                {withFollowUpLabels(myHistory).map((req) => (
                  <RequestGroup
                    key={req.id}
                    req={req}
                    done={true}
                    open={reqOpen[req.id] === true}        /* collapsed by default */
                    onToggle={toggleReq}
                    onFocusComment={focusComment}
                  />
                ))}
              </div>
            )}
            <button
              onClick={() => setHistOpen(!histOpen)}
              className="w-full flex items-center gap-2 px-5 text-[15px] font-bold text-text cursor-pointer hover:bg-bg-elevated/40"
              style={{ height: '56px' }}   /* match the right-panel action bar height */
            >
              <HistChevron size={16} className="text-muted" />
              <HistoryIcon size={15} className="text-muted" />
              {i18nT('apps.designTweak.requests.history')}
              <span className="text-[12px] text-muted font-normal">({myHistory.length})</span>
            </button>
          </div>
        </div>
      </div>

      {/* drag handle = the gap between panels. `separator` is the role a resize
          strip carries across the dashboard, and it takes a name so a screen
          reader announces the divider rather than an anonymous gap. It stays
          OUT of the tab order: the width it adjusts is cosmetic, both panels
          scroll and stay fully operable at any width, so there is no content or
          control here that only the pointer can reach. */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- pointer-only splitter: `onMouseDown` starts the drag, and a separator with no tab stop has no keyboard operation to mirror it with */}
      <div
        role="separator"
        aria-orientation="vertical"
        aria-label={i18nT('apps.designTweak.layout.drag_to_resize')}
        onMouseDown={onDragStart}
        title={i18nT('apps.designTweak.layout.drag_to_resize')}
        className="shrink-0 cursor-col-resize"
        style={{ width: '11px' }}
      />

      {/* ================= RIGHT PANEL (bordered container: preview + action bar) ================= */}
      <div
        className="flex-1 min-w-0 flex flex-col min-h-0"
        style={{ border: '1px solid var(--border)', borderRadius: '16px', overflow: 'hidden' }}
      >
        {/* upper: preview
            A framework project with no dev server running cannot be previewed from
            disk at all — its entry script is TypeScript. Rather than frame a page
            that is guaranteed to come up blank, say so and offer to start it. */}
        {previewId && previewProject?.needsDevServer && !previewProject?.previewUrl
          ? (
            <div className="flex-1 min-h-0 flex items-center justify-center p-6">
              <div className="flex flex-col items-start gap-3" style={{ maxWidth: '460px' }}>
                <div className="text-[15px] font-bold text-text">
                  {i18nT('apps.designTweak.preview.needs_dev_server_title')}
                </div>
                <div className="text-[13px] text-muted leading-snug">
                  {previewProject.unbundledEntry
                    ? i18nT('apps.designTweak.preview.unbundled_entry', {
                        entry: previewProject.unbundledEntry,
                      })
                    : i18nT('apps.designTweak.preview.no_html_entry')}
                </div>
                {previewProject.devCommand
                  ? (
                    <button
                      onClick={startDevServer}
                      disabled={starting}
                      className="h-9 px-4 rounded-xl text-[13px] font-bold cursor-pointer disabled:cursor-wait"
                      style={{ background: 'var(--accent)', color: 'var(--accent-fg)', border: 0 }}
                    >
                      {starting
                        ? i18nT('apps.designTweak.preview.starting')
                        : i18nT('apps.designTweak.preview.start_dev_server')}
                    </button>
                  )
                  : (
                    <div className="text-[12px]" style={{ color: 'var(--warn)' }}>
                      {i18nT('apps.designTweak.preview.no_dev_script_in_package_json')}
                    </div>
                  )}
                {previewProject.devCommand && (
                  <div className="text-[11px] text-muted">
                    {i18nT('apps.designTweak.preview.runs_command_in_project', {
                      command: previewProject.devCommand,
                      name: previewProject.name || '',
                    })}
                  </div>
                )}
                {/* The edit requests are persisted server-side, so the only draft this
                    page can hold is the dev-server URL field in the rail: the hand-off
                    is gated on that disclosure being closed. No hand-off while
                    `devOpen`: `devDraft` is unsaved. */}
                <ErrorNotice message={devError} askAgent={!devOpen} className="w-full" />
              </div>
            </div>
          )
          : previewId
            ? (
              <div
                className="flex-1 min-h-0 flex items-center justify-center p-3"
                style={{ position: 'relative' }}
              >
                {/*
                  SANDBOX — `allow-same-origin` is deliberate and SAFE HERE, do not
                  "fix" it away. It is dangerous only when the framed document
                  shares OUR origin, and it no longer does: `previewSrc` points at
                  one of the app's own loopback servers (127.0.0.1 on an ephemeral
                  port) for both the dev-server and the static-folder path, so the
                  token grants the previewed project only ITS OWN origin — exactly
                  what a dev server would have given it. Ports DO separate origins
                  under the same-origin policy (unlike cookies, which ignore them),
                  so that origin cannot touch the dashboard's.

                  The one way back to our origin was the gateway-proxied
                  `/api/proxy/<id>/` route, which served project-controlled files
                  from the dashboard origin: a hostile page could read that origin
                  off `document.referrer`, navigate itself there, and have its OWN
                  html run first-party. That route is now DELETED (it answers 410).
                  Navigating to any other dashboard URL gains an attacker nothing,
                  because navigation replaces the document with ours.

                  Removing this token instead would cost real preview fidelity:
                  an opaque origin makes `localStorage` throw SecurityError, and
                  turns same-origin `fetch`, ES-module loading and `@font-face`
                  into CORS-checked cross-origin requests that the preview server
                  deliberately does not answer — so previewed projects would lose
                  their own webfonts, which is the exact fidelity this tool exists
                  to review.

                  If you need to change this, change WHERE the frame is served from
                  (server.py `_StaticInjectHandler`), not this attribute.
                */}
                {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- `onLoad` is a resource event on the frame (the preview finished loading, so seed the overlay), not a gesture, so it has no keyboard equivalent to add */}
                <iframe
                  ref={iframeRef}
                  src={previewSrc}
                  onLoad={() => {
                    markFramed()
                    setEditMode(mode)
                    postToOverlay({ type: 'requests', items: pinItems })
                  }}
                  title={i18nT('apps.designTweak.preview.frame_title')}
                  style={{
                    width: DIMS[dims], height: '100%', border: '1px solid var(--border, #4a464f)',
                    borderRadius: 8, background: '#fff', maxWidth: '100%',
                  }}
                  sandbox="allow-scripts allow-same-origin allow-forms"                />

                {/* Status layer. Covers the frame while it is blank and explains why,
                    instead of leaving a white rectangle. The iframe stays mounted
                    underneath so it can still finish loading and fire onLoad. */}
                {previewState !== 'ready' && (
                  <div
                    style={{
                      position: 'absolute', inset: '12px',
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                      borderRadius: 8, background: 'var(--panel)', textAlign: 'center',
                      padding: '24px',
                    }}
                  >
                    {previewState === 'loading'
                      ? (
                        <div className="flex flex-col items-center gap-2">
                          <RefreshCw size={18} className="text-muted animate-spin" />
                          <div className="text-[13px] text-text">
                            {i18nT('apps.designTweak.preview.loading_name', {
                              name: previewProject?.name || i18nT('apps.designTweak.preview.fallback_name'),
                            })}
                          </div>
                          {isDevServer && (
                            <div className="text-[12px] text-muted">{previewProject?.previewUrl}</div>
                          )}
                        </div>
                      )
                      : (
                        <div className="flex flex-col items-center gap-2" style={{ maxWidth: '420px' }}>
                          <div className="text-[13px] font-bold text-text">
                            {isDevServer
                              ? i18nT('apps.designTweak.preview.dev_server_not_reachable')
                              : i18nT('apps.designTweak.preview.not_reachable')}
                          </div>
                          <div className="text-[12px] text-muted leading-snug">
                            {isDevServer
                              ? i18nT('apps.designTweak.preview.nothing_answered_at', {
                                  url: previewProject?.previewUrl || '',
                                })
                              : i18nT('apps.designTweak.preview.backend_did_not_answer')}
                          </div>
                          {previewNote && (
                            <div className="text-[11px] text-muted">({previewNote})</div>
                          )}
                          <button
                            onClick={() => setPreviewNonce(Date.now())}
                            className="mt-1 h-8 px-3 rounded-md text-[12px] font-bold cursor-pointer"
                            style={{ background: 'var(--accent)', color: 'var(--accent-fg)', border: 0 }}
                          >
                            {i18nT('apps.designTweak.preview.retry')}
                          </button>
                        </div>
                      )}
                  </div>
                )}
              </div>
            )
            : (
              <div
                className="flex-1 flex items-center justify-center text-muted text-sm text-center"
                style={{ paddingLeft: '40px', paddingRight: '40px' }}   /* px-10 is not in the host bundle */
              >
                {booting
                  ? (
                    <div className="flex flex-col items-center gap-2">
                      <RefreshCw size={18} className="animate-spin" />
                      <div>{i18nT('apps.designTweak.projects.looking_for_connected_apps')}</div>
                    </div>
                  )
                  : i18nT('apps.designTweak.projects.no_app_selected')}
              </div>
            )}

        {/* bottom: action bar (fixed, 56px — 40px tab pill + 8px gap to each bar edge; matches History header) */}
        <div
          className="shrink-0 flex items-center justify-between px-3"
          style={{ height: '56px', borderTop: '1px solid var(--border)' }}
        >
          {/* dimensions selector */}
          <div className="flex items-center gap-1">
            <div className="relative">
              <button
                onClick={() => setDimsOpen(!dimsOpen)}
                className="flex items-center gap-2 h-8 px-3 rounded-xl text-[13px] text-text cursor-pointer hover:bg-bg-elevated"
              >
                <Monitor size={15} />
                <span className="font-bold">{i18nT('apps.designTweak.preview.dimensions')}:</span>
                <span>{dimLabel(dims)}</span>
                <ChevronDown size={14} className="text-muted" />
              </button>
              {dimsOpen && (
                <div
                  className="rounded-xl border border-border bg-card shadow-lg overflow-hidden"
                  style={{ position: 'absolute', bottom: '40px', left: 0, minWidth: '180px', zIndex: 30 }}
                >
                  {Object.keys(DIMS).map((k) => (
                    <button
                      key={k}
                      onClick={() => { setDimsFor(previewId, k); setDimsOpen(false) }}
                      className={`block w-full text-left px-4 h-9 text-[13px] cursor-pointer hover:bg-bg-elevated ${k === dims ? 'text-text font-bold' : 'text-muted'}`}
                    >
                      {dimOptionLabel(k)}
                    </button>
                  ))}
                </div>
              )}
            </div>

            {/* Dev-server control. Sits beside Dimensions because that is where you
                are when a preview looks wrong — the add-form only covers new projects. */}
            {previewId && (
              <div className="relative">
                {previewProject?.previewUrl && !devOpen
                  ? (
                    <div
                      className="flex items-center gap-1 h-8 pl-2 pr-1 rounded-xl"
                      style={{ background: 'var(--accent-subtle)' }}
                    >
                      <span className="text-[12px]" style={{ color: 'var(--accent)' }}>
                        {previewProject.previewUrl.replace(/^https?:\/\//, '')}
                      </span>
                      <button
                        title={i18nT('apps.designTweak.devServer.preview_from_disk_instead')}
                        aria-label={i18nT('apps.designTweak.devServer.preview_from_disk_instead')}
                        onClick={() => setDevServer('')}
                        className="p-1 rounded-md cursor-pointer"
                        style={{ color: 'var(--accent)' }}
                      >
                        <X size={13} />
                      </button>
                    </div>
                  )
                  : !devOpen
                    ? (
                      <button
                        onClick={useDetectedDevServer}
                        disabled={detecting}
                        title={i18nT('apps.designTweak.devServer.detect_hint')}
                        className="flex items-center gap-2 h-8 px-3 rounded-xl text-[13px] text-muted hover:text-text hover:bg-bg-elevated cursor-pointer disabled:cursor-wait"
                      >
                        <Eye size={15} />
                        {detecting
                          ? i18nT('apps.designTweak.devServer.looking')
                          : i18nT('apps.designTweak.devServer.dev_server')}
                      </button>
                    )
                    : (
                      <div className="flex items-center gap-1">
                        <input
                          value={devDraft}
                          autoFocus
                          onChange={(e: React.ChangeEvent<HTMLInputElement>) => setDevDraft(e.target.value)}
                          {...ime.bindEnter({ onEnter: () => setDevServer(devDraft.trim()), onEscape: () => { setDevOpen(false); setDevDraft('') } })}
                          placeholder={i18nT('apps.designTweak.devServer.url_placeholder')}
                          className="h-8 px-2 rounded-md bg-bg-elevated border border-border text-[12px] text-text"
                          style={{ width: '190px' }}
                        />
                        <button
                          onClick={() => setDevServer(devDraft.trim())}
                          className="h-8 px-2 rounded-md text-[12px] font-bold cursor-pointer"
                          style={{ background: 'var(--accent)', color: 'var(--accent-fg)', border: 0 }}
                        >
                          {i18nT('apps.designTweak.devServer.use')}
                        </button>
                      </div>
                    )}
              </div>
            )}
          </div>
          {/* refresh + preview/edit toggle */}
          <div className="flex items-center gap-2">
            <button
              title={i18nT('apps.designTweak.preview.refresh_preview')}
              aria-label={i18nT('apps.designTweak.preview.refresh_preview')}
              onClick={() => previewId && setPreviewNonce(Date.now())}
              disabled={!previewId}
              className="p-2 rounded-md text-muted hover:text-text hover:bg-bg-elevated cursor-pointer disabled:opacity-40"
            >
              <RefreshCw size={15} />
            </button>
            <div
              className="flex items-center gap-1"
              style={{
                background: 'rgba(0,0,0,0.25)',   // theme-agnostic "darker" recessed track
                borderRadius: '14px', border: '1px solid var(--border)', padding: '4px',
              }}
            >
              <button
                onClick={() => setEditMode('preview')}
                className={`flex items-center justify-center gap-1.5 h-8 px-3 text-[13px] font-bold cursor-pointer transition-all ${mode === 'preview' ? 'text-accent-fg' : 'text-text hover:text-text'}`}
                style={{ borderRadius: '10px', background: mode === 'preview' ? 'var(--accent)' : 'transparent' }}
              >
                <Eye size={14} />
                {i18nT('apps.designTweak.modes.preview')}
              </button>
              <button
                onClick={() => setEditMode('edit')}
                className={`flex items-center justify-center gap-1.5 h-8 px-3 text-[13px] font-bold cursor-pointer transition-all ${mode === 'edit' ? 'text-accent-fg' : 'text-text hover:text-text'}`}
                style={{ borderRadius: '10px', background: mode === 'edit' ? 'var(--accent)' : 'transparent' }}
              >
                <Pencil size={14} />
                {i18nT('apps.designTweak.modes.edit')}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
