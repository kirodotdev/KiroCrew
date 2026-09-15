import type { ChatFolder } from '../types'

/** Breadcrumb separator — matches the server-side folder_breadcrumb (U+203A). */
export const FOLDER_PATH_SEP = ' › '

export interface OrderedFolder {
  readonly folder: ChatFolder
  /** Ancestor names root→parent (excludes the folder itself). Empty for root folders. */
  readonly ancestors: readonly string[]
  /** Depth in the tree (0 for root folders). Equals ancestors.length. */
  readonly depth: number
  /** Full ancestry path root→leaf, e.g. "Parent › Child". Equals the name for root folders. */
  readonly path: string
}

/**
 * The one order siblings are drawn in: stored `order`, then name as a tie-break
 * (the store permits duplicate order values, so a comparator without the second
 * key would leave the sequence to array position and shuffle on refetch).
 *
 * Both halves exist to agree with the Python reader, because `chat_folder_tree`
 * is what an agent picks a `before`/`after` anchor from and a sequence that
 * differs from the sidebar's makes the anchor wrong.
 *
 * `?? 0` is load-bearing, not defensive. A row written before the field existed
 * carries no `order`, and `GET /api/chat/folders` returns rows verbatim — so
 * `a.order - b.order` would be `NaN`, which is falsy, and the whole comparison
 * would silently fall through to a name-only order. 0 is what
 * `_chat_folder_order` coerces a missing key to.
 *
 * The name tie-break compares code units directly rather than calling
 * `localeCompare`. Two reasons, one per side: the host locale would order the
 * same two folders differently for two people (the i18n gate's rule), and ICU
 * collation would not match `_chat_folder_siblings`, which compares the raw
 * UTF-16 encoding.
 *
 * See `folderName` for why nothing is lowercased and `folderOrder` for which
 * `order` values are accepted; both mirror the Python reader exactly, and the
 * shared fixture `test/fixtures/chat_folder_sibling_order.json` is where that
 * agreement is checked rather than asserted.
 *
 * Exported because a folder's position is something an agent can set
 * (`chat_folder_move`'s `before`/`after`), so every surface that draws siblings
 * has to read it the same way — a render path that skips this comparator shows
 * a sequence the person never chose.
 */
export const bySidebarOrder = (a: ChatFolder, b: ChatFolder): number => {
  const byOrder = folderOrder(a) - folderOrder(b)
  if (byOrder !== 0) return byOrder
  const an = folderName(a)
  const bn = folderName(b)
  return an < bn ? -1 : an > bn ? 1 : 0
}

/**
 * A folder's `order` as a finite number, accepting exactly the set Python's
 * `_chat_folder_order` accepts.
 *
 * The store is read with a bare `JSON.parse` and never schema-checked, so `order`
 * can be any JSON value. Only a real number is taken, because the two languages'
 * conversions of everything else disagree: `Number('0x10')` is 16 and
 * `Number('1e3')` is 1000 where Python's `int()` raises on both, and `Number([5])`
 * is 5 where `int([5])` raises. A bool is excluded to match, since `isinstance` on
 * the Python side treats it as an `int` subclass and rejects it explicitly.
 *
 * `Math.trunc` matches `int()` on a fraction, and the clamp closes the top end:
 * Python integers are unbounded, so it can order `2**53 + 1` above `2**53` where
 * both collapse to one value here — ordered there, a tie here, and a tie hands the
 * pair to the name comparator, which can invert it.
 */
const folderOrder = (f: ChatFolder): number => {
  const v: unknown = f.order
  if (typeof v !== 'number' || !Number.isFinite(v)) return 0
  return Math.max(-Number.MAX_SAFE_INTEGER, Math.min(Number.MAX_SAFE_INTEGER, Math.trunc(v)))
}

