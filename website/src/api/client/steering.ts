/**
 * Kiro steering files: list, read, create, update (with the front-matter
 * declaration) and delete. Every verb takes the chat slot's session key, and
 * the three writes carry the listed project key as `X-Steering-Project`
 * (steering-viewer.md).
 */

import type { ClientTransport } from './transport'

/** Precondition header for a steering workspace write. Omitted when the caller
 *  has no project key, which the server treats as fail-closed for `workspace/`
 *  keys — an absent view is not an agreeing one. */
const projectHeader = (projectKey?: string): HeadersInit | undefined =>
  projectKey ? { 'X-Steering-Project': projectKey } : undefined

export function createSteeringEndpoints({ post, put, del, j, jfetch: fetch, sessionKeyHeader: _sk }: ClientTransport) {
  const files = {
    // Steering (Kiro steering files — ~/.kiro/steering + <project>/.kiro/steering)
    // sessionKey names the CHAT SLOT whose project `workspace/` keys resolve
    // against, exactly as it does for kirocrewAgents. Without it the server can
    // only fall back to "the single project every slot shares" and fails closed
    // with two chats on different projects, so project steering silently
    // disappears from a tab that has no way to say why. All five verbs take it:
    // a key created under one project must stay readable, editable and deletable
    // from the same page load.
    steeringFiles: (sessionKey?: string) =>
      fetch('/api/steering', { headers: sessionKey ? { 'X-Session-Key': sessionKey } : { ..._sk } }).then(j),
    steeringFile: (key: string, sessionKey?: string) =>
      fetch('/api/steering/' + key.split('/').map(encodeURIComponent).join('/'), {
        headers: sessionKey ? { 'X-Session-Key': sessionKey } : { ..._sk },
      }).then(j),
    // projectKey is the `project_key` the listing returned: a workspace write
    // echoes it so the server can refuse (409) when the chat slot has since been
    // re-pointed at a different project. The session key names the slot, and the
    // slot is precisely what can move, so it cannot close this on its own.
    createSteering: (name: string, content: string, source?: string, sessionKey?: string, projectKey?: string) =>
      post('/api/steering', { name, content, source }, sessionKey, projectHeader(projectKey)).then(j),
    /** Save a steering file. `declaration` optionally rewrites its front matter
     *  (mode and pattern) SERVER-side — an empty string on a field removes that
     *  key. The editor never splices YAML into its own
     *  textarea: the body is the user's document, and the server's writer
     *  preserves it byte for byte. */
    updateSteering: (
      key: string,
      content: string,
      sessionKey?: string,
      projectKey?: string,
      declaration?: { inclusion?: string; file_match_pattern?: string },
    ) =>
      put('/api/steering/' + key.split('/').map(encodeURIComponent).join('/'), { content, ...(declaration ?? {}) }, sessionKey, projectHeader(projectKey)).then(j),
    deleteSteering: (key: string, sessionKey?: string, projectKey?: string) =>
      del('/api/steering/' + key.split('/').map(encodeURIComponent).join('/'), undefined, sessionKey, projectHeader(projectKey)).then(j),
  }

  return { files }
}
