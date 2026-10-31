/** Window CustomEvents the dashboard socket re-broadcasts.
 *
 *  Some frame families have their consumer outside Redux and React Query: a
 *  page or panel listens on `window` instead (ArtifactDetailPage, AppHost,
 *  ChannelPage). `kirocrew-tool-call` and `cron_history`
 *  have no listener in this tree but stay part of what the socket emits. Every
 *  name and detail shape is a listener contract, so all of them live here. */

/** A `tool_call` frame, re-broadcast before the store sees it: a reducer that
 *  throws on a malformed payload must not also cost a listener its signal. */
export function emitToolCall(detail: unknown): void {
  window.dispatchEvent(new CustomEvent('kirocrew-tool-call', { detail }))
}

/** An artifact was deleted server-side. The detail page's deletion listener
 *  decides whether to navigate away or keep a dirty buffer on screen. */
export function emitArtifactDeleted(slug: string): void {
  window.dispatchEvent(new CustomEvent('kirocrew:artifact-deleted', { detail: { slug } }))
}

/** App dev-mode live reload: the gateway watched a dev-flagged app's ui/ dir
 *  change. AppHost listens for this and re-imports the bundle. */
export function emitAppReload(detail: { app: string }): void {
  window.dispatchEvent(new CustomEvent('mc:app-reload', { detail }))
}

/** A channel frame (message, agent status, created, closed, joined, left),
 *  forwarded with its envelope type. */
export function emitChannelEvent(type: string, data: unknown): void {
  window.dispatchEvent(new CustomEvent('kirocrew-channel', { detail: { type, data } }))
}

/** A cron run was recorded. */
export function emitCronHistory(detail: unknown): void {
  window.dispatchEvent(new CustomEvent('cron_history', { detail }))
}
