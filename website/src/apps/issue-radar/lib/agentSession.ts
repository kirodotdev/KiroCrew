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
// the namespace is shared and the filename keeps its
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
import { issueRadarApi, InvestigationSlotConflictError, type InvestigationRecord, type ItemKind, type RecordVerb, RepoRef } from '../api'

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

/** Release a link THIS tab claimed, when its first turn definitively never started.
 *
 * The expectation is our own slot key, so this can only ever clear OUR link — if
 * another tab has since claimed the record, the write is refused rather than
 * stealing it. Best-effort: failing to release leaves a resumable-but-empty
 * session, which the user can still delete, so it must not mask the seed error
 * that brought us here.
 *
 * Clearing the link is a COMPLETE release because the claim writes nothing else —
 * see the claim site. If a field is ever added to that write, it belongs here too, or
 * an abandoned reservation starts leaving traces the record never had. */
async function releaseClaim(
  repoRef: RepoRef, number: number, kind: ItemKind, verb: RecordVerb | undefined, slotKey: string,
): Promise<void> {
  await issueRadarApi
    .saveInvestigation(repoRef, number, { slot_key: '' }, kind, verb, slotKey)
    .catch(() => {})
}

/** Send the first turn and insist it was ACCEPTED.
 *
 * `api.sendChat` hands back the raw fetch response, and fetch resolves on 4xx/5xx, so
 * an unchecked call reports a rejected prompt as a seeded session. Both seed sites (a
 * fresh session, and healing a reservation whose turn never landed) go through here so
 * neither can drift from the other. */
async function seedTurn(prompt: string, slotKey: string): Promise<void> {
  const res: unknown = await api.sendChat(prompt, slotKey)
  if (res && typeof res === 'object' && 'ok' in res && !(res as Response).ok) {
    throw new Error(`could not seed the session (HTTP ${(res as Response).status})`)
  }
}

/** True once the slot has a turn (or one is running) — i.e. the seed landed.
 *
 * A thrown `sendChat` does NOT say whether the POST was accepted, and the two
 * outcomes need opposite handling: a started session must never be destroyed, while
 * an unstarted one must not keep the record's link or every retry resumes a session
 * that never receives the prompt. So the slot itself is asked. An unanswerable probe
 * counts as STARTED, because losing a running session is worse than leaving one
 * empty session behind. */
async function seedLanded(slotKey: string): Promise<boolean> {
  try {
    const detail = (await api.chatSlotDetail(slotKey)) as {
      messages?: unknown[]
      running?: boolean
    }
    return (detail?.messages?.length ?? 0) > 0 || detail?.running === true
  } catch {
    return true
  }
}

