/**
 * The local editors the ⋯ menu's "Open in <editor>" row can target, by
 * `dashboard.remote_editor.editor` id (`REMOTE_EDITOR_IDS` in
 * config/sections_dashboard.py). The id is the editor's URL scheme; the value is
 * its brand name, shown as is in every language.
 */
export const REMOTE_EDITORS: Readonly<Record<string, string>> = { vscode: 'VS Code' }

/** What follows `<editor>:` in a remote-editor link, up to the host. */
const sshRemoteAuthority = '//vscode-remote/ssh-remote+'

/**
 * The link that opens `absPath` in the viewer's local `editor`, which connects to
 * `host` over the viewer's own SSH: `<editor>://vscode-remote/ssh-remote+<host><path>`.
 *
 * null when the editor or host is unset, or the path is not absolute or has a
 * `.`/`..` segment: URL parsing drops dot segments before the editor reads the
 * `ssh-remote+<host>` segment, so `/a/../../ssh-remote+other/x` would connect to
 * another host. The host arrives as a DNS name or SSH alias (the branding
 * endpoint drops any other). Each path segment is percent-encoded, `!'()*`
 * included, so a `#`, `?` or space in a name stays part of the path and the
 * desktop app's editor-link check accepts it; a name that check refuses (a
 * backslash or control character) or that cannot be encoded (a lone surrogate
 * from a non-UTF-8 file name) gets no link.
 */
export function remoteEditorUrl(editor: string, host: string, absPath: string): string | null {
  if (!Object.hasOwn(REMOTE_EDITORS, editor) || !host || !absPath.startsWith('/')) return null
  const segments = absPath.split('/')
  if (segments.some(s => s === '.' || s === '..')) return null
  // The desktop app's editor-link check refuses these, so its link would do nothing.
  if (/[\u0000-\u001f\\]/.test(absPath)) return null
  const encode = (segment: string) =>
    encodeURIComponent(segment).replace(/[!'()*]/g, c => `%${c.charCodeAt(0).toString(16).toUpperCase()}`)
  try {
    return `${editor}:${sshRemoteAuthority}${host}${segments.map(encode).join('/')}`
  } catch {
    return null
  }
}
