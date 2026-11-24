/**
 * The sample crew (`?fixture=1`), shaped for a picture of a larger crew or of chosen asks. Read only with the sample
 * crew: a real crew is never changed here.
 *
 * - `crewn=<n>`: the sample's six Bots, filled up to `n` with stand-ins of the same shapes under new names (the
 *   stand-ins work, rest or have failed by turns, and ask nothing).
 * - `up=<slug,slug>`: only these Bots ask; every other ask of the sample is left out, and a Bot that asked then
 *   works (Atlas) or rests. `up=` with nothing: nobody asks.
 * - `asks=<n>`, `unread=<slug,slug>`, `longname=<slug>`: stand-ins that ask, own threads he has not read, a long name
 *   (below). `words=1` (`Workspace.tsx`) lets the Bots' words be read from the sample's threads (`fixtureWords`).
 * - Pico, the sample's Bot whose thread was cut, shows as failed whenever any of these words is given.
 */
import { ICON_BG_PALETTE } from '../../../lib/avatarPoses'
import type { XrBot, XrCrew } from '../model'

const NAMES = ['Nova', 'Echo', 'Rhea', 'Tomo', 'Lark', 'Vega', 'Kit', 'Bram', 'Ines', 'Yuki', 'Dara', 'Olin', 'Wren', 'Zed', 'Faye', 'Hugo', 'Ione', 'Jett', 'Kora', 'Lev', 'Moss', 'Nell', 'Orin', 'Pax']
const STATES = ['working', 'idle', 'working', 'idle', 'working', 'idle', 'failed', 'working'] as const

