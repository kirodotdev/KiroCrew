/** Reject paths with traversals or sensitive credential directories/files.
 *
 * Shared by DiffBlock (diff "Open" action) and ToolCallLine (file-op tool
 * pill's side-panel icon) so both gate the side-panel open on identical
 * rules — a path unsafe to render as a diff is equally unsafe to open raw.
 */
export function isSafePath(p: string): boolean {
  // Split on BOTH separators. Two of the three consumers hand this the path a
  // tool call actually operated on (`extractToolFilePath` over the tool's JSON
  // input), so on Windows it arrives native: `C:\Users\me\.aws\credentials`.
  // Splitting on `/` alone made that one single segment, and since every test
  // below compares whole segments, nothing could match — the refusal was inert
  // on that platform for every entry in the list AND for `..`. DiffBlock is
  // unaffected either way: its paths come from a git diff header, which is
  // always `/`-separated.
  const segments = p.toLowerCase().split(/[/\\]/)
  if (segments.some(seg => seg === '..')) return false
  const sensitive = ['.aws', '.ssh', '.env', '.git', '.midway', '.gnupg', '.docker', '.kube', '.npmrc', '.pypirc', '.netrc', '.git-credentials']
  return !segments.some(seg => sensitive.some(s => seg === s || seg.startsWith(s + '.')))
}
