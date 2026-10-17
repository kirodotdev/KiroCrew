/** The Artifacts page's two create doors: "Add Artifact" (import a file from
 *  the user's machine) and "New Artifact" (a blank document opened in the
 *  editor), each filed into the folder being browsed. */
import { useState, useRef, useCallback } from 'react'
import { useMutation } from '@tanstack/react-query'
import { api } from '../api/client'
import { markJustCreatedBlank } from '../lib/blankHandoff'
import { planFileImport, wasContentRedacted, type ImportPlan } from '../lib/artifactImport'
import type { Artifact } from '../types'
import { i18nT } from '../i18n/t'
import type { QueryClient } from '@tanstack/react-query'
import type { NavigateFunction } from 'react-router-dom'
import { importRejectionText } from './artifactsPageModel'

/** The page's "Add Artifact" and "New Artifact" state and handlers, called
 *  where they sat in the page so their hooks register in the page's order. */
export function useArtifactsPageCreate({ qc, navigate, invalidateFolders, view, filtersActive, scopeFolderId }: {
  qc: QueryClient
  navigate: NavigateFunction
  invalidateFolders: () => void
  view: 'grid' | 'table'
  filtersActive: boolean
  scopeFolderId: string
}) {
  // ── Add Artifact — import a file from the user's machine ──────
  // The file's text is COPIED into artifact storage, so the artifact does not
  // stay bound to the file on disk (see lib/artifactImport.ts for why).
  const addFileInputRef = useRef<HTMLInputElement>(null)
  // `addError` holds FAILURES (a rejected request, a refused import); the
  // pure client-side pick checks (unsupported type, too large, empty, not
  // text) are validation hints and live in `pickHint`, rendered as plain text.
  const [addError, setAddError] = useState<string | null>(null)
  const [pickHint, setPickHint] = useState<string | null>(null)
  const addArtifactMut = useMutation({
    mutationFn: async (vars: ImportPlan & { folder: string }) => {
      // Create unfiled, then file by id. `POST /api/artifacts` resolves its
      // `folder` field with mkdir -p semantics, so a folder id that went
      // stale between the pick and the save (folder deleted in another tab,
      // or a bookmarked ?folder=<id> URL) is not recognised as an id and gets
      // treated as a NAME — minting a junk folder called e.g. "a1b2c3d4e5f6".
      // The dedicated folder endpoint resolves ids only and errors on a stale
      // one, so the worst case is an artifact left at the library root.
      // This mirrors New Folder, which passes `parent_id` for the same reason.
      const art = (await api.createArtifact({
        name: vars.name,
        content: vars.content,
        kind: vars.kind,
      })) as Artifact
      // The store redacts credential-like text on every READ but stores the
      // POST body verbatim, so a file carrying real credential material would
      // read back as placeholders — and the next edit would save those
      // placeholders over the imported text. Refuse the import rather than
      // leave an artifact that silently corrupts itself (and rather than keep
      // the secret in the library at all).
      if (wasContentRedacted(vars.content, art.content)) {
        try {
          await api.deleteArtifact(art.slug)
        } catch {
          // Best effort: the refusal message is what matters, and the artifact
          // is reachable in the library if this cleanup did not land.
        }
        return { art, filed: false, redacted: true }
      }
      let filed = true
      if (vars.folder) {
        try {
          await api.setArtifactFolder(art.slug, vars.folder)
        } catch {
          // The artifact exists and is reachable — only its placement failed.
          // Surfaced below rather than failing the whole add.
          filed = false
        }
      }
      return { art, filed, redacted: false }
    },
    onSuccess: ({ art, filed, redacted }) => {
      qc.invalidateQueries({ queryKey: ['artifacts'] })
      invalidateFolders()
      if (redacted) {
        setAddError(i18nT('pages.artifactsPage.add_artifact_error_redacted'))
        return
      }
      if (!filed) {
        // Stay put so the note is actually read; the artifact is at the root.
        setAddError(i18nT('pages.artifactsPage.add_artifact_error_unfiled'))
        return
      }
      // Open the new artifact: it confirms the file rendered, and it is the
      // only reliable feedback — a fresh artifact is unpinned, so it would be
      // invisible to a user whose library is filtered to Starred.
      navigate(`/artifacts/${art.slug}`)
    },
  })
  const handleAddArtifact = useCallback(() => {
    setAddError(null)
    setPickHint(null)
    addFileInputRef.current?.click()
  }, [])
  const handleAddArtifactFile = useCallback(async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    // Clear the input first so picking the SAME file again re-fires `change`
    // (the value is unchanged otherwise, and the event never fires).
    e.target.value = ''
    if (!file) return
    setAddError(null)
    setPickHint(null)
    const result = await planFileImport(file)
    if (!result.ok) {
      // 'unreadable' is a caught read failure; the rest are pick validation.
      if (result.reason === 'unreadable') setAddError(importRejectionText(result.reason))
      else setPickHint(importRejectionText(result.reason))
      return
    }
    // File it into the folder being browsed, matching New Folder's placement.
    const parent = view === 'grid' && !filtersActive ? scopeFolderId : ''
    addArtifactMut.mutate({ ...result.plan, folder: parent })
  }, [addArtifactMut, view, filtersActive, scopeFolderId])

  // ── New Artifact — start a blank document in the library ───────
  // Create-first, name-later: the artifact is created empty and the detail
  // page opens with its editor already focused, so the user starts typing
  // immediately instead of answering a name prompt before they know what
  // they are writing. The kind is left unspecified so the store defaults it
  // to markdown and marks it auto-assigned — the first save that looks like
  // JSON or SVG re-types it (see detect_editor_kind in artifact_store/rules.py).
  //
  // The cost of create-first is litter (abandoned empty documents); the
  // detail page pays for it by discarding an untouched blank on leave, which
  // is why `justCreatedBlank` is handed over in navigation state.
  const newArtifactMut = useMutation({
    mutationFn: async (vars: { folder: string }) => {
      const art = (await api.createArtifact({
        name: i18nT('pages.artifactsPage.untitled_artifact_name'),
        content: '',
      })) as Artifact
      let filed = true
      if (vars.folder) {
        try {
          // Same reasoning as the import path: file by id through the
          // dedicated endpoint, which errors on a stale folder id rather
          // than minting a junk folder from it.
          await api.setArtifactFolder(art.slug, vars.folder)
        } catch {
          filed = false
        }
      }
      return { art, filed }
    },
    onSuccess: ({ art, filed }) => {
      qc.invalidateQueries({ queryKey: ['artifacts'] })
      invalidateFolders()
      if (!filed) setAddError(i18nT('pages.artifactsPage.add_artifact_error_unfiled'))
      // One-shot, module-scoped: a reload must NOT re-arm the detail page's
      // cleanup on a document the user has come back to.
      markJustCreatedBlank(art.slug, art.name)
      navigate(`/artifacts/${art.slug}`)
    },
  })
  const handleNewArtifact = useCallback(() => {
    setAddError(null)
    if (newArtifactMut.isPending) return
    const parent = view === 'grid' && !filtersActive ? scopeFolderId : ''
    newArtifactMut.mutate({ folder: parent })
  }, [newArtifactMut, view, filtersActive, scopeFolderId])
  return {
    addFileInputRef,
    addError,
    setAddError,
    pickHint,
    setPickHint,
    addArtifactMut,
    handleAddArtifact,
    handleAddArtifactFile,
    newArtifactMut,
    handleNewArtifact,
  }
}
