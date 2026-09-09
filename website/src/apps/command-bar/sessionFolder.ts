/**
 * Where a contributed command's session lands in the sidebar.
 *
 * A contributed row opens a NEW session every time it runs, and those sessions are
 * generated work: a link pasted into a template, not a conversation the reader
 * started. Left unfiled they pile up at the top level and push the reader's own
 * chats down, and two different commands' runs interleave there with nothing
 * separating them. So each command gets a folder of its own, under one parent that
 * says where all of them came from.
 *
 * The folders are matched BY NAME on every run rather than remembered by id: there
 * is nowhere durable to keep an id (the launcher holds no per-command state), and a
 * name lookup is also what lets the reader rename or move a folder without the next
 * run recreating the old one somewhere else — a renamed leaf simply stops matching
 * and a fresh one is made, which is visible and undoable, where a remembered id
 * would silently refile into a folder they had deliberately moved on from.
 *
 * Everything here is BEST-EFFORT by construction: filing is cosmetic, and the caller
 * runs it only after the prompt is already seeded, so no failure in this module can
 * cost the reader the text they pasted.
 *
 * **Keep this module copy-free.** It is exempt from the i18n literal rule in
 * `eslint.i18n.config.js` because the one string it holds is a durable value the
 * server stores and this code later matches by name. Any reader-facing COPY added
 * here would inherit that exemption silently — put copy in the component.
 */

import { api } from '../../api/client'

/** The subset of a `GET /api/chat/folders` row this module reads. */
export interface ChatFolderRow {
  id?: string
  name?: string
  parent_id?: string
}

/**
 * Parent folder every contributed-command session is filed under.
 *
 * Deliberately NOT localized, and that is the point rather than an oversight. This
 * string is written to the server as a folder's name and matched by that name on the
 * next run, so a translated copy would create a second folder the moment the reader
 * switches language — leaving every earlier session stranded under a name nothing
 * looks for any more. A durable identifier that happens to be readable is not UI
 * copy, which is the same category `issue-radar/lib/wireValues.ts` is exempted for.
 */
export const COMMAND_SESSION_FOLDER = 'Command Bar Sessions'

/**
 * Longest folder name the server keeps, mirroring `chat_folders.py`, which stores
 * `name.strip()[:100]` on both create and rename.
 *
 * Matching has to be done against the name the server STORED, not the one we asked
 * for. Without this, a command title longer than the limit is silently shortened on
 * create, the next run's lookup for the full title misses, and every single run makes
 * another folder — the one failure here that compounds instead of staying cosmetic.
 * A manifest title may be up to 120 characters, so the gap is reachable rather than
 * theoretical.
 */
const SERVER_NAME_LIMIT = 100

/** The name the server will actually store for `raw`. */
function storedName(raw: string): string {
  // Truncated by CODE POINT, not by UTF-16 unit. `slice` counts units, so a title whose
  // 100th unit falls inside a surrogate pair loses half a character and the name carries
  // a lone surrogate -- an invalid string, persisted. It also happens to be what the
  // server means: Python slices its own strings by code point.
  return [...raw.trim()].slice(0, SERVER_NAME_LIMIT).join('')
}

/** Same rule, for the bounded tag. */
function boundedTag(raw: string): string {
  return [...raw].slice(0, LABEL_ROOM).join('')
}

/** Rows only; a non-array response (an error envelope) yields nothing to match. */
function rows(value: unknown): ChatFolderRow[] {
  return Array.isArray(value) ? (value as ChatFolderRow[]) : []
}

/** The fields of a contributed row that decide what its folder is called. */
export interface NamedCommand {
  id: string
  title: string
  appLabel: string
}

/**
 * Longest readable tag the discriminator carries.
 *
 * Bounded so the ` (tag)` suffix always fits inside `SERVER_NAME_LIMIT` with room left
 * for the title. Without a bound a long label could consume the whole stored name and
 * leave nothing of the title the reader is looking for.
 */
const LABEL_ROOM = 32

