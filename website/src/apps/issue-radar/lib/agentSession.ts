// Shared "open an agent chat session for one GitHub item" orchestration, used
// by BOTH the issue Investigate action (lib/investigate.ts) and the pull-request
// Review action (lib/review.ts). Only the seed PROMPT and the slot TITLE differ
// between the two; every other step — resolve the per-repo chat folder, create a
// slot filed into it, seed + auto-run the first turn, link the local record so a
// repeat click RESUMES instead of duplicating, navigate to /chat — is identical,
// so it lives here once.
//
// This is deliberately SELF-CONTAINED — it touches no KiroCrew-core files. A
// first-party app runs inside the dashboard bundle, so it can dispatch the same
// Redux thunks (`createSlot`, `switchSlot`) and call the same `api` chat
// primitives the dashboard's own "New Chat" uses (verified precedents:
// file-explorer / auto-research import the store + api client directly).
//
// The per-item record is the SAME store on both sides
// (via /api/apps/issue-radar/investigation), NAMESPACED by item kind. On GitHub
// the namespace is shared and the filename keeps its historical
// ``investigation-{number}.json`` form: issues and pull requests are drawn from
// ONE number sequence per repo, so they cannot collide. GitLab numbers them
// independently — issue ``#5`` and merge request ``!5`` are unrelated items — so a
// change request passes ``kind: 'pull'`` and gets its own record. Omitting it
// there would make "Review MR !5" resume issue #5's session and overwrite its
// findings.
import { useCallback, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAppDispatch } from '../../../store'
import { createSlot, switchSlot, deleteSlot } from '../../../store/chatSlice'
import { api } from '../../../api/client'
import { issueRadarApi, type InvestigationRecord, type ItemKind, RepoRef } from '../api'

/** One folder per connected repo groups all its sessions. */
const FOLDER_PREFIX = 'Issue Radar - '
/** Keep the slot title short enough to read in the folder's session list. */
const TITLE_MAX = 48

export function truncate(s: string, max: number = TITLE_MAX): string {
  return s.length > max ? s.slice(0, max).trimEnd() + '…' : s
}

/** Resolve the "Issue Radar - <repo>" chat folder id, creating it on first use.
 * Matches by name — folders have no upsert. */
async function resolveFolderId(repo: string): Promise<string> {
  const name = `${FOLDER_PREFIX}${repo}`
  const folders = (await api.chatFolders()) as Array<{ id: string; name: string }>
  const existing = Array.isArray(folders) ? folders.find((f) => f.name === name) : undefined
  if (existing?.id) return existing.id
  const created = (await api.createChatFolder(name)) as { id: string }
  return created.id
}

/** One request to open (or resume) a session for one provider item. */
/** True when an error means the slot no longer exists (a 404 from the slot
 * detail fetch), as opposed to a transient failure reaching the gateway. */
function isMissingSlot(e: unknown): boolean {
  const msg = e instanceof Error ? e.message : String(e ?? '')
  return /\b404\b/.test(msg) || /not found/i.test(msg)
}

export interface OpenSessionArgs {
  repoRef: RepoRef
  /** Issue OR change-request number. */
  number: number
  /** Which sequence `number` belongs to. Defaults to `issue`; a change request
   * must pass `pull`, because on GitLab the two are numbered independently and a
   * shared record would resume the wrong session. */
  kind?: ItemKind
  /** Slot title, already formatted (e.g. "#123 · Fix the thing"). */
  title: string
  /** The fully-built seed prompt for the first turn. */
  prompt: string
  /** The item's existing record, when it has one (drives resume). */
  existing: InvestigationRecord | null
}

export interface UseAgentSession {
  /** Open (or resume) the session, then navigate to /chat. Returns the linked
   * record, or null on failure. */
  openSession: (args: OpenSessionArgs) => Promise<InvestigationRecord | null>
  busy: boolean
  error: Error | null
}

