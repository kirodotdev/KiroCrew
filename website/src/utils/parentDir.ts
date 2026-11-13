/**
 * The directory containing `p`, or `null` at a filesystem root. Separator-aware
 * because the backend prints paths with the host's own separator (`str(Path)`),
 * so on a Windows gateway every one is a backslash; a `/`-only dirname is POSIX-only.
 */
export function parentDir(p: string): string | null {
  const trimmed = p.replace(/[\\/]+$/, '')
  const cut = Math.max(trimmed.lastIndexOf('/'), trimmed.lastIndexOf('\\'))
  if (cut < 0) return null
  // `/x` → `/`; `C:\x` → `C:\`. Keep the separator so the root stays a path.
  if (cut === 0 || /^[a-z]:$/i.test(trimmed.slice(0, cut))) return trimmed.slice(0, cut + 1)
  const parent = trimmed.slice(0, cut)
  return parent === trimmed ? null : parent
}
