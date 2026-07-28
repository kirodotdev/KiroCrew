import type { RepoPermissions } from '../api'

/** A repo is read-only when we know its permissions and it lacks write
 * (push/triage). Unknown permissions → not flagged. */
export function isReadOnly(perms?: RepoPermissions | null): boolean {
  if (!perms) return false
  return !(perms.push || perms.triage)
}

/** Outlined (no-fill) "Read Only" tag shown next to a repo lacking write
 * access. Kept short with zero vertical padding and a capped line-height so it
 * fits inside a single text row and never changes the row's height.
 *
 * `vertical` turns the pill on its side for the collapsed rail strip, where the
 * padding axes swap: the text runs down the block axis, so the length padding
 * becomes vertical and the pill's thickness comes from the line-height. */
export default function ReadOnlyTag({ vertical = false }: { vertical?: boolean }) {
  if (vertical) {
    return (
      <span
        className="inline-flex items-center flex-shrink-0 rounded-full border border-border py-1.5 text-[10px] leading-[14px] text-muted whitespace-nowrap"
        style={{ writingMode: 'vertical-rl' }}
      >
        Read Only
      </span>
    )
  }
  return (
    <span className="inline-flex items-center flex-shrink-0 rounded-full border border-border px-1.5 py-0 text-[10px] leading-[14px] text-muted whitespace-nowrap">
      Read Only
    </span>
  )
}
