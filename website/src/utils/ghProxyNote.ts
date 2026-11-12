/**
 * The gh proxy's note on a remote workspace command's result.
 *
 * In a remote session, a `gh` the workspace does not have of its own is Kiro
 * Crew's gh proxy: the command runs on the gateway machine with its owner's
 * GitHub login. The workspace tool server leads the result with one line per
 * such command (`workspace_tools.GH_PROXY_RESULT_PREFIX`), and only that tool
 * server writes the first line, so the row can say where the command ran.
 */

/** Mirrors `workspace_tools.GH_PROXY_RESULT_PREFIX`; the login follows it. The
 *  result may arrive bare or as an MCP text content inside JSON (`"text":"`),
 *  whose encoder may escape the middle dot. */
const GH_PROXY_NOTE_RE = /^(?:[^\n"]{0,40}"content":\s*\[\s*\{"type":\s*"text",\s*"text":\s*"|\[\{"type":\s*"text",\s*"text":\s*")?\[gh-proxy (?:·|\\u00b7) ran on your machine as @([A-Za-z0-9-]{1,39}):/

export interface GhProxyNote {
  /** The gateway owner's GitHub login the command ran as. */
  login: string
}

export function ghProxyNote(output: string | undefined | null): GhProxyNote | null {
  if (!output) return null
  const match = GH_PROXY_NOTE_RE.exec(output)
  return match ? { login: match[1] } : null
}
