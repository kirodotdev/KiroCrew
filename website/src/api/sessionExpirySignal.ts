/** Dependency-free bridge to the dashboard's existing authentication recovery.
 * Standalone SDK documents without a dashboard handler keep their own error path.
 */
type SessionExpiryHandler = (response: Response) => void
let handler: SessionExpiryHandler | undefined

/** Install recovery without importing the dashboard client into SDK bundles. */
export function installSessionExpiryHandler(next: SessionExpiryHandler): () => void {
  handler = next
  return () => { if (handler === next) handler = undefined }
}

/** Signal only the backend's explicit expiry response; never consume its body. */
export function noteSessionExpiredResponse(response: Response): boolean {
  if (response.status !== 403 || response.headers.get('X-Auth-Required') !== 'true') return false
  handler?.(response)
  return true
}
