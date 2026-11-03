/**
 * Section goals run as ONE loop on the page's conductor (PoC). A doc's headings or a
 * board's sections each carry their own goal text, all checked at one rate set for
 * the whole page; the conductor's loop wakes at that rate and checks every goal as
 * the file stands then. Adding, changing or removing a goal is only an edit to the
 * file, so the loop changes only when the page gains its first goal, loses its last
 * one, or its rate changes.
 *
 * The page arms the loop itself, through the same route as a session's goal control
 * (`/api/autonudge`), so it never depends on the conductor doing it. The loop is
 * ungated: a work-ledger watch is gated and skips every tick on which no worker
 * reported, which would skip the goal checks too. Each check reads the ledger
 * instead, so the loop stands in for the gateway's patrol while the page has goals.
 */

/** The banner every goal wake shows in the transcript: how the page tells a goal
 *  check from the person's own asks. */
export const GOALS_TAG = '[Goals]'
const OPEN = '<<goals>>'
const CLOSE = '<</goals>>'
export const GOALS_BANNER = `${GOALS_TAG} Checking the goal sections`

export const DEFAULT_GOAL_EVERY = 10

export interface GoalLoopSpec {
  slotKey: string
  kind: 'doc' | 'board'
  docId: string
  title?: string
  /** Minutes between checks, for every goal on the page. */
  every: number
  /** Whether the page has any goal now. */
  active: boolean
}

/** What a goal wake asks of the conductor: short, and enough on its own, since a
 *  wake carries none of an ask's context. */
export function goalInstruction(spec: Pick<GoalLoopSpec, 'kind' | 'docId' | 'title'>): string {
  const name = spec.title?.trim() || (spec.kind === 'doc' ? 'Untitled doc' : 'Untitled board')
  const file = `docs/${spec.docId}.md under your Kiro Crew data home ($KIROCREW_HOME, else ~/.kiro/crew)`
  const where = spec.kind === 'doc'
    ? `Read the doc file ${file}. Each heading followed by a line <!-- goal <id>: ... --> has a goal with that id; it covers that heading's section, down to the next heading of the same or a higher level.`
    : `Read the board file ${file} (JSON). Each shape of kind "section" with a "goal" has one; its id is the section's id, and it covers the shapes inside that section.`
  const anchor = spec.kind === 'doc' ? 'data-anchor="<the heading\'s exact text>" data-place="margin"' : 'data-anchor="shape:<section id>" data-place="corner"'
  const write = spec.kind === 'doc'
    ? 'Write results only inside that section, with a doc-insert block after an exact line from inside it.'
    : 'Change only shapes inside that section (board-edit, or board-add with "in": that section\'s id).'
  return `${OPEN}
Goal check for the ${spec.kind} "${name}". ${where} For each goal, look at its section as it is now and do what the goal asks: start or steer workers for real work and keep them on your work ledger, never a second worker for something one of yours already has. ${write} Never touch anything outside a goal's section. Keep one item per goal in your status overlay artifact (${anchor}): what it is doing now, moving while work runs (ov-running) and still once settled.
Reply with one block per goal, each opened by a line of its own, [goal <id>], then a short report of what you found and did. When there is nothing to report, the block is exactly "Nothing new." and nothing else. The person reads each block on that goal's pin, so write it for them.
Also read your work ledger and act on what your workers reported since the last check. This loop is what wakes you while the page has goals, so arm or change no loop of your own (no monitor_start, no monitor_update).
${CLOSE}`
}

/** Whether a transcript message is a goal wake. */
export const isGoalWake = (text: string) => text.includes(GOALS_TAG) || text.includes(OPEN)

/** The fields of the slot route's loop record the page reads. */
interface LoopRecord { id: string; idle_secs?: number; active?: boolean; gate?: boolean; banner?: string | null }

async function readLoop(slotKey: string): Promise<LoopRecord | null> {
  const resp = await fetch(`/api/autonudge/slot/${encodeURIComponent(slotKey)}`)
  if (!resp.ok) return null
  const data = await resp.json().catch(() => ({})) as { loop?: LoopRecord | null }
  return data.loop ?? null
}

async function write(url: string, method: 'POST' | 'PATCH' | 'DELETE', body?: unknown): Promise<LoopRecord> {
  const init: RequestInit = { method }
  if (body !== undefined) Object.assign(init, { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  const resp = await fetch(url, init)
  const data = await resp.json().catch(() => ({})) as { loop?: LoopRecord; error?: string }
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`)
  return data.loop as LoopRecord
}

const loopUrl = (id: string) => `/api/autonudge/${encodeURIComponent(id)}`

/** Run a check now, so a new or revived goal is seen to work. A turn already in
 *  flight refuses it, and the loop's own timer covers that. */
const fireNow = (id: string) => fetch(`${loopUrl(id)}/fire`, { method: 'POST' }).catch(() => undefined)

/** The page's own loop: ungated (it must fire at the goal rate whatever the ledger
 *  says) and bannered. The gateway's patrol and a conductor's own watch are gated. */
const isGoalLoop = (loop: LoopRecord) => loop.gate === false && loop.banner === GOALS_BANNER

/**
 * Bring the conductor's loop in line with the page's goals: the page's own ungated
 * loop at *every* minutes while there is a goal, and gone once there is none.
 *
 * A session holds one loop. The gateway's default patrol on a conductor gives way
 * to a dashboard arm, so arming over it simply replaces it, and the goal check
 * keeps the ledger in view itself. Once the goals go, deleting the loop lets the
 * gateway arm its patrol again when the conductor next needs one. A loop the
 * conductor armed itself refuses the arm, and that refusal is thrown.
 */
export async function syncGoalLoop(spec: GoalLoopSpec): Promise<'armed' | 'updated' | 'removed' | 'unchanged'> {
  const loop = await readLoop(spec.slotKey)
  const block = goalInstruction(spec)
  const idle = Math.round(spec.every * 60)
  if (spec.active) {
    if (!loop || !isGoalLoop(loop)) {
      const made = await write('/api/autonudge', 'POST', {
        slot_key: spec.slotKey, message: block, banner: GOALS_BANNER, idle_secs: idle, max_cycles: 0, max_runtime_secs: 0, gate: false,
      })
      if (made?.id) await fireNow(made.id)
      return 'armed'
    }
    // Its message is always this page's block, so a changed title or rate rewrites it whole.
    const patch: Record<string, unknown> = { message: block }
    if (loop.idle_secs !== idle) patch.idle_secs = idle
    const revived = loop.active === false
    if (revived) patch.active = true
    await write(loopUrl(loop.id), 'PATCH', patch)
    if (revived) await fireNow(loop.id)
    return 'updated'
  }
  if (!loop || !isGoalLoop(loop)) return 'unchanged'
  await write(loopUrl(loop.id), 'DELETE')
  return 'removed'
}
