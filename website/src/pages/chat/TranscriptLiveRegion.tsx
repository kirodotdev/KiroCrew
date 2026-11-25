/**
 * The transcript's one live region.
 *
 * It stays mounted while only its text changes. Screen readers can therefore
 * announce each message once without treating virtualized transcript rows as
 * new content when scrolling mounts them again.
 */
export function TranscriptLiveRegion({ announcement }: { announcement: string }) {
  return <span className="sr-only" aria-live="polite" aria-atomic="true">{announcement}</span>
}
