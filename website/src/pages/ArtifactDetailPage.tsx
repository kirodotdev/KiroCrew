import { safeSetItem } from '../utils/safeStorage'
import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import WebAppArtifactCard from '../components/WebAppArtifactCard'
import { useNavigate, useParams } from 'react-router-dom'
import { useQuery, useQueryClient, useMutation } from '@tanstack/react-query'
import { ArrowLeft, AlertTriangle, ArrowUp, Camera, ExternalLink, Download, GitFork, Pencil, RefreshCw, X, AlertCircle, RotateCcw, Plus, Sparkles, MessageSquare, Monitor, Undo2, Upload, Star, Folder as FolderIcon } from 'lucide-react'
import { useTheme } from '../hooks/useTheme'
import { type IframeSelection } from '../hooks/useCommentBridge'
import { useAppDispatch, useAppSelector } from '../store'
import { switchSlot } from '../store/chatSlice'
import { fetchSlots, addSlotOptimistic, removeSlotOptimistic } from '../store/dashboardSlice'
import { safeHttpUrl } from '../lib/safeUrl'
import { sanitizeCssValue } from '../lib/cssSanitize'
import { THEME_VAR_NAMES, buildSrcdoc } from '../lib/widgetSrcdoc'
import { api } from '../api/client'
import { PageHeader, Card, Badge, Btn, Input } from '../components/ui'
import SimpleSelect from '../components/SimpleSelect'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from '../components/ui/dropdown-menu'
import ReadingWidthToggle from '../components/ReadingWidthToggle'
import { useReadingWidth } from '../hooks/useReadingWidth'
import { useArtifactFolders, useMoveArtifactToFolder } from '../hooks/useArtifactFolders'
import { FolderPickerItems } from '../components/FolderMoveSubmenu'
import { folderBreadcrumb } from '../utils/artifactFolderTree'
import { CommentPopover } from '../components/CommentOverlay'
import { CommentsSidebar } from '../components/CommentsSidebar'
import { ArtifactChatPanel } from '../components/ArtifactChatPanel'
import { CommentThreadPopover } from '../components/CommentThreadPopover'
import { findCoords, resolveSourcePos } from '../components/MarkdownPanel'
// Artifact body renderers, extracted here so the chat side panel shares them.
import { ArtifactBodyNative, ArtifactBodyIframe, isEditableKind } from '../components/ArtifactBody'
import { useArtifactPopouts } from '../hooks/useArtifactPopouts'
import { forwardToMain, type NavIntent } from '../utils/artifactPopout'
import { writePrefill } from '../utils/navIntent'
import { announceCommentsChanged, onCommentsChanged } from '../utils/artifactCommentsSync'
import { setArtifactEditing } from '../utils/artifactEditGuard'
import { consumeJustCreatedBlank } from '../lib/blankHandoff'
import { hasPendingArtifactWrite } from '../lib/artifactWrites'
import { USER_SELECTABLE_KINDS } from '../lib/artifactKinds'
import { PublishHub } from '../components/PublishHub'
import type { Artifact, ArtifactEvent, ArtifactComment, CommentAnchor, ChatSlot } from '../types'

import { i18nT } from '../i18n/t'
import { fmtDateFields } from '../i18n/format'
import ErrorNotice from '../components/ErrorNotice'
/**
 * The artifact's active companion session: the bound slot for `slug`, or the most
 * recently active one if a race or a History-page resume left more than one.
 * Module-level so `openCompanionChat` can apply the identical rule to a freshly
 * fetched slots payload, not just the Redux snapshot.
 */
function pickBoundSlot(slots: ChatSlot[] | undefined, slug: string): ChatSlot | null {
  const matches = (slots ?? []).filter((x) => x.artifact === slug)
  if (matches.length <= 1) return matches[0] ?? null
  return [...matches].sort((a, b) =>
    (b.last_activity_ts || '').localeCompare(a.last_activity_ts || ''))[0]
}

function readThemeVars(): Record<string, string> {
  if (typeof window === 'undefined' || typeof document === 'undefined') return {}
  const computed = getComputedStyle(document.documentElement)
  const out: Record<string, string> = {}
  for (const name of THEME_VAR_NAMES) {
    const v = sanitizeCssValue(computed.getPropertyValue(name))
    if (v) out[name] = v
  }
  return out
}

export { isEditableKind }

/**
 * Header folder chip: shows where the artifact is filed and opens
 * a picker to move it (metadata-only — no version bump). Mirrors the tag-chip
 * row's inline-mutation pattern.
 */