/**
 * A folder's name for the tie-break, or `''` when it is not a string.
 *
 * `toLowerCase` is NOT used. It reads the browser's Unicode tables where the Python
 * reader's `str.lower()` reads the interpreter's, so a character whose case mapping
 * differs between those two versions folds differently on each side — and neither
 * side owns both tables, so no code can close that skew.
 *
 * `A`-`Z` fold anyway, by ARITHMETIC on the code unit (`+32`), which is what the
 * Python reader's literal 26-entry table does. That range is fixed in every Unicode
 * version, so the fold costs no version dependency — and it is worth having,
 * because a store written before `order` existed has every sibling tied at 0, and
 * the tie-break alone decides those sidebars.
 *
 * A non-string is NOT stringified, because that is where the two languages part
 * company: `String({a: 1})` is `'[object Object]'` where Python's `str` gives
 * `"{'a': 1}"`, and `String(true)` is `'true'` where `str(True)` is `'True'`.
 * Reading the whole class as empty makes both sides agree by construction.
 */
const folderName = (f: ChatFolder): string =>
  typeof f.name === 'string'
    ? f.name.replace(/[A-Z]/g, c => String.fromCharCode(c.charCodeAt(0) + 32))
    : ''

/**
 * Flatten folders into pre-order (tree) sequence so children sit directly under
 * their parent, siblings sorted by `order` then name. Each entry carries its
 * ancestor names (for breadcrumb rendering) and depth (for indentation).
 * Orphans (parent_id pointing at a missing folder) are treated as roots.
 * Cycle/depth guarded.
 *
 * Shared by the folder pickers (move-to-folder submenu, new-chat-in-folder)
 * so the indented tree ordering stays identical everywhere.
 */
export function orderFoldersWithPaths(folders: readonly ChatFolder[]): OrderedFolder[] {
  const byId = new Map(folders.map(f => [f.id, f]))
  const childrenOf = (pid: string) =>
    folders
      .filter(f => {
        const parent = f.parent_id && byId.has(f.parent_id) ? f.parent_id : ''
        return parent === pid
      })
      .sort(bySidebarOrder)

  const out: OrderedFolder[] = []
  const walk = (folder: ChatFolder, ancestors: string[], visited: Set<string>) => {
    if (visited.has(folder.id) || ancestors.length > 20) return
    visited.add(folder.id)
    out.push({
      folder,
      ancestors: [...ancestors],
      depth: ancestors.length,
      path: [...ancestors, folder.name].join(FOLDER_PATH_SEP),
    })
    for (const child of childrenOf(folder.id)) walk(child, [...ancestors, folder.name], visited)
  }
  const visited = new Set<string>()
  for (const root of childrenOf('')) walk(root, [], visited)
  // Safety net: surface any folder the walk missed (e.g. a cycle root) so no
  // destination silently disappears from the picker.
  for (const f of folders) if (!visited.has(f.id)) out.push({ folder: f, ancestors: [], depth: 0, path: f.name })
  return out
}

/**
 * Collect a folder's id plus every descendant id (children, grandchildren, …).
 * Used to keep re-parenting acyclic: a folder may not move into itself or any
 * folder inside its own subtree. O(N): one pass builds a parent→children
 * index, then a BFS visits only the subtree; the visited set doubles as the
 * result and guarantees termination on corrupt parent_id cycles.
 */
export function collectFolderSubtreeIds(folders: readonly ChatFolder[], rootId: string): Set<string> {
  const childrenOf = new Map<string, string[]>()
  for (const f of folders) {
    if (!f.parent_id) continue
    const siblings = childrenOf.get(f.parent_id)
    if (siblings) siblings.push(f.id)
    else childrenOf.set(f.parent_id, [f.id])
  }
  const out = new Set<string>([rootId])
  const queue: string[] = [rootId]
  for (let i = 0; i < queue.length; i++) {
    for (const child of childrenOf.get(queue[i]) ?? []) {
      if (!out.has(child)) {
        out.add(child)
        queue.push(child)
      }
    }
  }
  return out
}
