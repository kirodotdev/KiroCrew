import { useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { store } from '../store'
import { api } from '../api/client'
import { fileReadUrl } from '../utils/fileReadUrl'
import { i18nT } from '../i18n/t'
import { pathBasename } from '../utils/pathBasename'

/**
 * The open file's place in the two libraries, for `MarkdownPanel`: whether it
 * is a knowledge-library source (and the mutations that add or remove it) and
 * whether it is an artifact (and the mutations that promote, snapshot or pin
 * it). The panel header, the full-screen toolbar and the ⋯ menu all read
 * these hooks, which share React Query's cache. Failures go to the caller's
 * `ReportError`, which renders them through the panel's ErrorNotice.
 */

/** Report a failure to the panel, which renders it through the shared
 *  ErrorNotice (replacing the blocking `alert()` these paths used to raise). */
export type ReportError = (message: string) => void

/**
 * File-level knowledge-library state: query for the config + already-added
 * status, mutation to register the file as a source. Always-on so the
 * inline row-2 buttons and the overflow ⋮ entry share a single fetch via
 * React Query's cache.
 */
export function useFileKnowledgeState(filePath: string, onError: ReportError) {
  const queryClient = useQueryClient()
  const { data, error: queryError } = useQuery({
    queryKey: ['knowledge-config', filePath],
    queryFn: async () => {
      const r = await fetch('/api/knowledge/config')
      // A failed read used to resolve to `null`, which rendered as "not added"
      // — a failure dressed as a state. Reject instead so the panel can say so.
      if (!r.ok) throw new Error(i18nT('components.markdownPanel.knowledge_status_failed'))
      const cfg = await r.json()
      const sr = await fetch(`/api/knowledge/sources?uri=${encodeURIComponent(filePath)}`)
      // A failed sources read must not resolve to `[]`: that would read as "not
      // added" and hide the Remove affordance (and offer Add) for a file that
      // may already be in the library. Reject like the config read above does,
      // so the panel's query-error notice renders instead of a wrong state.
      if (!sr.ok) throw new Error(i18nT('components.markdownPanel.knowledge_status_failed'))
      const sources: { id?: string; source_type?: string }[] = await sr.json()
      // sourceId + sourceType back the Remove affordance: only a per-file
      // local_file source (one the panel itself can create) is removable here —
      // folder/wider sources belong to the Knowledge page's own delete flows.
      // A file can match more than one source (e.g. a folder source AND a
      // local_file source), so look for the removable local_file among all
      // matches rather than trusting whichever the backend returned first.
      const localFile = sources.find((s) => s.source_type === 'local_file')
      return {
        ...cfg,
        alreadyAdded: sources.length > 0,
        sourceId: localFile?.id ?? null,
        sourceType: localFile?.source_type ?? null,
      }
    },
  })
  const formats: string[] | null = data?.enabled ? data.supported_formats : null
  const alreadyAdded = data?.alreadyAdded ?? false
  const sourceId = data?.sourceId ?? null
  const sourceType = data?.sourceType ?? null
  // After a remove, the add mutation's `isSuccess` ("added") can still be true
  // from an earlier add this session, which would paint the Add button that
  // comes back green ("in library") for a file that was just removed. Hold the
  // add mutation's reset in a ref so the remove's onSuccess (declared first)
  // can clear that stale success.
  const resetAddRef = useRef<() => void>(() => {})
  const { mutate: remove, isPending: removing } = useMutation({
    mutationFn: async () => {
      // Encode the id as a single path segment: a source id originates from an
      // imported bundle and is otherwise only validated as a non-empty string,
      // so an id like `../../../sessions` would otherwise resolve to a
      // different route and land an irreversible DELETE on it.
      const res = await fetch(`/api/knowledge/sources/${encodeURIComponent(sourceId)}`, { method: 'DELETE' })
      // 404 just means the source is already gone — removing an absent thing
      // is success, so the panel falls back to the Add affordance instead of
      // reporting a failure dressed as an error.
      if (res.status === 404) return 'gone' as const
      if (!res.ok) {
        // The backend's failure bodies ("not found" / "internal server error")
        // are raw English diagnostics; the panel reports the localized string
        // instead so the error notice reads consistently in every locale.
        await res.json().catch(() => null)
        throw new Error(i18nT('components.markdownPanel.failed_to_remove_source'))
      }
      return 'removed' as const
    },
    onSuccess: () => {
      // Clear any stale add-success so the returning Add button is not drawn
      // green for a file we just removed.
      resetAddRef.current()
      queryClient.invalidateQueries({ queryKey: ['knowledge-config', filePath] })
    },
    onError: (err) => onError((err as Error).message),
  })
  const { mutate: add, isPending: adding, isSuccess: added, data: addResult, reset } = useMutation({
    mutationFn: async () => {
      const name = pathBasename(filePath) || filePath
      const res = await fetch('/api/knowledge/sources', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, source_type: 'local_file', uri: filePath }),
      })
      if (res.status === 409) return 'exists' as const
      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: 'failed' }))
        throw new Error(err.error || i18nT('components.markdownPanel.failed_to_add_source'))
      }
      return 'created' as const
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['knowledge-config', filePath] })
    },
    onError: (err) => onError((err as Error).message),
  })
  // Keep the ref pointed at the live add-reset so the remove's onSuccess
  // (declared above the add mutation) can clear a stale add-success.
  resetAddRef.current = reset
  return { formats, alreadyAdded, sourceId, sourceType, add, adding, added, addResult, reset, remove, removing, queryError }
}

