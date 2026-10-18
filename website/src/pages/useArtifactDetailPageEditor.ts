/**
 * The record writes the header and the editor make: tags, the publication
 * re-check, the star, the document type, the title rename and the editor's
 * start, save, flush and snapshot paths; and the editor's lifecycle around them
 * (the fresh-blank hand-off and its cleanup, the save chord, the edit flag the
 * WebSocket transport reads, the leave guards a dirty buffer raises). The revert
 * and the upstream sync banner's writes stay in the facade, and the folder move
 * is `FolderChip`'s. `conflictToken` reads a stale-write 409 for these paths and
 * for the banner.
 *
 * The `ArtifactDetailPage` facade calls each hook here at the point its code
 * sits in the page's hook order, which `ArtifactDetailPage.owners.test.tsx`
 * pins: React runs a component's effects in registration order, so moving one
 * is a behaviour change even when every test of the moved code still passes.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { setArtifactEditing } from '../utils/artifactEditGuard'
import { hasPendingArtifactWrite } from '../lib/artifactWrites'
import type { Artifact } from '../types'

import { i18nT } from '../i18n/t'
import { usePublishNavigationStake, useRegisterNavigationLeaveGuard } from '../components/NavigationLeaveGuard'
import type {
  ArtifactDetailPageStateRead, ArtifactDetailPageDocumentRead,
} from './useArtifactDetailPageDocument'

/** The live `current_token` from a stale-write 409 (see ArtifactConflictError), or
 * `undefined` when `err` is anything else. */
export function conflictToken(err: unknown): string | undefined {
  if (!(err instanceof ApiError) || err.status !== 409) return undefined
  try {
    const token = (JSON.parse(err.body) as { current_token?: unknown }).current_token
    return typeof token === 'string' ? token : undefined
  } catch {
    return undefined
  }
}

