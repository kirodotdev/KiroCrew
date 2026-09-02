// Shared "open an agent chat session for one GitHub item" orchestration, used
// by BOTH the issue Investigate action (lib/investigate.ts) and the pull-request
// Review action (lib/review.ts). Only the seed PROMPT and the slot TITLE differ
// between the two; every other step — resolve the per-repo chat folder, create a
// slot filed into it and already titled, seed + auto-run the first turn, link the
// local record so a repeat click RESUMES instead of duplicating, navigate to
// /chat — is identical, so it lives here once.
//
// This deliberately FORKS NOTHING — a first-party app runs inside the dashboard
// bundle, so it dispatches the same Redux thunks (`createSlot`, `switchSlot`) and
// calls the same `api` chat primitives the dashboard's own "New Chat" uses
// (verified precedents: file-explorer / auto-research import the store + api
// client directly). Where a primitive is missing something this flow needs, the
// fix belongs in that primitive rather than in a private copy here.
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
import { readSendReceipt } from '../../../utils/sendDelivery'
import { isMissingSlotError } from '../../../utils/thunkError'
import { issueRadarApi, type InvestigationRecord, type ItemKind, RepoRef } from '../api'
import { repoScopeKey } from './links'

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

/** Identity of one investigable item, for state that must not follow the user to
 * a different one.
 *
 * The detail pane is NOT keyed by the selected item (`Workspace.tsx` renders
 * `<IssueDetail issue={activeIssue} />` with no `key`), so selecting another issue
 * re-renders the SAME component instance with a new prop. A bare boolean "this
 * click was declined" therefore survives the switch, and the next item would
 * render its re-run affordance and force past the guard on a first click -- on an
 * item nobody declined. Storing WHICH item was declined and comparing at render
 * closes that without an effect, so there is no window where the wrong label is
 * painted. */
export function itemKey(repoRef: RepoRef, number: number, kind: ItemKind = 'issue'): string {
  return `${repoScopeKey(repoRef)}#${kind}:${number}`
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
  /** Open a replacement session even though the item's work already CONCLUDED.
   *
   * Off by default, and that default is the point: a concluded record whose
   * session has been closed must not silently start the work over (see the
   * concluded branch below). The UI sets this only from a second, explicit
   * click, so re-doing finished work is always something the user asked for. */
  force?: boolean
}

export interface UseAgentSession {
  /** Open (or resume) the session, then navigate to /chat. Returns the linked
  /** Open (or resume) the session, then navigate to /chat. Returns the linked
   * record, or null on failure.
   *
   * A DECLINED click also returns a record -- the freshly-read one -- because the
   * callers' only use of the return value is to write it into their query cache,
   * and a decline is precisely the moment their cached copy has been proven
   * stale. So null-ness does not mean "declined": `concludedFor` is the decline
   * signal, and it is what the button reads to offer Start over. */
  openSession: (args: OpenSessionArgs) => Promise<InvestigationRecord | null>
  busy: boolean
  error: Error | null
  /** `itemKey` of the item whose click was DECLINED because its work had already
   * concluded and its session is gone — or null when the last click was not
   * declined. Not an error: nothing failed and there is nothing to retry.
   *
   * An IDENTITY rather than a boolean, because the detail pane is reused across
   * items (see `itemKey`), so a flag would follow the user to the next one and
   * force past the guard there on a first click. Callers compare it against their
   * own item.
   *
   * Cleared by the next `openSession` rather than by a separate resetter: every
   * call clears it on entry, so a click declined again simply re-sets it. */
  concludedFor: string | null
  /** Go to the chat page with its Older Sessions pane already open — where a
   * closed session's transcript is.
   *
   * The counterpart to declining the click. A concluded item's session has been
   * closed, and `api_chat_slot_delete` archives a session before popping the
   * slot, so the transcript is still readable; rehydrating it is what is refused
   * (`adopt_closed` gates that and this app never passes it). So the only correct
   * affordance is to point at history — and pointing has to be something the user
   * can DO, not a sentence naming a pane they then have to find.
   *
   * Lives here rather than in the button because navigation is already this
   * module's job (`openSession` ends in `navigate('/chat')`), which keeps
   * `AgentSessionButton` free of router context — it is shared presentation, and
   * two of its tests render it without a router. */
  openOlderSessions: () => void
}

