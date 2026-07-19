import { safeSetItem } from '../utils/safeStorage'
import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import WebAppArtifactCard from '../components/WebAppArtifactCard'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useQuery, useQueryClient, useMutation } from '@tanstack/react-query'
import { ArrowLeft, AlertTriangle, Camera, ExternalLink, Download, Pencil, X, AlertCircle, RotateCcw, Plus, Sparkles, Link2, MessageSquare, Monitor, Undo2, Upload, Folder as FolderIcon } from 'lucide-react'
import { useTheme } from '../hooks/useTheme'
import { type IframeSelection } from '../hooks/useCommentBridge'
import { useAppDispatch } from '../store'
import { switchSlot } from '../store/chatSlice'
import { sanitizeCssValue } from '../lib/cssSanitize'
import { THEME_VAR_NAMES, buildSrcdoc } from '../lib/widgetSrcdoc'
import { api } from '../api/client'
import { PageHeader, Card, Badge, Btn } from '../components/ui'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from '../components/ui/dropdown-menu'
import { ArtifactSharePanel } from '../components/ArtifactSharePanel'
import ReadingWidthToggle from '../components/ReadingWidthToggle'
import { useReadingWidth } from '../hooks/useReadingWidth'
import { useArtifactFolders, useMoveArtifactToFolder } from '../hooks/useArtifactFolders'
import { FolderPickerItems } from '../components/FolderMoveSubmenu'
import { folderBreadcrumb } from '../utils/artifactFolderTree'
import { CommentPopover } from '../components/CommentOverlay'
import { CommentsSidebar } from '../components/CommentsSidebar'
import { CommentThreadPopover } from '../components/CommentThreadPopover'
import { findCoords, resolveSourcePos } from '../components/MarkdownPanel'
// Artifact body renderers, extracted here so the chat side panel shares them.
import { ArtifactBodyNative, ArtifactBodyIframe, isEditableKind } from '../components/ArtifactBody'
import { useArtifactPopouts } from '../hooks/useArtifactPopouts'
import { forwardToMain, type NavIntent } from '../utils/artifactPopout'
import { writePrefill } from '../utils/navIntent'
import { announceCommentsChanged, onCommentsChanged } from '../utils/artifactCommentsSync'
import { PublishHub } from '../components/PublishHub'
import type { Artifact, ArtifactEvent, ArtifactComment, CommentAnchor } from '../types'

// Artifact "Iterate" affordances are hidden pending an artifact redesign
// (task P472753393). This gates every user-facing entry point into the
// iterate flow — the header Sparkles button, the anchored-comment creation
// path (`commentable`), the CommentsSidebar "Ask agent" action, and the
// "click Iterate" tips — while leaving iterateWithAgent / buildPromptForChat
// and the agent-driven `iterated` lifecycle event fully intact. The durable
// comment stack (view / doc-level add / reply / resolve / review) stays fully
// available; only the iterate round-trip and the anchored-selection creation
// (which existed only to feed iterate in the fork) are hidden. Flip to `true`
// (or delete the gate) when the redesign lands.
// NOTE (MeshClaw sync): the upstream keeps these visible — do NOT let a sync
// re-show them; see skills/meshclaw-sync/SKILL.md → "Fork-initiated UX
// divergences" (verdict SKIP_FORKUX).
const SHOW_ARTIFACT_ITERATE = false

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
 * Header folder chip (Mesh-2720): shows where the artifact is filed and opens
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
          title={current ? `Filed in ${path} — click to move` : 'Not in a folder — click to file'}
          aria-label={current ? `Folder: ${path}. Move to folder` : 'Move to folder'}
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
  return d.toLocaleString(undefined, {
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
      <div className="text-[12px] text-muted">No lifecycle events yet.</div>
    )
  }
  // Render newest first so the most recent activity is at the top.
  const ordered = [...events].sort((a, b) => (a.ts < b.ts ? 1 : a.ts > b.ts ? -1 : 0))
  const verb = (t: ArtifactEvent['type'], md?: ArtifactEvent['metadata']) => {
    if (t === 'comment') {
      const action = typeof md?.action === 'string' ? md.action : ''
      return { deleted: 'Comment removed', reviewed: 'Comment marked for review', resolved: 'Comment resolved' }[action] ?? 'Comment'
    }
    return {
      created: 'Created',
      edited: 'Edited',
      iterated: 'Iterated',
      referenced: 'Referenced',
      reverted: 'Reverted',
    }[t] ?? t
  }
  // Distinct hues per type so created/edited/iterated don't visually blur
  // together (nrb feedback). reverted uses warn (orange) to flag its
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
              {ev.by && <span className="text-muted">by {ev.by}</span>}
              {ev.type === 'comment' ? null : ev.type === 'reverted' && ev.from_version != null ? (
                <span className="text-muted">v{ev.from_version} → v{ev.version}</span>
              ) : (
                ev.version != null && <span className="text-muted">→ v{ev.version}</span>
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
                title={`Open session ${ev.session_id}`}
              >
                from session {ev.session_id}
              </button>
            ) : ev.type === 'reverted' && ev.from_version != null ? (
              <span className="text-[11px] text-muted mt-0.5">
                content copied from v{ev.from_version}
              </span>
            ) : ev.session_id === 'dashboard:ui' ? (
              <span className="text-[11px] text-muted mt-0.5">via dashboard</span>
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
          title="Focus the popped-out window"
          aria-label="Focus popped-out window"
        >
          <Monitor size={13} />
        </button>
        <button
          type="button"
          onClick={() => bringBack(slug)}
          className="p-1.5 rounded-md border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all"
          title="Bring the artifact back into this window"
          aria-label="Bring artifact back to this window"
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
      title="Pop out into its own window"
      aria-label="Pop out to window"
    >
      <ExternalLink size={13} />
    </button>
  )
}

