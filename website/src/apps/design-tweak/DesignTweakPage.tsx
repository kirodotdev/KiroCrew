// Design Tweak — dashboard builtin page (compiled React, SPA-native).
// Ported from the federated-ESM app (window.__kirocrew_modules) to a compiled
// builtin: real ESM imports, SPA-native api/router/chat wiring, and strict
// types. Layout, styling and behavior are preserved from the original; the
// authoring style is now plain JSX (the original `createElement` form hid every
// className/style/`d` value from the shared i18n lint's attribute exemptions).
//
// Design source: Figma "Michelle Playground" frame 232:2123 (see design/).
// Two-panel layout inside Kiro Crew's content area: resizable left rail + preview.
import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import type React from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  RefreshCw, ChevronDown, ChevronRight, Folder, FolderOpen,
  Send, Plus, Monitor, Eye, Pencil, History as HistoryIcon, X,
  MessageSquare, Archive, Trash2, MoreHorizontal,
} from 'lucide-react'
import Clickable from '../../components/Clickable'
import { FolderBody } from '../../components/FolderBody'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem,
} from '../../components/ui/dropdown-menu'
import { i18nT } from '../../i18n/t'
import { timeAgo as relativeTime } from '../../utils/timeAgo'
// Agent-facing prompt text — deliberately untranslated, so it lives in the
// i18n lint's sanctioned `src/apps/*/prompts.ts` home rather than inline here.
import { SESSION_TITLE, SESSION_SEED, REQUEST_PROMPT } from './prompts'
import {
  loopbackPreviewSrc, requestPayloadPath,
  fetchProjects, fetchQueue, fetchHistory, fetchHealth, selectProject, submitComment,
  clearRequest, deleteRequest, setPreviewUrl, detectDevServer, chatRoute,
  createChatSlot, sendChatMessage, setChatSlotProject,
  addProject as apiAddProject,
  pickFolder as apiPickFolder,
  removeProject as apiRemoveProject,
  sendRequest as apiSendRequest,
  markDelivered,
  readSlotTranscript,
  deleteComment as apiDeleteComment,
  startDevServer as apiStartDevServer,
  stopDevServer as apiStopDevServer,
} from './api'
import { deliveryVerdict, needsDeliveryRetry } from './delivery'
import { useImeGuard } from '../../hooks/useImeGuard'
import type {
  Project, Request, Comment, ThreadEntry, EditSelection,
  PreviewScoped, OverlayMessage,
  SubmitResponse, ChatSlotResponse,
} from './types'

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
  + 'focus-within:w-auto focus-within:h-auto focus-within:ml-0 focus-within:opacity-100'

// --- Per-app chat session (mirrors the host's useChatSession slotting) ---------
// Each web app (by its folder path) maps to ONE deterministic chat slot, so all
// its edit requests land as turns in the SAME session (persists across visits).
// slotKey = "design-tweak-" + djb2(path) — identical to the host's hash so the
// slot lines up with what /chat?sid=<key> opens.
const APP_SLOT_PREFIX = 'design-tweak'

// Stand-in when a project has no name. It is NOT copy: it is handed to
// `SESSION_TITLE` / `SESSION_SEED`, so translating it would translate what the
// agent is told, and it is echoed back verbatim in the "sent to <label> session"
// status so the two always name the same session.
const FALLBACK_APP_LABEL = 'app'

/**
 * FNV-1a over one 32-bit lane. `seed` picks the offset basis, so four lanes
 * with different seeds give four independent digests of the same input.
 *
 * Both bytes of every UTF-16 code unit are mixed. Folding to the low byte
 * alone would make `/a/Ā` (U+0100) collide with `/a/\0`, which is exactly the
 * class of bug this function replaced.
 */
function fnv1a32(str: string, seed: number): number {
  let h = seed >>> 0
  for (let i = 0; i < str.length; i++) {
    const c = str.charCodeAt(i)
    h = Math.imul(h ^ (c & 0xff), 0x01000193) >>> 0
    h = Math.imul(h ^ ((c >>> 8) & 0xff), 0x01000193) >>> 0
  }
  return h >>> 0
}

/**
 * 128-bit digest of a project path, base36 per lane.
 *
 * This is the slot's IDENTITY, so a collision does not merely look untidy: two
 * different projects would resolve to the same chat slot and their turns, seeds
 * and session scope would merge into whichever one got there first. The prior
 * implementation was a single 32-bit `h * 31 + c` fold, where a collision needs
 * no adversary and no birthday luck -- any two paths differing only in their
 * last two characters can hit it directly, because `'1' * 31 + 'n'` equals
 * `'3' * 31 + '0'`, so `/tmp/project-1n` and `/tmp/project-30` both hashed to
 * `19gozug`. Four independent lanes remove that structural class outright.
 *
 * Deliberately not `crypto.subtle.digest`: it is async, and this runs inside
 * the synchronous slot-key derivation on the render path.
 */
function projectSlotDigest(str: string): string {
  const s = str || ''
  return [0x811c9dc5, 0x9e3779b9, 0x85ebca6b, 0xc2b2ae35]
    .map((seed) => fnv1a32(s, seed).toString(36))
    .join('')
}
function slotKeyFor(path: string): string { return `${APP_SLOT_PREFIX}-${projectSlotDigest(path || '')}` }

// ── Server state ─────────────────────────────────────────────────────────────

/** Poll cadence for the three list endpoints — unchanged from the old interval. */
const POLL_MS = 5000

/**
 * Query keys. All three lists sit under one `['design-tweak']` prefix so a
 * mutation can invalidate the whole set in a single call.
 */
const DT_KEY = {
  all: ['design-tweak'] as const,
  projects: ['design-tweak', 'projects'] as const,
  queue: ['design-tweak', 'queue'] as const,
  history: ['design-tweak', 'history'] as const,
  health: ['design-tweak', 'health'] as const,
}

