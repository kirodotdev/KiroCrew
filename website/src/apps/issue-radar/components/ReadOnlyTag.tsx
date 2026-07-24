import type { RepoPermissions } from '../api'

/** A repo is read-only when we know its permissions and it lacks write
 * (push/triage). Unknown permissions → not flagged. */
export function isReadOnly(perms?: RepoPermissions | null): boolean {
  if (!perms) return false
  return !(perms.push || perms.triage)
}

/** Outlined (no-fill) "Read Only" tag shown next to a repo lacking write
 * access. Kept short with zero vertical padding and a capped line-height so it
 * fits inside a single text row and never changes the row's height. */
export default function ReadOnlyTag() {
  return (
    <span className="inline-flex items-center flex-shrink-0 rounded-full border border-border px-1.5 py-0 text-[10px] leading-[14px] text-muted whitespace-nowrap">
      Read Only
    </span>
  )
}