/**
 * A short, fixed-width, deterministic tag for a row id.
 *
 * The escape hatch for when a READABLE tag cannot separate two rows — two app labels
 * that agree on their first `LABEL_ROOM` characters, say. Row ids are unique by
 * construction (`app:<app>:<command>`), so hashing the whole id is the one
 * discriminator that no amount of bounding can collapse. FNV-1a: not cryptographic,
 * which this is not for, and eight hex characters is a small enough field to sit in a
 * folder name.
 */
function idTag(id: string): string {
  let hash = 0x811c9dc5
  for (let i = 0; i < id.length; i++) {
    hash ^= id.charCodeAt(i)
    hash = Math.imul(hash, 0x01000193) >>> 0
  }
  return hash.toString(16).padStart(8, '0')
}

/**
 * What to call the leaf folder for the command with this row id.
 *
 * The title, because that is what the reader picked and what they will look for in the
 * sidebar. But a title is not unique: nothing stops two apps contributing `Review all
 * PRs`, and a leaf keyed on the title alone would then interleave their runs — the
 * exact thing this filing exists to prevent.
 *
 * So a discriminator is appended ONLY when another row currently offered carries the
 * same title: the contributing app's label between two apps, and the row's own id when
 * the collision is inside ONE app, where the label separates nothing. Appending one
 * always would put a redundant parenthesis on every folder for a collision almost
 * nobody has; appending it never would silently merge the two that do. Which rows are
 * "currently offered" is the same set the launcher renders, so a collision that appears
 * when a second app is installed renames nothing already filed — the old leaf simply
 * stops matching, and the reader sees a new one appear beside it rather than two
 * commands quietly sharing one.
 *
 * The suffix is given its own room inside the stored limit rather than being appended
 * and clamped afterwards. Clamping the finished string slices the discriminator off in
 * exactly the case it exists for: two colliding titles at or over the limit differ only
 * in the suffix, so cutting it puts them back in one leaf.
 *
 * Returns `''` when the id names no row, or its title is blank — the caller files
 * nothing in that case.
 */
export function commandFolderName(commands: ReadonlyMap<string, NamedCommand>, id: string): string {
  const command = commands.get(id)
  const title = command?.title.trim() ?? ''
  if (!command || !title) return ''
  // Grouped by the name the SERVER will keep, not the raw title. Two distinct titles
  // that agree on their first 100 characters are different strings here and identical
  // ones there, so comparing raw titles finds no collision, adds no discriminator, and
  // lets the server file both commands into one folder -- the failure the discriminator
  // exists to prevent, reached without any duplicate title at all.
  const stored = storedName(title)
  const colliding = [...commands.values()].filter(
    other => other.id !== command.id && storedName(other.title) === stored,
  )
  if (colliding.length === 0) return title
  const group = [command, ...colliding]
  // Ordered so every member of the group derives the SAME answer for every other
  // member, which is what makes the uniqueness test below mean anything.
  const ordered = [...group].sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))
  // A readable tag is preferred, but only while it actually separates the rows it has
  // to. The app label between two apps; the row's own command id when the collision is
  // inside ONE app, where the label separates nothing. Both are bounded to keep the
  // title visible, and bounding can make either collide in turn -- two app labels
  // agreeing on their first 32 characters, say.
  const readable = (of: NamedCommand): string =>
    boundedTag(
      ordered.some(other => other.id !== of.id && other.appLabel.trim() === of.appLabel.trim())
        ? of.id.split(':').pop() || of.id
        : of.appLabel.trim(),
    )
  // Three tiers, each tried only when the one above fails to distinguish anything, so
  // uniqueness inside the group is GUARANTEED rather than likely: a readable tag, then a
  // hash of the whole row id, then that hash plus the row's position in the ordered
  // group. Row ids are unique by construction, so the third tier always separates --
  // it exists because a hash is a compression and two ids CAN share one, however
  // unlikely, and "unlikely" is not the same claim as "cannot".
  const tag = (of: NamedCommand): string => {
    const plain = readable(of)
    if (plain.length > 0 && ordered.filter(other => readable(other) === plain).length === 1) {
      return plain
    }
    const hashed = idTag(of.id)
    if (ordered.filter(other => idTag(other.id) === hashed).length === 1) return hashed
    return `${hashed}-${ordered.findIndex(other => other.id === of.id) + 1}`
  }
  const suffix = ` (${tag(command)})`
  return `${storedName(`${[...title].slice(0, Math.max(1, SERVER_NAME_LIMIT - suffix.length)).join('')}${suffix}`)}`
}

