/** Reject paths with traversals or sensitive credential directories/files.
 *
 * Shared by DiffBlock (diff "Open" action) and ToolCallLine (file-op tool
 * pill's side-panel icon) so both gate the side-panel open on identical
 * rules — a path unsafe to render as a diff is equally unsafe to open raw.
 */
export function isSafePath(p: string): boolean {
  const segments = p.toLowerCase().split('/')
  if (segments.some(seg => seg === '..')) return false
  const sensitive = ['.aws', '.ssh', '.env', '.git', '.midway', '.gnupg', '.docker', '.kube', '.npmrc', '.pypirc', '.netrc', '.git-credentials']
  return !segments.some(seg => sensitive.some(s => seg === s || seg.startsWith(s + '.')))
}
