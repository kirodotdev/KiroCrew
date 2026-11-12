/**
 * A Crew API error body in the dashboard's own error shape.
 *
 * Crew API routes (`/v1/...`) answer an error as
 * `{"error": {"type", "code", "message", "param", "request_id"}}`. Every dashboard
 * error reader (`parseErrorCode`, the stale-owner signal, `friendlyErrText`) reads the
 * flat `{"error": "<message>", "code": "<code>"}` the `/api/*` routes send. This turns
 * the first into the second, keeping the other fields at the top level, so one reader
 * serves both. Any other body is returned unchanged.
 */
export function flattenCrewApiError(body: string): string {
  const trimmed = body.trim()
  if (!trimmed.startsWith('{')) return body
  let parsed: unknown
  try {
    parsed = JSON.parse(trimmed)
  } catch {
    return body
  }
  const error = (parsed as { error?: unknown } | null)?.error
  if (!error || typeof error !== 'object' || Array.isArray(error)) return body
  const { type, code, message, request_id: requestId } = error as Record<string, unknown>
  if (typeof type !== 'string' || typeof code !== 'string' || typeof requestId !== 'string') return body
  return JSON.stringify({ ...error, error: typeof message === 'string' ? message : '', code })
}

/** Whether *r* is a 403 whose body, in either error shape, carries code `owner_only`. */
export async function isOwnerOnlyRefusal(r: Response): Promise<boolean> {
  if (r.status !== 403) return false
  const body = flattenCrewApiError(await r.clone().text().catch(() => ''))
  try {
    return (JSON.parse(body) as { code?: unknown } | null)?.code === 'owner_only'
  } catch {
    return false
  }
}