/** The header's and the editor's record writes, and the editor lifecycle. */
export function useArtifactDetailPageEditor({
  stateRead, documentRead,
}: {
  stateRead: ArtifactDetailPageStateRead
  documentRead: ArtifactDetailPageDocumentRead
}) {
  const {
    slug, confirm, confirmOpen, blankClaim, justCreatedBlank, setRenaming, nameDraft, setNameDraft,
    queryClient, editing, setEditing, setPreviewDuringEdit, editedContent, setEditedContent, setSaving,
    setSaveError, editGenerationRef, editBaseTokenRef, overwriteTokenRef, tokenWriteInFlightRef,
    setSaveConflict, setAddingTag, setNewTag,
  } = stateRead
  const { isCurrent, artifact, editable, dirty } = documentRead
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
  }, [artifact, queryClient, slug, setSaveError])

  const [reprobing, setReprobing] = useState(false)
  const [reprobeError, setReprobeError] = useState<string | null>(null)
  /** Re-check the destination behind a publish notice, and clear it if it no longer holds. */
  const reprobeNotice = useCallback(async () => {
    if (!artifact) return
    setReprobeError(null)
    setReprobing(true)
    try {
      await api.reprobeArtifactNotice(artifact.slug)
      // The record may now carry no notice at all, so the banner's own condition has
      // to be re-evaluated from fresh data -- and the library indicators read the same
      // fields, so they are invalidated too.
      await queryClient.invalidateQueries({ queryKey: ['artifact', slug] })
      await queryClient.invalidateQueries({ queryKey: ['artifacts'] })
    } catch (err) {
      // A reprobe failure is NOT a save failure. Routing it to `setSaveError` put it in
      // the editor's save channel, whose surrounding copy tells the user their edit did
      // not persist -- untrue, and it sends them to re-save content nothing touched.
      setReprobeError(err instanceof Error ? err.message : String(err))
    } finally {
      setReprobing(false)
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
  }, [artifact, updateTagsMut, setAddingTag, setNewTag])

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
  }, [artifact, pinning, queryClient, slug, setSaveError])

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
  }, [artifact, queryClient, slug, setSaveError])

  // ── Inline title rename ────────────────────────────────────────────────────
  const titleButtonRef = useRef<HTMLButtonElement | null>(null)
  const startRenaming = useCallback(() => {
    if (!artifact) return
    setNameDraft(artifact.name)
    setRenaming(true)
  }, [artifact, setNameDraft, setRenaming])

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
  }, [artifact, nameDraft, queryClient, slug, setRenaming, setSaveError])

  // ── Edit / save / cancel / revert handlers ────────────────────────────────
  const startEditing = useCallback(() => {
    if (!artifact || !editable) return
    editGenerationRef.current += 1
    setEditedContent(artifact.content ?? '')
    editBaseTokenRef.current = artifact.content_token
    overwriteTokenRef.current = undefined
    setSaveConflict(false)
    setEditing(true)
    setSaveError(null)
  }, [artifact, editable, editBaseTokenRef, editGenerationRef, overwriteTokenRef, setEditedContent, setEditing, setSaveConflict, setSaveError])

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

  // Every way out of an edit (Cancel, Escape, the version switch) ends it here, so
  // none of them leaves its save notice or preview toggle behind. Advancing the
  // generation also stops a write still on the wire from re-arming the notice.
  const endEditing = useCallback(() => {
    editGenerationRef.current += 1
    setEditing(false)
    setEditedContent('')
    setSaveError(null)
    setSaveConflict(false)
    setPreviewDuringEdit(false)
  }, [editGenerationRef, setEditedContent, setEditing, setPreviewDuringEdit, setSaveConflict, setSaveError])

  const cancelEditing = useCallback(async () => {
    if (dirty && !(await confirm({
      title: i18nT('pages.artifactDetailPage.discard_unsaved_changes'),
      confirmLabel: i18nT('pages.artifactDetailPage.discard_changes_button'),
    }))) return
    endEditing()
  }, [dirty, confirm, endEditing])

  // `overwrite` is passed ONLY by the relabelled Save button's own click after a
  // 409. Every other save path (Cmd+S, Cmd+Shift+S, Snapshot) keeps the base
  // token, so it is refused again instead of silently replacing newer content.
  const handleSave = useCallback(async (snapshot = false, overwrite = false) => {
    if (!artifact || !dirty || tokenWriteInFlightRef.current) return
    const editGeneration = editGenerationRef.current
    const writeSlug = artifact.slug
    tokenWriteInFlightRef.current = true
    // Same race as commitRename: the discard snapshot still reads empty until
    // the query refetches, so disarm synchronously or an unmount landing
    setSaving(true)
    setSaveError(null)
    try {
      // snapshot=true → bumps version (creates a new numbered snapshot).
      // snapshot=false → silently updates the live state without versioning,
      // matching the explicit-snapshot model.
      const saved = await api.updateArtifact(writeSlug, {
        content: editedContent,
        snapshot,
        expected_token: overwrite ? overwriteTokenRef.current : editBaseTokenRef.current,
      })
      await queryClient.invalidateQueries({ queryKey: ['artifact', writeSlug] })
      if (snapshot) {
        await queryClient.invalidateQueries({ queryKey: ['artifact-versions', writeSlug] })
        await queryClient.invalidateQueries({ queryKey: ['artifact-events', writeSlug] })
      }
      if (editGenerationRef.current !== editGeneration) return
      editBaseTokenRef.current = saved?.content_token
      overwriteTokenRef.current = undefined
      setSaveConflict(false)
      if (snapshot) {
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
      const liveToken = conflictToken(err)
      if (liveToken !== undefined) {
        await queryClient.invalidateQueries({ queryKey: ['artifact', writeSlug] })
        if (editGenerationRef.current !== editGeneration) return
        // Nothing was written. Keep the draft, arm the explicit Save as a
        // deliberate overwrite, and refresh what the page shows.
        overwriteTokenRef.current = liveToken
        setSaveConflict(true)
        setSaveError(i18nT('pages.artifactDetailPage.save_conflict_changed_since_read'))
      } else if (editGenerationRef.current === editGeneration) {
        setSaveError(err instanceof Error ? err.message : String(err))
      }
    } finally {
      tokenWriteInFlightRef.current = false
      setSaving(false)
    }
  }, [artifact, dirty, editedContent, queryClient, editBaseTokenRef, editGenerationRef, overwriteTokenRef, setEditedContent, setEditing, setPreviewDuringEdit, setSaveConflict, setSaveError, setSaving, tokenWriteInFlightRef])

  // Stash for the keyboard handler effect — keeps deps minimal.
  const handleSaveRef = useRef(handleSave)
  useEffect(() => { handleSaveRef.current = handleSave }, [handleSave])

  // Flush unsaved editor edits to the server as a silent live save so a
  // subsequent pull/overwrite sees live_dirty and checkpoints them — closes a
  // data-loss path where pulling mid-edit discarded the working buffer.
  const flushLiveEdits = useCallback(async () => {
    if (!editing || !dirty || !artifact) return
    const editGeneration = editGenerationRef.current
    const writeSlug = artifact.slug
    // Holds off a save for as long as this flush is on the wire. The banner
    // cannot start a flush during a save: it is disabled while `saving`.
    tokenWriteInFlightRef.current = true
    try {
      await api.updateArtifact(writeSlug, {
        content: editedContent,
        snapshot: false,
        expected_token: editBaseTokenRef.current,
      })
      await queryClient.invalidateQueries({ queryKey: ['artifact', writeSlug] })
    } catch (err) {
      // A stale flush must not overwrite newer content ahead of a pull: stay in
      // the editor with the conflict shown, and let the caller abort. Retrying
      // the pull stays refused; only the explicit Save may overwrite.
      const liveToken = conflictToken(err)
      if (liveToken !== undefined) {
        await queryClient.invalidateQueries({ queryKey: ['artifact', writeSlug] })
        if (editGenerationRef.current === editGeneration) {
          overwriteTokenRef.current = liveToken
          setSaveConflict(true)
          setSaveError(i18nT('pages.artifactDetailPage.save_conflict_changed_since_read'))
        }
      }
      throw err
    } finally {
      tokenWriteInFlightRef.current = false
    }
    if (editGenerationRef.current !== editGeneration) return
    // Drop out of edit mode after flushing. The buffer is now persisted (and a
    // subsequent pull checkpoints it as a version), so once the post-mutate
    // refetch lands the pulled/overwritten content the viewer must render
    // artifact.content — not the stale pre-pull editedContent. Leaving
    // editing=true would keep showing the old buffer, flip `dirty` back to
    // true, and let the next Save/Cmd+S silently overwrite the pulled content.
    setEditing(false)
    setEditedContent('')
    setPreviewDuringEdit(false)
  }, [editing, dirty, artifact, editedContent, queryClient, editBaseTokenRef, editGenerationRef, overwriteTokenRef, setEditedContent, setEditing, setPreviewDuringEdit, setSaveConflict, setSaveError, tokenWriteInFlightRef])

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
  }, [artifact, queryClient, slug, setSaveError, setSaving])

  // Cmd+S / Ctrl+S to save; Esc to cancel edit.
  useEffect(() => {
    if (!editing) return
    const h = (e: KeyboardEvent) => {
      // While the discard-confirm dialog is open, the keyboard belongs to it.
      // Escape dismisses it via the dialog's own handler (re-running the guard
      // here would re-open the dialog the same keystroke just closed), and the
      // save shortcut must not fire — a mid-dialog Cmd+S would persist the very
      // draft the user is about to confirm discarding.
      if (confirmOpen) return
      // Own the save chord whenever editing, not only when dirty, so it never
      // falls through to AppKit's default (selecting the word under the cursor).
      // Match case-insensitively: with Shift held e.key is 'S', so an exact
      // 's' match makes the Cmd+Shift+S snapshot branch unreachable. Read the
      // Shift state from e.shiftKey (Cmd+Shift+S → snapshot, Cmd+S → silent
      // save) and only issue the write when dirty so a clean buffer does not
      // trigger a redundant save.
      //
      // Do NOT gate on !e.defaultPrevented here. This editor mounts no onSave
      // into Pierre, yet Pierre's capture handler still preventDefaults the
      // chord and then no-ops (onSaveRef is undefined) — so an already-prevented
      // event carries no save. Standing down on it would drop both the save and
      // the snapshot. This document handler is the only one that actually saves.
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') {
        e.preventDefault()
        if (dirty) handleSaveRef.current(e.shiftKey)
      }
      // An Escape that closes the More menu (or its Send submenu) belongs to
      // the menu: Radix does not stop the event, so without this check it would
      // also leave edit mode, or open the discard dialog over a dirty buffer.
      if (e.key === 'Escape') {
        if (e.target instanceof Element && e.target.closest('[role="menu"]')) return
        cancelEditing()
      }
    }
    document.addEventListener('keydown', h)
    return () => document.removeEventListener('keydown', h)
  }, [editing, dirty, cancelEditing, confirmOpen])

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

  // In-app exits the page does not own (the global sidebar, the command
  // palette, a popout's forwarded navigation, browser Back) swap the route
  // without unloading the document, so `beforeunload` never sees them. They
  // ask the shell's leave registry, which needs a synchronous answer, so a
  // dirty buffer refuses the exit and opens the app's own discard dialog; after
  // a discard the user repeats the navigation. The refused navigation cannot be
  // resumed from here: the registry does not pass its target.
  useRegisterNavigationLeaveGuard(() => {
    if (!dirty) return true
    // A second refusal while the dialog is up replaces it: `useConfirm`
    // answers the pending ask "no" and shows one dialog.
    void confirm({
      title: i18nT('pages.artifactDetailPage.discard_unsaved_changes'),
      confirmLabel: i18nT('pages.artifactDetailPage.discard_changes_button'),
    }).then(ok => {
      // endEditing, as Cancel does: it advances the edit generation, so a save
      // still on the wire cannot re-arm a failure or conflict notice for the
      // buffer the user just discarded.
      if (ok) endEditing()
    })
    return false
  })
  usePublishNavigationStake(dirty)

  return {
    reprobing, reprobeError, reprobeNotice, addTag, removeTag, pinning, togglePin, changingKind, changeKind,
    titleButtonRef, startRenaming, commitRename, startEditing, endEditing, cancelEditing, handleSave,
    flushLiveEdits, handleSnapshotLive,
  }
}

export type ArtifactDetailPageEditorRead = ReturnType<typeof useArtifactDetailPageEditor>
