/**
 * Pages around the desk: where each one stands, which are put away, and how
 * they are found again. Plain data, so the rules can be tested without a
 * scene.
 *
 * A page is one of the dashboard's own surfaces (a Bot's chat, dashboard or
 * work, or the crew's asks) pulled out into the room. It stays where the
 * person put it, as a direction from their seat. Every page stands at reading
 * distance and is never scaled below full size, so its text keeps its angle
 * at the eye wherever it stands.
 *
 * A new page opens in the first free place nearest to where the person
 * looks: inside what the glasses show when there is room, further round when
 * there is not. Pages never open on top of each other. A page that is put
 * away is listed on the shelf, and comes back to where the person looks.
 */
import { DEG, FULL } from './metrics'

export type PageKind = 'chat' | 'dash' | 'work' | 'hub'

export interface PageRec {
  kind: PageKind
  /** The Bot it belongs to; '' for the crew's asks. */
  slug: string
  /** The thread it shows; '' until the Bot has one, and for the crew's asks. */
  slot: string
  topic: string
  /** Where it stands, as seen from the seat: degrees to the right of straight ahead, and degrees up. */
  yaw: number
  pitch: number
  /** Put away: not drawn, and listed on the shelf. */
  stored: boolean
}

/** How many pages can be out or put away at once. Each one holds a painted canvas. */
export const PAGE_MAX = 12
/** A page's width and height in degrees at reading distance, and the gap kept between two. */
export const PAGE_W_DEG = (2 * Math.atan(FULL.width / 2 / FULL.distance)) / DEG
const GAP_DEG = 2.5
/** The rows a page can open in: just under eye level first, clear of the desk, then a row above. */
const ROWS = [-4, 19]
/** How far round the seat a page may open, each way. */
const REACH_DEG = 150

/** Two pages show the same thing when kind, Bot and thread agree. The crew's asks are one page. */
export const samePage = (a: Pick<PageRec, 'kind' | 'slug' | 'slot'>, b: Pick<PageRec, 'kind' | 'slug' | 'slot'>): boolean =>
  a.kind === 'hub' || b.kind === 'hub' ? a.kind === b.kind : a.slug === b.slug && a.slot === b.slot

/** A page's centre in the person's space, in metres. */
export function pagePos(yaw: number, pitch: number): [number, number, number] {
  const r = FULL.distance * Math.cos(pitch * DEG)
  return [r * Math.sin(yaw * DEG), FULL.distance * Math.sin(pitch * DEG), -r * Math.cos(yaw * DEG)]
}

/** The direction of a point in the person's space, as a page place. */
export function placeOf(x: number, y: number, z: number): { yaw: number; pitch: number } {
  return { yaw: Math.atan2(x, -z) / DEG, pitch: Math.atan2(y, Math.hypot(x, z)) / DEG }
}

const clash = (a: { yaw: number; pitch: number }, b: { yaw: number; pitch: number }): boolean =>
  Math.abs(a.yaw - b.yaw) < PAGE_W_DEG + GAP_DEG - 0.01 && Math.abs(a.pitch - b.pitch) < 20

/**
 * Where a new page opens: the free place nearest to where the person looks.
 * Places are fixed around the seat, a page's width apart, so pages stand
 * edge to edge however the head was turned when each was opened. The two
 * places either side of straight ahead are inside the glasses' view
 * together. The row just under eye level fills first, then the row above.
 */
export function freePlace(pages: readonly PageRec[], gazeYaw: number): { yaw: number; pitch: number } | null {
  const out = pages.filter(p => !p.stored)
  const step = PAGE_W_DEG + GAP_DEG
  const yaws: number[] = []
  for (let k = 0; (k + 0.5) * step <= REACH_DEG; k += 1) yaws.push(-(k + 0.5) * step, (k + 0.5) * step)
  yaws.sort((a, b) => Math.abs(a - gazeYaw) - Math.abs(b - gazeYaw) || a - b)
  for (const pitch of ROWS) {
    for (const yaw of yaws) {
      const place = { yaw, pitch }
      if (!out.some(p => clash(p, place))) return place
    }
  }
  return null
}

const KINDS: readonly string[] = ['chat', 'dash', 'work', 'hub']

/** Read stored pages. A record that is not exactly a page is dropped; the rest are kept, up to the limit. */
export function parsePages(raw: string | null | undefined): PageRec[] {
  if (!raw) return []
  try {
    const v: unknown = JSON.parse(raw)
    if (!Array.isArray(v)) return []
    const out: PageRec[] = []
    for (const r of v as Partial<PageRec>[]) {
      if (!r || typeof r !== 'object' || !KINDS.includes(String(r.kind))) continue
      if (typeof r.slug !== 'string' || typeof r.slot !== 'string' || typeof r.yaw !== 'number' || typeof r.pitch !== 'number') continue
      if (!Number.isFinite(r.yaw) || !Number.isFinite(r.pitch) || Math.abs(r.pitch) > 80) continue
      const rec: PageRec = { kind: r.kind as PageKind, slug: r.slug.slice(0, 120), slot: r.slot.slice(0, 200), topic: typeof r.topic === 'string' ? r.topic.slice(0, 200) : '', yaw: r.yaw, pitch: r.pitch, stored: r.stored === true }
      if (out.some(p => samePage(p, rec))) continue
      out.push(rec)
      if (out.length === PAGE_MAX) break
    }
    return out
  } catch {
    return []
  }
}
