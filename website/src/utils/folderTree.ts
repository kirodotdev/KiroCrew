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
 * The sidebar's folder sort modes — `dashboard.folder_sort` in the gateway config.
 * `custom` is the stored order above (the default, and the only order that existed
 * before the mode did); `name` is an ASCII-case-insensitive natural order (only
 * `A`-`Z` fold — see `folderName`); `created` is
 * newest first. Mirrors `FOLDER_SORT_MODES` in `config/sections.py`, and the Python
 * reader in `mcp_dashboard.py` sorts with the same three keys — the shared fixture
 * carries a `mode` per case so that agreement is checked, not asserted.
 */
export type FolderSortMode = 'custom' | 'name' | 'created'
export const FOLDER_SORT_MODES: readonly FolderSortMode[] = ['custom', 'name', 'created']

/**
 * The stored mode, or `custom` for anything else — the same fallback the loader
 * makes, so a value this build does not know (an absent key on an older gateway, a
 * hand edit) renders the order every earlier build drew rather than nothing.
 */
export const readFolderSortMode = (raw: unknown): FolderSortMode =>
  typeof raw === 'string' && (FOLDER_SORT_MODES as readonly string[]).includes(raw)
    ? (raw as FolderSortMode)
    : 'custom'

/**
 * The comparator for one folder sort mode. `custom` IS `bySidebarOrder`, by identity,
 * so nothing about today's order changes for a person who never picks a mode. The
 * other two are VIEW orders layered on top of it: each falls through to
 * `bySidebarOrder` for the pairs it cannot separate (equal names up to zero
 * padding, equal or missing stamps), so the manual arrangement still decides
 * those and the order stays total. None of them writes anything — choosing a mode
 * never rewrites a stored `order`, which is what lets Custom restore the manual
 * arrangement exactly.
 *
 * Every surface that draws siblings must sort with the person's mode through here
 * (the sidebar at every depth, the folder pickers, and the MCP tree on the Python
 * side); a surface sorting with `bySidebarOrder` while the person has picked
 * `name` shows a sequence they never chose.
 */
export const folderComparator = (mode: FolderSortMode): ((a: ChatFolder, b: ChatFolder) => number) => {
  if (mode === 'name') {
    return (a, b) => naturalNameCompare(folderName(a), folderName(b)) || bySidebarOrder(a, b)
  }
  if (mode === 'created') {
    return (a, b) => {
      const ca = folderCreated(a)
      const cb = folderCreated(b)
      // A row with no stamp predates the stamp itself, so it is older than every
      // stamped row: newest first puts it last. Two unstamped rows fall through.
      if ((ca === null) !== (cb === null)) return ca === null ? 1 : -1
      if (ca !== null && cb !== null && ca !== cb) return cb - ca
      return bySidebarOrder(a, b)
    }
  }
  return bySidebarOrder
}

/**
 * A folder's `created_at` (epoch seconds, written by every folder creator since the
 * `created` mode existed) as a finite number, or `null` when the row has none.
 * Accepts exactly what `folderOrder` accepts and for the same reason: the Python
 * reader's `_chat_folder_created` takes the same set, so a junk value reads as
 * "no stamp" on both sides instead of sorting differently on each. Clamped to the
 * range JavaScript holds exactly so a hand-written stamp past it compares the same
 * as Python's unbounded int does after its own clamp.
 */
const folderCreated = (f: ChatFolder): number | null => {
  const v: unknown = f.created_at
  if (typeof v !== 'number' || !Number.isFinite(v)) return null
  return Math.max(-Number.MAX_SAFE_INTEGER, Math.min(Number.MAX_SAFE_INTEGER, v))
}

/**
 * Whether the `created` comparator can place this folder by date at all. A folder
 * from before the stamp existed has none, so in `created` mode it sorts after every
 * stamped row in the stored order — the person who picks the mode on an existing
 * tree sees the order they already had. The surfaces that offer the mode read this
 * to say so; it is the comparator's own rule (`folderCreated`), not a second one.
 */
export const folderHasCreatedStamp = (f: ChatFolder): boolean => folderCreated(f) !== null

/** Maximal runs of ASCII digits and of everything else, in order. `[0-9]` and not
 *  `\d`: `\d` is ASCII-only in JavaScript anyway, but the Python side's `str.isdigit`
 *  would not be, so the rule is spelled the same way on both sides. */
const NAME_RUNS = /[0-9]+|[^0-9]+/g

/**
 * Case-folded natural order for the `name` mode: `01.` < `02.` < `10.`, which is
 * what a person numbering folders means, and what code-unit order gets wrong
 * (`"10"` < `"2"`). Both inputs are already `folderName`-folded.
 *
 * The two names are cut into runs (see `NAME_RUNS`) and compared run by run. A
 * digit run against a digit run compares by VALUE — leading zeros stripped, then
 * length, then the digits themselves — so the number is never converted and a
 * long run cannot overflow. A text run against a text run compares by code unit,
 * exactly as `bySidebarOrder`'s tie-break does. A digit run sorts before a text run
 * at the same position. When every compared run is equal, the shorter name sorts
 * first; two names equal up to zero padding compare as 0 and the caller falls
 * through to `bySidebarOrder`.
 *
 * Mirrors `_chat_folder_natural_key` in `mcp_dashboard.py` operation for operation,
 * because `chat_folder_tree` in this mode must list what the sidebar draws; the
 * shared fixture's `name` cases are where that is checked.
 */
