/**
 * The artifact detail page (`/artifacts/:slug`, and the pop-out window
 * `ArtifactPopoutFrame` renders). This file is the page's only import path:
 * it calls the owner hooks beside it in the page's hook order and renders the
 * two views. It keeps the comment counts and the side-panel state machine (the
 * owners and views read them, and two callbacks here set the panel), the revert
 * (its native confirm is allowlisted for this file), and the companion-chat
 * open path, its context entry and the upstream sync banner, which hold
 * agent-facing and provider strings that are not translated. New work goes to
 * the owner whose part it is:
 *   useArtifactDetailPageDocument.ts   page state, the artifact and its versions
 *   useArtifactDetailPageEditor.ts     header and editor writes, editor lifecycle
 *   useArtifactDetailPageComments.ts   comments, the anchored composer, threads
 *   useArtifactDetailPageCompanion.ts  the companion chat session
 *   ArtifactDetailPageHeader.tsx       the sticky header and its More menu
 *   ArtifactDetailPageBody.tsx         everything below the header
 */
import { useIsMobile } from '../hooks/useIsMobile'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ArrowUp, Camera, GitFork, Globe, RefreshCw } from 'lucide-react'
import { fetchSlots } from '../store/dashboardSlice'
import { safeHttpUrl } from '../lib/safeUrl'
import { api } from '../api/client'
import { PageHeader, Card, Btn } from '../components/ui'
// Artifact body renderers, extracted here so the chat side panel shares them.
import { isEditableKind } from '../components/ArtifactBody'
import { filterCommentsForForward } from '../lib/commentFilter'
import { writePrefill } from '../utils/navIntent'
import type { Artifact, ArtifactComment, CommentAnchor } from '../types'

import { i18nT } from '../i18n/t'
import { errMessage } from '../utils/thunkError'
import ErrorNotice from '../components/ErrorNotice'
import { useArtifactDetailPageState, useArtifactDetailPageDocument } from './useArtifactDetailPageDocument'
import { conflictToken, useArtifactDetailPageEditor } from './useArtifactDetailPageEditor'
import {
  type PendingAnchor, useArtifactDetailPageComments, useArtifactDetailPageComposer,
  useArtifactDetailPageThreads,
} from './useArtifactDetailPageComments'
import { pickBoundSlot, useArtifactDetailPageCompanion } from './useArtifactDetailPageCompanion'
import { useArtifactDetailPageMenu, ArtifactDetailPageHeader } from './ArtifactDetailPageHeader'
import { ArtifactDetailPageBody } from './ArtifactDetailPageBody'

export { isEditableKind }

