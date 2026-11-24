/**
 * What a thing is made of: the parts it opens into when it is pulled apart.
 *
 * Each part is one real piece of what the Crew knows about the thing, read
 * from the same fields as everything else. Nothing is made up to fill a
 * slot: a thing with one fact opens into one part.
 *
 * | Thing              | Its parts                                                           |
 * |--------------------|---------------------------------------------------------------------|
 * | a piece of work    | what it is and how it stands; the latest line of its thread; each   |
 * |                    | ask that waits on the person, with its exact command                |
 * | an ask             | the ask and its command; the work it belongs to; that work's latest |
 * | a key result       | what is measured; the reading against the target                    |
 * | a Bot (its home)   | how it stands and its last line; its oldest ask; what it works on;  |
 * |                    | its goal                                                            |
 * | a job, a report    | what it is and when                                                 |
 *
 * The data has no diff, no check results and no review thread for a piece
 * of work, so none are shown. They become parts here when a field for them
 * exists.
 */
import type { XrGlance } from './glance'
import { PARTS_MAX } from './handling'
import type { XrBot, XrNeed } from './model'
import { reportKey } from './plots'
import type { Binding, BoundState } from './sceneDoc'

export type PartHead = 'what' | 'latest' | 'ask' | 'goal' | 'work'

export interface Part {
  head: PartHead
  /** Crew-written text: a title, a line, a request. */
  text: string
  /** An exact command, when the part is an ask for a tool call. */
  command: string
  state: BoundState | ''
  /** Epoch seconds the part is dated by; 0 when it has no time. */
  at: number
  /** Parts that have no place of their own. Set on the last one. */
  more: number
  /** The part in trouble: it waits on the person, or it failed. It is the one that is lit. */
  lit: boolean
}

const part = (head: PartHead, text: string, o: Partial<Part> = {}): Part => ({ head, text, command: '', state: '', at: 0, more: 0, lit: o.state === 'needs' || o.state === 'failed', ...o })
const ask = (n: XrNeed): Part => part('ask', n.action || n.topic, { command: n.command, state: 'needs', at: n.ts })

/** The parts of the thing a binding names. Empty when the Bot or the field is not there. */
export function partsOf(bind: Binding | null, bot: XrBot | undefined, glance: XrGlance | null | undefined): Part[] {
  if (!bind || !bot) return []
  const f = bind.field
  const rest = f.slice(f.indexOf(':') + 1)
  let out: Part[] = []
  if (f === 'state') {
    const oldest = [...bot.needs].sort((a, b) => a.ts - b.ts)[0]
    const busy = bot.things.filter(t => t.state === 'working')
    out = [
      part('what', bot.line || bot.label, { state: bot.state, at: bot.lastTs }),
      ...(oldest ? [ask(oldest)] : []),
      ...(busy.length ? [part('work', busy[0].topic || busy[0].line, { state: 'working', at: busy[0].ts })] : []),
      ...(glance?.goals.length ? [part('goal', glance.goals[0])] : []),
    ]
  } else if (f.startsWith('thing:')) {
    const t = bot.things.find(x => x.slot === rest)
    if (t) {
      out = [
        part('what', t.topic || t.line, { state: t.state === 'idle' && !t.direct ? 'done' : t.state, at: t.ts }),
        ...(t.topic && t.line ? [part('latest', t.line)] : []),
        ...bot.needs.filter(n => n.slot === rest).map(ask),
      ]
    }
  } else if (f.startsWith('need:')) {
    const n = bot.needs.find(x => x.id === rest)
    const t = n ? bot.things.find(x => x.slot === n.slot) : undefined
    if (n) out = [ask(n), ...(n.topic ? [part('work', n.topic, { state: t?.state ?? '' })] : []), ...(t?.line ? [part('latest', t.line)] : [])]
  } else if (f.startsWith('goal:')) {
    const n = glance?.numbers[Number(rest)]
    if (n) out = [part('goal', n.label), part('what', n.target ? `${n.value} / ${n.target}` : n.value, { at: n.at })]
  } else if (f.startsWith('job:')) {
    const j = bot.jobs.find(x => x.id === rest)
    if (j) out = [part('what', j.name, { state: j.on ? 'idle' : 'failed', at: j.next })]
  } else if (f.startsWith('report:')) {
    const r = glance?.reports.find(x => reportKey(x) === f)
    if (r) out = [part('what', r.title, { state: 'done', at: r.at })]
  }
  const shown = out.filter(p => p.text).slice(0, PARTS_MAX)
  if (shown.length) shown[shown.length - 1] = { ...shown[shown.length - 1], more: out.filter(p => p.text).length - shown.length }
  return shown
}
