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
  return raw.trim().slice(0, SERVER_NAME_LIMIT)
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
 * What to call the leaf folder for the command with this row id.
 *
 * The title, because that is what the reader picked and what they will look for in the
 * sidebar. But a title is not unique: nothing stops two apps contributing `Review all
 * PRs`, and a leaf keyed on the title alone would then interleave their runs — the
 * exact thing this filing exists to prevent.
 *
 * So the app label is appended ONLY when another row currently offered carries the
 * same title. Appending it always would put a redundant parenthesis on every folder
 * for a collision almost nobody has; appending it never would silently merge the two
 * that do. Which rows are "currently offered" is the same set the launcher renders, so
 * a collision that appears when a second app is installed renames nothing already
 * filed — the old leaf simply stops matching, and the reader sees a new one appear
 * beside it rather than two commands quietly sharing one.
 *
 * Returns `''` when the id names no row, or its title is blank — the caller files
 * nothing in that case.
 */
export function commandFolderName(commands: ReadonlyMap<string, NamedCommand>, id: string): string {
  const command = commands.get(id)
  const title = command?.title.trim() ?? ''
  if (!command || !title) return ''
  for (const other of commands.values()) {
    if (other.id !== command.id && other.title.trim() === title) {
      return `${title} (${command.appLabel})`
    }
  }
  return title
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

/** Existing folder, or a freshly created one; `null` when neither yields an id. */
async function ensureFolder(
  list: ChatFolderRow[],
  name: string,
  parentId: string,
): Promise<string | null> {
  const found = folderAt(list, name, parentId)
  if (found?.id) return found.id
  const created = (await api.createChatFolder(name, parentId || undefined)) as ChatFolderRow | null
  return created?.id || null
}

/**
 * File `slotKey` under `Command Bar Sessions / <commandTitle>`, creating whichever of
 * the two folders does not exist yet.
 *
 * `cached` is the sidebar's own `['chat-folders']` list, passed in rather than fetched.
 * `GET /api/chat/folders` walks the on-disk session list synchronously to count
 * archived sessions per folder — a cost that scales with how much history the reader
 * has — and the dashboard already keeps that list in a cache the WebSocket seeds. A
 * fetch here would pay for a scan per command run to learn what is already known. The
 * fetch remains only as the cold-start fallback, for a launcher used before anything
 * has populated that cache.
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
 *   it twice, splitting runs across identically named trees. Serializing that would
 *   put a lock in front of the reader's session appearing at all, and the recovery is
 *   one drag.
 */
export async function fileSessionInCommandFolder(
  slotKey: string,
  commandTitle: string,
  cached?: readonly ChatFolderRow[],
): Promise<string | null> {
  const leafName = storedName(commandTitle)
  if (!slotKey || !leafName) return null
  try {
    const list = cached && cached.length > 0 ? rows(cached) : rows(await api.chatFolders())
    const parentId = await ensureFolder(list, storedName(COMMAND_SESSION_FOLDER), '')
    if (!parentId) return null
    // Matched against the SAME listing: when the parent was just created it has no
    // children yet, so a miss here is correct rather than stale.
    const leafId = await ensureFolder(list, leafName, parentId)
    if (!leafId) return null
    await api.setSlotFolder(slotKey, leafId)
    return leafId
  } catch {
    return null
  }
}