/**
 * File-level artifact state: existing artifact for this source_path,
 * adding/snapshotting mutations. `live_dirty` flows through so
 * the inline Snapshot button can gate visibility/enable correctly.
 */
export function useFileArtifactState(filePath: string, content: string, onError: ReportError) {
  const queryClient = useQueryClient()
  const { data, error: queryError } = useQuery({
    queryKey: ['artifact-by-source-path', filePath],
    queryFn: async () => {
      const res = await api.artifacts({ source_path: filePath })
      const list = (res?.artifacts ?? []) as { slug: string; name: string }[]
      if (list.length === 0) return null
      try {
        const full = await api.artifact(list[0].slug)
        return { slug: list[0].slug, name: list[0].name, live_dirty: !!full.live_dirty, pinned: !!full.pinned }
      } catch {
        return { slug: list[0].slug, name: list[0].name, live_dirty: false, pinned: false }
      }
    },
  })
  const existing = data ?? null
  const { mutate: add, isPending: adding, isSuccess: added, reset: resetAdd } = useMutation({
    mutationFn: async () => {
      const name = pathBasename(filePath) || filePath
      const ext = '.' + (filePath.split('.').pop() || '').toLowerCase()
      const kind: 'markdown' | 'json' | 'svg' | 'html' | 'text' =
        ext === '.md' || ext === '.markdown' || ext === '.mdx' ? 'markdown'
        : ext === '.json' || ext === '.jsonl' ? 'json'
        : ext === '.svg' ? 'svg'
        : ext === '.html' || ext === '.htm' ? 'html'
        : 'text'
      // Re-read the file rather than promoting the in-memory `content`.
      // /api/file-read truncates very large files and flags it with
      // X-Truncated; the panel's copy carries no such marker, so promoting it
      // would persist a 512 KB prefix AS THOUGH it were the whole document --
      // and a disposable file is COPIED, so nothing would reference the
      // original and the loss would be silent and permanent. Reading here also
      // means the artifact captures the file as it is at promote time.
      const res = await fetch(fileReadUrl(filePath))
      if (!res.ok) throw new Error(i18nT('components.markdownPanel.cannot_read_file'))
      if (res.headers.get('X-Truncated') === 'true') {
        throw new Error(i18nT('components.markdownPanel.file_too_large_to_add'))
      }
      // Same class as the truncation refusal above: the response is not the
      // document. For a file the read could not decode the body is an envelope
      // (`{"binary": true, ...}`), so promoting `res.text()` would store that
      // JSON AS the artifact's content -- and an artifact is COPIED, so nothing
      // would reference the original and the wrong content would be permanent.
      // The affordance is hidden for such a file; this is the chokepoint, which
      // is what covers the fullscreen toolbar's copy of the button and any
      // later caller.
      if (res.headers.get('X-File-Binary') === 'true') {
        throw new Error(i18nT('components.markdownPanel.binary_file_cannot_be_added'))
      }
      const fresh = await res.text()
      // Same slot is passed as the X-Session-Key so the server's
      // restricted-session gate sees the REAL session. With the transport's
      // shared `dashboard:ui` placeholder an incognito session could persist a
      // promoted file its own restriction was meant to refuse.
      const promoteSlot = store.getState().chat.activeSlot
      const created = await api.createArtifact({
        name,
        content: fresh,
        kind,
        source_path: filePath,
        description: i18nT('components.markdownPanel.tracking_description', { path: filePath }),
        origin_session_key: promoteSlot || undefined,
      }, promoteSlot ? `dashboard:${promoteSlot}` : undefined)
      return created as { slug: string; version: number }
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['artifact-by-source-path', filePath] })
      queryClient.invalidateQueries({ queryKey: ['artifacts'] })
    },
    onError: (err) => onError((err as Error).message),
  })
  const { mutate: snapshot, isPending: snapshotting, isSuccess: snapshotted } = useMutation({
    mutationFn: async () => {
      if (!existing) throw new Error('no existing artifact')
      await api.updateArtifact(existing.slug, { snapshot: true })
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['artifact-by-source-path', filePath] })
      queryClient.invalidateQueries({ queryKey: ['artifact', existing?.slug] })
      queryClient.invalidateQueries({ queryKey: ['artifact-versions', existing?.slug] })
      queryClient.invalidateQueries({ queryKey: ['artifact-events', existing?.slug] })
    },
    onError: (err) => onError((err as Error).message),
  })
  const { mutate: toggleSave, isPending: toggling } = useMutation({
    mutationFn: async () => {
      // Read the owning slot ONCE, up front: both branches below pass it as the
      // X-Session-Key so a restricted slot is gated on the pin as well as the save.
      const saveSlot = store.getState().chat.activeSlot
      // Saved == pinned (consistent with the Artifacts tab/page + chat bookmark).
      if (existing) {
        await api.setArtifactPinned(
          existing.slug,
          !existing.pinned,
          saveSlot ? `dashboard:${saveSlot}` : undefined,
        )
        return
      }
      // Not yet an artifact — create (file-backed), then pin. createArtifact
      // dedups on source_path server-side, so this stays idempotent.
      const name = pathBasename(filePath) || filePath
      const ext = '.' + (filePath.split('.').pop() || '').toLowerCase()
      const kind: 'markdown' | 'json' | 'svg' | 'html' | 'text' =
        ext === '.md' || ext === '.markdown' || ext === '.mdx' ? 'markdown'
        : ext === '.json' || ext === '.jsonl' ? 'json'
        : ext === '.svg' ? 'svg'
        : ext === '.html' || ext === '.htm' ? 'html'
        : 'text'
      const created = await api.createArtifact({
        name, content, kind, source_path: filePath, description: i18nT('components.markdownPanel.tracking_description', { path: filePath }), origin_session_key: saveSlot || undefined,
      }, saveSlot ? `dashboard:${saveSlot}` : undefined) as { slug: string }
      await api.setArtifactPinned(created.slug, true, saveSlot ? `dashboard:${saveSlot}` : undefined)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['artifact-by-source-path', filePath] })
      queryClient.invalidateQueries({ queryKey: ['artifacts'] })
    },
    onError: (err) => onError((err as Error).message),
  })
  const saved = !!existing?.pinned
  return { existing, add, adding, added, resetAdd, snapshot, snapshotting, snapshotted, toggleSave, toggling, saved, queryError }
}