/**
 * A folder with this exact name under this exact parent.
 *
 * The parent is compared as well as the name, so a leaf the reader happens to have
 * named `Approve and merge all PRs` somewhere else in their tree is not mistaken for
 * ours. An absent `parent_id` is the top level, which is how the backend spells it.
 */
function folderAt(list: ChatFolderRow[], name: string, parentId: string): ChatFolderRow | undefined {
  return list.find(f => f?.name === name && String(f?.parent_id ?? '') === parentId)
}

/**
 * File `slotKey` under `Command Bar Sessions / <commandTitle>`, creating whichever of
 * the two folders does not exist yet.
 *
 * `cached` is the sidebar's own `['chat-folders']` list, passed in rather than fetched.
 * `GET /api/chat/folders` walks the on-disk session list synchronously to count
 * archived sessions per folder — a cost that scales with how much history the reader
 * has — and the dashboard already keeps that list in a cache the WebSocket seeds. A
 * fetch here would pay for a scan per command run to learn what is already known.
 *
 * A cache MISS is never trusted, though, and that asymmetry is the point. A hit is
 * proof the folder exists; a miss might only mean the cache has not heard about a
 * folder an earlier run created — a warm cache stale about exactly that folder (a
 * dropped WebSocket) would otherwise make a duplicate of it on every run. So the first
 * miss spends one authoritative read and re-checks before anything is created. The
 * common path still issues no request at all, and the cold-start path is the same one
 * read.
 *
 * Returns the leaf folder id on success and `null` on every failure — a refused
 * create, a rate limit, a folder cap, an offline gateway. Nothing is rethrown: the
 * session and its prompt are already in place by the time this runs, and a rejected
 * promise here would surface as an unhandled rejection for an outcome the reader can
 * fix with one drag.
 *
 * Two known residuals, both cosmetic and both left alone deliberately:
 *
 * - The leaf is found by name, so renaming or moving it makes the next run create a
 *   fresh one rather than refiling into wherever the reader moved the old one.
 * - Two tabs launching at the same instant can each see the parent missing and create
 *   it twice, splitting runs across identically named trees. Only an atomic
 *   get-or-create on the endpoint would close that, and it does not exist; serializing
 *   here would put a lock in front of the reader's session appearing at all.
 */
export async function fileSessionInCommandFolder(
  slotKey: string,
  commandTitle: string,
  cached?: readonly ChatFolderRow[],
  onFolderCreated?: () => void,
): Promise<string | null> {
  const leafName = storedName(commandTitle)
  if (!slotKey || !leafName) return null
  try {
    let list = rows(cached)
    let read = false
    if (list.length === 0) {
      list = rows(await api.chatFolders())
      read = true
    }
    let madeOne = false

    /** Existing folder, or a freshly created one; `null` when neither yields an id. */
    const resolve = async (name: string, parentId: string): Promise<string | null> => {
      const hit = folderAt(list, name, parentId)
      if (hit?.id) return hit.id
      if (!read) {
        // The one re-read, spent on the first miss rather than on every run.
        read = true
        list = rows(await api.chatFolders())
        const fresh = folderAt(list, name, parentId)
        if (fresh?.id) return fresh.id
      }
      const created = (await api.createChatFolder(name, parentId || undefined)) as ChatFolderRow | null
      if (!created?.id) return null
      madeOne = true
      // Recorded so the leaf lookup below sees the parent this call just made, without
      // a third read to learn about it.
      list = [...list, { id: created.id, name, parent_id: parentId }]
      return created.id
    }

    const parentId = await resolve(storedName(COMMAND_SESSION_FOLDER), '')
    if (!parentId) return null
    const leafId = await resolve(leafName, parentId)
    if (!leafId) return null
    await api.setSlotFolder(slotKey, leafId)
    // Told AFTER the write lands, and only when this run actually created something: the
    // cache the caller read from does not know about it, and the push that would seed it
    // is not guaranteed to arrive.
    if (madeOne) onFolderCreated?.()
    return leafId
  } catch {
    return null
  }
}