export const naturalNameCompare = (a: string, b: string): number => {
  const ra = a.match(NAME_RUNS) ?? []
  const rb = b.match(NAME_RUNS) ?? []
  const n = Math.min(ra.length, rb.length)
  for (let i = 0; i < n; i++) {
    const x = ra[i]
    const y = rb[i]
    const xDigits = x.charCodeAt(0) >= 48 && x.charCodeAt(0) <= 57
    const yDigits = y.charCodeAt(0) >= 48 && y.charCodeAt(0) <= 57
    if (xDigits !== yDigits) return xDigits ? -1 : 1
    if (xDigits) {
      const xs = x.replace(/^0+/, '')
      const ys = y.replace(/^0+/, '')
      if (xs.length !== ys.length) return xs.length - ys.length
      if (xs !== ys) return xs < ys ? -1 : 1
    } else if (x !== y) {
      return x < y ? -1 : 1
    }
  }
  return ra.length - rb.length
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
 * A folder's name as a STRING, for anything that will call a string method on it
 * or render it.
 *
 * `ChatFolder.name` is typed `string`, and at the type level this reader is
 * redundant — but the value comes from `folders.json` on disk, which a hand edit
 * or an older writer can leave holding a number, `null`, or an object. Every
 * consumer that then calls `.toLowerCase()`, `.split()` or `.length` on it throws,
 * and one throw inside a `useMemo` takes the whole sidebar down: the crash is in
 * render, so there is no row left to explain it and no way to clear the search box
 * that triggered it.
 *
 * Same rule as {@link folderName} above, and for the same reason: a non-string
 * reads as EMPTY rather than being stringified, so a malformed folder simply does
 * not match and does not highlight instead of matching the literal text
 * `[object Object]`.
 */
export const folderNameText = (f: ChatFolder): string =>
  typeof f.name === 'string' ? f.name : ''

/**
 * Flatten folders into pre-order (tree) sequence so children sit directly under
 * their parent, siblings sorted with the person's folder sort mode (`custom` =
 * stored `order` then name). Each entry carries its ancestor names (for breadcrumb
 * rendering) and depth (for indentation).
 * Orphans (parent_id pointing at a missing folder) are treated as roots.
 * Cycle/depth guarded.
 *
 * Shared by the folder pickers (move-to-folder submenu, new-chat-in-folder)
 * so the indented tree ordering stays identical everywhere — which is why the
 * mode is a parameter: a picker drawing the stored order beside a sidebar sorted
 * by name would show two sequences for one tree.
 */
export function orderFoldersWithPaths(
  folders: readonly ChatFolder[],
  mode: FolderSortMode = 'custom',
): OrderedFolder[] {
  const byId = new Map(folders.map(f => [f.id, f]))
  const compare = folderComparator(mode)
  const childrenOf = (pid: string) =>
    folders
      .filter(f => {
        const parent = f.parent_id && byId.has(f.parent_id) ? f.parent_id : ''
        return parent === pid
      })
      .sort(compare)

  const out: OrderedFolder[] = []
  const walk = (folder: ChatFolder, ancestors: string[], visited: Set<string>) => {
    if (visited.has(folder.id) || ancestors.length > 20) return
    visited.add(folder.id)
    // Read through the guard, not raw. `ancestors` and `path` leave this module and
    // are consumed as STRINGS: the launcher feeds each ancestor to `fuzzyMatch` as a
    // keyword, and that calls `.toLowerCase()` on its candidate — so one non-string
    // parent name off disk throws inside the caller's render memo and takes the
    // launcher down. Guarding at the call sites would leave the next consumer to
    // rediscover it; guarding here makes the contract "these are strings" true for
    // all four. It also stops `path` from coercing: `join` would render a numeric
    // name as `42`, which is exactly the stringification this module's own rule
    // rejects.
    const self = folderNameText(folder)
    out.push({
      folder,
      ancestors: [...ancestors],
      depth: ancestors.length,
      path: [...ancestors, self].join(FOLDER_PATH_SEP),
    })
    for (const child of childrenOf(folder.id)) walk(child, [...ancestors, self], visited)
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

/**
 * Does an unchecked ANCESTOR already account for this folder's absence?
 *
 * Unchecking a parent takes its whole block away, so the child is gone for the
 * parent's reason and not for its own. Both the reveal rows and the announced
 * count fold such a folder into its ancestor, which is what keeps one hide
 * announced exactly once instead of once per level.
 *
 * Cycle-guarded: a hand-edited folders.json can contain a `parent_id` loop, and
 * this runs on every render.
 */
export function coveredByHiddenAncestor(
  folder: ChatFolder,
  folders: readonly ChatFolder[],
  unchecked: ReadonlySet<string>,
): boolean {
  let cur = folder.parent_id ? folders.find(p => p.id === folder.parent_id) : undefined
  const seen = new Set<string>([folder.id])
  while (cur && !seen.has(cur.id)) {
    seen.add(cur.id)
    if (unchecked.has(cur.id)) return true
    cur = cur.parent_id ? folders.find(p => p.id === cur!.parent_id) : undefined
  }
  return false
}