export interface OpenSessionArgs {
  repoRef: RepoRef
  /** Issue OR change-request number. */
  number: number
  /** Which sequence `number` belongs to. Defaults to `issue`; a change request
   * must pass `pull`, because on GitLab the two are numbered independently and a
   * shared record would resume the wrong session. */
  kind?: ItemKind
  /** Which SESSION VERB this session is, when the item can carry more than one at
   * a time. Omitted means the item's primary record. Two verbs sharing a record
   * would share one `slot_key`, so the second click would resume the first verb's
   * session and overwrite its link. */
  verb?: RecordVerb
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
    async ({ repoRef, number, kind = 'issue', verb, title, prompt, existing }: OpenSessionArgs): Promise<InvestigationRecord | null> => {
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
          // A LINKED SLOT THAT STILL EXISTS IS ALWAYS RESUMED, never repaired.
          //
          // A reservation whose first turn never landed does leave the record pointing
          // at a turn-less slot, and repairing that automatically is not possible from
          // here: an empty slot is indistinguishable from one whose seed is in flight in
          // another tab. Both look like "no turn yet". Acting on that guess deletes a
          // session that is starting, and since this verb PUSHES COMMITS AND POSTS
          // REPLIES, two agents racing on one change request is far worse than one
          // session the user has to clear by hand.
          //
          // Telling the two apart needs the reservation to carry when it was made, so a
          // young claim reads as active and an old one as abandoned. That is a
          // server-side lease, not a client heuristic, so this path resumes and leaves
          // the empty session visible rather than pretending it can decide.
          if (resumed) {
            const res = await issueRadarApi.saveInvestigation(repoRef, number, {}, kind, verb)
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

        // CLAIM THE RECORD BEFORE SEEDING. A re-entry guard lives in one tab and
        // cannot see a click in another, so the only thing that can order two tabs
        // is the record itself: the write proceeds only if the stored link is still
        // what this tab last saw. Claiming first is what makes losing SAFE — the
        // slot has no turn yet, so it can be removed, whereas a claim after the
        // seed would leave a running agent to either destroy or orphan.
        //
        // The claim writes LINK FIELDS ONLY — never `status`, `verdict` or any other
        // state a reader sees as the item's own truth. That is what makes releasing it
        // complete: the reservation can be abandoned by clearing the link, with nothing
        // left over to restore. Writing the lifecycle here instead would mean a
        // released claim had to also undo a status the record may never have had, and a
        // rejected seed would strand a finished item reading as `investigating`. The
        // link fields are inert without `slot_key` and are rewritten by the next
        // attempt, so they carry no such claim on the truth.
        let claimed: InvestigationRecord | null
        try {
          const res = await issueRadarApi.saveInvestigation(repoRef, number, {
            slot_key: slot.key,
            folder_id: folderId,
          }, kind, verb, existing?.slot_key ?? null)
          claimed = res.investigation
        } catch (e) {
          // Typed rather than message-sniffed: the api client parses the 409 body
          // and hands back the live record.
          if (!(e instanceof InvestigationSlotConflictError)) throw e
          const winner = e.current
          if (!winner?.slot_key) throw e
          // Another tab won. Drop this tab's unseeded slot and adopt the winner's
          // session, which is what the user wanted either way.
          await dispatch(deleteSlot(slot.key)).unwrap().catch(() => {})
          createdSlotKey = null
          await dispatch(switchSlot(winner.slot_key)).unwrap()
          navigate('/chat')
          return winner
        }

        // Seed + auto-run the first turn (background task; persisted + survives
        // the navigation). await ensures the user message is stored before we
        // switch, so it paints immediately on arrival.
        //
        // Because the link is claimed ABOVE, a seed that never starts must also
        // release that link — otherwise the record points at a slot with no turn,
        // and every later click resumes that empty session instead of starting the
        // work. That is the one cost of claiming first, and it is paid here.
        try {
          const seedInFlight = seedTurn(prompt, slot.key)
          createdSlotKey = null
          await seedInFlight
        } catch (e) {
          // A throw does not say whether the POST landed, so ask the slot.
          if (!(await seedLanded(slot.key))) {
            await dispatch(deleteSlot(slot.key)).unwrap().catch(() => {})
            await releaseClaim(repoRef, number, kind, verb, slot.key)
          }
          throw e
        }
        // The session is running, so NOW the item is genuinely under investigation.
        // Guarded by this tab's own slot key, so a record another tab has since taken
        // over is not restamped. Best-effort: the work is already under way, and
        // failing the call over a metadata write would report a session that exists as
        // a failure and invite the user to start a second one.
        const stamp = () => issueRadarApi.saveInvestigation(
          repoRef, number, { status: 'investigating' }, kind, verb, slot.key,
        )
        let running: InvestigationRecord | null = claimed
        try {
          running = (await stamp()).investigation
        } catch {
          try {
            running = (await stamp()).investigation
          } catch {
            // The write did not land, but the agent IS running — and the record it
            // failed to overwrite may say `resolved` from a previous pass. Returning
            // that unchanged would show the row as finished over work that just
            // started, so the lifecycle we know to be true is reported locally and the
            // next successful read reconciles the stored copy.
            running = claimed ? { ...claimed, status: 'investigating' } : claimed
          }
        }

        await dispatch(switchSlot(slot.key)).unwrap().catch(() => {})
        navigate('/chat')
        return running
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