export function useAgentSession(): UseAgentSession {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<Error | null>(null)

  const openSession = useCallback(
    async ({ repoRef, number, kind = 'issue', title, prompt, existing }: OpenSessionArgs): Promise<InvestigationRecord | null> => {
      setBusy(true)
      // Set once a slot exists but is not yet linked to an investigation record;
      // cleared on success. See the rollback in the catch below.
      let createdSlotKey: string | null = null
      setError(null)
      try {
        // ── Resume: reattach to a still-live session. switchSlot fetches the
        // slot detail; a deleted slot 404s (the api client throws), so we fall
        // through to open a fresh one.
        if (existing?.slot_key) {
          // Only a slot that is genuinely GONE justifies opening a replacement.
          // Catching everything here turned any transient failure (network blip,
          // 500) into "the session was deleted", so a live session got orphaned
          // and its record overwritten. And saveInvestigation stays OUTSIDE this
          // fallback: a failed timestamp touch is not a reason to re-create the
          // session the user just resumed.
          let resumed = false
          try {
            await dispatch(switchSlot(existing.slot_key)).unwrap()
            resumed = true
          } catch (e) {
            if (!isMissingSlot(e)) throw e
          }
          if (resumed) {
            const res = await issueRadarApi.saveInvestigation(repoRef, number, {}, kind)
            navigate('/chat')
            return res.investigation
          }
        }

        // ── Fresh session: folder → slot (filed) → seed+run → link.
        const folderId = await resolveFolderId(repoRef.repo)
        const slot = await dispatch(createSlot({ folder_id: folderId })).unwrap()
        // The slot is persisted but not yet linked to an investigation record, so
        // a failure before the seed leaves an EMPTY session behind — and the next
        // attempt, finding no record, would create another one. Rollback covers
        // exactly that window and stops the moment the seed is in flight: once the
        // POST may have been accepted the agent is (or is about to be) running,
        // and deleting the slot would CANCEL the user's review over what may be a
        // transient metadata write failure. An unlinked-but-working session is
        // strictly better than a destroyed one.
        createdSlotKey = slot.key
        // Best-effort readable title; the session works regardless.
        api.renameSlot(slot.key, title).catch(() => {})
        // Seed + auto-run the first turn (background task; persisted + survives
        // the navigation). await ensures the user message is stored before we
        // switch, so it paints immediately on arrival.
        // api.sendChat hands back the raw fetch response, and fetch RESOLVES on
        // 4xx/5xx — so without this check a rejected prompt still got recorded and
        // navigated to, leaving a resumable but empty session.
        const seedInFlight = api.sendChat(prompt, slot.key)
        createdSlotKey = null
        const seeded = await seedInFlight
        if (seeded && typeof seeded === 'object' && 'ok' in seeded && !(seeded as Response).ok) {
          // Rejected outright, so nothing is running: the empty slot is safe (and
          // wrong) to remove.
          await dispatch(deleteSlot(slot.key)).unwrap().catch(() => {})
          throw new Error(`could not seed the session (HTTP ${(seeded as Response).status})`)
        }
        const res = await issueRadarApi.saveInvestigation(repoRef, number, {
          slot_key: slot.key,
          folder_id: folderId,
          status: 'investigating',
        }, kind)
        await dispatch(switchSlot(slot.key)).unwrap().catch(() => {})
        navigate('/chat')
        return res.investigation
      } catch (e) {
        // Only ever removes a slot whose agent turn was never started (see
        // createdSlotKey above), so a retry does not stack up empty sessions and a
        // running review is never destroyed. The original failure is what the user
        // needs to see, so a failed cleanup is swallowed rather than masking it.
        if (createdSlotKey) {
          await dispatch(deleteSlot(createdSlotKey)).unwrap().catch(() => {})
        }
        setError(e as Error)
        return null
      } finally {
        setBusy(false)
      }
    },
    [dispatch, navigate],
  )

  return { openSession, busy, error }
}
