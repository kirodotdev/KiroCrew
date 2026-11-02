/**
 * The URL path of a crew route, `/api/agents/<name><suffix>`.
 *
 * A name that needs percent-encoding (a space becomes `%20`) is sent as an
 * unpadded base64url segment instead, `/api/agents/b64/<segment><suffix>`: a
 * path-normalising reverse proxy decodes `%20` back to a bare space, the
 * request-line delimiter, and answers 502 before the gateway sees the request
 * (#19976, #20083). The server's twin is `kiro_crew/agent_url_path.py`;
 * both forms reach the same handler.
 */
export function agentPath(name: string, suffix = ''): string {
  // `encodeURIComponent` leaves `!'()*` alone where the server's
  // `quote(safe='')` escapes them; both sides use the base64url form for
  // those too, so a name has one spelling everywhere.
  const encoded = encodeURIComponent(name)
  if (encoded === name && !/[!'()*]/.test(name)) return `/api/agents/${encoded}${suffix}`
  return `/api/agents/b64/${base64Url(name)}${suffix}`
}

/** Unpadded base64url of the UTF-8 bytes, the server's canonical spelling. */
export function base64Url(text: string): string {
  let binary = ''
  for (const byte of new TextEncoder().encode(text)) binary += String.fromCharCode(byte)
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}