export default function ArtifactDetailPage({ popout = false }: { popout?: boolean } = {}) {
  const stateRead = useArtifactDetailPageState()
  const {
    slug, navigate, confirmDialog, queryClient, dispatch, selectedVersion, setSelectedVersion, editing,
    setSaving, setSaveError,
  } = stateRead
  // ── Inline-comment state (durable via /api/artifacts/:slug/comments) ──
  const commentsQuery = useQuery<{ comments: ArtifactComment[]; remote_sync_error?: string | null }>({
    queryKey: ['artifact-comments', slug],
    queryFn: () => api.artifactComments(slug),
    enabled: !!slug,
    staleTime: 30_000,
  })
  // Memoized because it is the dep of rootIdOf / unreadRootIds / markThreadRead:
  // the `?? []` fallback (query not yet resolved) is a fresh array each render,
  // which would rebuild all three — and the overlay/sidebar memos keyed on them —
  // on every render. React Query keeps `data` referentially stable between
  // refetches that resolve deep-equal, so this changes only on real data.
  const durableComments = useMemo(() => commentsQuery.data?.comments ?? [], [commentsQuery.data?.comments])
  // Two counts, deliberately distinct.
  //
  // `displayCommentCount` drives what the human sees — the toggle badge, the
  // sidebar auto-reveal and the "add one" tip — so it counts every durable
  // comment: a resolved thread is still there to be revealed and read.
  const displayCommentCount = durableComments.length
  // `commentCount` is what the AGENT is told about, so it omits resolved
  // threads: counting those re-asks the agent to act on its own completed work.
  // It keeps the shorter name because the prompt copy below interpolates it.
  const commentCount = useMemo(
    () => filterCommentsForForward(durableComments).length,
    [durableComments],
  )
  const remoteSyncError = commentsQuery.data?.remote_sync_error ?? null
  // Right-hand panel state machine: the comments sidebar and the companion
  // chat panel share the same flex space, icon-toggled and mutually exclusive.
  // 'none' keeps the artifact full-width — an empty comment panel is just
  // wasted space on a dashboard or infographic — and comments auto-reveal only
  // once the artifact has at least one comment (see the effect below). A manual
  // show/hide applies to the current view only; we intentionally do NOT persist
  // it, so every artifact independently does the right thing instead of a
  // global pin re-opening empty panels everywhere.
  const isMobile = useIsMobile()
  const [panel, setPanel] = useState<'none' | 'comments' | 'chat'>('none')
  // On a narrow viewport an open side panel takes the whole width and the
  // document body is hidden — a comment box portalled to <body> would otherwise
  // keep floating over the panel, anchored to text that is no longer on screen.
  const narrowPanelOpen = isMobile && panel !== 'none'
  // Flipped once the user manually toggles, so the comment-driven auto-reveal
  // below stops overriding an explicit choice — but only for the current
  // artifact (cleared on navigation; see the effect).
  const sidebarUserToggledRef = useRef(false)
  const toggleSidebar = useCallback(() => {
    sidebarUserToggledRef.current = true
    setPanel(p => (p === 'comments' ? 'none' : 'comments'))
  }, [])
  // Auto-reveal the comments panel when the artifact has comments; collapse it
  // when it has none. Reacts to displayCommentCount so adding the first comment
  // reveals the panel and removing the last collapses it — unless the user has taken
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
      // NOT while narrow. There the panel takes the whole pane and the artifact
      // body steps aside, so auto-revealing lands every commented artifact on
      // comments about content the reader cannot see, with a close button to
      // find first. Auto-reveal was written for the side-by-side layout, where
      // the body stayed visible beside it. A manual open still survives, via the
      // user-toggled override this effect returns on above.
      return displayCommentCount > 0 && !isMobile ? 'comments' : 'none'
    })
  }, [slug, displayCommentCount, isMobile])
  const documentRead = useArtifactDetailPageDocument({ popout, stateRead })
  const { detailQuery, isCurrent, versionQuery, artifact, dirty } = documentRead
  const editorRead = useArtifactDetailPageEditor({ stateRead, documentRead })
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
  }, [artifact, selectedVersion, isCurrent, detailQuery.data?.version, queryClient, slug, setSaveError, setSaving, setSelectedVersion])
  const commentsRead = useArtifactDetailPageComments({ durableComments, stateRead, documentRead })
  const { postAnchoredMut } = commentsRead
  // Anchored add (from the selection toolbar's composer).
  const submitAnchored = useCallback((text: string, pending: PendingAnchor): Promise<boolean> => {
    let anchor: CommentAnchor | undefined
    if (pending.anchor) {
      anchor = { quote: pending.anchor, prefix: pending.prefix, suffix: pending.suffix }
      // Native text selections carry an offset; iframe selections omit it.
      if (pending.startOffset != null) {
        anchor.start_offset = pending.startOffset
        anchor.end_offset = pending.endOffset ?? pending.startOffset + pending.anchor.length
      }
    }
    // Resolve, never throw: on `false` the toolbar keeps the typed text and its
    // persisted draft and shows the in-box notice (this mutation deliberately
    // skips the page banner so a refusal is reported once); the panel reveal
    // below is a reaction to a STORED comment, so it waits for success.
    return postAnchoredMut.mutateAsync({
      text,
      scope: 'private',
      anchor,
    }).then(() => {
      // Adding a comment hands control back to the comment-driven default: reveal
      // the panel now, and clear the manual override so the auto effect can
      // collapse it again if every comment is later removed.
      //
      // EXCEPT when the chat panel is open. An anchored add is reachable while
      // chatting (the body stays visible in the left column), and switching panels
      // would yank the conversation out from under the user. The toolbar's comment
      // badge already increments, so the add is still visibly acknowledged. Same
      // rationale as the auto-reveal guard in the panel effect above.
      // Narrow: keep the override SET. Clearing it hands control back to the
      // auto-reveal effect, which is gated off while narrow -- so the panel the
      // user just posted into would be closed again the moment
      // `displayCommentCount` changes. Revealing it here is a user-initiated open,
      // which is exactly what the override means.
      sidebarUserToggledRef.current = isMobile
      setPanel(p => (p === 'chat' ? p : 'comments'))
      return true
    }, () => false)
  }, [postAnchoredMut, isMobile])
  const composerRead = useArtifactDetailPageComposer({
    submitAnchored, popout, stateRead, documentRead, commentsRead,
  })
  const { clearSelectionState } = composerRead
  /** Structured context entry naming the artifact — injected as background context
   *  with a short TTL, so the user's first message can be natural
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
  const companionRead = useArtifactDetailPageCompanion({
    durableComments, setPanel, sidebarUserToggledRef, buildCompanionContext, popout, stateRead, documentRead,
  })
  const {
    sendNav, slotsLoaded, boundSlot, sessionOpBusyRef, injectedVersionRef, injectContextMut,
    createBoundSession,
  } = companionRead
  /** Sparkle flow: resume the active bound session if one exists, else create a
   *  new one. With `address`, stage (never auto-send) the address-comments
   *  message via the writePrefill sessionStorage channel — the embedded ChatPage
   *  consumes it when the slot activates. (The panels are mutually exclusive, so
   *  a "Draft a prompt in chat" click always mounts the chat panel fresh — the
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
    // Resume freshness nudge: if the artifact moved past the AGENT's last action,
    // inject a fresh short-lived context entry so the agent doesn't act on
    // stale-version assumptions. `last_agent_ts`, not `last_activity_ts`: the
    // latter moves on a user prompt, and a prompt sent after the edit does not
    // tell the agent the artifact changed. An older gateway that does not send
    // `last_agent_ts` falls back to `last_activity_ts`. Best-effort — ISO
    // timestamps compare lexicographically; a miss just means the agent re-reads
    // via artifact_get.
    const injected = injectedVersionRef.current.get(boundSlotResolved.key)
    const agentActedTs = boundSlotResolved.last_agent_ts ?? boundSlotResolved.last_activity_ts
    const artifactIsStale = Boolean(
      agentActedTs && artifact.updated_at &&
      artifact.updated_at > agentActedTs
    )
    // A reload empties the memory-only claim, so NO CLAIM cannot be told from NEVER
    // INJECTED, and re-sending on that ambiguity delivers one entry twice.
    if (injected !== artifact.version && artifactIsStale) {
      const nudgeVersion = artifact.version
      injectedVersionRef.current.set(boundSlotResolved.key, nudgeVersion)
      injectContextMut.mutate({ slotKey: boundSlotResolved.key, version: nudgeVersion, slug, refresh: true })
    }
  }, [artifact, panel, commentCount, boundSlot, slotsLoaded, slug, dispatch,
      createBoundSession, injectContextMut, injectedVersionRef, sessionOpBusyRef])
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
  }, [slug, popout, navigate, dirty, setSaveError])

  // Drop a pending in-iframe selection when the user switches to edit mode or
  // pages between versions — those interactions kill the underlying selection
  // anyway (the DOM toolbar unmounts with `commentable` and closes itself).
  useEffect(() => { if (editing || !isCurrent) clearSelectionState() }, [editing, isCurrent, clearSelectionState])
  const threadsRead = useArtifactDetailPageThreads({ durableComments, stateRead, documentRead, composerRead })
  const menuRead = useArtifactDetailPageMenu({ stateRead, documentRead, composerRead, companionRead })
  if (detailQuery.isLoading || (!isCurrent && versionQuery.isLoading))
    return <div className="p-6 text-muted">{i18nT('pages.artifactDetailPage.loading')}</div>
  // A failed historical-snapshot fetch used to fall through to "Not found"
  // (artifact is undefined either way); surface it as the load failure it is.
  const loadError = detailQuery.error ?? (!isCurrent ? versionQuery.error : null)
  if (loadError) {
    return (
      <>
        <div className="sticky top-0 z-10 bg-bg border-b border-border">
          <PageHeader title={i18nT('pages.artifactDetailPage.artifact')} subtitle={slug} />
        </div>
        <div className="px-4 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0">
          <Card>
            {/* Load failure: no editor buffer exists yet (a version switch
                clears it), so the hand-off risks nothing. */}
            <ErrorNotice
              title={i18nT('pages.artifactDetailPage.failed_to_load_artifact')}
              message={(errMessage(loadError) || i18nT('components.errorBoundary.something_went_wrong'))}
              askAgent
            />
            <div className="mt-3 flex flex-wrap gap-2">
              {/* In a popout this forwards to the main window (the popout must
                  never become the library page); in the main app it's a plain
                  local navigation. */}
              <Btn onClick={() => sendNav({ path: '/artifacts' })}>{i18nT('pages.artifactDetailPage.back_to_library')}</Btn>
              {!detailQuery.error && (
                <Btn onClick={() => setSelectedVersion(null)}>{i18nT('pages.artifactDetailPage.back_to_live')}</Btn>
              )}
            </div>
          </Card>
        </div>
      </>
    )
  }
  if (!artifact) return <div className="p-6 text-muted">{i18nT('pages.artifactDetailPage.not_found')}</div>

  return (
    <>
      <ArtifactDetailPageHeader
        artifact={artifact} displayCommentCount={displayCommentCount} panel={panel}
        toggleSidebar={toggleSidebar} handleRevert={handleRevert} openCompanionChat={openCompanionChat}
        popout={popout} stateRead={stateRead} documentRead={documentRead} editorRead={editorRead}
        composerRead={composerRead} menuRead={menuRead}
      />

      <ArtifactDetailPageBody
        artifact={artifact} commentsQuery={commentsQuery} durableComments={durableComments}
        displayCommentCount={displayCommentCount} commentCount={commentCount}
        remoteSyncError={remoteSyncError} isMobile={isMobile} panel={panel} setPanel={setPanel}
        narrowPanelOpen={narrowPanelOpen} sidebarUserToggledRef={sidebarUserToggledRef}
        toggleSidebar={toggleSidebar} openCompanionChat={openCompanionChat}
        UpstreamSyncBanner={UpstreamSyncBanner} stateRead={stateRead} documentRead={documentRead}
        editorRead={editorRead} commentsRead={commentsRead} composerRead={composerRead}
        companionRead={companionRead} threadsRead={threadsRead} menuRead={menuRead}
      />
      {confirmDialog}
    </>
  )
}