export default function ArtifactDetailPage({ popout = false }: { popout?: boolean } = {}) {
  const { slug = '' } = useParams<{ slug: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const dispatch = useAppDispatch()
  const { theme, colorTheme, themeVersion } = useTheme()
  const [selectedVersion, setSelectedVersion] = useState<number | null>(null)
  const [editing, setEditing] = useState(false)
  // Round 8 polish: while editing, the user can flip to a rendered
  // preview of the edit buffer (matches the side panel's Edit/Preview
  // toggle). Stays in edit mode — content isn't committed until Save
  // and isn't discarded until Cancel.
  const [previewDuringEdit, setPreviewDuringEdit] = useState(false)
  const { readingWidth, toggle: toggleReadingWidth, previewStyle: mdPreviewStyle } = useReadingWidth()
  const [editedContent, setEditedContent] = useState('')
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [showPublish, setShowPublish] = useState(false)
  // Tag editing (Mesh-1654 round 4): tags shown in the header are editable
  // inline. Adding a tag posts metadata-only (no version bump). Removing a
  // tag works the same way.
  const [addingTag, setAddingTag] = useState(false)
  const [searchParams] = useSearchParams()
  const [showShare, setShowShare] = useState(() => searchParams.get('share') === '1')
  // Tracks the publication error the user explicitly dismissed, so the
  // auto-opened error panel can be closed (AutoSDE) yet re-opens when a *new*
  // (different) error appears. Comparing the value — not a bool — gives the
  // reset-on-new-error behaviour without a TDZ-prone effect over `artifact`.
  const [dismissedError, setDismissedError] = useState<string | null>(null)
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
  // Comments live in a collapsible right-hand sidebar. It stays collapsed by
  // default — an empty comment panel is just wasted space on a dashboard or
  // infographic — and auto-reveals only once the artifact has at least one
  // comment (see the effect below). A manual show/hide applies to the current
  // view only; we intentionally do NOT persist it, so every artifact
  // independently does the right thing instead of a global pin re-opening
  // empty panels everywhere.
  const [sidebarOpen, setSidebarOpen] = useState(false)
  // Flipped once the user manually toggles, so the comment-driven auto-reveal
  // below stops overriding an explicit choice — but only for the current
  // artifact (cleared on navigation; see the effect).
  const sidebarUserToggledRef = useRef(false)
  const toggleSidebar = useCallback(() => {
    sidebarUserToggledRef.current = true
    setSidebarOpen(v => !v)
  }, [])
  // Auto-reveal the sidebar when the artifact has comments; collapse it when it
  // has none. Reacts to commentCount so adding the first comment reveals the
  // panel and removing the last collapses it — unless the user has taken manual
  // control via the toggle. React Router reuses this component across the
  // parameterized route, so navigating to a different artifact clears the
  // manual-toggle override, giving every artifact the comment-driven default.
  const sidebarNavRef = useRef(slug)
  useEffect(() => {
    if (sidebarNavRef.current !== slug) {
      sidebarNavRef.current = slug
      sidebarUserToggledRef.current = false
    }
    if (sidebarUserToggledRef.current) return
    setSidebarOpen(commentCount > 0)
  }, [slug, commentCount])
  const [popover, setPopover] = useState<{ x: number; y: number; anchor: string; line?: number; column?: number; prefix?: string; suffix?: string; startOffset?: number; endOffset?: number } | null>(null)
  // Bidirectional anchor↔comment linking (item #5): flash a sidebar row when
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
  // historical versions (Mesh-1654 round 11 bug fix, found by nrb).
  const isCurrent = !selectedVersion

  const versionQuery = useQuery<Artifact>({
    queryKey: ['artifact', slug, 'version', selectedVersion],
    queryFn: () => api.artifactVersion(slug, selectedVersion as number),
    enabled: !!slug && !!selectedVersion && !isCurrent,
  })

  const artifact = isCurrent ? detailQuery.data : versionQuery.data
  const editable = !!artifact && isEditableKind(artifact.kind) && isCurrent
  const dirty = editing && !!artifact && editedContent !== (artifact.content ?? '')

  // Reconcile sharing/version drift made directly at the publishing provider
  // when the detail page opens (#4): external visibility changes and pointer
  // rollbacks surface here. Fires once per slug; the invalidate-driven refetch
  // keeps the stable artifact_id so this effect won't re-trigger into a loop.
  const refreshedSlugRef = useRef<string | null>(null)
  const refreshSharingMut = useMutation({
    mutationFn: () => api.refreshArtifactSharing(slug),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['artifact', slug] }),
  })
  const triggerRefresh = refreshSharingMut.mutate
  useEffect(() => {
    const pubId = artifact?.publication?.artifact_id
    if (!pubId || refreshedSlugRef.current === slug) return
    refreshedSlugRef.current = slug
    triggerRefresh()
  }, [artifact?.publication?.artifact_id, slug, triggerRefresh])

  // ── Tag editing handlers (Mesh-1654 round 4) ────────────────────────────
  const updateTagsMut = useCallback(async (newTags: string[]) => {
    if (!artifact) return
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

  // ── Edit / save / cancel / revert handlers ────────────────────────────────
  const startEditing = useCallback(() => {
    if (!artifact || !editable) return
    setEditedContent(artifact.content ?? '')
    setEditing(true)
    setSaveError(null)
  }, [artifact, editable])

  const cancelEditing = useCallback(() => {
    if (dirty && !window.confirm('Discard unsaved changes?')) return
    setEditing(false)
    setEditedContent('')
    setSaveError(null)
    setPreviewDuringEdit(false)
  }, [dirty])

  const handleSave = useCallback(async (snapshot = false) => {
    if (!artifact || !dirty) return
    setSaving(true)
    setSaveError(null)
    try {
      // snapshot=true → bumps version (creates a new numbered snapshot).
      // snapshot=false → silently updates the live state without versioning,
      // matching the explicit-snapshot model from Mesh-1654 round 5.
      await api.updateArtifact(artifact.slug, { content: editedContent, snapshot })
      await queryClient.invalidateQueries({ queryKey: ['artifact', slug] })
      if (snapshot) {
        await queryClient.invalidateQueries({ queryKey: ['artifact-versions', slug] })
        await queryClient.invalidateQueries({ queryKey: ['artifact-events', slug] })
        // Snapshot is a deliberate checkpoint — drop out of edit mode
        // so the user sees the result. Plain Save (silent) keeps the
        // user in the editor (AutoSDE round 13 UX fix): after the query
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

  // Snapshot the current live state without an edit. Used by the Snapshot
  // button when not editing — captures whatever is on disk / current.html
  // as a new numbered version. Mesh-1654 round 6: snapshot anytime live
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
      `Revert to v${targetVersion}? This creates a new version (v${newVersion}) with v${targetVersion}'s content. The current state stays in version history.`,
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
  // Anchored (selection-driven) comment creation existed in the fork solely to
  // feed the Iterate flow, so it is gated behind SHOW_ARTIFACT_ITERATE with the
  // rest of the iterate affordances. Doc-level comments via the CommentsSidebar
  // remain available for all kinds regardless of this flag.
  const commentable = SHOW_ARTIFACT_ITERATE && !!artifact && !editing && isCurrent && (
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
      // Replying to a resolved thread auto-reopens it (feedback #10). A second
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
    sidebarUserToggledRef.current = false
    setSidebarOpen(true)
    setPopover(null)
    window.getSelection()?.removeAllRanges()
  }, [popover, postCommentMut])

  // Doc-level add (from the sidebar) — works for ALL kinds, including
  // HTML/widget where in-iframe text selection isn't reachable.
  const addDocComment = useCallback((text: string) => {
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

  // Build the chat-injection prompt. Comments are durable and the agent reads
  // them itself via `artifact_get_comments`, so we NEVER dump comment text into
  // the prompt anymore — `addressComments` just nudges the agent to read+act on
  // the open ones. The plain header is the generic "discuss this" entry point
  // for ALL kinds (including widgets that can't be edited inline).
  const buildPromptForChat = useCallback((addressComments = false): string => {
    if (!artifact) return ''
    const header = `Iterate on artifact \`${artifact.slug}\` (${artifact.name})`
    if (addressComments && commentCount > 0) {
      return header + `: please review and address the ${commentCount} open comment${commentCount === 1 ? '' : 's'} on this artifact (use the artifact_get_comments tool to read them).`
    }
    return header + ': '
  }, [artifact, commentCount])

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

  /** Open a fresh chat slot pre-loaded with this artifact in the input.
   * Always creates a NEW session so historical context from unrelated
   * conversations doesn't contaminate the iterate loop. The user reviews
   * the prefill and clicks Send — we never auto-send because comment
   * dumps can be long and may need editing.
   *
   * Works regardless of pending comment state: the comment-less path
   * (just `Iterate on artifact <slug>: `) is the primary "discuss this"
   * entry point and is available for ALL artifact kinds (including
   * widgets that can't be edited inline).
   */
  const iterateWithAgent = useCallback(async (addressComments = false) => {
    if (!artifact) return
    const prompt = buildPromptForChat(addressComments)
    try {
      const res = await api.createChatSlot(`Artifact: ${artifact.name}`)
      sendNav({ path: '/chat', slotKey: res.key, prefill: { slotKey: res.key, prompt } })
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err))
    }
  }, [artifact, buildPromptForChat, sendNav])

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

  // Persistent active comment (feedback #4); NO transitory flash (feedback #5).
  const [activeCommentId, setActiveCommentId] = useState<string | null>(null)
  const [bodyScrollNonce, setBodyScrollNonce] = useState(0)
  // Read/unread tracking (feedback #9): a per-artifact set of seen comment ids
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
    return <div className="p-6 text-muted">Loading…</div>
  if (detailQuery.error) {
    const msg = detailQuery.error instanceof Error ? detailQuery.error.message : String(detailQuery.error)
    return (
      <>
        <PageHeader title="Artifact" subtitle={slug} />
        <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">
          <Card>
            <div className="flex items-start gap-3">
              <AlertTriangle className="lucide-inline text-danger" />
              <div>
                <div className="text-sm text-danger font-medium">Failed to load artifact</div>
                <div className="text-[13px] text-muted mt-1">{msg}</div>
              </div>
            </div>
            <div className="mt-3">
              {/* In a popout this forwards to the main window (the popout must
                  never become the library page); in the main app it's a plain
                  local navigation. */}
              <Btn onClick={() => sendNav({ path: '/artifacts' })}>← Back to library</Btn>
            </div>
          </Card>
        </div>
      </>
    )
  }
  if (!artifact) return <div className="p-6 text-muted">Not found.</div>

  const sel =
    'bg-bg-elevated border border-border rounded-md px-2 py-1 text-text text-[12px] font-body outline-none cursor-pointer transition-colors focus-ring'

  // Cron-source warning shown only while editing — surface the foot-gun
  // (next cron run will create a newer version) without noisy chrome on
  // read-only views.
  const showCronWarning = editing && artifact.source === 'cron'

  return (
    <>
      <PageHeader title={artifact.name} subtitle={`Artifact: ${artifact.slug}`} />
      <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        <div className="flex flex-wrap items-center gap-2 mb-4">
          {!popout && (
            <Btn onClick={() => {
              if (dirty && !window.confirm('Discard unsaved changes?')) return
              navigate('/artifacts')
            }} className="flex items-center gap-1">
              <ArrowLeft size={13} /> Back
            </Btn>
          )}
          <Badge variant="aim">{artifact.kind}</Badge>
          <FolderChip artifact={artifact} />
          {artifact.tags.map((t) => (
            <span key={t} className="inline-flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded bg-bg-elevated border border-border text-muted group">
              {t}
              <button
                type="button"
                onClick={() => removeTag(t)}
                className="opacity-0 group-hover:opacity-100 hover:text-danger transition-opacity bg-transparent border-none cursor-pointer p-0 inline-flex items-center"
                title={`Remove tag ${t}`}
                aria-label={`Remove tag ${t}`}
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
              placeholder="tag…"
              className="text-[11px] px-1.5 py-0.5 rounded bg-bg-elevated border border-accent text-text outline-none"
              style={{ width: '90px' }}
              aria-label="Add a tag"
            />
          ) : (
            <button
              type="button"
              onClick={() => setAddingTag(true)}
              className="inline-flex items-center gap-0.5 text-[11px] px-1.5 py-0.5 rounded border border-dashed border-border text-muted hover:text-text hover:border-border-strong cursor-pointer bg-transparent transition-colors"
              title="Add a tag (comma-separated tags supported)"
              aria-label="Add a tag"
            >
              <Plus size={10} /> tag
            </button>
          )}
          <span className="mc-art-toolbar ml-auto flex items-center gap-2 text-[13px] text-muted">
            <span>Version</span>
            <select
              className={sel}
              disabled={saving}
              value={selectedVersion === null ? 'live' : String(selectedVersion)}
              onChange={(e) => {
                if (dirty && !window.confirm('Discard unsaved changes?')) return
                setEditing(false)
                setEditedContent('')
                const raw = e.target.value
                if (raw === 'live') {
                  setSelectedVersion(null)
                } else {
                  setSelectedVersion(parseInt(raw, 10))
                }
              }}
            >
              {/* Live = always-current state. Distinct from any numbered
                  snapshot because in the explicit-snapshot model saves
                  update Live without bumping versions, so Live can be
                  ahead of the latest numbered snapshot. */}
              <option value="live">Live</option>
              {versions.slice().reverse().map((v) => (
                <option key={v} value={v}>
                  v{v}
                </option>
              ))}
            </select>

            {/* Revert: only meaningful when viewing a historical version */}
            {!isCurrent && (
              <button
                type="button"
                onClick={handleRevert}
                disabled={saving}
                className="px-2 py-1 rounded-md text-[12px] font-medium border border-warn/40 text-warn hover:border-warn cursor-pointer transition-all disabled:opacity-40"
                title={`Revert to v${selectedVersion}`}
                aria-label={`Revert to v${selectedVersion}`}
              >
                <span className="inline-flex items-center gap-1"><RotateCcw size={13} /> Revert</span>
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
                  title="Save to Live (Cmd+S) — updates the live state without versioning"
                >
                  {saving ? 'Saving…' : 'Save'}
                </button>
                <button
                  type="button"
                  onClick={() => handleSave(true)}
                  disabled={!dirty || saving}
                  className="px-2 py-1 rounded-md text-[12px] font-medium border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all disabled:opacity-40"
                  title="Snapshot (Cmd+Shift+S) — save and create a new version"
                >
                  <span className="inline-flex items-center gap-1"><Camera size={13} /> Snapshot</span>
                </button>
                <button
                  type="button"
                  onClick={cancelEditing}
                  disabled={saving}
                  className="px-2 py-1 rounded-md text-[12px] font-medium border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all disabled:opacity-40"
                  title="Cancel (Esc)"
                >
                  <span className="inline-flex items-center gap-1"><X size={13} /> Cancel</span>
                </button>
                <button
                  type="button"
                  onClick={() => setPreviewDuringEdit(p => !p)}
                  disabled={saving}
                  className={`px-2 py-1 rounded-md text-[12px] font-medium border cursor-pointer transition-all disabled:opacity-40 ${previewDuringEdit ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text hover:border-border-strong'}`}
                  title={previewDuringEdit ? 'Back to editor' : 'Preview rendered output of current edits'}
                >
                  {previewDuringEdit ? 'Edit' : 'Preview'}
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
                    title="Snapshot — capture the current state as a new version"
                  >
                    <span className="inline-flex items-center gap-1"><Camera size={13} /> Snapshot</span>
                  </button>
                )}
                {editable && (
                  <button
                    type="button"
                    onClick={startEditing}
                    className="px-2 py-1 rounded-md text-[12px] font-medium border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all"
                    title="Edit content"
                    aria-label="Edit content"
                  >
                    <Pencil size={13} />
                  </button>
                )}
                {/* Iterate — primary "discuss with agent" action for all kinds;
                    for widgets it is the only way to ask the agent to change the
                    artifact. Comments are durable and read by the agent via
                    artifact_get_comments, so this no longer bundles them. Hidden
                    in a popout window since it navigates away to a new chat — you
                    iterate from the main dashboard, alongside the popped-out view.
                    Also hidden while the fork keeps Iterate off (SHOW_ARTIFACT_ITERATE). */}
                {SHOW_ARTIFACT_ITERATE && !popout && (
                  <button
                    type="button"
                    onClick={() => iterateWithAgent()}
                    className="px-2 py-1 rounded-md text-[12px] font-medium border border-accent text-accent-fg bg-accent cursor-pointer hover:bg-accent-hover hover:shadow-[0_0_12px_var(--accent-glow)] transition-all"
                    title="Iterate — discuss this artifact with the agent"
                    aria-label="Iterate"
                  >
                    <Sparkles size={13} />
                  </button>
                )}
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
              className={`p-1.5 rounded-md border cursor-pointer transition-all ${sidebarOpen ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text hover:border-border-strong'}`}
              title={sidebarOpen ? 'Hide comments' : 'Show comments'}
              aria-label="Toggle comments"
              aria-pressed={sidebarOpen}
            >
              <span className="inline-flex items-center gap-1">
                <MessageSquare size={13} />
                {commentCount > 0 && (
                  <span className="ml-0.5 px-1 rounded bg-accent/20 text-[10px]">{commentCount}</span>
                )}
              </span>
            </button>
            <button
              type="button"
              onClick={() => setShowShare((s) => !s)}
              className={`p-1.5 rounded-md border cursor-pointer transition-all ${artifact.publication ? 'border-ok/40 text-ok hover:border-ok' : 'border-border text-muted hover:text-text hover:border-border-strong'}`}
              title={artifact.publication ? 'Published — manage sharing' : 'Publish or share this artifact'}
              aria-label="Publish"
            >
              <Link2 size={13} />
            </button>
            {/* Pop out — opens the artifact in its own live browser window
                (was a throwaway blob: tab). Swaps to Focus + Bring-back once
                out. Not shown inside the popout window itself (the frame's
                Return button handles closing). */}
            {!popout && <ArtifactPopoutControl slug={slug} name={artifact.name} />}
            {/* Publish action — shown for non-webapp publishable kinds */}
            {artifact.kind !== 'webapp' && (
              <Btn
                type="button"
                onClick={() => setShowPublish(v => !v)}
                className="p-1.5 rounded"
                title="Publish"
                aria-label="Publish"
              >
                <Upload size={13} />
              </Btn>
            )}
            <Btn
              type="button"
              onClick={downloadAsHtml}
              className="p-1.5 rounded-md border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all"
              title="Download"
              aria-label="Download"
            >
              <Download size={13} />
            </Btn>
          </span>
        </div>

        {artifact.description && (
          <div className="mb-3 text-sm text-muted italic">{artifact.description}</div>
        )}

        {showCronWarning && (
          <div className="mb-3 flex items-start gap-2 px-3 py-2 rounded-md border border-warn/40 bg-warn-subtle text-[13px] text-warn">
            <AlertCircle size={14} className="lucide-inline shrink-0 mt-0.5" />
            <span>
              <strong>Heads up:</strong> this artifact is regenerated by a cron job. Your edits will be preserved in version history, but the next cron run will create a newer version that overrides what you save here.
            </span>
          </div>
        )}

        {saveError && (
          <div className="mb-3 px-3 py-2 rounded-md border border-danger/40 bg-danger-subtle text-[13px] text-danger">
            <strong>Save failed:</strong> {saveError}
          </div>
        )}

        {(showShare ||
          (!!artifact.publication?.last_error &&
            dismissedError !== artifact.publication?.last_error)) && (
          <ArtifactSharePanel
            artifact={artifact}
            onClose={() => {
              setShowShare(false)
              setDismissedError(artifact.publication?.last_error ?? null)
            }}
          />
        )}

        {/* Publish panel — toggled by the Upload toolbar button */}
        {showPublish && artifact.kind !== 'webapp' && (
          <div className="mb-3">
            <PublishHub artifact={artifact} onClose={() => setShowPublish(false)} />
          </div>
        )}

        {artifact.kind === 'webapp' ? (
          <WebAppArtifactCard artifact={artifact} />
        ) : (
        <div className="flex gap-4 items-start">
          <div className="flex-1 min-w-0">
            {usesIframe ? (
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

          {/* Collapsible right-hand comment sidebar. Durable, threaded, works
              for ALL kinds (doc-level add for HTML/widget; anchored add for
              markdown/text via the inline popover above). */}
          {sidebarOpen && (
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
              onAskAgent={SHOW_ARTIFACT_ITERATE && commentCount > 0 ? () => iterateWithAgent(true) : undefined}
              onClose={toggleSidebar}
              onCommentClick={activateFromSidebar}
              onEditComment={editComment}
              activeCommentId={activeCommentId}
            />
          )}
        </div>
        )}

        <div className="mt-3 text-[12px] text-muted">
          Created {artifact.created_at} &middot; Updated {artifact.updated_at} &middot;{' '}
          {/* "Live" reflects the always-current state. Numbered versions
              are historical snapshots — when one is selected, isCurrent is
              false (because the dropdown is non-Live). */}
          {selectedVersion === null
            ? `Showing Live (v${detailQuery.data?.version ?? '?'})`
            : `Showing v${effectiveVersion} (historical)`}
          {dirty && <span className="ml-2 text-warn">• unsaved changes</span>}
          {/* `commentable` already implies SHOW_ARTIFACT_ITERATE (see its definition). */}
          {commentable && commentCount === 0 && (
            <span className="ml-2 text-muted/80">Tip: select text to anchor a comment, or use the <strong>Comments</strong> panel to add one.</span>
          )}
          {/* The Comments panel is always available; the Iterate mention is gated. */}
          {!commentable && !editing && isCurrent && (
            <span className="ml-2 text-muted/80">
              Tip: use the <strong>Comments</strong> panel to comment
              {SHOW_ARTIFACT_ITERATE ? <>, or <strong>Iterate</strong> to chat with the agent</> : null}.
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

        {/* Phase 5 (Mesh-1654): lifecycle event log + activity timeline. */}
        <div className="mt-6">
          <h3 className="text-[13px] font-semibold text-text-strong mb-2">Activity</h3>
          <ActivityTimeline
            events={eventsQuery.data?.events ?? []}
            navigateToSlot={(slotKey) => sendNav({ path: '/chat', slotKey })}
          />
        </div>
      </div>
    </>
  )
}

