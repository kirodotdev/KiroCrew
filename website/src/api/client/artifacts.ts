/**
 * Artifacts: the library list/detail/versions/events, create/settle/update/
 * delete, folders and pins, session docs and materialize, publishing, sharing
 * and upstream sync, remote-provider clone/fork/detail/comments, sandbox-doc
 * URLs, local comments, and publish-provider deploy/teardown.
 *
 * `browseRemoteArtifacts` is defined in `api/client.ts`: the i18n gate reads
 * its query-string template as copy, and moving the line would count as
 * writing it.
 */

import type { SessionDoc, PublishProviderDescriptor } from '../../types'
import { toApiError } from '../apiError'
import type { ClientTransport } from './transport'

export interface AppPublishProvider {
  id: string
  label: string
  icon: string
  kinds: string[]
  configured: boolean
  setupRoute: string
  endpoint: string
}

export function createArtifactsEndpoints({ get, post, del, patch, j, jfetch: fetch, checkSessionExpired, removeAuthBanner }: ClientTransport) {
  const library = {
    // Artifacts
    /** List artifacts. `session` scopes to the artifacts one chat session
     *  ORIGINATED; `touchedBy` widens that to every artifact the session was
     *  involved with — created, read, edited, iterated on or reverted — which is
     *  what the in-session Artifacts tab lists. `pinned` filters on the star. */
    artifacts: (filters?: { tag?: string; kind?: string; q?: string; source_path?: string; snippet?: boolean; contentMatch?: boolean; session?: string; touchedBy?: string; pinned?: boolean }) => {
      const params = new URLSearchParams()
      if (filters?.tag) params.set('tag', filters.tag)
      if (filters?.kind) params.set('kind', filters.kind)
      if (filters?.q) params.set('q', filters.q)
      if (filters?.source_path) params.set('source_path', filters.source_path)
      if (filters?.snippet) params.set('snippet', '1')
      if (filters?.contentMatch) params.set('content', '1')
      if (filters?.session) params.set('session', filters.session)
      if (filters?.touchedBy) params.set('touched_by', filters.touchedBy)
      if (filters?.pinned !== undefined) params.set('pinned', filters.pinned ? '1' : '0')
      const s = params.toString()
      return get(`/api/artifacts${s ? `?${s}` : ''}`).then(j)
    },
    artifact: (slug: string) => get(`/api/artifacts/${encodeURIComponent(slug)}`).then(j),
    artifactVersion: (slug: string, version: number) =>
      get(`/api/artifacts/${encodeURIComponent(slug)}/versions/${version}`).then(j),
    artifactVersions: (slug: string) =>
      get(`/api/artifacts/${encodeURIComponent(slug)}/versions`).then(j),
    artifactEvents: (slug: string) =>
      get(`/api/artifacts/${encodeURIComponent(slug)}/events`).then(j),
    /** Record a `referenced` breadcrumb so a chat session that merely OPENED an
     *  artifact still counts as having touched it (the "This session" section
     *  reads the same event log via `touched_by`).
     *
     *  Unlike every other method here this cannot use the shared `post` helper:
     *  that helper hardcodes `X-Session-Key: dashboard:ui`, which the events
     *  handler deliberately maps to "no session" — the breadcrumb would be
     *  recorded against nothing and never surface in the panel. The real slot
     *  key is therefore sent scope-qualified (`dashboard:<slot>`), the same form
     *  MCP callers send and the one the store's `_strip_session_scope` normalizes
     *  to the bare slot that `touched_by` compares against.
     *
     *  Rejects on a non-2xx like the other methods (notably 403 for an incognito
     *  slot, which is correct deny-by-default) — callers treat a breadcrumb as
     *  best-effort and swallow the failure rather than failing the user's click. */
    recordArtifactReference: (slug: string, slot: string, metadata?: { message_ts?: string; widget_index?: number }) =>
      fetch(`/api/artifacts/${encodeURIComponent(slug)}/events`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Session-Key': `dashboard:${slot}` },
        body: JSON.stringify({ type: 'referenced', ...(metadata ? { metadata } : {}) }),
      }).then(j),
    createArtifact: (
      body: { name: string; content: string; kind?: string; source?: string; description?: string; tags?: string[]; slug?: string; source_path?: string; origin_session_key?: string; folder?: string },
      // Pass the owning slot (as `dashboard:<slot>`) when the save is made on
      // behalf of a chat session, so the server's restricted-session gate sees the
      // real session instead of the shared placeholder.
      sessionKey?: string,
    ) =>
      // DEFAULTED from origin_session_key rather than left to each caller. Every
      // save that belongs to a chat session already names it in the body for
      // attribution, so deriving the header from that makes the gate apply by
      // construction -- an opt-in argument meant a caller that only set
      // origin_session_key (WidgetFrame's save-as-artifact) still sent the shared
      // `dashboard:ui` placeholder and an incognito session's write was allowed
      // through. An explicit sessionKey still wins for callers that need to differ.
      post(
        '/api/artifacts',
        body,
        sessionKey
          ?? (body.origin_session_key ? `dashboard:${body.origin_session_key}` : undefined),
      ).then(j),
    /** Atomically resolve a just-created blank document being left: keep it, save
     *  the draft still in the editor, or delete the abandoned shell. The store
     *  decides under its own lock -- deciding here would race a concurrent save. */
    settleBlankArtifact: (
      slug: string,
      body: { untitled_name: string; draft: string; allow_delete: boolean },
    ) =>
      post(`/api/artifacts/${encodeURIComponent(slug)}/settle`, body).then(j) as
        Promise<{ outcome: 'kept' | 'saved' | 'deleted' }>,
    updateArtifact: (slug: string, body: { content?: string; name?: string; kind?: string; description?: string; tags?: string[]; actor?: 'user' | 'agent'; event_type?: 'edited' | 'iterated' | 'reverted'; from_version?: number; snapshot?: boolean; expected_token?: string }) =>
      patch(`/api/artifacts/${encodeURIComponent(slug)}`, body).then(j),
    deleteArtifact: (slug: string) => del(`/api/artifacts/${encodeURIComponent(slug)}`).then(j),
    // Artifact library folders
    artifactFolders: () => get('/api/artifact-folders').then(j),
    createArtifactFolder: (body: { name: string; parent_id?: string; color?: string }) =>
      post('/api/artifact-folders', body).then(j),
    updateArtifactFolder: (id: string, body: { name?: string; parent_id?: string; order?: number; icon?: string; color?: string }) =>
      patch(`/api/artifact-folders/${encodeURIComponent(id)}`, body).then(j),
    deleteArtifactFolder: (id: string, deleteContents: boolean) =>
      del(`/api/artifact-folders/${encodeURIComponent(id)}?delete_contents=${deleteContents ? 'true' : 'false'}`).then(j),
    /** Move an artifact into a folder ("" = unfile to root). Metadata-only — no version bump. */
    setArtifactFolder: (slug: string, folderId: string) =>
      patch(`/api/artifacts/${encodeURIComponent(slug)}/folder`, { folder_id: folderId }).then(j),
    /** Pin/unpin (favorite) an artifact. Metadata-only — no version bump. */
    // sessionKey: pass `dashboard:<slot>` when the pin is made on behalf of a chat
    // session, so the server's restricted-session gate sees the real session rather
    // than the transport's shared `dashboard:ui` placeholder (which satisfies the
    // `if sk:` check but names no session, so a restricted slot was never gated).
    setArtifactPinned: (slug: string, pinned: boolean, sessionKey?: string) =>
      patch(`/api/artifacts/${encodeURIComponent(slug)}/pin`, { pinned }, sessionKey).then(j),
    /** Virtual list of non-code documents from chat sessions. Pass `session`
     * (a slot key) to scope to a single session. */
    artifactSessionDocs: (session?: string) =>
      get(`/api/artifacts/session-docs${session ? `?session=${encodeURIComponent(session)}` : ''}`).then(j) as Promise<{ docs: SessionDoc[] }>,
    /** Turn a session document path into a real, saved (pinned) file-backed artifact.
     * `originSessionKey` records the saving session; `slug_collided_with` names the plain slug, present only when the store had to suffix it. */
    materializeArtifact: (path: string, originSessionKey?: string) =>
      post('/api/artifacts/materialize', { path, ...(originSessionKey ? { origin_session_key: originSessionKey } : {}) }).then(j) as Promise<{ slug: string; slug_collided_with?: string }>,
    // Artifact publishing / sharing. Local publish/sharing management
    // only — remote-browse / clone / fork surfaces are not part of this edition.
    publishArtifact: (slug: string, body: { visibility?: 'PRIVATE' | 'SHARED' | 'PUBLIC'; shared_with?: string[]; provider?: string }) =>
      post(`/api/artifacts/${encodeURIComponent(slug)}/publish`, body).then(j),
    /** Publishing providers available for an artifact kind, with per-kind support
     *  + sharing/sync/discovery descriptors. Drives the share-panel
     *  picker (selector shown only when >1 capable provider). */
    getArtifactPublishProviders: (kind: string): Promise<{ providers: PublishProviderDescriptor[]; kind: string }> =>
      get(`/api/artifacts/publish-providers?kind=${encodeURIComponent(kind)}`).then(j),
    /** Provider-routed clone/fork of a remote artifact into the local store.
     *  external_id travels in the body (not the path) — provider-native ids can
     *  contain "/", which a path segment can't carry. */
    cloneRemoteArtifact: (provider: string, externalId: string) =>
      post(`/api/remote-artifacts/${encodeURIComponent(provider)}/clone`, { external_id: externalId }).then(j),
    forkRemoteArtifact: (provider: string, externalId: string) =>
      post(`/api/remote-artifacts/${encodeURIComponent(provider)}/fork`, { external_id: externalId }).then(j),
  }

  const remoteAndComments = {
    // Read-only detail fetch for a provider-hosted artifact (metadata + content),
    // powering the remote-artifact detail page's viewer. external_id can contain
    // "/", so it is percent-encoded into the path segment.
    remoteArtifactDetail: (provider: string, externalId: string) =>
      get(`/api/remote-artifacts/${encodeURIComponent(provider)}/${encodeURIComponent(externalId)}`).then(j),
    // Remote artifact comments (view-without-fork): these write straight through
    // to the provider (scope=shared) and are TTL-cached server-side. external_id
    // + comment_id travel in the path, percent-encoded (provider-native ids may
    // contain "/").
    remoteArtifactComments: (provider: string, externalId: string) =>
      get(`/api/remote-artifacts/${encodeURIComponent(provider)}/${encodeURIComponent(externalId)}/comments`).then(j),
    postRemoteArtifactComment: (provider: string, externalId: string, body: { text: string; anchor?: object }) =>
      post(`/api/remote-artifacts/${encodeURIComponent(provider)}/${encodeURIComponent(externalId)}/comments`, body).then(j),
    replyRemoteArtifactComment: (provider: string, externalId: string, commentId: string, body: { text: string }) =>
      post(`/api/remote-artifacts/${encodeURIComponent(provider)}/${encodeURIComponent(externalId)}/comments/${encodeURIComponent(commentId)}/reply`, body).then(j),
    markReviewRemoteComment: (provider: string, externalId: string, commentId: string) =>
      post(`/api/remote-artifacts/${encodeURIComponent(provider)}/${encodeURIComponent(externalId)}/comments/${encodeURIComponent(commentId)}/review`, {}).then(j),
    deleteRemoteComment: (provider: string, externalId: string, commentId: string) =>
      del(`/api/remote-artifacts/${encodeURIComponent(provider)}/${encodeURIComponent(externalId)}/comments/${encodeURIComponent(commentId)}`).then(j),
    updateArtifactSharing: (slug: string, body: { visibility: 'PRIVATE' | 'SHARED' | 'PUBLIC'; shared_with?: string[] }) =>
      patch(`/api/artifacts/${encodeURIComponent(slug)}/sharing`, body).then(j),
    unpublishArtifact: (slug: string) => del(`/api/artifacts/${encodeURIComponent(slug)}/publish`).then(j),
    /** Re-check a published artifact's destination and clear a notice that no longer holds.
     *
     *  A publish notice ("still rolling out", "delivery network disabled") is recorded once,
     *  at publish time, and the ordinary happy path never revisits it -- so a link that has
     *  since finished rolling out kept an amber "still rolling out" banner forever. This asks
     *  the destination again and clears `notice` / `notice_code` only when the condition has
     *  actually cleared; it is deliberately user-triggered rather than a timer, because the
     *  answer costs a call to the destination. */
    reprobeArtifactNotice: (slug: string) =>
      post(`/api/artifacts/${encodeURIComponent(slug)}/publish/reprobe-notice`, {}).then(j),
    /** Stash model-authored HTML and get back a URL a sandboxed iframe can load.
     *
     *  Artifact and widget frames cannot use a `blob:` URL: some WebKit-based
     *  in-app browsers refuse the load outright and can take the page down with
     *  it, and a sandboxed `srcdoc` frame blank-renders on WebKit. The returned
     *  URL carries a short-lived client-bound token and the response pins
     *  `Content-Security-Policy: sandbox`, so the document keeps an opaque origin
     *  even opened top-level. See dashboard/handlers/sandbox_doc.py.
     */
    sandboxDocUrl: (html: string) =>
      post('/api/sandbox-doc', { html }).then(j) as Promise<{ url: string }>,
    refreshArtifactSharing: (slug: string) => post(`/api/artifacts/${encodeURIComponent(slug)}/publish/refresh`, {}).then(j),
    pullLatest: (slug: string) =>
      post(`/api/artifacts/${encodeURIComponent(slug)}/pull-latest`, {}).then(j),
    upstreamStatus: (slug: string) =>
      get(`/api/artifacts/${encodeURIComponent(slug)}/upstream-status`).then(j),
    overwriteRemote: (slug: string) =>
      post(`/api/artifacts/${encodeURIComponent(slug)}/overwrite-remote`, {}).then(j),
    // Artifact comments (durable, local per-slug store)
    artifactComments: (slug: string) =>
      get(`/api/artifacts/${encodeURIComponent(slug)}/comments`).then(j),
    postArtifactComment: (slug: string, body: { text: string; scope?: string; anchor?: object; is_agent?: boolean; author?: string }) =>
      post(`/api/artifacts/${encodeURIComponent(slug)}/comments`, body).then(j),
    replyArtifactComment: (slug: string, commentId: string, body: { text: string; is_agent?: boolean; author?: string }) =>
      post(`/api/artifacts/${encodeURIComponent(slug)}/comments/${encodeURIComponent(commentId)}/reply`, body).then(j),
    markCommentReview: (slug: string, commentId: string) =>
      post(`/api/artifacts/${encodeURIComponent(slug)}/comments/${encodeURIComponent(commentId)}/review`, {}).then(j),
    resolveComment: (slug: string, commentId: string) =>
      post(`/api/artifacts/${encodeURIComponent(slug)}/comments/${encodeURIComponent(commentId)}/resolve`, {}).then(j),
    reopenComment: (slug: string, commentId: string) =>
      post(`/api/artifacts/${encodeURIComponent(slug)}/comments/${encodeURIComponent(commentId)}/reopen`, {}).then(j),
    deleteArtifactComment: (slug: string, commentId: string) =>
      del(`/api/artifacts/${encodeURIComponent(slug)}/comments/${encodeURIComponent(commentId)}`).then(j),
    editArtifactComment: (slug: string, commentId: string, body: { text: string }) =>
      patch(`/api/artifacts/${encodeURIComponent(slug)}/comments/${encodeURIComponent(commentId)}`, body).then(j),
  }

  const publishing = {
    artifactTeardown: (slug: string) => post(`/api/deploy/teardown/${slug}`, { confirm: true }).then(j),
    publishProviders: () => get('/api/publish-providers').then(j) as Promise<{ providers: AppPublishProvider[] }>,
    /** Publish through a CORE-registry destination, resolved by its registry name.
     *  Separate from `publishToProvider` on purpose: that one routes at an app's declared
     *  endpoint and falls back to `/api/deploy/deploy`, which is per-artifact deploy
     *  infrastructure -- a different destination, not a different spelling of this one. */
    publishArtifactToCoreProvider: async (slug: string, providerName: string) => {
      const r = await post(`/api/artifacts/${encodeURIComponent(slug)}/publish`, {
        visibility: 'PUBLIC',
        shared_with: [],
        provider: providerName,
      })
      checkSessionExpired(r)
      if (r.ok) { removeAuthBanner(); return r.json() }
      if (r.status === 409) { return r.json() }
      // Parse before surfacing. The body is JSON, so returning its raw text put
      // `{"error": "No AWS account is registered yet..."}` verbatim in the error line --
      // the provider's carefully worded remedy delivered wrapped in syntax.
      const text = await r.text()
      try {
        const parsed = JSON.parse(text)
        const msg = typeof parsed?.error === 'string' ? parsed.error : text
        return { error: msg }
      } catch {
        return { error: text }
      }
    },
    publishToProvider: async (slug: string, providerId: string, provider?: AppPublishProvider, ttlHours?: number) => {
      // Route to the provider's declared endpoint with the payload shape
      // that _do_deploy expects (site_id + artifact_slug). ttl_hours is sent on
      // BOTH preview and confirm so the previewed TTL matches what is deployed
      // (omitting it here makes preview use the backend 72h default).
      const endpoint = provider?.endpoint || '/api/deploy/deploy'
      const payload: Record<string, unknown> = { site_id: slug, artifact_slug: slug, provider_id: providerId }
      if (ttlHours !== undefined) payload.ttl_hours = ttlHours
      const r = await post(endpoint, payload)
      checkSessionExpired(r)
      if (r.ok) { removeAuthBanner(); return r.json() }
      // 409 = scan blocked — parse body so PublishHub can render findings panel
      if (r.status === 409) { return r.json() }
      throw await toApiError(r)
    },
  }

  return { library, remoteAndComments, publishing }
}