function FolderChip({ artifact }: { artifact: Artifact }) {
  const { folders } = useArtifactFolders()
  const moveArtifact = useMoveArtifactToFolder()
  const chain = folderBreadcrumb(folders, artifact.folder_id || '')
  const current = chain.length ? chain[chain.length - 1] : null
  const path = chain.map(f => f.name).join(' › ')
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className={`inline-flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded border cursor-pointer bg-bg-elevated transition-colors ${
            current ? 'border-border text-muted hover:text-text' : 'border-dashed border-border text-muted hover:text-text hover:border-border-strong'
          }`}
          title={current ? i18nT('pages.artifactDetailPage.filed_in_click_to_move', { path }) : i18nT('pages.artifactDetailPage.not_in_a_folder_click_to_file')}
          aria-label={current ? i18nT('pages.artifactDetailPage.folder_move_to_folder', { path }) : i18nT('pages.artifactDetailPage.move_to_folder')}
        >
          <FolderIcon size={10} className={current ? 'text-accent' : undefined} />
          {current ? current.name : 'folder'}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="min-w-[190px] max-h-[300px] overflow-y-auto">
        <FolderPickerItems
          folders={folders}
          currentFolderId={artifact.folder_id || null}
          onPick={(fid) => moveArtifact(artifact.slug, fid || '')}
          Item={DropdownMenuItem}
        />
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/** Format an ISO timestamp into a short human-readable string for the
 * activity timeline ('5/25/26, 10:31 PM'). Falls back to the raw string
 * if Date parsing fails. */
function formatEventTs(ts: string): string {
  if (!ts) return '?'
  const d = new Date(ts)
  if (isNaN(d.getTime())) return ts
  return fmtDateFields(d, {
    year: '2-digit', month: 'numeric', day: 'numeric',
    hour: 'numeric', minute: '2-digit',
  })
}

/** Lifecycle activity timeline. Renders a chronological feed of
 * created/edited/iterated/referenced/reverted events from the artifact's
 * audit log. */
const ActivityTimeline = memo(function ActivityTimeline({
  events, navigateToSlot,
}: {
  events: ArtifactEvent[]
  navigateToSlot: (slotKey: string) => void
}) {
  if (!events.length) {
    return (
      <div className="text-[12px] text-muted">{i18nT('pages.artifactDetailPage.no_lifecycle_events_yet')}</div>
    )
  }
  // Render newest first so the most recent activity is at the top.
  const ordered = [...events].sort((a, b) => (a.ts < b.ts ? 1 : a.ts > b.ts ? -1 : 0))
  const verb = (t: ArtifactEvent['type'], md?: ArtifactEvent['metadata']) => {
    if (t === 'comment') {
      const action = typeof md?.action === 'string' ? md.action : ''
      return {
        deleted: i18nT('pages.artifactDetailPage.comment_removed'),
        reviewed: i18nT('pages.artifactDetailPage.comment_marked_for_review'),
        resolved: i18nT('pages.artifactDetailPage.comment_resolved'),
      }[action] ?? i18nT('pages.artifactDetailPage.comment')
    }
    return {
      created: i18nT('pages.artifactDetailPage.created'),
      edited: i18nT('pages.artifactDetailPage.edited'),
      iterated: i18nT('pages.artifactDetailPage.iterated'),
      referenced: i18nT('pages.artifactDetailPage.referenced'),
      reverted: i18nT('pages.artifactDetailPage.reverted'),
    }[t] ?? t
  }
  // Distinct hues per type so created/edited/iterated don't visually blur
  // together. reverted uses warn (orange) to flag its
  // 'undo-style' semantics; iterated uses info (cyan) so agent-driven
  // updates visually separate from user edits (accent/violet).
  const dot = (t: ArtifactEvent['type']) => ({
    created: 'var(--ok)',
    edited: 'var(--accent)',
    iterated: 'var(--info)',
    referenced: 'var(--muted)',
    reverted: 'var(--warn)',
    comment: 'var(--muted)',
  }[t] ?? 'var(--muted)')
  // Some session_id values are markers, not real chat slots — skip the
  // 'from session …' link for those so users don't get sent to the wrong
  // slot. The dashboard's browser client uses 'dashboard:ui' for every
  // request; cron jobs prefix with 'cron:'. Real slot keys never contain
  // a colon (they're random IDs).
  const isRealSlotKey = (sk?: string) =>
    !!sk && sk !== 'dashboard:ui' && !sk.startsWith('cron:') && !sk.startsWith('ui:')
  return (
    <ul className="space-y-1.5 m-0 p-0 list-none">
      {ordered.map((ev, i) => (
        <li key={i} className="flex items-start gap-2 text-[12px]">
          <span
            className="mt-1.5 inline-block w-1.5 h-1.5 rounded-full shrink-0"
            style={{ background: dot(ev.type) }}
            aria-hidden
          />
          <div className="flex-1 min-w-0">
            <div className="flex flex-wrap items-baseline gap-x-2">
              <span className="font-medium text-text">{verb(ev.type, ev.metadata)}</span>
              {ev.by && <span className="text-muted">{i18nT('pages.artifactDetailPage.by')} {ev.by}</span>}
              {ev.type === 'comment' ? null : ev.type === 'reverted' && ev.from_version != null ? (
                <span className="text-muted">{i18nT('pages.artifactDetailPage.v')}{ev.from_version} {i18nT('pages.artifactDetailPage.v_2')}{ev.version}</span>
              ) : (
                ev.version != null && <span className="text-muted">{i18nT('pages.artifactDetailPage.v_2')}{ev.version}</span>
              )}
              <span className="text-muted ml-auto">{formatEventTs(ev.ts)}</span>
            </div>
            {/* Comment events carry a snippet of the affected comment (and the
                agent's reason on deletes) so the timeline stays readable after
                the comment itself is gone. */}
            {ev.type === 'comment' && typeof ev.metadata?.comment_snippet === 'string' && ev.metadata.comment_snippet ? (
              <div className="text-[11px] text-muted mt-0.5 truncate" title={String(ev.metadata.comment_snippet)}>
                “{ev.metadata.comment_snippet}”
                {typeof ev.metadata.reason === 'string' && ev.metadata.reason ? ` — ${ev.metadata.reason}` : ''}
              </div>
            ) : null}
            {/* Source qualifier under the headline. For real chat slots this
                is a clickable link; for dashboard / cron / unknown markers
                it's plain muted text so users don't think it's actionable. */}
            {ev.session_id && isRealSlotKey(ev.session_id) ? (
              <button
                type="button"
                onClick={() => navigateToSlot(ev.session_id as string)}
                className="text-[11px] text-accent hover:underline cursor-pointer bg-transparent border-none p-0 mt-0.5"
                title={i18nT('pages.artifactDetailPage.open_session', { sessionId: ev.session_id })}
              >
                {i18nT('pages.artifactDetailPage.from_session')} {ev.session_id}
              </button>
            ) : ev.type === 'reverted' && ev.from_version != null ? (
              <span className="text-[11px] text-muted mt-0.5">
                {i18nT('pages.artifactDetailPage.content_copied_from_v')}{ev.from_version}
              </span>
            ) : ev.session_id === 'dashboard:ui' ? (
              <span className="text-[11px] text-muted mt-0.5">{i18nT('pages.artifactDetailPage.via_dashboard')}</span>
            ) : null}
          </div>
        </li>
      ))}
    </ul>
  )
})

/**
 * The pop-out control in the artifact detail toolbar. Opens the artifact in its
 * own browser window and, once it's out, swaps to Focus + Bring-back (mirrors
 * the chat session popout menu). Kept as a child so the `useArtifactPopouts`
 * subscription only runs on the main dashboard — never inside the popout window
 * itself (where this control isn't rendered).
 */
function ArtifactPopoutControl({ slug, name }: { slug: string; name: string }) {
  const { isPoppedOut, open, focus, bringBack } = useArtifactPopouts()
  if (isPoppedOut(slug)) {
    return (
      <>
        <button
          type="button"
          onClick={() => focus(slug)}
          className="p-1.5 rounded-md border border-accent text-accent bg-accent-subtle cursor-pointer transition-all"
          title={i18nT('pages.artifactDetailPage.focus_the_popped_out_window')}
          aria-label={i18nT('pages.artifactDetailPage.focus_popped_out_window')}
        >
          <Monitor size={13} />
        </button>
        <button
          type="button"
          onClick={() => bringBack(slug)}
          className="p-1.5 rounded-md border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all"
          title={i18nT('pages.artifactDetailPage.bring_the_artifact_back_into_this_window')}
          aria-label={i18nT('pages.artifactDetailPage.bring_artifact_back_to_this_window')}
        >
          <Undo2 size={13} />
        </button>
      </>
    )
  }
  return (
    <button
      type="button"
      onClick={() => open(slug, name)}
      className="p-1.5 rounded-md border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all"
      title={i18nT('pages.artifactDetailPage.pop_out_into_its_own_window')}
      aria-label={i18nT('pages.artifactDetailPage.pop_out_to_window')}
    >
      <ExternalLink size={13} />
    </button>
  )
}

export default function ArtifactDetailPage({ popout = false }: { popout?: boolean } = {}) {
  const { slug = '' } = useParams<{ slug: string }>()
  const navigate = useNavigate()
  // Claimed once from the library's "New Artifact" action, which creates the
  // document empty and hands it over here. Two behaviours hang off it: the editor
  // opens focused (so the user starts typing rather than hunting for an Edit
  // button), and a document left empty and unnamed is cleaned up when they leave.
  // A page RELOAD clears the module-level hand-off, so a revisit never arms
  // either behaviour — which is the point of keeping it out of router state.
  //
  // Claimed in an EFFECT, never during render. Claiming a one-shot hand-off is a
  // side effect, and React may begin a render and then throw it away; a discarded
  // render had already spent the hand-off, so the surviving render concluded this
  // was not a fresh blank and the editor silently failed to open. Effects run only
  // on committed renders. The ref keeps the claim idempotent per instance, so a
  // double-invoked effect (StrictMode, remount) reuses the recorded answer rather
  // than re-asking a hand-off that is already spent.
  // Holds the name the library CREATED the document with, or null when this page
  // was not handed a fresh blank. Carried rather than re-derived: the untitled
  // placeholder is localised, so re-translating it on departure would let a
  // language change in between look like a deliberate rename.
  // The claim carries the SLUG as well as the name. Both are needed: this route is
  // reused when navigating straight from one artifact to another, so the page has
  // to be able to tell "the blank I was handed" from "whatever is on screen now".
  const [blankClaim, setBlankClaim] = useState<{ slug: string; createdName: string } | null>(
    null,
  )
  const claimRef = useRef<{ slug: string; claimed: string | null } | null>(null)
  useEffect(() => {
    if (claimRef.current?.slug !== slug) {
      claimRef.current = { slug, claimed: slug ? consumeJustCreatedBlank(slug) : null }
    }
    const claimed = claimRef.current.claimed
    setBlankClaim(claimed === null ? null : { slug, createdName: claimed })
  }, [slug])
  const justCreatedBlank = blankClaim !== null
  // Inline title rename. The artifact API has always accepted a name change;
  // the dashboard just never exposed one, which create-first naming makes
  // necessary (a document called "Untitled" you cannot rename is useless).
  const [renaming, setRenaming] = useState(false)
  const [nameDraft, setNameDraft] = useState('')
  // Has the user done anything with this document? Set the moment an action is
  // taken, locally, by every write path on this page — rename, save, tag, comment.
  //
  // Deliberately a flag and not a re-reading of the artifact record. Deriving the
  // answer from the record meant asking a value that lags: a write that had been
  // issued but not yet round-tripped left the document still looking brand new,
  // which is how five review rounds of "this deleted my work" happened. Set once,
  // locally, at the moment of the act — nothing to be stale about.
  // NOTE: there is deliberately no per-write-path flag here any more. Asking each
  // handler to announce itself failed four separate review rounds in a row, each
  // time because one path did not -- a folder move in a child component, a
  // live-edit flush, a snapshot, a revert, comment resolution -- and an
  // unannounced write let a document be deleted with its own request still in the
  // air. In-flight writes are now counted centrally in `lib/artifactWrites`, hooked
  // at the API transport where a request cannot avoid passing through, so a write
  // path added later is covered without anyone remembering to opt in. Writes the
  // server has already APPLIED need no client help at all: it re-reads the record
  // under its own lock.
  const queryClient = useQueryClient()
  const dispatch = useAppDispatch()
  const { theme, colorTheme, themeVersion } = useTheme()
  const [selectedVersion, setSelectedVersion] = useState<number | null>(null)
  const [editing, setEditing] = useState(false)
  // While editing, the user can flip to a rendered preview of the edit
  // buffer (matches the side panel's Edit/Preview toggle). Stays in edit
  // mode — content isn't committed until Save and isn't discarded until
  // Cancel.
  const [previewDuringEdit, setPreviewDuringEdit] = useState(false)
  const { readingWidth, toggle: toggleReadingWidth, previewStyle: mdPreviewStyle } = useReadingWidth()
  const [editedContent, setEditedContent] = useState('')
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [showPublish, setShowPublish] = useState(false)
  // Tag editing: tags shown in the header are editable inline. Adding a tag
  // posts metadata-only (no version bump). Removing a tag works the same way.
  const [addingTag, setAddingTag] = useState(false)
  const [newTag, setNewTag] = useState('')
  // ── Inline-comment state (durable via /api/artifacts/:slug/comments) ──
  const commentsQuery = useQuery<{ comments: ArtifactComment[]; remote_sync_error?: string | null }>({
    queryKey: ['artifact-comments', slug],
    queryFn: () => api.artifactComments(slug),
    enabled: !!slug,
    staleTime: 30_000,
  })
  const durableComments = commentsQuery.data?.comments ?? []
  const commentCount = durableComments.length
  const remoteSyncError = commentsQuery.data?.remote_sync_error ?? null
  // Right-hand panel state machine: the comments sidebar and the companion
  // chat panel share the same flex space, icon-toggled and mutually exclusive.
  // 'none' keeps the artifact full-width — an empty comment panel is just
  // wasted space on a dashboard or infographic — and comments auto-reveal only
  // once the artifact has at least one comment (see the effect below). A manual
  // show/hide applies to the current view only; we intentionally do NOT persist
  // it, so every artifact independently does the right thing instead of a
  // global pin re-opening empty panels everywhere.
  const [panel, setPanel] = useState<'none' | 'comments' | 'chat'>('none')
  // Flipped once the user manually toggles, so the comment-driven auto-reveal
  // below stops overriding an explicit choice — but only for the current
  // artifact (cleared on navigation; see the effect).
  const sidebarUserToggledRef = useRef(false)
  const toggleSidebar = useCallback(() => {
    sidebarUserToggledRef.current = true
    setPanel(p => (p === 'comments' ? 'none' : 'comments'))
  }, [])
  // Auto-reveal the comments panel when the artifact has comments; collapse it
  // when it has none. Reacts to commentCount so adding the first comment reveals
  // the panel and removing the last collapses it — unless the user has taken
  // manual control via a toggle, and NEVER by auto-switching away from an open
  // chat panel (the chat panel only opens on explicit action, so yanking it for
  // a comment default would discard user intent). React Router reuses this
  // component across the parameterized route, so navigating to a different
  // artifact clears the manual-toggle override and resets the panel, giving
  // every artifact the comment-driven default.
  const sidebarNavRef = useRef(slug)
  useEffect(() => {
    const navigated = sidebarNavRef.current !== slug
    if (navigated) {
      sidebarNavRef.current = slug
      sidebarUserToggledRef.current = false
    }
    if (sidebarUserToggledRef.current) return
    setPanel(p => {
      if (p === 'chat' && !navigated) return p
      return commentCount > 0 ? 'comments' : 'none'
    })
  }, [slug, commentCount])
  const [popover, setPopover] = useState<{ x: number; y: number; anchor: string; line?: number; column?: number; prefix?: string; suffix?: string; startOffset?: number; endOffset?: number } | null>(null)
  // Bidirectional anchor↔comment linking: flash a sidebar row when
  // its in-iframe highlight is clicked; scroll the iframe highlight when a
  // sidebar comment is clicked. Nonce forces a re-trigger on repeat clicks.
  const [iframeScrollTarget, setIframeScrollTarget] = useState<{ id: string; nonce: number } | null>(null)
  const previewRef = useRef<HTMLDivElement>(null)
  const bodyRef = useRef<HTMLDivElement>(null)
  const selectingRef = useRef(false)

  // Reset version selection AND any in-progress edit when navigating between
  // artifacts. React Router v6 reuses the component instance for parameterized
  // routes, so without this reset, viewing v5 of one artifact then navigating
  // to another would attempt to fetch v5 of the new artifact (which may not
  // exist), and stale edit state would leak into the new artifact.
  useEffect(() => {
    setSelectedVersion(null)
    setEditing(false)
    setEditedContent('')
    setSaveError(null)
    setPopover(null)
    setAddingTag(false)
    setNewTag('')
    setRenaming(false)
    setNameDraft('')
    // No reset of the touched flag here -- it is keyed by slug, so a different
    // artifact already gets a fresh verdict, and clearing it at this point would
    // race the departing document's cleanup.
  }, [slug])

  const detailQuery = useQuery<Artifact>({
    queryKey: ['artifact', slug],
    queryFn: () => api.artifact(slug),
    enabled: !!slug,
  })
  const versionsQuery = useQuery<{ slug: string; versions: number[] }>({
    queryKey: ['artifact-versions', slug],
    queryFn: () => api.artifactVersions(slug),
    enabled: !!slug,
  })
  const eventsQuery = useQuery<{ slug: string; events: ArtifactEvent[] }>({
    queryKey: ['artifact-events', slug],
    queryFn: () => api.artifactEvents(slug),
    enabled: !!slug,
  })

  const versions = versionsQuery.data?.versions || []
  const effectiveVersion = selectedVersion ?? detailQuery.data?.version ?? null
  // Live is the always-current state; numbered snapshots are historical
  // even when N == latest version. CRITICAL: do NOT treat selectedVersion
  // === detailQuery.data?.version as "current" — that conflates the
  // selected snapshot with Live and shows live content under a "vN" label,
  // which makes silent saves between snapshots look like they're mutating
  // historical versions.
  const isCurrent = !selectedVersion

  const versionQuery = useQuery<Artifact>({
    queryKey: ['artifact', slug, 'version', selectedVersion],
    queryFn: () => api.artifactVersion(slug, selectedVersion as number),
    enabled: !!slug && !!selectedVersion && !isCurrent,
  })

  const artifact = isCurrent ? detailQuery.data : versionQuery.data
  const editable = !!artifact && isEditableKind(artifact.kind) && isCurrent
  const dirty = editing && !!artifact && editedContent !== (artifact.content ?? '')

  // ── Tag editing handlers ────────────────────────────
  const updateTagsMut = useCallback(async (newTags: string[]) => {
    if (!artifact) return
    // Same in-flight window as commitRename / handleSave: the record still reads
    setSaveError(null)
    try {
      await api.updateArtifact(artifact.slug, { tags: newTags })
      await queryClient.invalidateQueries({ queryKey: ['artifact', slug] })
      // Tags-only updates don't bump version, so no need to invalidate
      // versions or events queries.
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err))
    }
  }, [artifact, queryClient, slug])

  const addTag = useCallback((raw: string) => {
    const cleaned = raw.trim().toLowerCase()
    if (!artifact || !cleaned) return
    if (artifact.tags.includes(cleaned)) {
      setNewTag('')
      setAddingTag(false)
      return
    }
    updateTagsMut([...artifact.tags, cleaned])
    setNewTag('')
    setAddingTag(false)
  }, [artifact, updateTagsMut])

  const removeTag = useCallback((tag: string) => {
    if (!artifact) return
    updateTagsMut(artifact.tags.filter(t => t !== tag))
  }, [artifact, updateTagsMut])

  // ── Star (pinned) ─────────────────────────────────────────────────────────
  // `pinned` is record-level, not per-version, and it is the RETENTION control:
  // ArtifactStore.prune_auto_widgets only sweeps records that are unpinned, so
  // starring an auto-registered widget is what keeps it. The library already
  // exposes this on rows and cards; the detail page is where you actually read
  // an artifact and decide whether to keep it, so it belongs here too.
  const [pinning, setPinning] = useState(false)
  const togglePin = useCallback(async () => {
    if (!artifact || pinning) return
    const next = !artifact.pinned
    setSaveError(null)
    setPinning(true)
    // Starring is investment, so it also has to stop the just-created-blank
    try {
      await api.setArtifactPinned(artifact.slug, next)
      // Patch the cached record in place — do NOT invalidate ['artifact', slug].
      // A refetch pulls whatever content is now on the server into the cache, and
      // useWebSocket deliberately withholds exactly that invalidation while an
      // edit buffer is open (see the isArtifactEditing branch): moving the
      // editor's baseline under a stale buffer makes the next Save overwrite an
      // agent's update, silently. `pinned` is a record-level boolean, so there is
      // nothing to re-read — mirror the library's pinMut, which likewise only
      // invalidates the list.
      queryClient.setQueryData(
        ['artifact', slug],
        (old: Artifact | undefined) => (old ? { ...old, pinned: next } : old),
      )
      // The list carries the star column, the Starred filter and the Starred
      // StatCard, and holds no edit buffer, so it refetches normally.
      await queryClient.invalidateQueries({ queryKey: ['artifacts'] })
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err))
    } finally {
      setPinning(false)
    }
  }, [artifact, pinning, queryClient, slug])

  // ── Document type ─────────────────────────────────────────────────────────
  const [changingKind, setChangingKind] = useState(false)
  const changeKind = useCallback(async (next: string) => {
    if (!artifact || next === artifact.kind) return
    // Choosing a type is doing something, and it lands before the refetch — so
    setChangingKind(true)
    setSaveError(null)
    try {
      await api.updateArtifact(artifact.slug, { kind: next })
      await queryClient.invalidateQueries({ queryKey: ['artifact', slug] })
      await queryClient.invalidateQueries({ queryKey: ['artifacts'] })
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err))
    } finally {
      setChangingKind(false)
    }
  }, [artifact, queryClient, slug])

  // ── Inline title rename ────────────────────────────────────────────────────
  const titleButtonRef = useRef<HTMLButtonElement | null>(null)
  const startRenaming = useCallback(() => {
    if (!artifact) return
    setNameDraft(artifact.name)
    setRenaming(true)
  }, [artifact])

  const commitRename = useCallback(async () => {
    const next = nameDraft.trim()
    setRenaming(false)
    // The input is about to unmount, which would drop keyboard focus to the body
    // and lose the user's place. Hand it back to the control they opened it from.
    requestAnimationFrame(() => titleButtonRef.current?.focus())
    if (!artifact || !next || next === artifact.name) return
    // Mark BEFORE awaiting: an unmount racing this PATCH would otherwise see a
    setSaveError(null)
    try {
      // Renames are metadata-only — no version bump, no lifecycle event — and
      // the slug is unaffected, so links and bookmarks keep resolving.
      await api.updateArtifact(artifact.slug, { name: next })
      await queryClient.invalidateQueries({ queryKey: ['artifact', slug] })
      await queryClient.invalidateQueries({ queryKey: ['artifacts'] })
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err))
    }
  }, [artifact, nameDraft, queryClient, slug])

  // ── Edit / save / cancel / revert handlers ────────────────────────────────
  const startEditing = useCallback(() => {
    if (!artifact || !editable) return
    setEditedContent(artifact.content ?? '')
    setEditing(true)
    setSaveError(null)
  }, [artifact, editable])

  // A freshly created blank document opens straight into the editor. Guarded on
  // emptiness rather than firing blind so a reload of an artifact the user has
  // since typed into does not yank them back into edit mode; guarded on
  // `!editing` so it never fights a manual Cancel.
  const autoOpenedRef = useRef(false)
  useEffect(() => {
    if (!justCreatedBlank || autoOpenedRef.current) return
    if (!artifact || !editable) return
    if ((artifact.content ?? '') !== '') return
    autoOpenedRef.current = true
    startEditing()
  }, [justCreatedBlank, artifact, editable, startEditing])

  // Discard an abandoned blank. Create-first naming buys a zero-click "start
  // writing" path at the cost of littering the library with empty Untitled
  // documents, so leaving one untouched cleans it up on the way out. Four
  // conditions must ALL hold, so this can only ever remove the document this
  // page just created and nothing the user invested in: it was handed over as
  // just-created-blank, it is still at v1, its content is still empty, and its
  // name is still the untitled default. Fires on unmount (in-app navigation);
  // closing the tab outright cannot reliably issue the request, which just
  // leaves one empty document behind.
  const discardStateRef = useRef<
    { slug: string; draft: string; createdName: string } | null
  >(null)
  // Text typed into the editor and not yet saved. Local state, so like the flag
  // above it can never be stale. Whitespace alone is not a draft.
  const draft = dirty && editedContent.trim() !== '' ? editedContent : ''
  // Only ever describes the document this page was handed as a fresh blank, and is
  // never overwritten by a LATER one. Navigating straight from the blank to another
  // artifact reuses this route: the new artifact renders before the cleanup runs, so
  // an ungated assignment would replace the snapshot and strand the blank's unsaved
  // draft. Matching on the claim's own slug is what prevents that -- and it is never
  // reset to null here, because the cleanup still needs it after the claim is gone.
  if (blankClaim && artifact && isCurrent && artifact.slug === blankClaim.slug) {
    discardStateRef.current = { slug: blankClaim.slug, draft, createdName: blankClaim.createdName }
  }
  // Sticky disarm — the safety catch that makes the four conditions above
  // trustworthy under a race. The snapshot above is rebuilt from `artifact` on
  // every render, and `artifact` only refreshes when the query refetches, so a
  // write that is still IN FLIGHT is invisible to it: rename to "Release plan"
  // and immediately click a sidebar link and the unmount would read the
  // pre-rename snapshot (still "Untitled", still empty), pass all four
  // conditions, and DELETE the document the user just named. Every write that
  // invalidates the discard verdict therefore calls `disarmDiscard()`
  // SYNCHRONOUSLY, before awaiting its request. Nulling the snapshot alone
  // would not work — the next render re-populates it from the same stale
  // artifact — so the disarm is a separate flag, and it is deliberately
  // one-way: once the user has named or saved this document, it is theirs to
  // keep. Reset only when navigating to a different artifact.
  useEffect(() => {
    if (!justCreatedBlank) return
    return () => {
      const snap = discardStateRef.current
      if (!snap) return
      // Consumed: this document is being settled now and must not be settled twice.
      discardStateRef.current = null
      // The rule: did you name it, or put something in the body? Then it's yours
      // — keep it. Otherwise this is the empty document you opened and walked
      // away from, so clean it up.
      //
      // One thing this page knows for certain and the server cannot: a write it
      // issued itself, which may not be acknowledged yet. That makes DELETION
      // unsafe -- but it must not skip the call, because the editor may still be
      // holding text the user typed and never saved. Naming a document and then
      // navigating away should not throw away its first paragraph. So the flag
      // suppresses deletion only; the draft rescue still runs, and needs nothing
      // more than the stored content being empty.
      // Everything else is ONE atomic server call. It deliberately does not read
      // and then decide: reading here and acting afterwards leaves a window, and a
      // save landing in that window — from a popout window on the same document,
      // or from an agent — would be overwritten or deleted. Re-reading cannot
      // close that gap, because the gap is between the read and the write. So this
      // states the intent and the store resolves it while holding its own lock:
      // keep the document, save the draft, or delete the empty shell.
      //
      // Fails closed: a rejected request leaves the document alone.
      api.settleBlankArtifact(snap.slug, {
        untitled_name: snap.createdName,
        draft: snap.draft,
        // Asked at unmount, per document, of a registry that counts requests
        // rather than call sites -- so a write in flight for THIS document
        // withholds permission to delete it, whichever handler issued it.
        allow_delete: !hasPendingArtifactWrite(snap.slug),
      })
        .then(() => { queryClient.invalidateQueries({ queryKey: ['artifacts'] }) })
        .catch(() => {})
    }
  }, [justCreatedBlank, queryClient])

  const cancelEditing = useCallback(() => {
    if (dirty && !window.confirm(i18nT('pages.artifactDetailPage.discard_unsaved_changes'))) return
    setEditing(false)
    setEditedContent('')
    setSaveError(null)
    setPreviewDuringEdit(false)
  }, [dirty])

  const handleSave = useCallback(async (snapshot = false) => {
    if (!artifact || !dirty) return
    // Same race as commitRename: the discard snapshot still reads empty until
    // the query refetches, so disarm synchronously or an unmount landing
    setSaving(true)
    setSaveError(null)
    try {
      // snapshot=true → bumps version (creates a new numbered snapshot).
      // snapshot=false → silently updates the live state without versioning,
      // matching the explicit-snapshot model.
      await api.updateArtifact(artifact.slug, { content: editedContent, snapshot })
      await queryClient.invalidateQueries({ queryKey: ['artifact', slug] })
      if (snapshot) {
        await queryClient.invalidateQueries({ queryKey: ['artifact-versions', slug] })
        await queryClient.invalidateQueries({ queryKey: ['artifact-events', slug] })
        // Snapshot is a deliberate checkpoint — drop out of edit mode
        // so the user sees the result. Plain Save (silent) keeps the
        // user in the editor: after the query
        // refetches, artifact.content matches editedContent, dirty
        // becomes false, and the user can keep iterating.
        setEditing(false)
        setEditedContent('')
        setPreviewDuringEdit(false)
      }
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }, [artifact, dirty, editedContent, queryClient, slug])

  // Stash for the keyboard handler effect — keeps deps minimal.
  const handleSaveRef = useRef(handleSave)
  useEffect(() => { handleSaveRef.current = handleSave }, [handleSave])

  // Flush unsaved editor edits to the server as a silent live save so a
  // subsequent pull/overwrite sees live_dirty and checkpoints them — closes a
  // data-loss path where pulling mid-edit discarded the working buffer.
  const flushLiveEdits = useCallback(async () => {
    if (!editing || !dirty || !artifact) return
    await api.updateArtifact(artifact.slug, { content: editedContent, snapshot: false })
    // Drop out of edit mode after flushing. The buffer is now persisted (and a
    // subsequent pull checkpoints it as a version), so once the post-mutate
    // refetch lands the pulled/overwritten content the viewer must render
    // artifact.content — not the stale pre-pull editedContent. Leaving
    // editing=true would keep showing the old buffer, flip `dirty` back to
    // true, and let the next Save/Cmd+S silently overwrite the pulled content.
    setEditing(false)
    setEditedContent('')
    setPreviewDuringEdit(false)
    await queryClient.invalidateQueries({ queryKey: ['artifact', slug] })
  }, [editing, dirty, artifact, editedContent, queryClient, slug])

  // Snapshot the current live state without an edit. Used by the Snapshot
  // button when not editing — captures whatever is on disk / current.html
  // as a new numbered version. Snapshot anytime live
  // differs from the latest numbered version (e.g. after silent saves or
  // external file edits to source_path).
  const handleSnapshotLive = useCallback(async () => {
    if (!artifact) return
    setSaving(true)
    setSaveError(null)
    try {
      // No content field — backend reads live state and snapshots it.
      await api.updateArtifact(artifact.slug, { snapshot: true })
      await queryClient.invalidateQueries({ queryKey: ['artifact', slug] })
      await queryClient.invalidateQueries({ queryKey: ['artifact-versions', slug] })
      await queryClient.invalidateQueries({ queryKey: ['artifact-events', slug] })
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }, [artifact, queryClient, slug])

  const handleRevert = useCallback(async () => {
    if (!artifact || !selectedVersion || isCurrent) return
    const targetVersion = selectedVersion
    const newVersion = (detailQuery.data?.version ?? 1) + 1
    const ok = window.confirm(
      i18nT('pages.artifactDetailPage.revert_confirm', { version: targetVersion, newVersion }),
    )
    if (!ok) return
    setSaving(true)
    setSaveError(null)
    try {
      // Fetch the historical version's content (versionQuery may already have
      // it, but going through the API ensures we don't fight an in-flight
      // refetch). Then write it as a new version via PATCH, tagged as a
      // 'reverted' event with the source version pinned so the activity
      // timeline can render it as a revert (not a generic edit) and skip
      // the broken 'from session dashboard:ui' link.
      const versionData = await api.artifactVersion(artifact.slug, targetVersion)
      await api.updateArtifact(artifact.slug, {
        content: versionData.content ?? '',
        event_type: 'reverted',
        from_version: targetVersion,
      })
      await queryClient.invalidateQueries({ queryKey: ['artifact', slug] })
      await queryClient.invalidateQueries({ queryKey: ['artifact-versions', slug] })
      await queryClient.invalidateQueries({ queryKey: ['artifact-events', slug] })
      setSelectedVersion(null)
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }, [artifact, selectedVersion, isCurrent, detailQuery.data?.version, queryClient, slug])

  // Cmd+S / Ctrl+S to save; Esc to cancel edit.
  useEffect(() => {
    if (!editing) return
    const h = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 's' && dirty) {
        e.preventDefault()
        // Cmd+Shift+S → snapshot (creates a new version), Cmd+S → silent save.
        handleSaveRef.current(e.shiftKey)
      }
      if (e.key === 'Escape') cancelEditing()
    }
    document.addEventListener('keydown', h)
    return () => document.removeEventListener('keydown', h)
  }, [editing, dirty, cancelEditing])

  // Tell the WS transport this artifact is being edited, so a live
  // `artifact_update` does not refetch the content out from under the editor and
  // leave Save poised to overwrite it.
  //
  // Keyed on `editing`, NOT `dirty`: the exposure window opens when the editor
  // OPENS, not when the buffer first diverges. With a dirty-only guard an update
  // arriving while the editor sat open-and-clean would move the baseline, and the
  // very next keystroke would make the (now stale) buffer dirty and Save would
  // overwrite the update.
  //
  // When editing ends, refetch what was withheld so the page is not left showing
  // pre-update content.
  const wasEditingRef = useRef(false)
  useEffect(() => {
    setArtifactEditing(slug, editing)
    if (wasEditingRef.current && !editing) {
      queryClient.invalidateQueries({ queryKey: ['artifact', slug] })
      queryClient.invalidateQueries({ queryKey: ['artifact-versions', slug] })
    }
    wasEditingRef.current = editing
    return () => setArtifactEditing(slug, false)
  }, [slug, editing, queryClient])

  // Warn the browser about unsaved edits on close / reload / nav-away.
  useEffect(() => {
    if (!dirty) return
    const handler = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = '' }
    window.addEventListener('beforeunload', handler)
    return () => window.removeEventListener('beforeunload', handler)
  }, [dirty])

  // ── Inline-comment handlers ──────────────────────────────────────────────
  // Comments only make sense for kinds where text→source coords resolve
  // cleanly: markdown (via data-sourcepos) and text (rendered === source).
  // JSON / SVG selection produces noisy anchors; revisit when there's a real
  // user demand.
  // Anchored (selection-driven) comment creation: select text in the rendered
  // body to pin a comment to that exact span. The anchor (quote + prefix/suffix
  // + rendered-text offsets + version) is persisted server-side and surfaced to
  // the agent via artifact_get_comments, so a comment is a durable, located
  // instruction rather than a free-floating note. Doc-level comments via the
  // CommentsSidebar remain available for all kinds.
  // markdown AND text: both now render behind a `previewRef` (ContentRenderer
  // attaches it to the markdown DOM and to the <pre> used for text), so a
  // selection has a root to map back to source in either. Do not add a kind here
  // without confirming its renderer attaches the ref, or the popover silently
  // never opens.
  const commentable = !!artifact && !editing && isCurrent && (
    artifact.kind === 'markdown' || artifact.kind === 'text'
  )
  const isMarkdown = artifact?.kind === 'markdown'
  const sourceContent = artifact?.content ?? ''

  const handleMouseUp = useCallback(() => {
    if (!commentable) return
    const sel = window.getSelection()
    const raw = sel?.toString() ?? ''
    if (!sel || sel.isCollapsed || !raw.trim()) return
    const root = previewRef.current
    if (!root || !sel.anchorNode || !root.contains(sel.anchorNode)) return
    const range = sel.getRangeAt(0)
    if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) return
    const anchor = raw.trim()
    const rect = range.getBoundingClientRect()
    // For markdown, walk the rendered DOM to map (anchorNode, offset) back to
    // (line, col) in the source via data-sourcepos. For text artifacts the
    // rendered text equals the source so findCoords is exact.
    const coords = isMarkdown
      ? (resolveSourcePos(range, root, sourceContent) ?? findCoords(sourceContent, raw) ?? findCoords(sourceContent, anchor))
      : (findCoords(sourceContent, raw) ?? findCoords(sourceContent, anchor))
    // Rendered-text offset of the selection start, in the same space the
    // highlighter's indexTextNodes/rangeForAnchor use — pins the highlight to
    // THIS occurrence when the quote repeats (line/col drive the agent prompt;
    // the offset drives the visual anchor).
    const preRange = document.createRange()
    preRange.setStart(root, 0)
    preRange.setEnd(range.startContainer, range.startOffset)
    const startOffset = preRange.toString().length + (raw.length - raw.trimStart().length)
    setPopover({ x: rect.left, y: rect.bottom, anchor, line: coords?.line, column: coords?.column, startOffset, endOffset: startOffset + anchor.length })
  }, [commentable, isMarkdown, sourceContent])

  const invalidateComments = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['artifact-comments', slug] })
  }, [queryClient, slug])

  // Cross-window mirroring: a popout and the main window are separate JS
  // contexts with separate query caches, so a comment posted in one wouldn't
  // show in the other until staleness (~30s). Announce every local mutation
  // and refetch on announcements from other windows — comments mirror
  // immediately in both directions.
  const invalidateAndAnnounce = useCallback(() => {
    invalidateComments()
    announceCommentsChanged(slug)
  }, [invalidateComments, slug])
  useEffect(() => {
    if (!slug) return
    return onCommentsChanged(slug, invalidateComments)
  }, [slug, invalidateComments])

  // Writes go through useMutation (use-react-query guideline): errors surface
  // instead of being swallowed and cache invalidation is centralized. Errors
  // invalidate locally only (safety-net refetch) — a failed mutation didn't
  // change server state, so there's nothing for other windows to sync.
  const onMutErr = useCallback(() => invalidateComments(), [invalidateComments])
  const postCommentMut = useMutation({
    mutationFn: (vars: { text: string; scope?: string; anchor?: object }) => api.postArtifactComment(slug, vars),
    onSuccess: invalidateAndAnnounce, onError: onMutErr,
  })
  const replyCommentMut = useMutation({
    mutationFn: (vars: { parentId: string; text: string }) => api.replyArtifactComment(slug, vars.parentId, { text: vars.text }),
    onSuccess: (_d: unknown, vars: { parentId: string; text: string }) => {
      // The reply itself succeeded — announce it immediately so other windows
      // mirror it, regardless of what the follow-up reopen does.
      invalidateAndAnnounce()
      // Replying to a resolved thread auto-reopens it. A second
      // announce picks up the status change; a reopen failure only refetches
      // locally (the reply was already announced above).
      const parent = durableComments.find(c => c.id === vars.parentId)
      if (parent && parent.status === 'resolved') {
        api.reopenComment(slug, vars.parentId).then(invalidateAndAnnounce).catch(onMutErr)
      }
    },
    onError: onMutErr,
  })
  const resolveCommentMut = useMutation({ mutationFn: (id: string) => api.resolveComment(slug, id), onSuccess: invalidateAndAnnounce, onError: onMutErr })
  const markReviewCommentMut = useMutation({ mutationFn: (id: string) => api.markCommentReview(slug, id), onSuccess: invalidateAndAnnounce, onError: onMutErr })
  const reopenCommentMut = useMutation({ mutationFn: (id: string) => api.reopenComment(slug, id), onSuccess: invalidateAndAnnounce, onError: onMutErr })
  const removeCommentMut = useMutation({ mutationFn: (id: string) => api.deleteArtifactComment(slug, id), onSuccess: invalidateAndAnnounce, onError: onMutErr })
  const editCommentMut = useMutation({ mutationFn: (vars: { id: string; text: string }) => api.editArtifactComment(slug, vars.id, { text: vars.text }), onSuccess: invalidateAndAnnounce, onError: onMutErr })

  // Anchored add (from the inline selection popover, markdown/text only).
  const addComment = useCallback((text: string) => {
    if (!popover) return
    let anchor: CommentAnchor | undefined
    if (popover.anchor) {
      anchor = { quote: popover.anchor, prefix: popover.prefix, suffix: popover.suffix }
      // Native text selections carry an offset; iframe selections omit it.
      if (popover.startOffset != null) {
        anchor.start_offset = popover.startOffset
        anchor.end_offset = popover.endOffset ?? popover.startOffset + popover.anchor.length
      }
    }
    postCommentMut.mutate({
      text,
      scope: 'private',
      anchor,
    })
    // Adding a comment hands control back to the comment-driven default: reveal
    // the panel now, and clear the manual override so the auto effect can
    // collapse it again if every comment is later removed.
    //
    // EXCEPT when the chat panel is open. An anchored add is reachable while
    // chatting (the body stays visible in the left column), and switching panels
    // would yank the conversation out from under the user. The toolbar's comment
    // badge already increments, so the add is still visibly acknowledged. Same
    // rationale as the auto-reveal guard in the panel effect above.
    sidebarUserToggledRef.current = false
    setPanel(p => (p === 'chat' ? p : 'comments'))
    setPopover(null)
    window.getSelection()?.removeAllRanges()
  }, [popover, postCommentMut])

  // Doc-level add (from the sidebar) — works for ALL kinds, including
  // HTML/widget where in-iframe text selection isn't reachable.
  const addDocComment = useCallback((text: string) => {
    // A DOCUMENT-level comment needs no text selection, so unlike the anchored
    // path it can be left on an empty document -- and it must still count as
    postCommentMut.mutate({ text, scope: 'private' })
  }, [postCommentMut])

  const replyComment = useCallback((parentId: string, text: string) => {
    replyCommentMut.mutate({ parentId, text })
  }, [replyCommentMut])

  const resolveComment = useCallback((id: string) => { resolveCommentMut.mutate(id) }, [resolveCommentMut])

  const markReviewComment = useCallback((id: string) => { markReviewCommentMut.mutate(id) }, [markReviewCommentMut])

  const reopenComment = useCallback((id: string) => { reopenCommentMut.mutate(id) }, [reopenCommentMut])

  const removeComment = useCallback((id: string) => { removeCommentMut.mutate(id) }, [removeCommentMut])
  const editComment = useCallback((id: string, text: string) => { editCommentMut.mutate({ id, text }) }, [editCommentMut])

  /**
   * Single navigation dispatcher for every affordance that leaves the artifact
   * view. In the main dashboard it navigates locally (seeding the composer
   * prefill / active slot first). Inside a popout window it must NOT touch the
   * router — an in-window navigate() would remount the entire dashboard inside
   * the popout — so the intent is forwarded to a main dashboard window (or a
   * new tab when none is alive) and this window stays pinned to its artifact.
   */
  const sendNav = useCallback((intent: NavIntent) => {
    if (popout) { forwardToMain(intent); return }
    if (intent.prefill) writePrefill(intent.prefill.slotKey, intent.prefill.prompt)
    if (intent.slotKey) dispatch(switchSlot(intent.slotKey))
    navigate(intent.path)
  }, [popout, dispatch, navigate])

  // ── Companion chat ─────────────────────────────────────────────────────────
  // The active bound session is resolved from the Redux slots snapshot (the WS
  // `slots` event carries each slot's `artifact` binding) — zero extra
  // endpoints. The frontend flow keeps it to <=1 active bound session per slug
  // (archive-then-create); the resolver tolerates more by picking the most
  // recently active, so races and history resumes degrade gracefully.
  // `?? []` mirrors TaskProgressBar: the slots array is typed non-optional but
  // the store hydrates from localStorage, so a stale persisted shape can hand
  // back undefined and a bare .filter() would crash the whole artifact page.
  const slots = useAppSelector((s) => s.dashboard.slots)
  // `slots` is `[]` BOTH before the first fetch and when nothing is bound, so the
  // empty array alone cannot be read as "no bound session" — see openCompanionChat.
  const slotsLoaded = useAppSelector((s) => s.dashboard.slotsLoaded)
  // The winner drives the panel; the full set matters when archiving, because a
  // two-window creation race can leave more than one slot bound to this slug and
  // archiving only the winner lets pickBoundSlot reopen the leftover.
  const boundSlots = useMemo(
    () => (slots ?? []).filter((x) => x.artifact === slug),
    [slots, slug],
  )
  const boundSlot = useMemo(() => pickBoundSlot(slots, slug), [slots, slug])
  const [chatCreating, setChatCreating] = useState(false)
  // Serializes the two session-lifecycle entry points. `chatCreating` cannot do
  // this job: it is React state (so a second handler in the same tick still sees
  // the old value) and it is only set INSIDE createBoundSession, which runs
  // AFTER newCompanionChat's `await` on the archive — leaving a window where a
  // rapid double-click starts two flows, both resolve the same boundSlot, both
  // archive it (the second 404s and proceeds by design), and both create a
  // replacement. That yields two active bound sessions for one artifact, the
  // exact invariant the archive-then-create ordering exists to protect.
  const sessionOpBusyRef = useRef(false)
  // Versions already announced to a session via context injection, so repeated
  // panel opens don't stack duplicate freshness nudges.
  const injectedVersionRef = useRef<Map<string, number>>(new Map())

  /** Structured context entry naming the artifact — injected ephemeral (consumed
   *  on the next user message) so the user's first message can be natural
   *  ("summarize this") with no slug boilerplate in the composer. */
  const buildCompanionContext = useCallback((): string => {
    if (!artifact) return ''
    return (
      `Companion chat for artifact \`${artifact.slug}\` ("${artifact.name}", kind=${artifact.kind}, ` +
      `v${artifact.version}, source_path=${artifact.source_path || 'none'}, ` +
      `${commentCount} open comment${commentCount === 1 ? '' : 's'}).\n` +
      `Use artifact_get / artifact_update / artifact_get_comments with this slug. ` +
      `Anchored comments carry the exact quoted span they refer to — treat each as ` +
      `an instruction about that span, and triage every one you act on.`
    )
  }, [artifact, commentCount])

  /** Create a fresh bound session with a ONE-round-trip critical path: the
   *  create response — which carries the `artifact` binding — is dispatched
   *  straight into the Redux slots list (addSlotOptimistic), so `boundSlot`
   *  resolves and the panel becomes interactive immediately. The silent context
   *  entry and the server-truth refresh run in the background: the context POST
   *  completes long before a human can type and send (it is consumed on the NEXT
   *  user message), and fetchSlots/WS snapshots reconcile the optimistic row
   *  with full slot metadata moments later.
   *
   *  `prefillText` is staged via writePrefill BEFORE the optimistic bind so
   *  ChatPage's slot-activation effect deterministically finds it on mount. */
  const createBoundSession = useCallback(async (prefillText?: string): Promise<string | null> => {
    if (!artifact) return null
    setChatCreating(true)
    try {
      // No `name`: the backend generates a unique slot key (reusing a
      // name-derived key would append onto an archived session's history file).
      // The pinned title keeps the sidebar readable.
      const res = await api.createChatSlot(
        undefined, undefined, undefined, undefined, undefined,
        i18nT('pages.artifactDetailPage.session_title', { name: artifact.name }), undefined, artifact.slug,
      )
      if (prefillText) writePrefill(res.key, prefillText)
      dispatch(addSlotOptimistic({
        key: res.key,
        title: res.title || i18nT('pages.artifactDetailPage.session_title', { name: artifact.name }),
        messages: 0,
        running: false,
        artifact: artifact.slug,
      } as ChatSlot))
      api.chatSlotContext(res.key, buildCompanionContext(), {
        source: 'artifact-companion', ephemeral: true,
      }).catch(() => undefined)
      injectedVersionRef.current.set(res.key, artifact.version)
      dispatch(fetchSlots())
      return res.key as string
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err))
      return null
    } finally {
      setChatCreating(false)
    }
  }, [artifact, buildCompanionContext, dispatch])

  /** Sparkle flow: resume the active bound session if one exists, else create a
   *  new one. With `address`, stage (never auto-send) the address-comments
   *  message via the writePrefill sessionStorage channel — the embedded ChatPage
   *  consumes it when the slot activates. (The panels are mutually exclusive, so
   *  an "Ask agent to address" click always mounts the chat panel fresh — the
   *  prefill is always picked up.) */
  const openCompanionChat = useCallback(async (opts?: { address?: boolean }) => {
    if (!artifact) return
    if (sessionOpBusyRef.current) return
    const address = opts?.address ?? false
    if (!address && panel === 'chat') { sidebarUserToggledRef.current = true; setPanel('none'); return }
    sidebarUserToggledRef.current = true
    const addressMsg = address && commentCount > 0
      ? `Please review and address the ${commentCount} open comment${commentCount === 1 ? '' : 's'} on this artifact.`
      : ''
    let bound = boundSlot
    if (!bound && !slotsLoaded) {
      // Cold load: the slots snapshot is still `[]` because the first fetch has
      // not landed, NOT because this artifact has no session. Creating here would
      // add a second active bound session for the slug the moment the real one
      // arrives. Resolve authoritatively first, applying the same tie-break rule.
      setPanel('chat')
      sessionOpBusyRef.current = true
      try {
        bound = pickBoundSlot(await dispatch(fetchSlots()).unwrap(), slug)
      } catch {
        bound = null   // fetch failed — fall through and create
      } finally {
        sessionOpBusyRef.current = false
      }
    }
    if (!bound) {
      setPanel('chat')
      sessionOpBusyRef.current = true
      try {
        await createBoundSession(addressMsg || undefined)
      } finally {
        sessionOpBusyRef.current = false
      }
      return
    }
    const boundSlotResolved = bound
    if (addressMsg) writePrefill(boundSlotResolved.key, addressMsg)
    setPanel('chat')
    // Resume freshness nudge: if the artifact moved past the session's last
    // activity, inject a fresh ephemeral context entry so the agent doesn't act
    // on stale-version assumptions. Best-effort — ISO timestamps compare
    // lexicographically; a miss just means the agent re-reads via artifact_get.
    const injected = injectedVersionRef.current.get(boundSlotResolved.key)
    if (
      injected !== artifact.version &&
      boundSlotResolved.last_activity_ts && artifact.updated_at &&
      artifact.updated_at > boundSlotResolved.last_activity_ts
    ) {
      injectedVersionRef.current.set(boundSlotResolved.key, artifact.version)
      api.chatSlotContext(boundSlotResolved.key, buildCompanionContext(), {
        source: 'artifact-companion', ephemeral: true,
      }).catch(() => undefined)
    }
  }, [artifact, panel, commentCount, boundSlot, slotsLoaded, slug, dispatch,
      createBoundSession, buildCompanionContext])

  /** "New chat": archive the current bound session FIRST (the existing red-X
   *  delete path — history preserved, resumable from the History page), then
   *  create fresh — so the <=1-active invariant never observably breaks. The
   *  optimistic remove mirrors the optimistic add: without it the resolver would
   *  keep picking the archived slot (it has a last_activity_ts, the new one
   *  doesn't) until the next WS snapshot prunes it. */
  const newCompanionChat = useCallback(async () => {
    // Reject a re-entrant click rather than queue it: the second click's intent
    // ("give me a fresh session") is already satisfied by the first.
    if (sessionOpBusyRef.current) return
    sessionOpBusyRef.current = true
    try {
      // Archive every slot bound to this slug, not just the resolved winner.
      for (const slot of boundSlots) {
        try {
          await api.deleteChatSlot(slot.key)
        } catch (err) {
          // ONLY a 404 means "already archived, nothing to do". Any other
          // failure means the old session is still live server-side, so creating
          // anyway would leave TWO bound sessions for this slug — and since the
          // resolver breaks ties on last_activity_ts, the OLD one keeps winning
          // and "New chat" silently appears to do nothing. Abort and say why.
          //
          // Read `status` structurally rather than via `instanceof ApiError`:
          // this fails closed on anything not provably a 404 (including an error
          // re-thrown or wrapped across a module boundary, where instanceof
          // silently stops matching), and it is verifiable in a test.
          const status = (err as { status?: unknown } | null | undefined)?.status
          if (status !== 404) {
            setSaveError(err instanceof Error ? err.message : String(err))
            return
          }
        }
        dispatch(removeSlotOptimistic(slot.key))
      }
      await createBoundSession()
    } finally {
      sessionOpBusyRef.current = false
    }
  }, [boundSlots, createBoundSession, dispatch])

  /** Full-page escape hatch — routes through sendNav so a popout forwards the
   *  intent to a main window instead of remounting the dashboard in-frame. */
  const openChatFull = useCallback(() => {
    if (!boundSlot) return
    sendNav({ path: '/chat', slotKey: boundSlot.key })
  }, [boundSlot, sendNav])

  // Deleted-artifact handling (consumes the `artifact_update {deleted}` WS event
  // relayed by useWebSocket as a window event): leave the page in the main
  // dashboard; a popout has no router so it surfaces an error instead.
  useEffect(() => {
    const onDeleted = (e: Event) => {
      const detail = (e as CustomEvent<{ slug: string }>).detail
      if (detail?.slug !== slug) return
      // Navigating away destroys the edit buffer, so a dirty page keeps its
      // content and surfaces the deletion instead — the user can still copy their
      // work out. A popout has no router and always surfaces the error.
      if (popout || dirty) {
        setSaveError(i18nT('pages.artifactDetailPage.this_artifact_was_deleted'))
      } else {
        navigate('/artifacts')
      }
    }
    window.addEventListener('kirocrew:artifact-deleted', onDeleted)
    return () => window.removeEventListener('kirocrew:artifact-deleted', onDeleted)
  }, [slug, popout, navigate, dirty])

  // Drop popover when the user switches to edit mode or pages between
  // versions — those interactions kill the underlying selection anyway.
  useEffect(() => { if (editing || !isCurrent) { setPopover(null) } }, [editing, isCurrent])

  // ── Export helpers (Open-in-new-tab + Download) ───────────────────────────
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const themeVars = useMemo(() => readThemeVars(), [theme, colorTheme, themeVersion])
  const usesIframe = artifact?.kind === 'widget' || artifact?.kind === 'html'
  const exportSrcdoc = useMemo(
    () => artifact?.content && usesIframe
      ? buildSrcdoc({ html: artifact.content, themeVars, mode: theme })
      : null,
    [artifact?.content, themeVars, theme, usesIframe],
  )

  // Persistent active comment; NO transitory flash.
  const [activeCommentId, setActiveCommentId] = useState<string | null>(null)
  const [bodyScrollNonce, setBodyScrollNonce] = useState(0)
  // Read/unread tracking: a per-artifact set of seen comment ids
  // in localStorage; a thread is unread if any of its comments is unseen.
  const readKey = `mc-cmt-read:${slug}`
  const [readIds, setReadIds] = useState<Set<string>>(new Set())
  useEffect(() => {
    try { setReadIds(new Set(JSON.parse(localStorage.getItem(readKey) || '[]'))) }
    catch { setReadIds(new Set()) }
  }, [readKey])
  const rootIdOf = useCallback(
    (c: ArtifactComment) => (c.parent_id && durableComments.some(x => x.id === c.parent_id) ? c.parent_id : c.id),
    [durableComments],
  )
  const unreadRootIds = useMemo(() => {
    const s = new Set<string>()
    for (const c of durableComments) if (!readIds.has(c.id)) s.add(rootIdOf(c))
    return s
  }, [durableComments, readIds, rootIdOf])
  const markThreadRead = useCallback((rootId: string) => {
    const ids = durableComments.filter(c => c.id === rootId || c.parent_id === rootId).map(c => c.id)
    setReadIds(prev => {
      const next = new Set(prev)
      ids.forEach(i => next.add(i))
      try { safeSetItem(readKey, JSON.stringify([...next])) } catch { /* quota */ }
      return next
    })
  }, [durableComments, readKey])
  // Opening a thread (bubble/highlight click, or the iframe bridge) → activate,
  // mark read, and open the floating thread popover. Markdown finds its anchor
  // via data-mc-cid; the iframe passes a viewport rect.
  const [openThread, setOpenThread] = useState<{ rootId: string; rect?: { x: number; y: number; w: number; h: number } } | null>(null)
  const openThreadHandler = useCallback((id: string, rect?: { x: number; y: number; w: number; h: number }) => {
    setActiveCommentId(id)
    markThreadRead(id)
    setOpenThread({ rootId: id, rect })
  }, [markThreadRead])
  // Sidebar comment clicked → activate, scroll the doc to the anchor, open popover.
  const activateFromSidebar = useCallback((id: string) => {
    setActiveCommentId(id)
    markThreadRead(id)
    if (usesIframe) {
      // The bridge scrolls the iframe, then posts the anchor rect → onOpenThread
      // opens the popover over the iframe.
      setIframeScrollTarget({ id, nonce: Date.now() })
    } else {
      setBodyScrollNonce(n => n + 1)
      setOpenThread({ rootId: id })
    }
  }, [markThreadRead, usesIframe])

  const downloadAsHtml = () => {
    if (!artifact) return
    const isMarkdownLike = artifact.kind === 'markdown' || artifact.kind === 'text' || artifact.kind === 'json' || artifact.kind === 'svg'
    const blobBody = exportSrcdoc ?? artifact.content ?? ''
    const mime = isMarkdownLike
      ? (artifact.kind === 'json' ? 'application/json' : artifact.kind === 'svg' ? 'image/svg+xml' : 'text/plain')
      : 'text/html'
    const ext = artifact.kind === 'markdown' ? 'md'
      : artifact.kind === 'json' ? 'json'
      : artifact.kind === 'svg' ? 'svg'
      : artifact.kind === 'text' ? 'txt'
      : 'html'
    const blob = new Blob([blobBody], { type: mime })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    const safeName = artifact.name.replace(/[^a-zA-Z0-9-_ ]/g, '')
    a.download = `${safeName || artifact.slug}-v${effectiveVersion}.${ext}`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    setTimeout(() => URL.revokeObjectURL(a.href), 60_000)
  }

  if (detailQuery.isLoading || (!isCurrent && versionQuery.isLoading))
    return <div className="p-6 text-muted">{i18nT('pages.artifactDetailPage.loading')}</div>
  if (detailQuery.error) {
    const msg = detailQuery.error instanceof Error ? detailQuery.error.message : String(detailQuery.error)
    return (
      <>
        <PageHeader title={i18nT('pages.artifactDetailPage.artifact')} subtitle={slug} />
        <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">
          <Card>
            <div className="flex items-start gap-3">
              <AlertTriangle className="lucide-inline text-danger" />
              <div>
                <div className="text-sm text-danger font-medium">{i18nT('pages.artifactDetailPage.failed_to_load_artifact')}</div>
                <div className="text-[13px] text-muted mt-1">{msg}</div>
              </div>
            </div>
            <div className="mt-3">
              {/* In a popout this forwards to the main window (the popout must
                  never become the library page); in the main app it's a plain
                  local navigation. */}
              <Btn onClick={() => sendNav({ path: '/artifacts' })}>{i18nT('pages.artifactDetailPage.back_to_library')}</Btn>
            </div>
          </Card>
        </div>
      </>
    )
  }
  if (!artifact) return <div className="p-6 text-muted">{i18nT('pages.artifactDetailPage.not_found')}</div>

  // Version-picker rows as two PARALLEL arrays, newest snapshot first, the order
  // the <option> list had. `live` leads as a static entry: Live is always-current
  // state, distinct from any numbered snapshot because in the explicit-snapshot
  // model saves update Live without bumping versions, so Live can be ahead of the
  // latest numbered snapshot.
  const versionsDesc = versions.slice().reverse()
  const versionOptions = ['live', ...versionsDesc.map(String)]
  const versionOptionLabels = [
    i18nT('pages.artifactDetailPage.live'),
    ...versionsDesc.map((v) => `${i18nT('pages.artifactDetailPage.v')}${v}`),
  ]

  // Cron-source warning shown only while editing — surface the foot-gun
  // (next cron run will create a newer version) without noisy chrome on
  // read-only views.
  const showCronWarning = editing && artifact.source === 'cron'

  return (
    <>
      <PageHeader
        title={renaming ? (
          <Input
            autoFocus
            value={nameDraft}
            aria-label={i18nT('pages.artifactDetailPage.artifact_name')}
            onChange={(e) => setNameDraft(e.target.value)}
            onBlur={commitRename}
            onKeyDown={(e) => {
              if (e.key === 'Enter') { e.preventDefault(); void commitRename() }
              else if (e.key === 'Escape') { e.preventDefault(); setRenaming(false) }
            }}
            className="px-2 py-0.5 text-2xl font-bold tracking-tight text-text-strong w-full max-w-[36rem]"
          />
        ) : (
          <Btn
            ref={titleButtonRef}
            onClick={startRenaming}
            title={i18nT('pages.artifactDetailPage.rename_this_artifact')}
            className="group gap-2 bg-transparent border-none p-0 text-2xl font-bold tracking-tight text-text-strong cursor-text hover:bg-transparent hover:border-none"
          >
            {artifact.name}
            <Pencil size={14} className="text-muted opacity-0 group-hover:opacity-100 transition-opacity shrink-0" aria-hidden="true" />
          </Btn>
        )}
        subtitle={i18nT('pages.artifactDetailPage.artifact_slug', { slug: artifact.slug })}
      />
      <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        <div className="flex flex-wrap items-center gap-2 mb-4">
          {!popout && (
            <Btn onClick={() => {
              if (dirty && !window.confirm(i18nT('pages.artifactDetailPage.discard_unsaved_changes'))) return
              navigate('/artifacts')
            }} className="flex items-center gap-1">
              <ArrowLeft size={13} /> {i18nT('pages.artifactDetailPage.back')}
            </Btn>
          )}
          {/* Type control. Markdown and plain text are indistinguishable by
            * content — "# Notes" is a heading in one and literal text in the
            * other — so the difference is intent and has to be stated, not
            * sniffed. Offered only for the inline-editable kinds; widget / html
            * render in a sandboxed iframe with no editor, so switching to one
            * would strand a document the user is typing in. Choosing a type also
            * PINS it, stopping the auto-detect from re-typing it later. */}
          {editable ? (
            /* SimpleSelect has no `title` channel, so the hover tooltip moves to
             * a wrapper element; the accessible name still rides on the trigger
             * itself via aria-label. */
            <div title={i18nT('pages.artifactDetailPage.change_how_this_document_is_rendered')}>
              <SimpleSelect
                options={USER_SELECTABLE_KINDS}
                value={artifact.kind}
                aria-label={i18nT('pages.artifactDetailPage.document_type')}
                disabled={changingKind}
                onChange={(v) => void changeKind(v)}
              />
            </div>
          ) : (
            <Badge variant="aim">{artifact.kind}</Badge>
          )}
          <FolderChip artifact={artifact} />
          {/* Only on the current version: a version snapshot does not carry the
            * live record-level `pinned`, so rendering it there could show a
            * stale star for state the user cannot meaningfully toggle. */}
          {isCurrent && (
            <button
              type="button"
              onClick={togglePin}
              disabled={pinning}
              className={`inline-flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded border transition-colors cursor-pointer disabled:cursor-default ${
                artifact.pinned
                  ? 'bg-accent/10 border-accent text-accent'
                  : 'bg-bg-elevated border-border text-muted hover:text-accent hover:border-accent'
              }`}
              title={artifact.pinned ? i18nT('pages.artifactsPage.starred_click_to_unstar') : i18nT('pages.artifactsPage.star_artifact')}
              aria-label={artifact.pinned ? i18nT('pages.artifactsPage.remove_star_from_artifact') : i18nT('pages.artifactsPage.star_artifact')}
              aria-pressed={!!artifact.pinned}
            >
              {pinning
                ? <RefreshCw size={10} className="animate-spin" />
                : <Star size={10} className={artifact.pinned ? 'fill-current' : ''} />}
              {artifact.pinned ? i18nT('pages.artifactsPage.starred') : i18nT('pages.artifactDetailPage.star')}
            </button>
          )}
          {artifact.tags.map((t) => (
            <span key={t} className="inline-flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded bg-bg-elevated border border-border text-muted group">
              {t}
              <button
                type="button"
                onClick={() => removeTag(t)}
                className="opacity-0 group-hover:opacity-100 hover:text-danger transition-opacity bg-transparent border-none cursor-pointer p-0 inline-flex items-center"
                title={i18nT('pages.artifactDetailPage.remove_tag', { name: t })}
                aria-label={i18nT('pages.artifactDetailPage.remove_tag', { name: t })}
              >
                <X size={10} />
              </button>
            </span>
          ))}
          {addingTag ? (
            <input
              type="text"
              value={newTag}
              onChange={e => setNewTag(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter') addTag(newTag)
                if (e.key === ',' || e.key === ' ') {
                  e.preventDefault()
                  if (newTag.trim()) addTag(newTag)
                }
                if (e.key === 'Escape') { setNewTag(''); setAddingTag(false) }
              }}
              onBlur={() => {
                if (newTag.trim()) addTag(newTag)
                else setAddingTag(false)
              }}
              autoFocus
              placeholder={i18nT('pages.artifactDetailPage.tag')}
              className="text-[11px] px-1.5 py-0.5 rounded bg-bg-elevated border border-accent text-text outline-none"
              style={{ width: '90px' }}
              aria-label={i18nT('pages.artifactDetailPage.add_a_tag')}
            />
          ) : (
            <button
              type="button"
              onClick={() => setAddingTag(true)}
              className="inline-flex items-center gap-0.5 text-[11px] px-1.5 py-0.5 rounded border border-dashed border-border text-muted hover:text-text hover:border-border-strong cursor-pointer bg-transparent transition-colors"
              title={i18nT('pages.artifactDetailPage.add_a_tag_comma_separated_tags_supported')}
              aria-label={i18nT('pages.artifactDetailPage.add_a_tag')}
            >
              <Plus size={10} /> {i18nT('pages.artifactDetailPage.tag_2')}
            </button>
          )}
          <span className="mc-art-toolbar ml-auto flex items-center gap-2 text-[13px] text-muted">
            <span>{i18nT('pages.artifactDetailPage.version')}</span>
            <SimpleSelect
              // Named so it is distinguishable from the document-type control
              // beside it -- both for assistive tech and for tests.
              aria-label={i18nT('pages.artifactDetailPage.version')}
              disabled={saving}
              options={versionOptions}
              optionLabels={versionOptionLabels}
              value={selectedVersion === null ? 'live' : String(selectedVersion)}
              onChange={(raw) => {
                if (dirty && !window.confirm(i18nT('pages.artifactDetailPage.discard_unsaved_changes'))) return
                setEditing(false)
                setEditedContent('')
                if (raw === 'live') {
                  setSelectedVersion(null)
                } else {
                  setSelectedVersion(parseInt(raw, 10))
                }
              }}
            />

            {/* Revert: only meaningful when viewing a historical version */}
            {!isCurrent && (
              <button
                type="button"
                onClick={handleRevert}
                disabled={saving}
                className="px-2 py-1 rounded-md text-[12px] font-medium border border-warn/40 text-warn hover:border-warn cursor-pointer transition-all disabled:opacity-40"
                title={i18nT('pages.artifactDetailPage.revert_to_v', { version: selectedVersion })}
                aria-label={i18nT('pages.artifactDetailPage.revert_to_v', { version: selectedVersion })}
              >
                <span className="inline-flex items-center gap-1"><RotateCcw size={13} /> {i18nT('pages.artifactDetailPage.revert')}</span>
              </button>
            )}

            {/* Editing controls (Save / Snapshot / Cancel / Preview) when
                editing; otherwise Edit + Iterate. Bar order: version, edit,
                iterate, publish, full screen, download. */}
            {editing ? (
              <>
                <button
                  type="button"
                  onClick={() => handleSave(false)}
                  disabled={!dirty || saving}
                  className={`px-2 py-1 rounded-md text-[12px] font-medium border transition-all disabled:opacity-40 ${dirty ? 'border-accent text-accent-fg bg-accent cursor-pointer hover:bg-accent-hover' : 'border-border text-muted cursor-default'}`}
                  title={i18nT('pages.artifactDetailPage.save_to_live_cmd_s_updates_the_live_state_withou')}
                >
                  {saving ? i18nT('pages.artifactDetailPage.saving') : i18nT('pages.artifactDetailPage.save')}
                </button>
                <button
                  type="button"
                  onClick={() => handleSave(true)}
                  disabled={!dirty || saving}
                  className="px-2 py-1 rounded-md text-[12px] font-medium border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all disabled:opacity-40"
                  title={i18nT('pages.artifactDetailPage.snapshot_cmd_shift_s_save_and_create_a_new_versi')}
                >
                  <span className="inline-flex items-center gap-1"><Camera size={13} /> {i18nT('pages.artifactDetailPage.snapshot')}</span>
                </button>
                <button
                  type="button"
                  onClick={cancelEditing}
                  disabled={saving}
                  className="px-2 py-1 rounded-md text-[12px] font-medium border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all disabled:opacity-40"
                  title={i18nT('pages.artifactDetailPage.cancel_esc')}
                >
                  <span className="inline-flex items-center gap-1"><X size={13} /> {i18nT('pages.artifactDetailPage.cancel')}</span>
                </button>
                <button
                  type="button"
                  onClick={() => setPreviewDuringEdit(p => !p)}
                  disabled={saving}
                  className={`px-2 py-1 rounded-md text-[12px] font-medium border cursor-pointer transition-all disabled:opacity-40 ${previewDuringEdit ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text hover:border-border-strong'}`}
                  title={previewDuringEdit ? i18nT('pages.artifactDetailPage.back_to_editor') : i18nT('pages.artifactDetailPage.preview_rendered_output_of_current_edits')}
                >
                  {previewDuringEdit ? i18nT('pages.artifactDetailPage.edit') : i18nT('pages.artifactDetailPage.preview')}
                </button>
              </>
            ) : (
              <>
                {isCurrent && artifact.live_dirty && (
                  <button
                    type="button"
                    onClick={handleSnapshotLive}
                    disabled={saving}
                    className="px-2 py-1 rounded-md text-[12px] font-medium border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all disabled:opacity-40"
                    title={i18nT('pages.artifactDetailPage.snapshot_capture_the_current_state_as_a_new_vers')}
                  >
                    <span className="inline-flex items-center gap-1"><Camera size={13} /> {i18nT('pages.artifactDetailPage.snapshot')}</span>
                  </button>
                )}
                {editable && (
                  <button
                    type="button"
                    onClick={startEditing}
                    className="px-2 py-1 rounded-md text-[12px] font-medium border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all"
                    title={i18nT('pages.artifactDetailPage.edit_content')}
                    aria-label={i18nT('pages.artifactDetailPage.edit_content')}
                  >
                    <Pencil size={13} />
                  </button>
                )}
                {/* Companion chat — toggles the embedded chat panel. Primary
                    "discuss with agent" action for all kinds; for widgets it is
                    the only way to ask the agent to change the artifact.
                    Comments are durable and read by the agent via
                    artifact_get_comments. Works in popout windows too — the
                    popout has its own store + WS. */}
                <button
                  type="button"
                  onClick={() => { void openCompanionChat() }}
                  className={`p-1.5 rounded-md border cursor-pointer transition-all ${panel === 'chat' ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text hover:border-border-strong'}`}
                  title={i18nT('pages.artifactDetailPage.chat_with_the_agent_about_this_artifact')}
                  aria-label={i18nT('pages.artifactDetailPage.toggle_agent_chat')}
                  aria-pressed={panel === 'chat'}
                >
                  <Sparkles size={13} />
                </button>
              </>
            )}

            {(!editing || previewDuringEdit) && (
              <ReadingWidthToggle value={readingWidth} onToggle={toggleReadingWidth} />
            )}
            {/* Comments toggle, Publish, Full screen, Download — icon-only to
                keep the top-right bar compact; labels live in tooltips. */}
            <button
              type="button"
              onClick={toggleSidebar}
              className={`p-1.5 rounded-md border cursor-pointer transition-all ${panel === 'comments' ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text hover:border-border-strong'}`}
              title={panel === 'comments' ? i18nT('pages.artifactDetailPage.hide_comments') : i18nT('pages.artifactDetailPage.show_comments')}
              aria-label={i18nT('pages.artifactDetailPage.toggle_comments')}
              aria-pressed={panel === 'comments'}
            >
              <span className="inline-flex items-center gap-1">
                <MessageSquare size={13} />
                {commentCount > 0 && (
                  <span className="ml-0.5 px-1 rounded bg-accent/20 text-[10px]">{commentCount}</span>
                )}
              </span>
            </button>
            {/* Pop out — opens the artifact in its own live browser window.
                Swaps to Focus + Bring-back once
                out. Not shown inside the popout window itself (the frame's
                Return button handles closing). */}
            {!popout && <ArtifactPopoutControl slug={slug} name={artifact.name} />}
            {/* Publish — the single publish surface. Web deploy (Publish to
                public web on the user's own AWS) and any future publish
                providers register into PublishHub, so this is the one and only
                publish action. Labeled (not icon-only) as the primary publish
                action. Shown for non-webapp kinds; webapp artifacts use their
                own deploy card. NOTE: the internal share/publish-provider
                surface (Link2 + ArtifactSharePanel) is intentionally absent
                here — a deliberate public-edition divergence, so an upstream
                sync must NOT re-add it. */}
            {artifact.kind !== 'webapp' && (
              <Btn
                type="button"
                onClick={() => setShowPublish(v => !v)}
                title={i18nT('pages.artifactDetailPage.publish_this_artifact')}
                aria-label={i18nT('pages.artifactDetailPage.publish')}
                aria-pressed={showPublish}
                className={showPublish ? 'border-accent text-accent bg-accent-subtle hover:bg-accent-subtle hover:text-accent' : ''}
              >
                <Upload size={13} /> {i18nT('pages.artifactDetailPage.publish')}
              </Btn>
            )}
            <Btn
              type="button"
              onClick={downloadAsHtml}
              className="p-1.5 rounded-md border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all"
              title={i18nT('pages.artifactDetailPage.download')}
              aria-label={i18nT('pages.artifactDetailPage.download')}
            >
              <Download size={13} />
            </Btn>
          </span>
        </div>

        {artifact.description && (
          <div className="mb-3 text-sm text-muted italic">{artifact.description}</div>
        )}

        {(artifact.fork_metadata || artifact.publication) && (
          <UpstreamSyncBanner artifact={artifact} onBeforeMutate={flushLiveEdits} onPulled={() => {
            queryClient.invalidateQueries({ queryKey: ['artifact', slug] })
            queryClient.invalidateQueries({ queryKey: ['upstream-status', slug] })
          }} />
        )}

        {showCronWarning && (
          <div className="mb-3 flex items-start gap-2 px-3 py-2 rounded-md border border-warn/40 bg-warn-subtle text-[13px] text-warn">
            <AlertCircle size={14} className="lucide-inline shrink-0 mt-0.5" />
            <span>
              <strong>{i18nT('pages.artifactDetailPage.heads_up')}</strong> {i18nT('pages.artifactDetailPage.this_artifact_is_regenerated_by_a_cron_job_your')}
            </span>
          </div>
        )}

        {/* No agent hand-off here, deliberately. `saveError` is set exactly when
            handleSave threw, so `dirty` is still true and `editedContent` was
            never persisted — a route change unmounts this page and the buffer is
            gone. Every other nav-away on this page gates on
            `dirty && confirm(discard_unsaved_changes)`, and the deleted-artifact
            handler sets `saveError` INSTEAD of navigating precisely so the user
            can copy their work out. A one-click navigation off this surface would
            destroy it, and would bypass the beforeunload guard too. */}
        <ErrorNotice
          message={saveError}
          title={i18nT('pages.artifactDetailPage.save_failed')}
          className="mb-3"
        />

        {/* Read-only publication sync-error surface: keeps a persisted sync
            error visible (no controls) if a publishing provider is ever
            registered. Inert in the public edition, where the registry is empty
            and `artifact.publication` is always null. */}
        {artifact.publication?.last_error && (
          <div className="mb-3 flex items-start gap-2 px-3 py-2 rounded-md border border-danger/40 bg-danger-subtle text-[13px] text-danger">
            <AlertCircle size={14} className="lucide-inline shrink-0 mt-0.5" />
            <span><strong>{i18nT('pages.artifactDetailPage.publication_sync_issue')}</strong> {artifact.publication.last_error}</span>
          </div>
        )}

        {/* Publish panel — toggled by the Publish toolbar button */}
        {showPublish && artifact.kind !== 'webapp' && (
          <div className="mb-3">
            <PublishHub artifact={artifact} onClose={() => setShowPublish(false)} />
          </div>
        )}

        {/* One flex row for EVERY kind, so the right-hand panels are siblings of
            whatever body is rendered. The toolbar's chat and comments toggles
            render unconditionally, so scoping the row to the non-webapp branch
            would let a click on a webapp artifact create/activate a session with
            nowhere to display. */}
        <div className="flex gap-4 items-start">
          <div className="flex-1 min-w-0">
            {artifact.kind === 'webapp' ? (
              <WebAppArtifactCard artifact={artifact} />
            ) : usesIframe ? (
              <>
                <ArtifactBodyIframe
                  artifact={artifact}
                  slug={slug}
                  previewStyle={mdPreviewStyle}
                  comments={durableComments}
                  onSelect={(sel: IframeSelection) => setPopover({ x: sel.x, y: sel.y, anchor: sel.quote, prefix: sel.prefix, suffix: sel.suffix })}
                  onOpenThread={(id: string, rect) => openThreadHandler(id, rect)}
                  scrollToCommentId={iframeScrollTarget}
                  activeId={activeCommentId}
                  unreadRootIds={unreadRootIds}
                />
                {popover && (
                  <CommentPopover
                    x={popover.x}
                    y={popover.y}
                    onSubmit={addComment}
                    onCancel={() => { setPopover(null); window.getSelection()?.removeAllRanges() }}
                  />
                )}
              </>
            ) : (
              <div
                ref={bodyRef}
                className="relative"
                style={mdPreviewStyle}
                onMouseDown={() => { selectingRef.current = true }}
                onMouseUp={() => { selectingRef.current = false; handleMouseUp() }}
              >
                <ArtifactBodyNative
                  kind={artifact.kind}
                  content={editing ? editedContent : (artifact.content ?? '')}
                  editing={editing && !previewDuringEdit}
                  onChange={setEditedContent}
                  previewRef={previewRef}
                  comments={durableComments}
                  activeCommentId={activeCommentId}
                  scrollNonce={bodyScrollNonce}
                  onActivateComment={openThreadHandler}
                  unreadRootIds={unreadRootIds}
                />
                {popover && (
                  <CommentPopover
                    x={popover.x}
                    y={popover.y}
                    onSubmit={addComment}
                    onCancel={() => { setPopover(null); window.getSelection()?.removeAllRanges() }}
                    containerRef={bodyRef}
                  />
                )}
              </div>
            )}
          </div>

          {/* Shared right-hand panel space: comments sidebar and companion chat
              panel are mutually exclusive flex siblings of the artifact body —
              icon-toggled, never overlays. The comment stack is durable,
              threaded, and works for ALL kinds (doc-level add for HTML/widget;
              anchored add for markdown/text via the inline popover above). */}
          {panel === 'comments' && (
            <CommentsSidebar
              comments={durableComments}
              loading={commentsQuery.isFetching}
              remoteSyncError={remoteSyncError}
              onAdd={addDocComment}
              onReply={replyComment}
              onResolve={resolveComment}
              onMarkReview={markReviewComment}
              onReopen={reopenComment}
              onDelete={removeComment}
              onRefresh={invalidateComments}
              onAskAgent={commentCount > 0 ? () => { void openCompanionChat({ address: true }) } : undefined}
              onClose={toggleSidebar}
              onCommentClick={activateFromSidebar}
              onEditComment={editComment}
              activeCommentId={activeCommentId}
            />
          )}
          {panel === 'chat' && (
            <ArtifactChatPanel
              slotKey={boundSlot?.key ?? null}
              creating={chatCreating}
              onNewChat={() => { void newCompanionChat() }}
              onOpenFull={openChatFull}
              onClose={() => { sidebarUserToggledRef.current = true; setPanel('none') }}
            />
          )}
        </div>

        <div className="mt-3 text-[12px] text-muted">
          {i18nT('pages.artifactDetailPage.created')} {artifact.created_at} {i18nT('pages.artifactDetailPage.updated')} {artifact.updated_at} {"\u00b7"}{' '}
          {/* "Live" reflects the always-current state. Numbered versions
              are historical snapshots — when one is selected, isCurrent is
              false (because the dropdown is non-Live). */}
          {selectedVersion === null
            ? i18nT('pages.artifactDetailPage.showing_live_v', { version: detailQuery.data?.version ?? '?' })
            : i18nT('pages.artifactDetailPage.showing_v_historical', { version: effectiveVersion })}
          {dirty && <span className="ml-2 text-warn">{i18nT('pages.artifactDetailPage.unsaved_changes')}</span>}
          {commentable && commentCount === 0 && (
            <span className="ml-2 text-muted/80">{i18nT('pages.artifactDetailPage.tip_select_text_to_anchor_a_comment_or_use_the')} <strong>{i18nT('pages.artifactDetailPage.comments')}</strong> {i18nT('pages.artifactDetailPage.panel_to_add_one')}</span>
          )}
          {!commentable && !editing && isCurrent && (
            <span className="ml-2 text-muted/80">
              {i18nT('pages.artifactDetailPage.tip_use_the')} <strong>{i18nT('pages.artifactDetailPage.comments')}</strong> {i18nT('pages.artifactDetailPage.panel_to_comment')}
              {i18nT('pages.artifactDetailPage.or')} <strong>{i18nT('pages.artifactDetailPage.agent_chat')}</strong> {i18nT('pages.artifactDetailPage.to_chat_with_the_agent')}.
            </span>
          )}
        </div>

        {openThread && (
          <CommentThreadPopover
            comments={durableComments}
            rootId={openThread.rootId}
            rect={openThread.rect}
            onClose={() => setOpenThread(null)}
            onReply={replyComment}
            onResolve={resolveComment}
            onMarkReview={markReviewComment}
            onReopen={reopenComment}
            onDelete={removeComment}
            onEditComment={editComment}
          />
        )}

        {/* Lifecycle event log + activity timeline. */}
        <div className="mt-6">
          <h3 className="text-[13px] font-semibold text-text-strong mb-2">{i18nT('pages.artifactDetailPage.activity')}</h3>
          <ActivityTimeline
            events={eventsQuery.data?.events ?? []}
            navigateToSlot={(slotKey) => sendNav({ path: '/chat', slotKey })}
          />
        </div>
      </div>
    </>
  )
}