export function useAgentSession(): UseAgentSession {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  const [concludedFor, setConcludedFor] = useState<string | null>(null)

  const openSession = useCallback(
    async ({ repoRef, number, kind = 'issue', title, prompt, existing, force = false }: OpenSessionArgs): Promise<InvestigationRecord | null> => {
      setBusy(true)
      // Set once a slot exists but is not yet linked to an investigation record;
      // cleared on success. See the rollback in the catch below.
      let createdSlotKey: string | null = null
      setError(null)
      setConcludedFor(null)
      try {
        // ── Resume: reattach to a still-live session, else fall through and open
        // a fresh one.
        //
        // PROBE before dispatching. `switchSlot.pending` mutates a lot of state
        // synchronously -- it assigns `activeSlot`, caches the outgoing slot's
        // activity, writes its message page, and pushes it onto the MRU
        // `slotHistory` -- and its rejected reducer unwinds almost none of that.
        // Undoing it from out here took three attempts and produced a new defect
        // each time: a key left pointing at a deleted session, then a restore that
        // raced a user switching chats, then a deleted key left in the Alt+`
        // history. So nothing is dispatched until the slot is known to be live,
        // and a decline touches the store not at all.
        //
        // The cost is one slot read on the resume path, duplicating what
        // `switchSlot` fetches when the session is in fact alive. That is the
        // right trade against unwinding shared chat state from a feature hook.
        const resumeIfLive = async (slotKey: string) => {
          try {
            await api.chatSlotDetail(slotKey, 1)
          } catch (e) {
            // Only a slot that is genuinely GONE justifies declining or opening a
            // replacement. Treating every failure as "deleted" is what orphaned
            // live sessions before, so a transient blip or a 500 still throws.
            if (!isMissingSlotError(e)) throw e
            return null
          }
          // The probe said live, so a missing-slot rejection HERE means the
          // session was deleted in the intervening moment. That is a genuine race
          // and it throws rather than falling through: falling through could reach
          // the decline below, which is only safe because nothing has been
          // dispatched. One retry re-probes and takes the honest branch.
          await dispatch(switchSlot(slotKey)).unwrap()
          // `saveInvestigation` stays outside the probe's error handling: a failed
          // timestamp touch is not a reason to re-create the session just resumed.
          const res = await issueRadarApi.saveInvestigation(repoRef, number, {}, kind)
          navigate('/chat')
          return res.investigation
        }

        if (existing?.slot_key) {
          const reattached = await resumeIfLive(existing.slot_key)
          if (reattached) return reattached

          // The slot this client knew about is gone. Before concluding anything,
          // re-read the record.
          //
          // The cached one is not good enough to decide this. It is read
          // cache-first with a 30s `staleTime` (InvestigateButton), and the
          // window that matters is exactly the one where it goes stale: the
          // agent records its verdict through the MCP tool, the session closes,
          // and the user clicks Resume -- all without the client necessarily
          // refetching. A record still reading `investigating` on this side
          // would then fall through to a silent re-run, which is the very
          // defect this guard exists to prevent, arriving by another route.
          //
          // Re-read only HERE: the happy path must not pay for it, and a stale
          // status is only dangerous once we know the session is unrecoverable.
          // A failed re-read deliberately propagates rather than falling back to
          // the cached value -- the same rule the probe follows for a transient
          // failure. Guessing "investigating" from a stale cache is what spends
          // an agent run and overwrites a verdict; guessing "concluded" would
          // show a finished-work message that may be false. Not knowing is
          // retryable, and neither guess is.
          const fresh = await issueRadarApi.getInvestigation(repoRef, number, kind)
          const current = fresh.investigation ?? existing

          // The record may name a DIFFERENT session than the one this client
          // cached -- another tab started over, so the investigation already has a
          // live replacement. The probe above says nothing about that slot, and
          // treating it as gone would open a THIRD session and overwrite the live
          // one's link, orphaning a session that is running. So try it too.
          if (current.slot_key && current.slot_key !== existing.slot_key) {
            const reattachedFresh = await resumeIfLive(current.slot_key)
            if (reattachedFresh) return reattachedFresh
          }


          // Whether a gone slot justifies opening a replacement depends on
          // whether the work was FINISHED when the session was closed, and the
          // record says so: the recording tool defaults `status` to `resolved`,
          // so a record still reading `investigating` never wrote a verdict.
          //
          // Only work still in progress earns a silent replacement. Closing the
          // session is how a user marks an investigation done, so re-seeding a
          // full investigation there would re-do finished work, spend a fresh
          // agent run, and overwrite the verdict already on the record -- all
          // from a button that reads "Resume". Say so instead, and let a second
          // explicit click (`force`) start over.
          //
          // The test is `present AND not investigating` rather than a list of
          // concluded statuses, so it fails CLOSED on a status this build does
          // not know: `archived` is today's third, and a fourth added later
          // declines the silent re-run rather than inheriting it.
          //
          // An ABSENT status is deliberately NOT concluded. The store normalizes
          // every record it writes (`status` falls back to `investigating` when
          // missing or unrecognised), so a record with no status never comes
          // from the API -- and absence is not evidence of finished work to
          // protect, so mirroring the store's own default is the honest reading.
          // Treating it as concluded would instead re-break the dead end this
          // fallback exists to fix.
          const concludedBefore = !!current.status && current.status !== 'investigating'
          if (!force && concludedBefore) {
            setConcludedFor(itemKey(repoRef, number, kind))
            // Return the REFRESHED record, not null. Both callers use the return
            // value for exactly one thing -- writing it into their query cache --
            // so handing back `current` repairs the stale read that sent us down
            // this path. Returning null instead left the pane showing "Start over"
            // beside a pill still claiming the investigation was running, which is
            // the same staleness this branch exists to correct, left on screen.
            return current
          }
        }

        // ── Fresh session: folder → slot (filed + titled) → seed+run → link.
        const folderId = await resolveFolderId(repoRef.repo)
        const slot = await dispatch(createSlot({ folder_id: folderId, title })).unwrap()
        // The slot is persisted but not yet linked to an investigation record, so
        // a failure before the seed leaves an EMPTY session behind — and the next
        // attempt, finding no record, would create another one. Rollback covers
        // exactly that window and stops the moment the seed is in flight: once the
        // POST may have been accepted the agent is (or is about to be) running,
        // and deleting the slot would CANCEL the user's review over what may be a
        // transient metadata write failure. An unlinked-but-working session is
        // strictly better than a destroyed one.
        createdSlotKey = slot.key
        // Seed + auto-run the first turn (background task; persisted + survives
        // the navigation). await ensures the user message is stored before we
        // switch, so it paints immediately on arrival.
        // api.sendChat hands back the raw fetch response, and fetch RESOLVES on
        // 4xx/5xx — so without this check a rejected prompt still got recorded and
        // navigated to, leaving a resumable but empty session.
        const seedInFlight = api.sendChat(prompt, slot.key)
        createdSlotKey = null
        const seeded = await seedInFlight
        // A REFUSAL, not merely a non-2xx: `/api/chat` also declines inside a 200
        // by answering `{ok:false}`, and a status-only check passed that as a
        // success -- recording and navigating to exactly the empty session this
        // guard exists to prevent. `readSendReceipt` owns that distinction for
        // every send site. An UNREADABLE 2xx receipt deliberately does NOT land
        // here: the request was accepted, so the seed may be running, and
        // deleting the slot would cancel real work over a mangled reply.
        if (seeded && typeof seeded === 'object' && 'ok' in seeded) {
          const { body, outcome } = await readSendReceipt(seeded as Response)
          if (outcome === 'refused') {
            // Rejected outright, so nothing is running: the empty slot is safe
            // (and wrong) to remove.
            await dispatch(deleteSlot(slot.key)).unwrap().catch(() => {})
            const reason = typeof body.error === 'string' && body.error
              ? body.error
              : `HTTP ${(seeded as Response).status}`
            throw new Error(`could not seed the session (${reason})`)
          }
        }
        const res = await issueRadarApi.saveInvestigation(repoRef, number, {
          slot_key: slot.key,
          folder_id: folderId,
          status: 'investigating',
          // NOT `findings: null` here, deliberately. Clearing at this point would
          // destroy the stored verdict the moment the replacement session opens,
          // before the new run has produced anything -- and the record is the only
          // copy, so an abandoned or failed Start over would lose it permanently.
          // That is worse than what it fixes, and worse specifically on the path
          // this PR documents as unprotected (a user who does not read the relabel
          // and clicks again).
          //
          // The prior verdict therefore survives, which leaves a real defect: the
          // replacement's own `record_investigation` MERGES per key, so a new
          // verdict that omits a key inherits the old one's. Fixing that belongs
          // where the transition is atomic -- store-side, replacing rather than
          // merging on the first record of a new run -- not at session open. Filed
          // separately; this path is unchanged from before the PR.
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

  // `?history=1` rather than a scroll target or a click simulation: the pane's
  // disclosure is ChatSidebar's own state, and the sidebar reads this param when
  // it mounts. Issue Radar is a different route, so arriving at /chat always
  // mounts a fresh sidebar and the param is read exactly once.
  const openOlderSessions = useCallback(() => { navigate('/chat?history=1') }, [navigate])

  return { openSession, busy, error, concludedFor, openOlderSessions }
}
