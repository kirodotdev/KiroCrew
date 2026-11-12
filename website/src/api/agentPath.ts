/**
 * The URL path of a crew route, `/api/agents/<name><suffix>`.
 *
 * A name that needs percent-encoding (a space becomes `%20`) is sent as an
 * unpadded base64url segment instead, `/api/agents/b64/<segment><suffix>`: a
 * path-normalising reverse proxy decodes `%20` back to a bare space, the
 * request-line delimiter, and answers 502 before the gateway sees the request.
 * The server's twin is `kiro_crew/agent_url_path.py`;
 * both forms reach the same handler.
 */
export function agentPath(name: string, suffix = ''): string {
  if (!needsProxySafePath(name)) return `/api/agents/${name}${suffix}`
  return `/api/agents/b64/${base64Url(name)}${suffix}`
}

/** Whether `name` would be percent-encoded in a path segment, the server's
 *  `needs_proxy_safe_path`. `encodeURIComponent` leaves `!'()*` alone where the
 *  server's `quote(safe='')` escapes them, so those count too and a name has one
 *  spelling everywhere. */
export function needsProxySafePath(name: string): boolean {
  return encodeURIComponent(name) !== name || [...name].some((c) => "!'()*".includes(c))
}

/** Unpadded base64url of the UTF-8 bytes, the server's canonical spelling. */
export function base64Url(text: string): string {
  let binary = ''
  for (const byte of new TextEncoder().encode(text)) binary += String.fromCharCode(byte)
  let encoded = btoa(binary).replaceAll('+', '-').replaceAll('/', '_')
  while (encoded.endsWith('=')) encoded = encoded.slice(0, -1)
  return encoded
}