// Static theme-token class sets (Tailwind JIT needs literal strings, not
// interpolated tone names).
const _SYNC_TONES: Record<string, { wrap: string; btn: string }> = {
  info: { wrap: 'border-info/40 bg-info-subtle text-info', btn: 'border-info/40 text-info hover:bg-info/10' },
  warn: { wrap: 'border-warn/40 bg-warn-subtle text-warn', btn: 'border-warn/40 text-warn hover:bg-warn/10' },
  danger: { wrap: 'border-danger/40 bg-danger-subtle text-danger', btn: 'border-danger/40 text-danger hover:bg-danger/10' },
}

function UpstreamSyncBanner({ artifact, onPulled, onBeforeMutate }: { artifact: Artifact; onPulled: () => void; onBeforeMutate?: () => Promise<void> }) {
  const fm = artifact.fork_metadata
  const pub = artifact.publication
  const [pulling, setPulling] = useState(false)
  const [overwriting, setOverwriting] = useState(false)
  const [snapshotting, setSnapshotting] = useState(false)
  const [error, setError] = useState('')
  // A benign, non-error outcome (e.g. "up to date" when the remote isn't
  // actually ahead) — rendered in a neutral tone, not danger-red, so a no-op
  // pull on an up-to-date fork doesn't look like a failure.
  const [notice, setNotice] = useState('')
  // The whole banner is gated on a registered publish provider: the public
  // edition ships an empty registry, so providers resolves to [] and this
  // component renders nothing (an artifact can only carry fork_metadata /
  // publication once a companion provider existed to create them anyway).
  const { data: providersData } = useQuery({
    queryKey: ['publish-providers', artifact.kind],
    queryFn: () => api.getArtifactPublishProviders(artifact.kind),
    staleTime: 300_000,
  })
  const providers = providersData?.providers || []
  // Cheap, non-blocking upstream check — the local content renders immediately;
  // this only drives the "pull available" / "conflict" affordance.
  const { data: status } = useQuery({
    queryKey: ['upstream-status', artifact.slug],
    queryFn: () => api.upstreamStatus(artifact.slug),
    staleTime: 15_000,
    enabled: providers.length > 0,
  })
  const upstreamAhead = !!status?.upstream_ahead
  const hasLocalEdits = !!artifact.live_dirty || !!status?.live_dirty || !!status?.local_ahead
  const cloudV = typeof status?.cloud_version === 'number' ? status.cloud_version : null

  const handlePull = async () => {
    setPulling(true)
    setError('')
    setNotice('')
    try {
      // Flush any unsaved editor edits to the server FIRST so they become
      // live_dirty and pull_upstream checkpoints them as a version — otherwise
      // the working buffer is lost when the post-pull refetch replaces the
      // live content.
      await onBeforeMutate?.()
      const res = await api.pullLatest(artifact.slug)
      if (res.error) setError(res.error)
      else if (res.pull_result && res.pull_result.pulled === false)
        // pulled=false is a benign no-op (the "Pull latest" button is always
        // shown on a fork as a manual check, even when the 15s-stale status
        // says nothing is ahead), so surface it as a neutral notice — not a
        // danger-styled error.
        setNotice(String(res.pull_result.reason || i18nT('pages.artifactDetailPage.nothing_to_pull')))
      else onPulled()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : i18nT('pages.artifactDetailPage.pull_failed'))
    } finally {
      setPulling(false)
    }
  }

  const handleOverwrite = async () => {
    setOverwriting(true)
    setError('')
    setNotice('')
    try {
      // Flush unsaved editor edits first so the overwrite pushes the user's
      // actual current edits (and they're snapshotted locally), not the
      // last-saved live state.
      await onBeforeMutate?.()
      const res = await api.overwriteRemote(artifact.slug)
      if (res.error) setError(res.error)
      else if (res.overwrite_result && res.overwrite_result.overwritten === false)
        setError(String(res.overwrite_result.reason || i18nT('pages.artifactDetailPage.could_not_overwrite')))
      else onPulled()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : i18nT('pages.artifactDetailPage.overwrite_failed'))
    } finally {
      setOverwriting(false)
    }
  }

  const handleSnapshot = async () => {
    setSnapshotting(true)
    setError('')
    setNotice('')
    try {
      // Flush editor buffer first (if any) so the snapshot captures it, then
      // snapshot=true versions the live body and auto-pushes to the provider.
      await onBeforeMutate?.()
      const res = await api.updateArtifact(artifact.slug, { snapshot: true })
      if ((res as { error?: string })?.error) setError(String((res as { error?: string }).error))
      else onPulled()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : i18nT('pages.artifactDetailPage.snapshot_failed'))
    } finally {
      setSnapshotting(false)
    }
  }

  // No registered provider → no sync surface (public edition renders nothing).
  if (providers.length === 0) return null

  // Provider label from the registry's self-described display_name — never a
  // hardcoded vendor string.
  const provLabel = providers.find((p) => p.name === pub?.provider)?.display_name
    || pub?.provider || 'the remote'

  // Local edits not yet published, with no remote drift: a visible prompt to
  // publish them. (When the remote is ALSO ahead, the pull/overwrite banner
  // below handles it and already reassures that local edits are checkpointed.)
  if (!fm && !upstreamAhead && pub && hasLocalEdits) {
    return (
      <div className="mb-3 flex items-center gap-2 px-3 py-2 rounded-md border text-[13px] border-warn/40 bg-warn-subtle text-warn">
        <Camera size={14} className="lucide-inline shrink-0" />
        <span className="flex-1">{i18nT('pages.artifactDetailPage.local_changes_not_yet_published_to')} {provLabel}.</span>
        {error && <span className="text-danger">{error}</span>}
        <Btn
          type="button"
          onClick={handleSnapshot}
          disabled={snapshotting}
          className="gap-1 px-2 py-0.5 text-[12px] font-medium border-warn/50 hover:bg-warn/10"
          title={i18nT('pages.artifactDetailPage.snapshot_the_current_content_as_a_new_version_an')}
        >
          <Camera size={12} />
          {snapshotting ? i18nT('pages.artifactDetailPage.publishing') : i18nT('pages.artifactDetailPage.snapshot_to_publish')}
        </Btn>
      </div>
    )
  }

  // Nothing to surface: not a fork and the remote copy isn't ahead.
  if (!fm && !upstreamAhead) return null

  const tone = _SYNC_TONES[upstreamAhead ? 'warn' : 'info']
  // Provider-supplied (fork/publication metadata) — validate the scheme before
  // rendering a clickable link so a malicious provider can't smuggle a
  // javascript:/file: URL into "View remote".
  const upstreamUrl = safeHttpUrl(fm?.upstream_url || pub?.view_url || '')
  return (
    <div className={`mb-3 flex items-center gap-2 px-3 py-2 rounded-md border text-[13px] ${tone.wrap}`}>
      {fm ? (
        <GitFork size={14} className="lucide-inline shrink-0" />
      ) : (
        <RefreshCw size={14} className="lucide-inline shrink-0" />
      )}
      <span className="flex-1">
        {fm ? (
          <>
            {i18nT('pages.artifactDetailPage.forked_from')} <strong>{fm.upstream_owner || 'someone'}</strong>{i18nT('pages.artifactDetailPage.s_artifact')}
            {fm.forked_at ? ` on ${fm.forked_at.slice(0, 10)}` : ''}
          </>
        ) : (
          <>{i18nT('pages.artifactDetailPage.published_artifact')}</>
        )}
        {upstreamUrl && (
          <>
            {' · '}
            <a href={upstreamUrl} target="_blank" rel="noopener noreferrer" className="underline hover:no-underline">
              {i18nT('pages.artifactDetailPage.view_remote')}
            </a>
          </>
        )}
        {upstreamAhead && (
          <> {i18nT('pages.artifactDetailPage.a_newer_version_is_available_in_the_remote_copy')}{cloudV ? ` (v${cloudV})` : ''}{pub ? ' — pull it down, or overwrite to keep yours' : ''}.</>
        )}
        {upstreamAhead && hasLocalEdits && (
          <> {i18nT('pages.artifactDetailPage.your_current_edits_are_saved_as_a_version_first')}</>
        )}
      </span>
      {(upstreamAhead || !!fm) && (
        <Btn
          type="button"
          onClick={handlePull}
          disabled={pulling || overwriting}
          className={`gap-1 px-2 py-0.5 text-[12px] font-medium ${tone.btn}`}
          title={i18nT('pages.artifactDetailPage.pull_the_latest_remote_content_as_a_new_local_ve')}
        >
          <RefreshCw size={12} className={pulling ? 'animate-spin' : undefined} />
          {i18nT('pages.artifactDetailPage.pull_latest')}
        </Btn>
      )}
      {upstreamAhead && !!pub && (
        <Btn
          type="button"
          onClick={handleOverwrite}
          disabled={overwriting || pulling}
          className={`gap-1 px-2 py-0.5 text-[12px] font-medium ${tone.btn}`}
          title={i18nT('pages.artifactDetailPage.push_your_local_version_up_as_the_remote_s_new_v')}
        >
          <ArrowUp size={12} className={overwriting ? 'animate-pulse' : undefined} />
          {i18nT('pages.artifactDetailPage.overwrite_remote')}
        </Btn>
      )}
      {error && <span className="text-danger text-[11px]">{error}</span>}
      {notice && !error && <span className="text-muted text-[11px]">{notice}</span>}
    </div>
  )
}