// Frozen empty fallbacks: a fresh `[]` literal per render would be a new
// identity every time and re-fire every `useMemo`/`useEffect` keyed on the list.
const NO_PROJECTS: Project[] = []
const NO_REQUESTS: Request[] = []

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
  // One instance covers both inputs; the binding's focus/blur reset makes sharing safe.
  const ime = useImeGuard()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const [selectedId, setSelectedId] = useState('') // UI: which project is picked in dropdown
  const [ddOpen, setDdOpen] = useState(false)
  // Mount flag for the dropdown's entrance: false on the first frame, flipped
  // on the next, so the panel transitions in instead of appearing instantly.
  const [ddIn, setDdIn] = useState(false)
  const ddTriggerRef = useRef<HTMLButtonElement | null>(null)
  const ddPanelRef = useRef<HTMLDivElement | null>(null)
  const [adding, setAdding] = useState(false)
  const [newPath, setNewPath] = useState('')
  const [detecting, setDetecting] = useState(false)
  const [devOpen, setDevOpen] = useState(false)     // dev-server editor in the action bar
  const [devDraft, setDevDraft] = useState('')
  const [starting, setStarting] = useState(false)
  const [devError, setDevError] = useState('')
  const [histOpen, setHistOpen] = useState(false)
  const [previewId, setPreviewId] = useState('')
  const [previewNonce, setPreviewNonce] = useState(1)
  // Dimensions are a per-app preference (keyed by project id, persisted).
  const [dimsMap, setDimsMap] = useState<Record<string, string>>(() => {
    try { return JSON.parse(localStorage.getItem('ste_dims_map') || 'null') || {} } catch { return {} }
  })
  const [dimsOpen, setDimsOpen] = useState(false)
  const dims = dimsMap[previewId] || 'desktop'
  const setDimsFor = useCallback((pid: string, k: string) => {
    if (!pid) return
    setDimsMap((m) => {
      const next = { ...m, [pid]: k }
      try { localStorage.setItem('ste_dims_map', JSON.stringify(next)) } catch { /* ignore */ }
      return next
    })
  }, [])
  const [mode, setMode] = useState<'preview' | 'edit'>('preview')
  const [status, setStatus] = useState('')
  const [reqOpen, setReqOpen] = useState<Record<string, boolean>>({})     // requestId -> expanded?
  const [sendingId, setSendingId] = useState('') // request currently being sent
  const iframeRef = useRef<HTMLIFrameElement | null>(null)

  // ---------- server state (React Query owns fetch + poll + dedupe) ----------
  //
  // Three GETs on one 5s cadence. React Query rather than a `setInterval` chain
  // for two reasons: overlapping polls (a slow /queue while the next tick fires)
  // used to resolve out of order and write a STALE list back over a fresh one,
  // and every mutation below now invalidates one prefix instead of racing a
  // hand-rolled refresh. `refetchInterval` is paused while the tab is
  // backgrounded (react-query default), so an idle panel costs nothing.
  //
  // `retry: false` deliberately: the poll IS the retry, and the shared client's
  // default one-retry-with-backoff would hold the boot state open for an extra
  // second every time the backend is still warming up.
  const projectsQuery = useQuery({
    queryKey: DT_KEY.projects,
    queryFn: fetchProjects,
    refetchInterval: POLL_MS,
    retry: false,
  })
  const queueQuery = useQuery({
    queryKey: DT_KEY.queue,
    queryFn: fetchQueue,
    refetchInterval: POLL_MS,
    retry: false,
  })
  const historyQuery = useQuery({
    queryKey: DT_KEY.history,
    queryFn: fetchHistory,
    refetchInterval: POLL_MS,
    retry: false,
  })
  // Read once, not polled: the backend's data home does not move while it runs.
  // Sole consumer is the payload path quoted into the agent prompt.
  const healthQuery = useQuery({ queryKey: DT_KEY.health, queryFn: fetchHealth, retry: false })
  const dataDir = healthQuery.data?.dataDir || ''
  // The prompt quotes a payload path built from the data home, and the sender
  // must not close over `dataDir` — the health read settles after the first
  // render, so a captured copy is the empty string and the agent is handed a
  // path with no data home in it. A ref is read at send time instead (same
  // reason as `previewIdRef` below).
  const dataDirRef = useRef(dataDir)
  useEffect(() => { dataDirRef.current = dataDir }, [dataDir])

  const projects = projectsQuery.data?.projects ?? NO_PROJECTS
  const activeId = projectsQuery.data?.activeId ?? ''
  const serving = !!projectsQuery.data?.serving
  const pending = queueQuery.data?.pending ?? NO_REQUESTS
  const history = historyQuery.data?.history ?? NO_REQUESTS
  // True until the FIRST projects fetch settles (`isPending` is false once it
  // errors too). Without it the panel paints its "no apps registered" empty state
  // during the fetch, so reopening the app reads as "my apps are gone" for a beat.
  const booting = projectsQuery.isPending

  /** Re-read all three lists. Awaited by callers that need the new state. */
  const refresh = useCallback(
    () => queryClient.invalidateQueries({ queryKey: DT_KEY.all }),
    [queryClient],
  )

  // Adopt the backend's idea of the active project on the first fetch that has
  // one. Both writes are fill-only (`cur || …`), so a later poll never yanks the
  // dropdown or the preview out from under the user.
  useEffect(() => {
    const d = projectsQuery.data
    if (!d) return
    setSelectedId((cur) => cur || d.activeId || (d.projects?.[0]?.id ?? ''))
    // Restore the last previewed project across visits/restarts — unless the
    // user explicitly disconnected.
    let wasDisconnected = false
    try { wasDisconnected = localStorage.getItem('ste_disconnected') === '1' } catch { /* ignore */ }
    if (d.activeId && !wasDisconnected) setPreviewId((cur) => cur || (d.activeId as string))
  }, [projectsQuery.data])

  // cid -> { req, comment } across pending AND history, so a follow-up can
  // resolve its origin comment even after that request was archived.
  const commentIndexRef = useRef<Record<string, { req: Request; comment: Comment }>>({})
  useEffect(() => {
    const idx: Record<string, { req: Request; comment: Comment }> = {}
    for (const req of [...pending, ...history]) {
      for (const c of req.comments || []) idx[c.cid] = { req, comment: c }
    }
    commentIndexRef.current = idx
  }, [pending, history])

  // Label a follow-up by its origin ("3.1") rather than a raw cid.
  const withFollowUpLabels = useCallback((reqs: Request[]): Request[] => reqs.map((req) => ({
    ...req,
    comments: (req.comments || []).map((c) => {
      if (!c.followUpTo) return c
      const origin = commentIndexRef.current[c.followUpTo]
      return origin
        ? { ...c, followUpLabel: `${origin.req.number}.${origin.comment.index}` }
        : c
    }),
  })), [])

  // Dismiss the app selector on an outside click or Escape, and drive its
  // entrance. Uses mousedown (not click) so the menu closes on press rather
  // than waiting for release, and `capture` so a stopPropagation() handler
  // deeper in the tree cannot swallow the dismissal.
  useEffect(() => {
    if (!ddOpen) { setDdIn(false); return }
    const raf = requestAnimationFrame(() => setDdIn(true))
    const onDown = (e: MouseEvent) => {
      // "Outside" is anything that is neither the trigger nor the panel — note
      // that the Connect button sits in the same row, so scoping to those two
      // elements (rather than their shared parent) means clicking Connect
      // dismisses the menu too.
      const inTrigger = ddTriggerRef.current?.contains(e.target as Node)
      const inPanel = ddPanelRef.current?.contains(e.target as Node)
      if (!inTrigger && !inPanel) { setDdOpen(false); setAdding(false) }
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { setDdOpen(false); setAdding(false) }
    }
    document.addEventListener('mousedown', onDown, true)
    document.addEventListener('keydown', onKey)
    return () => {
      cancelAnimationFrame(raf)
      document.removeEventListener('mousedown', onDown, true)
      document.removeEventListener('keydown', onKey)
    }
  }, [ddOpen])

  const toggleReq = useCallback((id: string) => {
    setReqOpen((m) => ({ ...m, [id]: !m[id] }))
  }, [])

  // Tracks each comment's last-seen status so we can auto-reload the preview
  // exactly once when a comment for the current app transitions to "done".
  const seenStatusRef = useRef<Record<string, string> | null>(null)

  // Which per-app chat slots we've ensured, mapped to the key the HOST returned
  // (it normalizes what we ask for, so we must not assume ours survived).
  const ensuredSlots = useRef<Map<string, string>>(new Map())
  const ensureSlot = useCallback(async (path: string, label: string): Promise<string> => {
    const want = slotKeyFor(path)
    if (ensuredSlots.current.has(want)) return ensuredSlots.current.get(want) as string
    // Slot creation is idempotent: an existing key returns that slot. Listing
    // first was worse than useless — the list holds only OPEN sessions, so a
    // closed one read as absent and we tried to create it again. `title` pins
    // the name so the session can never be auto-titled from a request body.
    let key = want
    try {
      const slot: ChatSlotResponse = await createChatSlot(want, SESSION_TITLE(label))
      if (slot?.key) key = slot.key
      // Before any seed or send: the slot decides the runner's file-search and
      // `@`-mention scope and which `.kiro/steering` loads, and slot creation
      // does not take a project. Failing this must not take the session down —
      // an unscoped session still works off the absolute paths in the seed — so
      // it is caught separately from the seed below rather than sharing its
      // catch, which would also skip the seed.
      try {
        await setChatSlotProject(key, path)
      } catch { /* unscoped is degraded, not broken: the seed carries absolute paths */ }
      // `messages` is a COUNT on this response, not a list. It was read as
      // `messages?.length`, which is `undefined` for a number — so the guard was
      // always true and the session seed went out again on every panel reload,
      // stacking framing prompts into the conversation.
      //
      // Seed only when the count positively says the slot is empty. `undefined`
      // means the shape was not what we expect, and NOT seeding is the safer
      // reading of that: a missing seed just costs some framing, while a spurious
      // one pollutes a live conversation every time the panel mounts.
      if (slot?.messages === 0) await sendChatMessage(SESSION_SEED(label, path), key)
    } catch { /* the send below creates the slot on demand anyway */ }
    ensuredSlots.current.set(want, key)
    return key
  }, [])

  // The preview frame's origin. Declared up here because both message paths
  // below are registered once for the panel's lifetime and must read the CURRENT
  // value; it is filled in by the effect next to `previewSrc`, where the URL the
  // iframe is actually pointed at is derived.
  const previewOriginRef = useRef('')

  // Post a message down into the preview overlay (host → overlay channel).
  //
  // Targets the frame's real origin, never '*'. That is possible because the
  // preview is served from one of the app's OWN loopback servers rather than the
  // dashboard origin, so the origin is both distinct and knowable. Until it is
  // resolved we send nothing: a dropped state message is retried by the effects
  // that own it, whereas a wildcard post would hand the payload to whatever
  // document happens to be framed.
  const postToOverlay = useCallback((msg: Record<string, unknown>) => {
    const target = previewOriginRef.current
    if (!target) return
    try {
      iframeRef.current?.contentWindow?.postMessage({ source: 'kiro-ste-host', ...msg }, target)
    } catch { /* iframe not ready */ }
  }, [])

  const summarizeEl = useCallback((payload?: { selection?: EditSelection } | null) => {
    const el = payload?.selection?.elements?.[0] || {}
    let name = el.tag || ''
    if (el.id) name += `#${el.id}`
    else if (el.classes?.length) name += '.' + el.classes.slice(0, 2).join('.')
    return name
  }, [])

  // Resizable left rail — default 500px, persisted, clamped 360–800.
  const [railW, setRailW] = useState<number>(() => {
    try { return Math.min(800, Math.max(360, parseInt(localStorage.getItem('ste_rail_w') || '', 10) || 500)) }
    catch { return 500 }
  })
  const onDragStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    const startX = e.clientX
    const startW = railW
    const onMove = (ev: MouseEvent) => {
      const w = Math.min(800, Math.max(360, startW + (ev.clientX - startX)))
      setRailW(w)
    }
    const onUp = (ev: MouseEvent) => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      const w = Math.min(800, Math.max(360, startW + (ev.clientX - startX)))
      try { localStorage.setItem('ste_rail_w', String(w)) } catch { /* ignore */ }
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }, [railW])

  const selected = projects.find((p) => p.id === selectedId)
  const connected = !!previewId && previewId === selectedId

  const switchTo = useCallback((p?: Project | null) => {
    if (!p) return
    setSelectedId(p.id)
    setPreviewId(p.id)               // instant — all projects are always served
    setPreviewNonce(Date.now())
    setMode('preview')
    setStatus('')
    try { localStorage.removeItem('ste_disconnected') } catch { /* ignore */ }
    // Persist as the "last previewed" so it restores after a restart.
    selectProject(p.id).catch(() => {})
  }, [])

  const connect = useCallback(() => switchTo(selected), [switchTo, selected])

  const disconnect = useCallback(() => {
    setPreviewId('')
    setStatus('')
    try { localStorage.setItem('ste_disconnected', '1') } catch { /* ignore */ }
  }, [])

  const removeProject = useCallback(async (p: Project) => {
    try {
      const out = await apiRemoveProject(p.id)
      if (out?.ok) {
        if (previewId === p.id) setPreviewId('')
        setSelectedId((cur) => (cur === p.id ? '' : cur))
        refresh()
      } else {
        setStatus(i18nT('apps.designTweak.status.remove_failed', {
          error: out?.error || i18nT('apps.designTweak.status.unknown'),
        }))
      }
    } catch (err) {
      setStatus(i18nT('apps.designTweak.status.remove_failed', { error: errMsg(err) }))
    }
  }, [previewId, refresh])

  const addProject = useCallback(async (pathArg?: string) => {
    const p = (pathArg || newPath).trim()
    if (!p) return
    try {
      const out = await apiAddProject(p)
      if (out?.ok) {
        setNewPath(''); setAdding(false); setDdOpen(false)
        switchTo(out.project)   // newly added apps preview immediately
        refresh()
        const name = out.project?.name || ''
        setStatus(
          out.updated === 'previewUrl'
            ? i18nT('apps.designTweak.status.set_dev_server_for', { name })
            : out.existing
              ? i18nT('apps.designTweak.status.already_registered', { name })
              : out.autoDetected
                ? i18nT('apps.designTweak.status.added_found_dev_server', {
                    name, url: out.project?.previewUrl || '',
                  })
                : (out.detected || []).length > 1
                  ? i18nT('apps.designTweak.status.added_many_dev_servers', {
                      name, n: (out.detected || []).length,
                    })
                  : out.project?.previewUrl
                    ? i18nT('apps.designTweak.status.added_dev_previewing', { name })
                    : i18nT('apps.designTweak.status.added_previewing', { name }),
        )
      } else {
        setStatus(i18nT('apps.designTweak.status.add_failed', {
          error: out?.error || i18nT('apps.designTweak.status.unknown'),
        }))
      }
    } catch (err) {
      setStatus(i18nT('apps.designTweak.status.add_failed', { error: errMsg(err) }))
    }
  }, [newPath, refresh, switchTo])

  // Picking a folder registers it straight away — the simple flow. Whether it
  // then previews from disk or needs a dev server is the backend's call, and the
  // preview panel says which.
  const pickFolder = useCallback(async () => {
    setStatus(i18nT('apps.designTweak.status.opening_folder_picker'))
    try {
      const out = await apiPickFolder()
      if (out?.ok && out.path) {
        setStatus('')
        addProject(out.path)
      } else if (out?.canceled) {
        setStatus('')
      } else {
        // picker unavailable (permission denied / no native chooser) — fall back to typing
        setAdding(true)
        setStatus(i18nT('apps.designTweak.status.picker_unavailable', {
          error: out?.error || i18nT('apps.designTweak.status.unknown'),
        }))
      }
    } catch (err) {
      setAdding(true)
      setStatus(i18nT('apps.designTweak.status.picker_unavailable', { error: errMsg(err) }))
    }
  }, [addProject])

  // Seal a draft request and hand the WHOLE batch to the agent as one prompt.
  // Seal-on-send: once sealed the request never accepts comments again, so the
  // next capture opens a fresh draft even while this batch is still running.
  //
  // ORDER: seal FIRST, then build the prompt from the snapshot `/send` RETURNS.
  // Both naive orders lose work. Dispatching first and sealing after leaves a
  // window where a second tab's capture joins the draft after the prompt was
  // built, so `/send` then seals a comment that was never dispatched and it is
  // stranded with no Send button. Sealing first closes that window — the seal is
  // the atomic cut, and the returned snapshot is exactly what was sealed, so the
  // prompt can never disagree with it. The original hazard of sealing first (a
  // failed dispatch leaving the request sealed but undelivered) is handled by the
  // retry path below rather than by reordering.
  // Build the prompt from a SEALED snapshot and hand it to the agent, then record
  // the acknowledgement. Split out of `sendRequest` because the retry bar needs
  // exactly this half WITHOUT re-sealing: a stranded request is already sealed, so
  // calling `/send` again would only report `already` and dispatch nothing.
  const deliverSealed = useCallback(async (snap: Request, req: Request) => {
    const sealedComments = snap.comments || []
    const msg = REQUEST_PROMPT(snap, sealedComments, requestPayloadPath(dataDirRef.current, req.id))

    // Route into THIS web app's dedicated session so every request for the
    // app is a turn in the same conversation.
    const proj = projects.find((p) => p.id === previewId)
    const path = snap.projectRoot || req.projectRoot || proj?.path || ''
    const label = proj?.name || FALLBACK_APP_LABEL
    // `sendChatMessage` pins `ws=1` so the host answers with JSON; without it
    // the reply is an SSE stream, and the parse error used to be "recovered"
    // by opening a NEW ad-hoc chat, so one request produced two sessions.
    const key = path ? await ensureSlot(path, label) : ''
    if (!key) {
      // No project folder, so there is no session to route into. Report it here
      // and leave the request sealed-unacknowledged: the rail keeps a Send
      // control for it, so nothing is lost. Never navigate away — this panel is
      // the whole surface for the app, and ejecting the user into the chat tab
      // to finish a send is not the flow.
      setStatus(i18nT('apps.designTweak.status.no_session_for_request', {
        number: snap.number ?? req.number,
      }))
      return false
    }
    try {
      await sendChatMessage(msg, key)
      // The ack can fail after a successful dispatch. That used to be tracked in
      // memory and lost on reload, which is what made a retry able to duplicate
      // the batch. It no longer needs remembering: `verifyDelivery` asks the
      // session itself, so a failed ack here just leaves the request to be
      // resolved from ground truth.
      await markDelivered(req.id).catch(() => {})
      setStatus(i18nT('apps.designTweak.status.sent_request_to_session', {
        number: snap.number ?? req.number, n: sealedComments.length, label,
      }))
      return true
    } catch (dispatchErr) {
      // The dispatch threw, so the batch may or may not have landed — the host
      // could have accepted it and failed on the way back. Say so and stop:
      // `verifyDelivery` settles it against the session on the next load or
      // refresh, and the rail keeps the request actionable meanwhile.
      setStatus(i18nT('apps.designTweak.status.dispatch_failed_unconfirmed', {
        number: snap.number ?? req.number, error: errMsg(dispatchErr),
      }))
      return false
    }
  }, [projects, previewId, ensureSlot])

  const sendRequest = useCallback(async (req: Request) => {
    if (!(req.comments || []).length) return
    setSendingId(req.id)
    try {
      const sealed = await apiSendRequest(req.id)
      if (!sealed?.ok) {
        setStatus(i18nT('pages.chatPage.send_failed_with_error', {
          error: sealed?.error || i18nT('apps.designTweak.status.unknown'),
        }))
        return
      }

      // The seal is an ATOMIC cut with exactly one winner. `already` means this
      // caller lost — the request was sealed by someone else (a second tab, or a
      // double-click). Dispatching anyway would hand the agent a second copy of
      // the same batch, so it would apply every edit twice. `ok` is true on this
      // path too, so checking it above is not enough to tell the cases apart.
      if (sealed.already) {
        setStatus(i18nT('apps.designTweak.status.already_sent', {
          number: sealed.request?.number ?? req.number,
        }))
        refresh()
        return
      }

      // Build the prompt from the SEALED snapshot, not from the `req` we were
      // rendered with — that one may be a render behind, and anything it missed
      // is exactly what would otherwise be stranded.
      await deliverSealed(sealed.request || req, req)
      refresh()
    } catch (err) {
      setStatus(i18nT('pages.chatPage.send_failed_with_error', { error: errMsg(err) }))
    } finally {
      setSendingId('')
    }
  }, [refresh, deliverSealed])

  // Requests the SESSION provably does not have. Only these get a send control.
  //
  // Deliberately not "everything unconfirmed": until `verifyDelivery` has
  // answered, an unconfirmed request might be one whose ack was merely lost, and
  // offering a send for that applies every edit twice. An empty set is the safe
  // default, so a check that has not run yet — or could not run — shows nothing.
  const [missingIds, setMissingIds] = useState<ReadonlySet<string>>(() => new Set())

  // Settle every sealed-but-unconfirmed request against the SESSION, silently.
  //
  // The panel cannot know from its own memory whether a sealed batch reached the
  // agent — a lost ack and a lost dispatch look identical after a reload, and
  // guessing either way is harmful. So ask: the prompt carries the request id and
  // the slot-detail read returns the recent transcript plus the pending queue.
  //
  // Found -> acknowledge it and the request goes quiet, so the user never sees a
  // control for work that is already in hand. Not found -> record it as provably
  // missing, which is what renders the send control. Lookup failed -> change
  // nothing, because an unavailable answer must not read as either one.
  //
  // Grouped BY PROJECT ROOT, and each group asks its OWN slot. Both halves matter:
  // a request only exists in the slot its dispatch used, so checking one project's
  // requests against another project's session reads them all as missing, and the
  // slot must be addressed by `slotKeyFor(root)` — the same key `ensureSlot`
  // dispatches to — because a raw path adopts a DIFFERENT, empty slot and makes
  // every answer "missing" while leaving a junk session behind.
  const verifyDelivery = useCallback(async (reqs: Request[]) => {
    const pending = reqs.filter((r) => r.state !== 'draft' && needsDeliveryRetry(r))
    if (!pending.length) return
    const proj = projects.find((p) => p.id === previewId)

    // Group by the root each request was actually dispatched under.
    const byRoot = new Map<string, Request[]>()
    for (const r of pending) {
      const root = r.projectRoot || proj?.path || ''
      if (!root) continue // Unattributable: no slot to ask, so leave it unsettled.
      const group = byRoot.get(root)
      if (group) group.push(r)
      else byRoot.set(root, [r])
    }
    if (!byRoot.size) return

    const missing: string[] = []
    let healed = 0
    for (const [root, group] of byRoot) {
      const label = projects.find((p) => p.path === root)?.name || proj?.name
      const transcript = await readSlotTranscript(
        slotKeyFor(root),
        SESSION_TITLE(label || FALLBACK_APP_LABEL),
      )
      if (!transcript) continue // Unknown for this group only; others still settle.
      for (const r of group) {
        const verdict = deliveryVerdict(r, transcript)
        if (verdict === 'delivered') {
          // Best-effort: a failed ack here just means the next pass retries it.
          await markDelivered(r.id).catch(() => {})
          healed += 1
        } else if (verdict === 'missing') {
          missing.push(r.id)
        }
      }
    }
    setMissingIds(new Set(missing))
    if (healed) refresh()
  }, [projects, previewId, refresh])

  // Retry a sealed request the session does not have. No re-seal (`/send` would
  // only answer `already`), and no duplicate risk: the bar this runs from is only
  // rendered once `verifyDelivery` has confirmed the batch is genuinely absent.
  const resendRequest = useCallback(async (req: Request) => {
    setSendingId(req.id)
    try {
      const dispatched = await deliverSealed(req, req)
      // Only a CONFIRMED dispatch retires the retry control. `deliverSealed`
      // reports its own failures rather than throwing, so "did not throw" is not
      // success — clearing on that would hide the button after a failed send. On
      // success the id must go now: `verifyDelivery` is what recomputes this set,
      // and until it re-runs the bar stays live and a second click sends the same
      // edits again. If the batch is somehow still absent, that re-verify puts it
      // back from ground truth.
      if (dispatched) {
        setMissingIds((prev) => {
          if (!prev.has(req.id)) return prev
          const next = new Set(prev)
          next.delete(req.id)
          return next
        })
      }
      refresh()
    } catch (err) {
      setStatus(i18nT('pages.chatPage.send_failed_with_error', { error: errMsg(err) }))
    } finally {
      setSendingId('')
    }
  }, [refresh, deliverSealed])

  // Settle sealed-but-unconfirmed requests against the session whenever the
  // queue is (re)read — on mount and on every poll. Keyed on the ids that still
  // need settling, so a steady state stops re-asking; a genuinely absent batch
  // resolves to `missing` and stays that way without re-querying each poll.
  const unsettledKey = pending
    .filter((r) => r.state !== 'draft' && needsDeliveryRetry(r))
    .map((r) => r.id)
    .join(',')
  useEffect(() => {
    if (!unsettledKey) return
    void verifyDelivery(pending)
    // `pending` is intentionally not a dep: it is a fresh array every poll, and
    // the ids in `unsettledKey` are what actually decide whether to re-ask.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [unsettledKey, verifyDelivery])

  // it cannot close over `previewId` — it would capture '' forever and stamp every
  // comment with an empty projectId. A ref is read at call time instead.
  const previewIdRef = useRef(previewId)
  useEffect(() => { previewIdRef.current = previewId }, [previewId])

  // Messages from the in-preview overlay (overlay → panel channel).
  useEffect(() => {
    async function onMsg(e: MessageEvent) {
      // Three gates, cheapest first. This is a trust boundary: the framed
      // document is an arbitrary user project, and `window` receives messages
      // from every frame and opener on the page.
      //   1. WINDOW IDENTITY — only the preview frame we mounted may drive this,
      //      the same barrier McpAppFrame and useCommentBridge use.
      //   2. ORIGIN — checkable now that the preview is served from a known
      //      loopback origin instead of the dashboard's own.
      //   3. TYPE ALLOWLIST — an unrecognised type is dropped rather than
      //      falling through to the handlers below.
      if (!e.source || e.source !== iframeRef.current?.contentWindow) return
      if (previewOriginRef.current && e.origin !== previewOriginRef.current) return
      const d = e?.data as OverlayMessage | undefined
      if (!d || d.source !== 'kiro-select-to-edit') return
      if (d.type !== 'capture' && d.type !== 'dispatch') return

      // New comment captured on an element → append it to the project's open
      // draft request and ack the pin. Nothing is dispatched: sending the batch
      // is an explicit, separate step.
      if (d.type === 'capture' && d.payload) {
        try {
          // Stamp the project explicitly. The overlay only knows its own page
          // URL, and the backend can only infer a project from a URL it proxies
          // — so the panel, which knows exactly what it is previewing, says so.
          const out: SubmitResponse = await submitComment({ ...d.payload, projectId: previewIdRef.current })
          if (out?.ok) {
            postToOverlay({
              type: 'created', clientRef: d.clientRef,
              id: out.cid,                       // overlay keys pins by comment
              number: out.label,                 // "3.1"
              status: 'new', element: summarizeEl(d.payload),
              locator: d.payload?.selection?.elements?.[0]?.locator || '',
              thread: [{ role: 'user', text: d.payload.comment }],
            })
            setReqOpen((m) => ({ ...m, [out.id as string]: true }))   // reveal the draft
            setStatus(i18nT('apps.designTweak.status.added_comment', {
              label: out.label ?? '', n: out.commentCount ?? 0, number: out.number ?? 0,
            }))
            refresh()
          } else {
            setStatus(i18nT('apps.designTweak.status.capture_failed', {
              error: out?.error || i18nT('apps.designTweak.status.unknown'),
            }))
          }
        } catch (err) {
          setStatus(i18nT('apps.designTweak.status.capture_failed', { error: errMsg(err) }))
        }
        return
      }

      // Reply typed on an existing comment's pin → a NEW comment in the CURRENT
      // draft, linked back via followUpTo. The already-sent request is untouched.
      if (d.type === 'dispatch' && d.id && d.text) {
        try {
          const origin = commentIndexRef.current[d.id]
          if (!origin) { setStatus(i18nT('apps.designTweak.status.follow_up_origin_missing')); return }
          const out: SubmitResponse = await submitComment({
            type: 'visual_edit_request',
            comment: d.text,
            followUpTo: d.id,
            projectId: previewIdRef.current,
            previewUrl: origin.comment.previewUrl,
            selection: { mode: 'single', elements: [{ locator: origin.comment.locator }] },
          })
          if (out?.ok) {
            setReqOpen((m) => ({ ...m, [out.id as string]: true }))
            setStatus(i18nT('apps.designTweak.status.follow_up_added', {
              label: out.label ?? '', number: out.number ?? 0,
            }))
            refresh()
          } else {
            setStatus(i18nT('apps.designTweak.status.follow_up_failed', {
              error: out?.error || i18nT('apps.designTweak.status.unknown'),
            }))
          }
        } catch (err) {
          setStatus(i18nT('apps.designTweak.status.follow_up_failed', { error: errMsg(err) }))
        }
        return
      }
    }
    window.addEventListener('message', onMsg)
    return () => window.removeEventListener('message', onMsg)
  }, [postToOverlay, summarizeEl, refresh])

  const previewProject = projects.find((p) => p.id === previewId)

  // Does a comment belong to the project currently in the preview?
  //
  // Matches on the explicit projectId the backend stamps at capture time. The
  // previewUrl check is only a fallback for comments captured before that field
  // existed — on its own it can recognise nothing but URLs this backend
  // proxies, so a project previewed straight from its dev server (no
  // `/proxy/<id>/` in the URL) would lose every pin on the first reload.
  const belongsToPreview = useCallback((c: PreviewScoped) => {
    if (!previewId) return false
    if (c?.projectId) return c.projectId === previewId
    // projectRoot is checked BEFORE the URL, because it is the field that stayed
    // correct when projectId did not. Requests captured while the panel held a
    // stale empty previewId have projectId:"" but the right folder — matching on
    // the folder recovers them with no migration of live data.
    const root = previewProject?.path
    if (root && c?.projectRoot) return c.projectRoot === root
    // Last resort, and only ever true for a URL this backend proxies: a project
    // framed from its dev server has no /proxy/<id>/ segment at all.
    return (c?.previewUrl || '').includes(`/proxy/${previewId}/`)
  }, [previewId, previewProject])

  // Requests and history are scoped to the app in the preview. Each web app is a
  // separate body of work with its own dedicated chat session, so mixing them in
  // one list invites sending app A's comment while looking at app B — and makes
  // the panel read as someone else's backlog the moment you switch.
  const myPending = useMemo(
    () => pending.filter((r) => belongsToPreview(r)), [pending, belongsToPreview])
  const myHistory = useMemo(
    () => history.filter((r) => belongsToPreview(r)), [history, belongsToPreview])

  // Where the preview iframe points. It is ALWAYS one of the app's own loopback
  // servers — a dev-server injecting proxy, or the static preview server — so the
  // frame never shares the dashboard's origin. There is deliberately NO
  // gateway-proxied fallback: that route IS the dashboard origin, and framing it
  // with `allow-scripts allow-same-origin` neutralises the sandbox, handing
  // previewed project JavaScript the dashboard's authenticated APIs and parent
  // DOM. If the loopback server could not bind there is nothing safe to frame, so
  // `previewSrc` stays empty and the unreachable state renders instead. Do not
  // reintroduce a same-origin fallback here. The nonce is the reload lever.
  const previewSrc = previewProject?.previewUrl
    ? loopbackPreviewSrc(previewProject.previewUrl, previewNonce)
    : ''

  // The frame's origin, which is now KNOWABLE — that is the whole point of
  // serving the preview from our own loopback port. It is the exact target for
  // every host → overlay postMessage, and the value inbound messages are checked
  // against, so neither direction has to fall back to a '*' wildcard.
  const previewOrigin = useMemo(() => {
    try { return new URL(previewSrc, window.location.href).origin } catch { return '' }
  }, [previewSrc])
  // The message listener and postToOverlay are registered once for the panel's
  // lifetime, so they read the current origin through the ref declared above
  // rather than closing over a stale one.
  useEffect(() => { previewOriginRef.current = previewOrigin }, [previewOrigin])

  // Preview lifecycle: 'loading' → 'ready', or → 'unreachable'.
  //
  // A blank iframe is ambiguous — still fetching, dev server not started, backend
  // restarting — and a cross-origin frame will not tell us which, so we probe
  // alongside it and report what we can actually establish.
  //
  // Who wins differs by origin, and getting this backwards hides a WORKING
  // preview behind an error card:
  //   • same-origin (the gateway-proxied fallback) — `onLoad` is trustworthy, so
  //     it wins outright. The probe only catches a backend that never answers.
  //   • cross-origin (either loopback server) — Chrome fires `load` for its own
  //     "can't connect" page, so `onLoad` alone would report success on a dead
  //     server. Readiness there waits for the probe to come back clean.
  const [previewState, setPreviewState] = useState<'loading' | 'ready' | 'unreachable'>('loading')
  const [previewNote, setPreviewNote] = useState('')
  // Is the framed document cross-origin? True whenever we resolved a loopback
  // previewUrl — which is now the normal case for static folders too, so this is
  // deliberately NOT the same question as "is this a dev server".
  const isCrossOrigin = !!previewProject?.previewUrl
  // Is the preview actually backed by the project's dev server? Drives user-facing
  // copy only. A static folder now also has a previewUrl, so testing previewUrl
  // alone would label every static preview a dev server.
  const isDevServer = isCrossOrigin && previewProject?.previewMode !== 'static'
  const probeOkRef = useRef(false)
  const framedRef = useRef(false)

  // Called by the iframe's onLoad.
  //   • same-origin — the frame rendering IS the answer, whatever the probe said.
  //     It overrides an earlier failure: a preview that visibly works must never
  //     stay behind an error card.
  //   • cross-origin — Chrome fires load for its own "can't connect" page, so a
  //     frame alone proves nothing; wait for the probe, and never override a
  //     probe that already failed.
  const markFramed = useCallback(() => {
    framedRef.current = true
    setPreviewState((s) => {
      if (!isCrossOrigin) return 'ready'
      if (s === 'unreachable') return s
      return probeOkRef.current ? 'ready' : s
    })
  }, [isCrossOrigin])

  useEffect(() => {
    if (!previewId) return
    // No safe URL to frame — the loopback preview server could not bind. There is
    // no same-origin fallback by design (see the note on `previewSrc`), so report
    // it as unreachable rather than probing an empty URL or framing about:blank.
    if (!previewSrc) {
      setPreviewState('unreachable')
      setPreviewNote('')
      return
    }
    setPreviewState('loading')
    setPreviewNote('')
    probeOkRef.current = false
    framedRef.current = false
    let cancelled = false

    const probe = async () => {
      try {
        if (isCrossOrigin) {
          // Opaque by design; all we learn is reachable vs not, which is the
          // only thing that matters for a server we do not control.
          await fetch(previewSrc, { mode: 'no-cors', cache: 'no-store' })
        } else {
          const r = await fetch(previewSrc, { cache: 'no-store' })
          // A 4xx is deliberately NOT an error: the backend answers a missing
          // entry point with a diagnostic page listing the HTML it did find,
          // which is more useful than anything this overlay could say.
          if (r.status >= 500) throw new Error(`backend returned ${r.status}`)
        }
        if (cancelled) return
        probeOkRef.current = true
        if (framedRef.current) setPreviewState((s) => (s === 'unreachable' ? s : 'ready'))
      } catch (err) {
        if (cancelled) return
        // Never let a failed probe overrule a frame that already rendered — a
        // same-origin fetch can fail for reasons the iframe does not care about.
        if (framedRef.current && !isCrossOrigin) return
        setPreviewState('unreachable')
        setPreviewNote(errMsg(err))
      }
    }
    void probe()

    // Backstop for a server that accepts the connection but never answers:
    // without it the frame sits blank forever.
    const slow = setTimeout(() => {
      if (cancelled) return
      setPreviewState((s) => (s === 'loading' ? 'unreachable' : s))
      setPreviewNote((n) => n || i18nT('apps.designTweak.preview.timed_out'))
    }, 12000)

    return () => { cancelled = true; clearTimeout(slow) }
  }, [previewId, previewSrc, isCrossOrigin])




  // Push the previewed app's COMMENTS down to the overlay as pins. The overlay
  // keys a pin by `id`, so each comment's cid becomes its pin id.
  //
  // Scoped by REQUEST, not by comment: a request belongs to exactly one project by
  // construction, and it carries projectRoot — which comments do not. Filtering
  // per comment therefore dropped every comment of a request whose projectId was
  // written empty, which is precisely the dev-server case, and an empty list makes
  // the overlay remove all pins.
  const pinItems = useMemo(() => {
    const items: Record<string, unknown>[] = []
    for (const req of myPending) {
      for (const c of req.comments || []) {
        items.push({
          id: c.cid,
          number: `${req.number}.${c.index}`,
          status: c.status,
          comment: c.comment,
          element: c.element,
          locator: c.locator,
          parentLocator: c.parentLocator,   // element deleted → pin to where it was
          point: c.point,                   // element not created yet → pin to the click
          thread: c.thread || [],
        })
      }
    }
    return items
  }, [myPending])

  useEffect(() => {
    if (!previewId) return
    postToOverlay({ type: 'requests', items: pinItems })
  }, [pinItems, previewId, postToOverlay])

  // Click a comment in the left rail → toggle its pin bubble open/closed.
  const focusComment = useCallback((c: Comment) => {
    if (c?.cid) postToOverlay({ type: 'toggle', id: c.cid })
  }, [postToOverlay])

  // Open the Chat tab AT this app's dedicated session (deterministic slot).
  const openInChat = useCallback((item: Request) => {
    const proj = projects.find((p) => p.id === previewId)
    const path = (item && item.projectRoot) || proj?.path || ''
    navigate(chatRoute(path ? slotKeyFor(path) : undefined))
  }, [navigate, projects, previewId])

  // Archive → move to History (backend /clear moves queue → handled/).
  const archiveReq = useCallback(async (req: Request) => {
    try { await clearRequest(req.id); refresh() }
    catch (err) { setStatus(i18nT('apps.designTweak.status.archive_failed', { error: errMsg(err) })) }
  }, [refresh])

  // Delete → permanently remove the request, its comments, and their pins.
  // It sits one icon away from Archive but is irreversible, so it takes the same
  // `confirm()` guard the Sessions sidebar uses for its destructive deletes.
  // Archive stays un-confirmed on purpose: the request is still in History.
  const deleteReq = useCallback(async (req: Request) => {
    if (!confirm(i18nT('apps.designTweak.requests.delete_confirm', { number: req.number }))) return
    try { await deleteRequest(req.id); refresh() }
    catch (err) { setStatus(i18nT('apps.designTweak.status.delete_failed', { error: errMsg(err) })) }
  }, [refresh])

  // Remove one comment from a draft (backend refuses once the request is sent).
  const deleteComment = useCallback(async (req: Request, c: Comment) => {
    try {
      const out = await apiDeleteComment(req.id, c.cid)
      if (out?.error) setStatus(out.error)
      refresh()
    } catch (err) {
      setStatus(i18nT('apps.designTweak.status.remove_failed', { error: errMsg(err) }))
    }
  }, [refresh])

  // Auto-reload the preview when a COMMENT for the current app finishes, so the
  // agent's edit shows without a manual refresh. Fires only on the →done edge.
  useEffect(() => {
    const prev = seenStatusRef.current
    const cur: Record<string, string> = {}
    let doneNow: { label: string } | null = null
    for (const req of pending) {
      for (const c of req.comments || []) {
        cur[c.cid] = c.status
        const belongs = belongsToPreview(c)
        if (belongs && c.status === 'done' && prev && prev[c.cid] && prev[c.cid] !== 'done') {
          doneNow = { label: `${req.number}.${c.index}` }
        }
      }
    }
    seenStatusRef.current = cur
    if (prev && doneNow && previewId) {
      setPreviewNonce(Date.now())     // bump iframe src → reload; pins re-anchor on load
      setStatus(i18nT('apps.designTweak.status.preview_refreshed', { label: doneNow.label }))
    }
  }, [pending, previewId, belongsToPreview])


  // Set or clear the dev-server URL of the project already being previewed.
  // Registering via the add-form only covers NEW projects; this is how an
  // existing one gets pointed at a dev server without re-adding its folder.
  const setDevServer = useCallback(async (url: string) => {
    if (!previewId) return
    try {
      const out = await setPreviewUrl(previewId, url)
      if (out?.error) { setStatus(out.error); return }
      setDevOpen(false)
      setDevDraft('')
      await refresh()
      setPreviewNonce(Date.now())      // reload the frame at the new target
      const name = previewProject?.name || FALLBACK_APP_LABEL
      setStatus(url
        ? i18nT('apps.designTweak.status.previewing_from_url', { name, url })
        : i18nT('apps.designTweak.status.previewing_from_disk', { name }))
    } catch (err) {
      setStatus(i18nT('apps.designTweak.status.failed', { error: errMsg(err) }))
    }
  }, [previewId, previewProject, refresh])

  // One click for the common case: find the dev server for THIS project and use
  // it. Falls back to revealing the input when there is nothing unambiguous.
  const useDetectedDevServer = useCallback(async () => {
    if (!previewId) return
    setDetecting(true)
    setStatus(i18nT('apps.designTweak.status.looking_for_dev_server'))
    try {
      const out = await detectDevServer(previewId)
      if (out?.suggested) { await setDevServer(out.suggested); return }
      if ((out?.candidates || []).length > 1) {
        setDevOpen(true)
        setDevDraft(out.candidates![0].url)
        setStatus(i18nT('apps.designTweak.status.servers_match_folder', {
          n: out.candidates!.length,
          ports: out.candidates!.map((c) => c.port).join(', '),
        }))
        return
      }
      setDevOpen(true)
      setStatus(i18nT('apps.designTweak.status.no_dev_server_found'))
    } catch (err) {
      setStatus(i18nT('apps.designTweak.status.detect_failed', { error: errMsg(err) }))
    } finally { setDetecting(false) }
  }, [previewId, setDevServer])

  // Start this project's own dev server, then preview it. Adopts a server the
  // user already has running rather than starting a second one.
  const startDevServer = useCallback(async () => {
    if (!previewId) return
    setStarting(true)
    setDevError('')
    setStatus(previewProject?.devCommand
      ? i18nT('apps.designTweak.status.starting_command', { command: previewProject.devCommand })
      : i18nT('apps.designTweak.status.starting_dev_server'))
    try {
      const out = await apiStartDevServer(previewId)
      if (!out?.ok) {
        setDevError(out?.error || i18nT('apps.designTweak.status.could_not_start_dev_server'))
        setStatus('')
        return
      }
      await refresh()
      setPreviewNonce(Date.now())
      // Report the DEV server's own address, not the injecting proxy's ephemeral
      // port — 5173 is the number the user recognises and can open themselves.
      const shown = out.devUrl || out.url || ''
      setStatus(out.adopted
        ? i18nT('apps.designTweak.status.using_running_dev_server', { url: shown })
        : i18nT('apps.designTweak.status.dev_server_running', { url: shown }))
    } catch (err) { setDevError(errMsg(err)) }
    finally { setStarting(false) }
  }, [previewId, previewProject, refresh])

  const stopDevServer = useCallback(async () => {
    if (!previewId) return
    try {
      await apiStopDevServer(previewId)
      setDevError('')
      await refresh()
      setPreviewNonce(Date.now())
      setStatus(i18nT('apps.designTweak.status.dev_server_stopped'))
    } catch (err) {
      setStatus(i18nT('apps.designTweak.status.stop_failed', { error: errMsg(err) }))
    }
  }, [previewId, refresh])

  const setEditMode = useCallback((m: 'preview' | 'edit') => {
    setMode(m)
    try {
      // Resolve the live theme tokens so the in-page overlay matches the host.
      const cs = getComputedStyle(document.documentElement)
      const v = (n: string) => cs.getPropertyValue(n).trim()
      const theme = {
        accent: v('--accent'), accentFg: v('--accent-fg'), panel: v('--panel'),
        card: v('--card'), bgElevated: v('--bg-elevated'),
        text: v('--text'), textStrong: v('--text-strong'), muted: v('--muted'),
        border: v('--border'), info: v('--info'), ok: v('--ok'), warn: v('--warn'),
      }
      postToOverlay({ type: 'state', editMode: m === 'edit', theme })
    } catch { /* iframe not ready */ }
  }, [postToOverlay])

  // Reference otherwise-derived-only state so an unused-locals build stays green
  // without dropping fields the backend contract and future UI still rely on.
  void activeId; void serving; void stopDevServer

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
                            className="flex items-center justify-center text-muted hover:text-text hover:bg-bg-elevated cursor-pointer opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100 focus-visible:opacity-100"
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
                {devError && (
                  <div
                    className="text-[12px] leading-snug"
                    style={{
                      color: 'var(--danger)', background: 'var(--danger-subtle)',
                      padding: '8px 10px', borderRadius: 8, whiteSpace: 'pre-wrap',
                    }}
                    role="alert"
                  >
                    {devError}
                  </div>
                )}
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

// Normalize a thrown value to a message string (replaces the original
// `err?.message || err` idiom in a type-safe way).
function errMsg(err: unknown): string {
  if (err instanceof Error) return err.message
  if (typeof err === 'string') return err
  try { return String(err) } catch { return i18nT('apps.designTweak.status.unknown_error') }
}
