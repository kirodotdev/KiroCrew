/**
 * Which two artifact states the "Compare with" view puts side by side.
 *
 * A state is either the always-current Live record or one numbered snapshot.
 * The page keys them as strings so one select can carry both: `live`, or the
 * snapshot number. The older state always goes on the left and the newer on the
 * right, whichever of the two the reader happens to be viewing, so the colours
 * read the same way every time (red leaves, green arrives).
 */
import type { Artifact } from '../types'
import { i18nT } from '../i18n/t'
import { fmtNumber } from '../i18n/format'

export const COMPARE_LIVE = 'live'

/** The select's value for "no comparison". */
export const COMPARE_NONE = 'none'

/** The key one state goes by: `live`, or the snapshot number as text. */
export function stateKey(version: number | null): string {
  return version === null ? COMPARE_LIVE : String(version)
}

/** A snapshot number from a key, or null for `live` and anything that is not a whole number. */
function snapshotOf(key: string): number | null {
  const n = Number(key)
  return Number.isInteger(n) && n > 0 ? n : null
}

/**
 * Kinds whose content is text a line diff can read. A web app is a reference
 * to something running and an image is bytes, so neither has two texts to set
 * beside each other.
 */
export function isComparableKind(kind: Artifact['kind']): boolean {
  return kind !== 'webapp' && kind !== 'image'
}

/**
 * The key to compare against, or null when the comparison cannot be drawn.
 * A key that names the state already on screen, a snapshot the artifact no
 * longer lists, or nothing at all each give null, so the page shows the
 * document exactly as it did before a comparison was asked for.
 */
export function activeCompareKey(
  wanted: string,
  shown: number | null,
  versions: readonly number[],
): string | null {
  if (!wanted || wanted === COMPARE_NONE || wanted === stateKey(shown)) return null
  if (wanted === COMPARE_LIVE) return wanted
  const n = snapshotOf(wanted)
  return n !== null && versions.includes(n) ? String(n) : null
}

/** The keys the select offers: Live first, then snapshots newest first, minus the state on screen. */
export function compareChoices(shown: number | null, versions: readonly number[]): string[] {
  const keys = [COMPARE_LIVE, ...versions.slice().reverse().map(String)]
  return keys.filter((key) => key !== stateKey(shown))
}

/** The snapshot number a key names, or null for Live (which has no snapshot to fetch). */
export function compareSnapshot(key: string | null): number | null {
  return key === null ? null : snapshotOf(key)
}

/** Both keys ordered oldest first: Live is the newest state there is. */
export function orderStates(a: string, b: string): [string, string] {
  const rank = (key: string) => (key === COMPARE_LIVE ? Number.MAX_SAFE_INTEGER : snapshotOf(key) ?? 0)
  return rank(a) <= rank(b) ? [a, b] : [b, a]
}

/** `Live`, or `v3`: how the version picker words a state, so every place that names one reads the same. */
export function stateLabel(key: string): string {
  return key === COMPARE_LIVE
    ? i18nT('pages.artifactDetailPage.live')
    : i18nT('pages.artifactDetailPage.version_label', { version: fmtNumber(Number(key), { useGrouping: false }) })
}

/** The file name the diff viewer is given, so it picks a syntax for the kind. */
export function compareFileName(artifact: Pick<Artifact, 'slug' | 'kind'>): string {
  const ext: Partial<Record<Artifact['kind'], string>> = {
    markdown: 'md', html: 'html', widget: 'html', svg: 'svg', json: 'json', dashboard: 'json',
  }
  return `${artifact.slug}.${ext[artifact.kind] ?? 'txt'}`
}
