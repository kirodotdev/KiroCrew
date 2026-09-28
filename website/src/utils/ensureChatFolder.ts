/**
 * One chat folder by name: find it, or create it when it is missing.
 *
 * Every dashboard caller that files sessions into a folder does the same three
 * steps against `/api/chat/folders` — list, find by name, create on a miss — and
 * folders have no upsert endpoint, so this is the one place those steps are
 * spelled. The transport is a parameter rather than an import: the app SDK reaches
 * the endpoint through its permission-scoped client while host code uses `api`
 * directly, and a helper bound to either would be unusable from the other.
 *
 * Three rules every caller gets by going through here:
 *
 * - **Matching is against the name the server STORED.** `chat_folders.py` keeps
 *   `name.strip()[:100]`, so a lookup for a longer name can never match what an
 *   earlier create left behind, and a folder is quietly made again on every run.
 *   A name that would overflow is cut to fit BEFORE it is matched or sent, with a
 *   short fingerprint of the whole name in its tail, so what the server stores is
 *   exactly what was asked for and two long names that agree on their first 100
 *   characters still land in two folders.
 * - **A folder is matched where the caller says.** A caller that names a parent
 *   (`''` is the top level, which is how the backend spells an absent `parent_id`)
 *   matches under exactly that parent; a caller that names none matches the name
 *   anywhere in the tree, so a folder the reader has moved keeps being found.
 * - **A cache miss is never trusted.** When the caller hands in the list it already
 *   holds, a hit is proof; a miss might only mean the cache has not heard about a
 *   folder an earlier run created, so one authoritative read is spent before
 *   anything is created. Without a cached list the one read happens up front.
 *
 * What is NOT decided here is what a failure means. A rejected list or create is
 * rethrown untouched, and only a create that answers without an id yields `null`,
 * so each call site keeps its own contract — swallow, or throw — exactly as it
 * had it before the helper existed. Nothing here closes the check-then-create
 * race either: two callers can still both miss and both create, and only an
 * atomic get-or-create on the endpoint could change that.
 */
import { sha256Hex } from './sha256Hex'


/** The subset of a `GET /api/chat/folders` row this helper reads. */
export interface ChatFolderRow {
  id?: string
  name?: string
  parent_id?: string
}

/**
 * Longest folder name the server keeps, mirroring `chat_folders.py`, which stores
 * `name.strip()[:100]` on both create and rename.
 */
export const SERVER_NAME_LIMIT = 100

/**
 * FNV-1a over UTF-16 code units, as eight hex characters. Not cryptographic: a short,
 * deterministic tag for the command bar's row-id discriminator, where the inputs are
 * a handful of ids the launcher itself minted. Not what `storedName` uses.
 */
export function fnv1a8(s: string): string {
  let hash = 0x811c9dc5
  for (let i = 0; i < s.length; i++) {
    hash ^= s.charCodeAt(i)
    hash = Math.imul(hash, 0x01000193) >>> 0
  }
  return hash.toString(16).padStart(8, '0')
}

/**
 * The fingerprint an over-long name carries in its tail: the first 128 bits of the
 * SHA-256 of the whole trimmed name, as 32 hex characters. Collision-resistant, so two
 * different names cannot be made to share a tail by choosing them; a 32-bit tag could.
 */
function nameTag(trimmed: string): string {
  return sha256Hex(trimmed).slice(0, 32)
}

/**
 * The name the server will actually store for `raw`.
 *
 * Within the limit that is the trimmed name itself. Over it, the server would cut
 * the tail and keep the first 100 code points, which merges every name sharing that
 * prefix into one folder. So the cut is made here instead, with the last 35 code
 * points spent on ` (<nameTag of the whole trimmed name>)`: the result is exactly 100
 * code points, so the server stores it verbatim and the next run's lookup matches,
 * and two long names that differ anywhere get two different tails.
 */
export function storedName(raw: string): string {
  // Cut by CODE POINT, not by UTF-16 unit. `slice` counts units, so a name whose
  // boundary falls inside a surrogate pair loses half a character and the name carries
  // a lone surrogate -- an invalid string, persisted. It also happens to be what the
  // server means: Python slices its own strings by code point.
  const trimmed = raw.trim()
  const points = [...trimmed]
  if (points.length <= SERVER_NAME_LIMIT) return trimmed
  const tag = ` (${nameTag(trimmed)})`
  return points.slice(0, SERVER_NAME_LIMIT - tag.length).join('') + tag
}

/** Rows only; a non-array response (an error envelope) yields nothing to match. */
export function folderRows(value: unknown): ChatFolderRow[] {
  return Array.isArray(value) ? (value as ChatFolderRow[]) : []
}

/**
 * A folder with this exact name under this exact parent. An absent `parent_id`
 * is the top level.
 */
function folderAt(list: readonly ChatFolderRow[], name: string, parentId: string): ChatFolderRow | undefined {
  return list.find(f => f?.name === name && String(f?.parent_id ?? '') === parentId)
}

export interface EnsureChatFolderOptions {
  /** `GET /api/chat/folders`. Anything but an array of rows is read as no rows. */
  list: () => Promise<unknown>
  /**
   * `POST /api/chat/folders` for `name` under `parentId` (`''` is the top level).
   * Only `.id` is read from what it resolves to.
   */
  create: (name: string, parentId: string) => Promise<unknown>
  /** The folder name as the caller derived it; cut to what the server stores. */
  name: string
  /**
   * Parent folder id to match under; `''` is the top level. Omitted means the name is
   * matched ANYWHERE in the tree (a folder the reader has nested still counts) and a
   * missing one is created at the top level.
   */
  parentId?: string
  /**
   * The folder list the caller already holds. A hit here is trusted; a miss costs
   * one `list()` before a create. Empty or omitted means one `list()` up front.
   */
  cached?: readonly ChatFolderRow[]
}

/**
 * The id of the folder called `name` under `parentId`, created if it does not
 * exist. `null` when the name is blank or a create answers without an id; a
 * rejected `list` or `create` propagates.
 */
export async function ensureChatFolder(opts: EnsureChatFolderOptions): Promise<string | null> {
  const name = storedName(opts.name)
  if (!name) return null
  const parentId = opts.parentId
  const find = (rows: readonly ChatFolderRow[]): ChatFolderRow | undefined =>
    parentId === undefined ? rows.find(f => f?.name === name) : folderAt(rows, name, parentId)
  let list = folderRows(opts.cached)
  let read = false
  if (list.length === 0) {
    list = folderRows(await opts.list())
    read = true
  }
  const hit = find(list)
  if (hit?.id) return hit.id
  if (!read) {
    // The one re-read, spent on the first miss rather than on every run.
    list = folderRows(await opts.list())
    const fresh = find(list)
    if (fresh?.id) return fresh.id
  }
  const created = (await opts.create(name, parentId ?? '')) as ChatFolderRow | null | undefined
  return created?.id ? created.id : null
}
