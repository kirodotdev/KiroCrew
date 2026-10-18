/**
 * The artifact page's state and the document it shows: the route's slug, the
 * edit buffer and the header's local state (`useArtifactDetailPageState`), and
 * the artifact, its versions and events, the snapshot on screen and the reset a
 * navigation to another artifact runs (`useArtifactDetailPageDocument`).
 *
 * The `ArtifactDetailPage` facade calls each hook here at the point its code
 * sits in the page's hook order, which `ArtifactDetailPage.owners.test.tsx`
 * pins: React runs a component's effects in registration order, so moving one
 * is a behaviour change even when every test of the moved code still passes.
 */
import { useImeGuard } from '../hooks/useImeGuard'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useTheme } from '../hooks/useTheme'
import { useAppDispatch } from '../store'
import { buildSrcdoc, readThemeVars } from '../lib/widgetSrcdoc'
import { api } from '../api/client'
import { useConfirm } from '../components/ConfirmDialog'
import { useReadingWidth } from '../hooks/useReadingWidth'
// Artifact body renderers, extracted here so the chat side panel shares them.
import { isEditableKind } from '../components/ArtifactBody'
import { useArtifactLiveReload } from '../hooks/useArtifactLiveReload'
import { consumeJustCreatedBlank } from '../lib/blankHandoff'
import type { Artifact, ArtifactEvent } from '../types'

import { i18nT } from '../i18n/t'
import { useGuideSelection } from '../guide/guidePredicates'

/** The route, the confirm dialog, and the page-local state the header and the editor share. */
export function useArtifactDetailPageState() {
  const { slug = '' } = useParams<{ slug: string }>()
  const navigate = useNavigate()
  const { confirm, confirmDialog, confirmOpen } = useConfirm()
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
  // Identifies the artifact and edit session that owns an async write. Navigation,
  // starting an edit and ending one advance it, so a late completion only touches
  // the edit session that sent it.
  const editGenerationRef = useRef(0)
  // Token of the content this edit started from (Artifact.content_token). Pinned at
  // edit start and advanced only by this page's own successful save, so a
  // background refetch can never re-base a stale buffer onto newer content.
  const editBaseTokenRef = useRef<string | undefined>(undefined)
  // The live token from a 409. Only the explicit Save sends it, as the deliberate
  // overwrite its relabelled button offers; every other write keeps the base
  // token and keeps being refused.
  const overwriteTokenRef = useRef<string | undefined>(undefined)
  // Set while a write carrying one of those tokens is on the wire. Only its own
  // response advances the base token, so a second write sent before it settles
  // (Cmd+S pressed twice) would be refused as stale with this tab the only
  // writer. A save skipped here leaves the buffer dirty, so nothing is lost.
  const tokenWriteInFlightRef = useRef(false)
  const [saveConflict, setSaveConflict] = useState(false)

  /** SEPARATE from `saveError`, which renders a "save failed" notice: a background /context
   *  failure told a user with a dirty draft that their SAVE had failed. This one renders in the
   *  chat panel's own column and names the real consequence. */
  const [contextError, setContextError] = useState<string | null>(null)
  // A resume nudge failing is not a failure to share: the earlier injection succeeded and the
  // chat works, so only the LATEST version is missing and the title must say so.
  const [contextErrorIsRefresh, setContextErrorIsRefresh] = useState(false)
  /** OUTCOME-FIRST, and never the raw transport text: `friendlyErrText` rewrites this
   *  endpoint's 429 into the tunnel rate-limit string, which names the wrong cause and
   *  promises an automatic retry nothing performs. The raw detail stays in the error
   *  journal that `ApiError` already writes. */
  const contextFailureMessage = useCallback(
    (isRefresh: boolean): string =>
      i18nT(
        isRefresh
          ? 'pages.artifactDetailPage.chat_context_stale_not_attached'
          : 'pages.artifactDetailPage.chat_context_not_attached',
      ),
    [],
  )
  const [sendToSessionError, setSendToSessionError] = useState<string | null>(null)
  const [showPublish, setShowPublish] = useState(false)
  // Tag editing: tags shown in the header are editable inline. Adding a tag
  // posts metadata-only (no version bump). Removing a tag works the same way.
  const [addingTag, setAddingTag] = useState(false)
  const [newTag, setNewTag] = useState('')
  const ime = useImeGuard()

  return {
    slug, navigate, confirm, confirmDialog, confirmOpen, blankClaim, justCreatedBlank, renaming, setRenaming,
    nameDraft, setNameDraft, queryClient, dispatch, theme, colorTheme, themeVersion, selectedVersion,
    setSelectedVersion, editing, setEditing, previewDuringEdit, setPreviewDuringEdit, readingWidth,
    toggleReadingWidth, mdPreviewStyle, editedContent, setEditedContent, saving, setSaving, saveError,
    setSaveError, editGenerationRef, editBaseTokenRef, overwriteTokenRef, tokenWriteInFlightRef, saveConflict,
    setSaveConflict, contextError, setContextError, contextErrorIsRefresh, setContextErrorIsRefresh,
    contextFailureMessage, sendToSessionError, setSendToSessionError, showPublish, setShowPublish, addingTag,
    setAddingTag, newTag, setNewTag, ime,
  }
}