// Static theme-token class sets (Tailwind JIT needs literal strings, not
// interpolated tone names).
const _SYNC_TONES: Record<string, { wrap: string; btn: string }> = {
  info: { wrap: 'border-info/40 bg-info-subtle text-info-text', btn: 'border-info/40 text-info-text hover:bg-info/10' },
  warn: { wrap: 'border-warn/40 bg-warn-subtle text-warn-text', btn: 'border-warn/40 text-warn-text hover:bg-warn/10' },
  danger: { wrap: 'border-danger/40 bg-danger-subtle text-danger-text', btn: 'border-danger/40 text-danger-text hover:bg-danger/10' },
}

// `busy` while the page's own save is on the wire: a flush started then would
// re-send that save's base token and be refused as stale.
function UpstreamSyncBanner({ artifact, busy = false, onPulled, onBeforeMutate }: { artifact: Artifact; busy?: boolean; onPulled: () => void; onBeforeMutate?: () => Promise<void> }) {
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
  const { data: providersData, error: providersError } = useQuery({
    queryKey: ['publish-providers', artifact.kind],
    queryFn: () => api.getArtifactPublishProviders(artifact.kind),
    staleTime: 300_000,
  })
  const providers = providersData?.providers || []
  // Cheap, non-blocking upstream check — the local content renders immediately;
  // this only drives the "pull available" / "conflict" affordance.
  const { data: status, error: statusError } = useQuery({
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
      // A refused flush is already shown by the page's conflict notice.
      if (conflictToken(e) === undefined)
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
      // A refused flush is already shown by the page's conflict notice.
      if (conflictToken(e) === undefined)
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
      if (conflictToken(e) === undefined)
        setError(e instanceof Error ? e.message : i18nT('pages.artifactDetailPage.snapshot_failed'))
    } finally {
      setSnapshotting(false)
    }
  }

  // A failed provider-registry or upstream-status probe used to hide the whole
  // banner, so a sync problem looked like "nothing to sync". Surface it instead.
  const probeError = providersError ?? statusError
  if (probeError) {
    return (
      <div className="mb-3 flex items-center gap-2 text-[13px]">
        {/* No hand-off: editor buffer editedContent may be dirty */}
        <ErrorNotice
          variant="inline"
          title={i18nT('pages.artifactDetailPage.sync_status_unavailable')}
          message={(errMessage(probeError) || i18nT('components.errorBoundary.something_went_wrong'))}
        />
      </div>
    )
  }

  // No registered provider → no sync surface (public edition renders nothing).
  if (providers.length === 0) return null

  // Provider label from the registry's self-described display_name — never a
  // hardcoded vendor string.
  const provLabel = providers.find((p) => p.name === pub?.provider)?.display_name
    || pub?.provider || 'the remote'
  // The publication's own link, scheme-checked before it becomes clickable so a
  // provider cannot smuggle a javascript:/file: URL into it. Worded like the
  // Publish panel's link to a public-web copy, since both open the published page.
  const publishedUrl = safeHttpUrl(pub?.view_url || '')
  const publishedLink = publishedUrl && (
    <a href={publishedUrl} target="_blank" rel="noopener noreferrer" className="underline hover:no-underline">
      {i18nT('pages.artifactDetailPage.open_published_page')}
    </a>
  )

  // Local edits not yet published, with no remote drift: a visible prompt to
  // publish them. (When the remote is ALSO ahead, the pull/overwrite banner
  // below handles it and already reassures that local edits are checkpointed.)
  if (!fm && !upstreamAhead && pub && hasLocalEdits) {
    return (
      <div className="mb-3 flex items-center gap-2 px-3 py-2 rounded-md border text-[13px] border-warn/40 bg-warn-subtle text-warn-text">
        <Camera size={14} className="lucide-inline shrink-0" />
        <span className="flex-1">
          {i18nT('pages.artifactDetailPage.local_changes_not_yet_published_to')} {provLabel}.
          {publishedLink && <>{' · '}{publishedLink}</>}
        </span>
        {/* No hand-off: editor buffer editedContent may be dirty */}
        <ErrorNotice variant="inline" message={error} />
        <Btn
          type="button"
          onClick={handleSnapshot}
          disabled={snapshotting || busy}
          className="gap-1 px-2 py-0.5 text-[12px] font-medium border-warn/50 hover:bg-warn/10"
          title={i18nT('pages.artifactDetailPage.snapshot_the_current_content_as_a_new_version_an')}
        >
          <Camera size={12} />
          {snapshotting ? i18nT('pages.artifactDetailPage.publishing') : i18nT('pages.artifactDetailPage.snapshot_to_publish')}
        </Btn>
      </div>
    )
  }

  // Not a fork and the remote copy isn't ahead: nothing to sync, so a published
  // artifact shows only where its published copy lives.
  if (!fm && !upstreamAhead) {
    if (!pub || !publishedLink) return null
    const info = _SYNC_TONES.info
    return (
      <div className={`mb-3 flex items-center gap-2 px-3 py-2 rounded-md border text-[13px] ${info.wrap}`}>
        <Globe size={14} className="lucide-inline shrink-0" />
        <span className="flex-1">
          {i18nT('pages.artifactDetailPage.published_to_provider', { provider: provLabel })}{' · '}{publishedLink}
        </span>
      </div>
    )
  }

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
          disabled={pulling || overwriting || busy}
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
          disabled={overwriting || pulling || busy}
          className={`gap-1 px-2 py-0.5 text-[12px] font-medium ${tone.btn}`}
          title={i18nT('pages.artifactDetailPage.push_your_local_version_up_as_the_remote_s_new_v')}
        >
          <ArrowUp size={12} className={overwriting ? 'animate-pulse' : undefined} />
          {i18nT('pages.artifactDetailPage.overwrite_remote')}
        </Btn>
      )}
      {/* No hand-off: editor buffer editedContent may be dirty */}
      <ErrorNotice variant="inline" message={error} />
      {notice && !error && <span className="text-muted text-[11px]">{notice}</span>}
    </div>
  )
}