export function shapedSample(crew: XrCrew, params: URLSearchParams, held = false): XrCrew {
  const n = params.get('crewn')
  // `held`: nobody asks yet (`uplate=1`, until a test lets the asks come), for a picture of an ask that comes later.
  const up = held ? '' : params.get('up')
  if (n === null && up === null && !['asks', 'unread', 'longname', 'askfailed', 'failed', 'names', 'without', 'runs'].some(k => params.has(k))) return crew
  const asking = new Set((up ?? '').split(',').filter(Boolean))
  const keeps = (slug: string) => up === null || asking.has(slug)
  let bots: XrBot[] = crew.bots.map(b => {
    // Its cut is one he has not seen: the rim draws it red, and it counts as failed (`rims.ts`).
    if (b.slug === 'pico') return { ...b, state: 'failed', cuts: b.cuts.map(c => ({ ...c, unread: true })) }
    if (keeps(b.slug)) return b
    return { ...b, needs: [], state: b.state === 'needs' ? (b.slug === 'atlas' ? 'working' : 'idle') : b.state }
  })
  const count = n === null ? bots.length : Math.max(0, Math.min(60, Number(n) || 0))
  for (let i = 0; crew.bots.length > 0 && bots.length < count; i += 1) {
    const name = NAMES[i % NAMES.length] + (i >= NAMES.length ? ` ${Math.floor(i / NAMES.length) + 1}` : '')
    const slug = name.toLowerCase().replace(' ', '-')
    const avatar = i % 3 === 1 ? {} : { kind: 'icon', pose: `pose-${((i * 3) % 11) + 1}`, bg: ICON_BG_PALETTE[(i * 2 + 1) % ICON_BG_PALETTE.length] }
    bots.push({ ...crew.bots[i % crew.bots.length], slug, name, label: name, avatar, state: STATES[i % STATES.length], needs: [], things: [], cuts: [], cut: '', thread: '', sessions: [] })
  }
  bots = bots.slice(0, count)
  const here = new Set(bots.map(b => b.slug))
  const needs = crew.needs.filter(x => here.has(x.bot) && keeps(x.bot))
  // `asks=<n>`: the first n stand-ins each ask one approval, as a worker of theirs whose turn waits on it
  // (`pending_approval` with its `request_id`), so each holds up a board whose stamp goes to that worker's chat slot.
  const base = crew.needs.find(x => x.kind === 'approval' && x.origin === 'native' && x.requestId)
  const asks = Math.max(0, Number(params.get('asks')) || 0)
  if (base && asks > 0) {
    const named = new Set(crew.bots.map(b => b.slug))
    let left = asks
    bots = bots.map(b => {
      if (named.has(b.slug) || left <= 0) return b
      left -= 1
      const slot = `w-${b.slug}`
      const need = { ...base, id: `a:${slot}:req-${b.slug}`, bot: b.slug, who: b.label, slot, requestId: `req-${b.slug}`, version: `${base.version}|${b.slug}`, action: base.action, ts: base.ts - left }
      needs.push(need)
      return { ...b, state: 'needs' as const, needs: [need] }
    })
  }
  // `failed=<slug>`: that Bot's own thread was cut too, as Pico's (a red cut he has not read), and it asks nothing:
  // a failed Bot at another place of the row, for a picture.
  const cutToo = params.get('failed')
  const picoCut = crew.bots.find(b => b.slug === 'pico')?.cuts[0]
  if (cutToo && picoCut) {
    const mine = new Set(needs.filter(x => x.bot === cutToo).map(x => x.id))
    for (let i = needs.length - 1; i >= 0; i -= 1) if (mine.has(needs[i].id)) needs.splice(i, 1)
    bots = bots.map(b => (b.slug === cutToo ? { ...b, state: 'failed' as const, needs: [], thread: b.thread || `member-${b.slug}`, cuts: [{ ...picoCut, slot: `member-${b.slug}`, id: `${picoCut.id}-${b.slug}`, unread: true }] } : b))
  }
  // `askfailed=<slug>`: that Bot, whose thread was cut, also asks one approval (a worker of its waits on it): it asks
  // AND has failed. Its state is the ask's, as the model reads a Bot with needs (`model.ts:422`).
  const both = params.get('askfailed')
  if (base && both) {
    bots = bots.map(b => {
      if (b.slug !== both) return b
      const slot = `w-${b.slug}`
      const need = { ...base, id: `a:${slot}:req-${b.slug}`, bot: b.slug, who: b.label, slot, requestId: `req-${b.slug}`, version: `${base.version}|${b.slug}`, action: base.action, ts: base.ts + 1 }
      needs.push(need)
      return { ...b, state: 'needs' as const, needs: [...b.needs, need] }
    })
  }
  // `unread=<slug,slug>`: these Bots' own threads ended a turn he has not read (as this window's own read state would say: the gateway keeps none, `ws.py` `_handle_slot_read`).
  const unread = new Set((params.get('unread') ?? '').split(',').filter(Boolean))
  // `longname=<slug,slug>`: these Bots go by a long name, for a picture of a heading that must still fit.
  const longs = new Set((params.get('longname') ?? '').split(',').filter(Boolean))
  // `names=<slug>:<name>,<slug>:<name>`: these Bots go by these names, for a picture of names in other scripts or alike.
  const named = new Map((params.get('names') ?? '').split(',').map(x => x.split(':')).filter(x => x.length === 2 && x[0] && x[1]).map(x => [x[0], x[1]] as const))
  bots = bots.map(b => {
    let out = b
    if (unread.has(b.slug)) out = { ...out, ownUnread: true, unread: true, rest: out.rest.map(r => (r.direct ? { ...r, unread: true } : r)) }
    if (longs.has(b.slug)) out = { ...out, label: `${b.label} of the Northern Rollout and Release Team` }
    if (named.has(b.slug)) out = { ...out, label: named.get(b.slug) ?? out.label }
    return out
  })
  // `runs=<slug>`: that Bot runs again after its thread was cut: it works, and the cut is still listed, for a
  // picture of a failure that is over with no act of his.
  const runs = params.get('runs')
  if (runs) bots = bots.map(b => (b.slug === runs ? { ...b, state: 'working' as const } : b))
  // `without=<ask id>`: that one ask is gone (answered elsewhere), for a picture of a Bot whose other ask still stands.
  const without = params.get('without')
  if (without) bots = bots.map(b => (b.needs.some(x => x.id === without) ? { ...b, needs: b.needs.filter(x => x.id !== without) } : b))
  const left = without ? needs.filter(x => x.id !== without) : needs
  return { ...crew, bots, needs: longs.size ? left.map(x => (longs.has(x.bot) ? { ...x, who: bots.find(b => b.slug === x.bot)?.label ?? x.who } : x)) : left }
}

/**
 * The sample crew with the sessions he read here marked read, as this window's own read state marks them after
 * `markSlotRead` (`useSays.read` with sample words): a read cut is no longer unread, so its rim is no longer red.
 */
export function readInSample(crew: XrCrew, read: ReadonlySet<string>): XrCrew {
  if (!read.size) return crew
  const bots = crew.bots.map(b => {
    if (!b.rest.some(r => read.has(r.slot)) && !b.cuts.some(c => read.has(c.slot))) return b
    const rest = b.rest.map(r => (read.has(r.slot) ? { ...r, unread: false } : r))
    const cuts = b.cuts.map(c => (read.has(c.slot) ? { ...c, unread: false } : c))
    const unread = rest.some(r => r.unread) || cuts.some(c => c.unread)
    return { ...b, rest, cuts, unread, ownUnread: b.ownUnread && !read.has(b.thread) }
  })
  return { ...crew, bots }
}
