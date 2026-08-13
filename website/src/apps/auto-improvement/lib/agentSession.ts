// Resumable chat sessions for auto-improvement subjects (pull requests,
// findings, the ruler, a run).
//
// The app this was ported from had exactly ONE chat affordance:
// `useChatLauncher().openChat({message})`, which always started a brand-new
// session and returned nothing. Clicking "Discuss this CR" twice gave you two
// unrelated conversations and no way back to the first, so context was lost
// every time.
//
// Here each subject gets a DURABLE session link: the slot key is persisted by
// the app backend (PUT /api/apps/auto-improvement/sessions/<key>), so a repeat
// click RESUMES the same conversation. Sessions are filed into one chat folder
// per repository, which keeps a night's run from scattering loose sessions
// through the sidebar.
//
// Deliberately self-contained and touching no core files: a first-party app runs
// inside the dashboard bundle, so it can dispatch the same Redux thunks and call
// the same `api` chat primitives the dashboard's own "New Chat" uses. Established
// precedent: issue-radar, file-explorer, and auto-research all import the store
// and api client directly.
import { useCallback, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { api } from '../../../api/client'
import { useAppDispatch } from '../../../store'
import { createSlot, deleteSlot, switchSlot } from '../../../store/chatSlice'

/** One folder per target repository groups every session from its runs. */
const FOLDER_PREFIX = 'Auto-Improve - '
/** Keep slot titles short enough to read in the folder's session list. */
const TITLE_MAX = 48

/** The kinds of thing a session can be about. Part of the record key, so two
 *  subjects of different kinds can share a number without colliding. */
export type SubjectKind = 'pr' | 'finding' | 'ruler' | 'run'

export interface SessionRecord {
  key: string
  slot_key?: string
  folder_id?: string
  status?: string
  subject?: string
  title?: string
  url?: string
}

export function truncate(s: string, max: number = TITLE_MAX): string {
  return s.length > max ? s.slice(0, max).trimEnd() + '…' : s
}

/** Strip a component down to something safe to put in a filename.
 *
 *  Path syntax is stripped rather than escaped: a run of dots collapses away
 *  entirely instead of surviving as `..-..-`, which is what a naive
 *  character-class filter leaves behind. */
function safeSegment(raw: string, fallback: string): string {
  const safe = raw
    // Drop path separators and dot-runs first: '../../etc' must not become
    // '..-..-etc'. A single dot inside a token (a version, a hash) is kept.
    .replace(/\.{2,}/g, '')
    .replace(/[/\\]+/g, '-')
    .replace(/[^A-Za-z0-9._-]+/g, '-')
    // Leading dots/dashes are meaningless here and a leading dot hides a file.
    .replace(/^[.\-]+|[.\-]+$/g, '')
  return safe || fallback
}

/** A deterministic 8-hex-char fingerprint of a raw string.
 *
 *  `safeSegment` is LOSSY on purpose (it drops path syntax), so two distinct raw
 *  values can collapse to the same safe segment — `team/service-api` and
 *  `team-service/api` both become `team-service-api`. When that segment is a chat
 *  session key, the collision RESUMES the wrong repository's conversation. Appending
 *  this fingerprint of the RAW value makes the mapping injective again while keeping
 *  the readable prefix and staying inside the `[A-Za-z0-9._-]` charset the backend
 *  re-validates. FNV-1a, 32-bit prime (`Math.imul` truncates operands to 32-bit, so
 *  the 64-bit prime would silently collapse — same caveat as `lib/widgetSlug.ts`);
 *  no crypto properties needed, only collision resistance for distinct inputs. */
function fingerprint(raw: string): string {
  let h = 0x811c9dc5 >>> 0
  for (let i = 0; i < raw.length; i++) {
    h = Math.imul(h ^ raw.charCodeAt(i), 0x01000193) >>> 0
  }
  const HEX = '0123456789abcdef'
  let out = ''
  for (let shift = 28; shift >= 0; shift -= 4) out += HEX[(h >> shift) & 0xf]
  return out
}

/** A filename-safe, INJECTIVE encoding of a component: the readable safe segment
 *  plus a fingerprint of the RAW input, so distinct raw values never share a key
 *  even when their safe segments collide. */
function keySegment(raw: string, fallback: string): string {
  return `${safeSegment(raw, fallback)}.${fingerprint(raw)}`
}

/** Stable record key for a subject. Kind-namespaced so `finding/7` and `pr/7`
 *  are different conversations, and REPO-namespaced so two repositories do not
 *  share one.
 *
 *  The repo segment is not decoration. Session records live at the data ROOT, not
 *  under the per-repository workspace (`store.sessions_dir`: "a chat session ...
 *  may reference any repo, so it is not scoped to the active one"), so that root is
 *  shared by every target the app is ever pointed at. Without the repo, discussing
 *  repo A's PR #1 and then repo B's PR #1 resumes A's conversation about a
 *  different pull request — and `ruler`/`run` subjects, whose ids are constants like
 *  `current`, collided for EVERY repository at once.
 *
 *  The key becomes a filename on the backend, which validates it again and rejects
 *  anything unsafe — this is the first of two gates, not the only one. */
export function sessionKey(kind: SubjectKind, id: string | number, repo: string): string {
  // `keySegment` (not `safeSegment`): the safe form is lossy, so `team/service-api`
  // and `team-service/api` would otherwise produce the same key and cross-resume each
  // other's chat. The fingerprint of the raw value keeps distinct repos/ids distinct.
  return `${kind}-${keySegment(repo, 'norepo')}-${keySegment(String(id), 'unknown')}`
}

const API = '/api/apps/auto-improvement'

async function loadRecord(key: string): Promise<SessionRecord | null> {
  const res = await fetch(`${API}/sessions/${encodeURIComponent(key)}`)
  if (!res.ok) return null
  const body = (await res.json()) as { session?: SessionRecord | null }
  return body.session ?? null
}

async function saveRecord(key: string, patch: Partial<SessionRecord>): Promise<SessionRecord | null> {
  const res = await fetch(`${API}/sessions/${encodeURIComponent(key)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  })
  if (!res.ok) return null
  const body = (await res.json()) as { session?: SessionRecord | null }
  return body.session ?? null
}

/** Resolve the "Auto-Improve - <repo>" folder id, creating it on first use.
 *  Matched by name because folders have no upsert endpoint. */
async function resolveFolderId(repo: string): Promise<string> {
  const name = `${FOLDER_PREFIX}${repo}`
  const folders = (await api.chatFolders()) as Array<{ id: string; name: string }>
  const existing = Array.isArray(folders) ? folders.find((f) => f.name === name) : undefined
  if (existing?.id) return existing.id
  const created = (await api.createChatFolder(name)) as { id: string }
  return created.id
}

/** True when an error means the slot is genuinely gone (404), as opposed to a
 *  transient failure reaching the gateway. Only the former justifies replacing
 *  a session; treating a network blip as "deleted" would orphan a live one. */
function isMissingSlot(e: unknown): boolean {
  const msg = e instanceof Error ? e.message : String(e ?? '')
  return /\b404\b/.test(msg) || /not found/i.test(msg)
}

export interface OpenSessionArgs {
  kind: SubjectKind
  /** Subject id — a PR number, a finding fingerprint, a run id. */
  id: string | number
  /** Repository slug ("owner/name") used for the folder. */
  repo: string
  /** Slot title, already formatted (e.g. "PR #42 · Speed up the parser"). */
  title: string
  /** The fully-built seed prompt for the first turn. */
  prompt: string
  /** Optional subject URL, stored on the record for link-out. */
  url?: string
}

export interface UseAgentSession {
  /** Open or resume the session, then navigate to /chat. Returns the record, or
   *  null if it could not be started. */
  openSession: (args: OpenSessionArgs) => Promise<SessionRecord | null>
  busy: boolean
  error: Error | null
}

export function useAgentSession(): UseAgentSession {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  // SYNCHRONOUS re-entry latch, keyed by session. `setBusy(true)` cannot serve as one:
  // React state is applied asynchronously, so a rapid double-click runs `openSession`
  // twice before either render lands — both see "no record", both create a seeded slot,
  // and the second `saveRecord` overwrites the first slot mapping, orphaning a live
  // conversation. A ref mutates immediately, which is what closes the window. Keyed
  // rather than a single boolean so opening two DIFFERENT subjects stays parallel.
  // Raised by the GPT review.
  const inFlight = useRef<Set<string>>(new Set())

  const openSession = useCallback(
    async ({ kind, id, repo, title, prompt, url }: OpenSessionArgs): Promise<SessionRecord | null> => {
      const key = sessionKey(kind, id, repo)
      if (inFlight.current.has(key)) return null
      inFlight.current.add(key)
      setBusy(true)
      setError(null)
      // Set once a slot exists but is not yet linked to a record; cleared as soon
      // as the seed is in flight. See the rollback in the catch below.
      let createdSlotKey: string | null = null
      try {
        // Resume: reattach to a still-live session. switchSlot fetches the slot
        // detail, so a deleted slot 404s and we fall through to a fresh one.
        const existing = await loadRecord(key)
        if (existing?.slot_key) {
          let resumed = false
          try {
            await dispatch(switchSlot(existing.slot_key)).unwrap()
            resumed = true
          } catch (e) {
            if (!isMissingSlot(e)) throw e
          }
          if (resumed) {
            navigate('/chat')
            return existing
          }
        }

        // Fresh session: folder -> slot (filed) -> seed and run -> link.
        const folderId = await resolveFolderId(repo)
        const slot = await dispatch(createSlot({ folder_id: folderId })).unwrap()
        // The slot is persisted but not yet linked, so a failure before the seed
        // would leave an empty session that the next click cannot find. Rollback
        // covers exactly that window and stops the moment the seed is in flight:
        // once the POST may have been accepted the agent is starting, and
        // deleting the slot would cancel real work over a metadata hiccup.
        createdSlotKey = slot.key
        // Best-effort readable title; the session works regardless.
        api.renameSlot(slot.key, truncate(title)).catch(() => {})
        const seedInFlight = api.sendChat(prompt, slot.key)
        createdSlotKey = null
        const seeded = await seedInFlight
        // fetch RESOLVES on 4xx/5xx, so without this check a rejected prompt
        // would still be recorded and navigated to, leaving an empty session.
        if (seeded && typeof seeded === 'object' && 'ok' in seeded && !(seeded as Response).ok) {
          await dispatch(deleteSlot(slot.key)).unwrap().catch(() => {})
          throw new Error(`could not seed the session (HTTP ${(seeded as Response).status})`)
        }
        const record = await saveRecord(key, {
          slot_key: slot.key,
          folder_id: folderId,
          status: 'open',
          subject: String(id),
          title,
          url,
        })
        await dispatch(switchSlot(slot.key)).unwrap().catch(() => {})
        navigate('/chat')
        return record
      } catch (e) {
        // Only ever removes a slot whose first turn never started, so a retry
        // cannot stack up empty sessions and a running one is never destroyed.
        // The original failure is what matters, so a failed cleanup is swallowed.
        if (createdSlotKey) {
          await dispatch(deleteSlot(createdSlotKey)).unwrap().catch(() => {})
        }
        setError(e as Error)
        return null
      } finally {
        // Release the latch on EVERY exit (resume, fresh, throw) or the subject can
        // never be opened again for the lifetime of the hook.
        inFlight.current.delete(key)
        setBusy(false)
      }
    },
    [dispatch, navigate],
  )

  return { openSession, busy, error }
}