export type ArtifactDetailPageStateRead = ReturnType<typeof useArtifactDetailPageState>

/** The artifact on screen (live or a snapshot), its versions and events, and the reset a navigation to another artifact runs. */
export function useArtifactDetailPageDocument({
  stateRead, popout,
}: {
  stateRead: ArtifactDetailPageStateRead
  popout: boolean
}) {
  const {
    slug, setRenaming, setNameDraft, theme, colorTheme, themeVersion, selectedVersion, setSelectedVersion,
    editing, setEditing, setPreviewDuringEdit, mdPreviewStyle, editedContent, setEditedContent, setSaveError,
    editGenerationRef, editBaseTokenRef, overwriteTokenRef, setSaveConflict, setContextError,
    setContextErrorIsRefresh, setAddingTag, setNewTag,
  } = stateRead
  // Bidirectional anchor↔comment linking: flash a sidebar row when
  // its in-iframe highlight is clicked; scroll the iframe highlight when a
  // sidebar comment is clicked. Nonce forces a re-trigger on repeat clicks.
  const [iframeScrollTarget, setIframeScrollTarget] = useState<{ id: string; nonce: number } | null>(null)
  const previewRef = useRef<HTMLDivElement>(null)
  const bodyRef = useRef<HTMLDivElement>(null)
  const iframeBodyRef = useRef<HTMLDivElement>(null)

  // Reset version selection AND any in-progress edit when navigating between
  // artifacts. React Router v6 reuses the component instance for parameterized
  // routes, so without this reset, viewing v5 of one artifact then navigating
  // to another would attempt to fetch v5 of the new artifact (which may not
  // exist), and stale edit state would leak into the new artifact.
  useEffect(() => {
    editGenerationRef.current += 1
    setSelectedVersion(null)
    setEditing(false)
    setEditedContent('')
    // Reset with the rest of the edit state: this flag is per-document, and
    // leaking it across a navigation left the next artifact opening its editor
    // into a rendered preview.
    setPreviewDuringEdit(false)
    setSaveError(null)
    editBaseTokenRef.current = undefined
    overwriteTokenRef.current = undefined
    setSaveConflict(false)

    // WITH `saveError`: the notice names the artifact it was raised on, so leaving it up reports
    // the departed document's failure against the arriving one, under a leaked title variant.
    setContextError(null)
    setContextErrorIsRefresh(false)
    setAddingTag(false)
    setNewTag('')
    setRenaming(false)
    setNameDraft('')
    // No reset of the touched flag here -- it is keyed by slug, so a different
    // artifact already gets a fresh verdict, and clearing it at this point would
    // race the departing document's cleanup.
  }, [slug, editBaseTokenRef, editGenerationRef, overwriteTokenRef, setAddingTag, setContextError, setContextErrorIsRefresh, setEditedContent, setEditing, setNameDraft, setNewTag, setPreviewDuringEdit, setRenaming, setSaveConflict, setSaveError, setSelectedVersion])

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
  // File-backed artifacts: an agent rewriting the backing file never passes
  // through a handler, so the artifact_update WS event does not fire for it.
  // Watch the live pointer and refetch through the shared cache instead. Bound to
  // detailQuery (the Live record) rather than the selected snapshot, so a
  // historical view still tracks the pointer it will return to. This also covers
  // /popout/artifact/:slug, which renders this page in its own window.
  useArtifactLiveReload(slug, detailQuery.data?.source_path)
  // The open artifact is the guide's pick (artifact_open), named as its card
  // in the library names it, so later steps stay about this one artifact.
  useGuideSelection('artifact_open', { selected: !popout && !!detailQuery.data, available: true, name: popout ? undefined : detailQuery.data?.name })

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

  // ── Export helpers (Open-in-new-tab + Download) ───────────────────────────
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const themeVars = useMemo(() => readThemeVars(), [theme, colorTheme, themeVersion])
  const usesIframe = artifact?.kind === 'widget' || artifact?.kind === 'html'
  // HTML/widget artifacts own a full-width iframe surface. Reading width only
  // constrains native document bodies.
  const contentWidthStyle = usesIframe ? undefined : mdPreviewStyle
  const exportSrcdoc = useMemo(
    () => artifact?.content && usesIframe
      ? buildSrcdoc({ html: artifact.content, themeVars, mode: theme })
      : null,
    [artifact?.content, themeVars, theme, usesIframe],
  )

  return {
    iframeScrollTarget, setIframeScrollTarget, previewRef, bodyRef, iframeBodyRef, detailQuery, versionsQuery,
    eventsQuery, versions, effectiveVersion, isCurrent, versionQuery, artifact, editable, dirty, usesIframe,
    contentWidthStyle, exportSrcdoc,
  }
}

export type ArtifactDetailPageDocumentRead = ReturnType<typeof useArtifactDetailPageDocument>
