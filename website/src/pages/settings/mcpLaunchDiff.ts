/**
 * What changed between a refused launch and the one approved before it, in
 * the shapes the approval card draws. Values follow the gateway's display
 * redaction, and the page draws a value only for a key the gateway shows in clear.
 */
import type { LaunchRefusal } from './mcpManagementModel'

/**
 * Each distinct command line once, in first-seen order. The gateway sends one
 * line per launch, and launches of one program often differ only in their env
 * (one per agent that declares the server), so the same line would repeat.
 */
export function distinctCommands(commands?: readonly string[][]): string[][] {
  const seen = new Set<string>()
  return (commands ?? []).filter(argv => {
    const key = JSON.stringify(argv)
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

/** One env key whose value changed between the approved and the new launch. */
export type EnvValueChange = { name: string; added: string[]; removed: string[] }

function envValues(envs?: readonly (readonly string[])[]): Map<string, Set<string>> {
  const out = new Map<string, Set<string>>()
  for (const env of envs ?? []) {
    for (const pair of env) {
      const at = pair.indexOf('=')
      if (at <= 0) continue
      const name = pair.slice(0, at)
      if (!out.has(name)) out.set(name, new Set())
      out.get(name)?.add(pair.slice(at + 1))
    }
  }
  return out
}

/** One launch as the page draws it: its command and its env pairs, a hidden value cut to its key. */
export type DrawnLaunch = { command: string[]; env: string[] }

/**
 * Which values may be drawn is the gateway's decision, not the page's: it
 * sends a value in clear only for the keys its display rule shows
 * (`_display_env_pair` in `mcp_gateway/launch_approval.py`) and every other
 * one as `<hidden sha256:…>`. So a value is drawn exactly when it is not opaque.
 */
function drawnEnv(env?: readonly string[]): string[] {
  return (env ?? []).map(pair => {
    const at = pair.indexOf('=')
    if (at <= 0) return pair
    return opaqueValue(pair.slice(at + 1)) ? pair.slice(0, at) : pair
  })
}

/** One launch's value for `name`, or null when that launch does not set it. */
function keyValue(env: readonly string[] | undefined, name: string): string | null {
  const pair = (env ?? []).find(p => p.startsWith(`${name}=`))
  return pair === undefined ? null : pair.slice(name.length + 1)
}

/** A value the gateway hid, redacted or cut: its entries can't be compared. */
function opaqueValue(value: string): boolean {
  return value.startsWith('<hidden') || value.includes('[REDACTED') || value.endsWith('…')
}

/**
 * The separator of one key's list values, read from ALL its values on both
 * sides together so both are split the same way: `;` when any value has one or
 * starts with a drive letter (Windows, where `C:` holds a colon), else `:`;
 * null for a key that is not a search path, whose value is compared whole.
 */
function listSeparator(name: string, values: Iterable<string>): string | null {
  // Only a search-path style key holds a list of folders: `PATH`, any `*PATH`
  // (`PYTHONPATH`, `LD_LIBRARY_PATH`) and the loader's `LD_`/`DYLD_` keys.
  // Anything else (`NODE_OPTIONS`) is a scalar, even with a colon in it.
  if (!(/PATH$/.test(name) || /^(LD|DYLD)_/.test(name))) return null
  const all = [...values]
  if (all.some(v => v.includes(';') || /^[A-Za-z]:[\\/]/.test(v))) return ';'
  return ':'
}

/**
 * The env keys set on both sides whose values changed, and for a visible value
 * the list entries added and removed (a PATH dir, say). A value the gateway hid
 * or redacted gives empty lists, so only its key is named; so does a change
 * that only reorders the entries.
 */
export function envValueChanges(refusal: LaunchRefusal): EnvValueChange[] {
  const before = envValues(refusal.approved_envs)
  const after = envValues(refusal.envs)
  const out: EnvValueChange[] = []
  for (const [name, now] of after) {
    const was = before.get(name)
    if (!was) continue
    const gone = [...was].filter(v => !now.has(v))
    const fresh = [...now].filter(v => !was.has(v))
    if (gone.length === 0 && fresh.length === 0) {
      // The same values on both sides can still hide a change in WHICH launch
      // carries the key (one launch dropped it, or it moved): name the key.
      // Only a command that runs on both sides is compared: a changed command
      // line is drawn in the columns, and does not move a key that is the same.
      const runs = (commands?: string[][]) => new Set((commands ?? []).map(c => JSON.stringify(c)))
      const both = new Set([...runs(refusal.approved_commands)].filter(c => runs(refusal.commands).has(c)))
      const sigs = (commands?: string[][], envs?: string[][]) =>
        new Set(
          (commands ?? []).flatMap((c, i) =>
            both.has(JSON.stringify(c)) ? [JSON.stringify([c, keyValue(envs?.[i], name)])] : [],
          ),
        )
      const a = sigs(refusal.approved_commands, refusal.approved_envs)
      const b = sigs(refusal.commands, refusal.envs)
      if (a.size !== b.size || [...a].some(s => !b.has(s))) out.push({ name, added: [], removed: [] })
      continue
    }
    if ([...gone, ...fresh].some(opaqueValue)) {
      out.push({ name, added: [], removed: [] })
      continue
    }
    // A side with no value of its own left (a launch only added, or only
    // dropped) is compared through all its values, so an added launch's PATH
    // shows only the dirs no approved PATH had.
    const sep = listSeparator(name, [...was, ...now])
    // An empty entry in a POSIX search path (`*PATH`) searches the current
    // folder, so it is kept and shown as `.`; the loader ignores one in an
    // `LD_`/`DYLD_` list, and Windows skips empty entries, so there it is dropped.
    // The Linux loader splits `LD_PRELOAD` on spaces as well as colons (ld.so(8)).
    const entries = (v: string): string[] =>
      sep === null
        ? v === '' ? [] : [v]
        : sep === ':' && /PATH$/.test(name)
          ? v.split(sep).map(e => (e === '' ? '.' : e))
          : sep === ':' && name === 'LD_PRELOAD'
            ? v.split(/[ :]+/).filter(Boolean)
            : v.split(sep).filter(Boolean)
    const left = new Set((gone.length > 0 ? gone : [...was]).flatMap(entries))
    const right = new Set((fresh.length > 0 ? fresh : [...now]).flatMap(entries))
    out.push({
      name,
      added: fresh.length > 0 ? [...right].filter(e => !left.has(e)) : [],
      removed: gone.length > 0 ? [...left].filter(e => !right.has(e)) : [],
    })
  }
  return out
}

/** One launch as the gateway displayed it: its command and its raw env pairs. */
type RawLaunch = { command: string[]; env: string[] }

/** One changed launch: what changed against the approved launch it replaced. */
export type LaunchChange = {
  /** `changed`: paired with an approved launch; `added` / `removed`: no partner. */
  kind: 'changed' | 'added' | 'removed'
  command: string[]
  envAdded: string[]
  envRemoved: string[]
  values: EnvValueChange[]
  before?: DrawnLaunch
  after?: DrawnLaunch
}

/**
 * Changes that say the same thing about the same command, kept once: when the
 * gateway's PATH moves, every launch of a command changes the same way, and
 * the reader needs that change once, not once per launch.
 */
export function distinctChanges(changes: readonly LaunchChange[]): LaunchChange[] {
  const seen = new Set<string>()
  return changes.filter(c => {
    const key = JSON.stringify([c.kind, c.command, c.envAdded, c.envRemoved, c.values])
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

/**
 * Whether a merged env-key list per side can show a change without hiding one.
 * The gateway orders each side by its own hash, so launches are never paired
 * by list position. It is safe in two cases only:
 * - `single`: one launch per side, so the pairing is the only one possible,
 *   unless a value is redacted or cut (equal displays could hide a change);
 * - `same`: both sides run the same number of launches and every launch sets
 *   the same pairs (compared as sets, so order is irrelevant), so no key can
 *   have moved or changed. A launch with a hidden, redacted or cut value is
 *   never `same`: equal displays cannot prove equal values. A change in launch count is never `same`: the
 *   extra launch's whole env is new to the reader. A launch whose command was
 *   replaced keeps the count and the env, so it is `same`, as in `single`:
 *   the new command is drawn as changed and no env key changed.
 * Anything else (several launches with different envs) returns `null`: the
 * caller draws each launch with its own keys.
 */
export function mergedEnvRule(refusal: LaunchRefusal): 'single' | 'same' | null {
  const before = (refusal.approved_commands ?? []).map((_, i) => refusal.approved_envs?.[i] ?? [])
  const after = (refusal.commands ?? []).map((_, i) => refusal.envs?.[i] ?? [])
  const value = (pair: string) => (pair.indexOf('=') > 0 ? pair.slice(pair.indexOf('=') + 1) : null)
  // A redacted or cut value stands for many values, so equal displays do not
  // prove an unchanged key; a hidden value's hash still tells a change.
  const lossy = (pair: string) => {
    const v = value(pair)
    return v !== null && (v.includes('[REDACTED') || v.endsWith('…'))
  }
  if (before.length === 1 && after.length === 1) return [...before, ...after].some(env => env.some(lossy)) ? null : 'single'
  if (before.length !== after.length) return null
  // A hidden value stands for many values too, so for `same` it never counts.
  const opaque = (pair: string) => value(pair) !== null && opaqueValue(value(pair) as string)
  if ([...before, ...after].some(env => env.some(opaque))) return null
  const key = (env: readonly string[]) => JSON.stringify([...new Set(env)].sort())
  const all = new Set([...before, ...after].map(key))
  return all.size <= 1 ? 'same' : null
}

/**
 * The launches that are not on the other side whole (same command, same env),
 * drawn with their clear values, and whether one of them runs a command both
 * sides run. When one does, a per-key comparison cannot prove which keys moved
 * between those launches, so a caller must not hide any of their keys.
 */
export function wholeLaunchChanges(refusal: LaunchRefusal): {
  before: DrawnLaunch[]
  after: DrawnLaunch[]
  sameCommandChanged: boolean
} {
  const was = rawLaunches(refusal.approved_commands, refusal.approved_envs)
  const now = rawLaunches(refusal.commands, refusal.envs)
  const gone = [...was].filter(([k]) => !now.has(k)).map(([, l]) => l)
  const fresh = [...now].filter(([k]) => !was.has(k)).map(([, l]) => l)
  const cmds = (m: Map<string, RawLaunch>) => new Set([...m.values()].map(l => JSON.stringify(l.command)))
  const wasCmds = cmds(was)
  const nowCmds = cmds(now)
  const onBoth = (l: RawLaunch) => wasCmds.has(JSON.stringify(l.command)) && nowCmds.has(JSON.stringify(l.command))
  const draw = (l: RawLaunch): DrawnLaunch => ({ command: l.command, env: drawnEnv(l.env) })
  return {
    before: gone.map(draw),
    after: fresh.map(draw),
    sameCommandChanged: [...gone, ...fresh].some(onBoth),
  }
}

/**
 * For each launch on `mine`, the env keys to mark: a pair (`KEY=value`) on a
 * launch the other side does not run whole, when the pair is not set on every
 * launch of both sides. That covers a key a launch gained and a key that may
 * have moved to another launch: without pairing launches, which the
 * gateway's list order cannot support, the reader is shown where to look.
 * A pair every launch sets on both sides (a shared PATH) is not marked.
 */
export function markedKeysPerLaunch(
  mine: { commands?: string[][]; envs?: string[][] },
  other: { commands?: string[][]; envs?: string[][] },
): Set<string>[] {
  const launches = (s: { commands?: string[][]; envs?: string[][] }) =>
    (s.commands ?? []).map((command, i) => ({ command, env: [...new Set(s.envs?.[i] ?? [])] }))
  const mineL = launches(mine)
  const otherL = launches(other)
  const counts = (ls: { env: string[] }[]) => {
    const m = new Map<string, number>()
    for (const l of ls) for (const p of l.env) m.set(p, (m.get(p) ?? 0) + 1)
    return m
  }
  const mineCount = counts(mineL)
  const otherCount = counts(otherL)
  const whole = new Set(otherL.map(l => JSON.stringify([l.command, [...l.env].sort()])))
  return mineL.map(l => {
    const out = new Set<string>()
    if (whole.has(JSON.stringify([l.command, [...l.env].sort()]))) return out
    for (const p of l.env) {
      const at = p.indexOf('=')
      const everywhere = (mineCount.get(p) ?? 0) === mineL.length && (otherCount.get(p) ?? 0) === otherL.length
      if (at > 0 && !everywhere) out.add(p.slice(0, at))
    }
    return out
  })
}

/** Drawn launches with each identical one kept once. */
export function distinctDrawn(launches: readonly DrawnLaunch[]): DrawnLaunch[] {
  const seen = new Set<string>()
  return launches.filter(l => {
    const key = JSON.stringify([l.command, l.env])
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

function rawLaunches(commands?: string[][], envs?: string[][]): Map<string, RawLaunch> {
  const out = new Map<string, RawLaunch>()
  ;(commands ?? []).forEach((command, i) => {
    const env = envs?.[i] ?? []
    out.set(JSON.stringify([command, env]), { command, env })
  })
  return out
}

function names(env: readonly string[]): Set<string> {
  return new Set(env.map(p => p.slice(0, Math.max(0, p.indexOf('=')))).filter(Boolean))
}

/** What changed from one approved launch to one new launch. */
function pairDiff(before: RawLaunch, after: RawLaunch): Omit<LaunchChange, 'kind' | 'command'> {
  const was = names(before.env)
  const now = names(after.env)
  return {
    envAdded: [...now].filter(n => !was.has(n)),
    envRemoved: [...was].filter(n => !now.has(n)),
    values: envValueChanges({
      reason: 'changed_needs_reapproval',
      approved_commands: [before.command],
      approved_envs: [before.env],
      commands: [after.command],
      envs: [after.env],
    }),
    before: { command: before.command, env: drawnEnv(before.env) },
    after: { command: after.command, env: drawnEnv(after.env) },
  }
}

/**
 * The summary per LAUNCH: each new launch is paired with the approved launch it
 * most likely replaced (same command first, then the fewest changed keys; on a
 * tie the first in the gateway's order), and its env is diffed against that one
 * launch only. A change a per-key summary would miss -- two launches swapping
 * values -- shows as a change on each launch. A launch with no partner is
 * reported as added or removed whole.
 */
export function launchChanges(refusal: LaunchRefusal): LaunchChange[] {
  const was = rawLaunches(refusal.approved_commands, refusal.approved_envs)
  const now = rawLaunches(refusal.commands, refusal.envs)
  const removed = [...was].filter(([k]) => !now.has(k)).map(([, l]) => l)
  const added = [...now].filter(([k]) => !was.has(k)).map(([, l]) => l)
  const free = new Set(removed.keys())
  const out: LaunchChange[] = []
  for (const after of added) {
    let best = -1
    let bestScore = Infinity
    for (const i of free) {
      // Only a launch of the same command is a partner: pairing across
      // commands would hide the command change behind an env diff.
      if (JSON.stringify(removed[i].command) !== JSON.stringify(after.command)) continue
      const d = pairDiff(removed[i], after)
      const score = d.envAdded.length + d.envRemoved.length + d.values.length
      if (score < bestScore) {
        best = i
        bestScore = score
      }
    }
    if (best < 0) {
      // No partner: still say how its env differs from the approved launches
      // of the same command, so an added key (`LD_PRELOAD`) is never hidden.
      out.push({ kind: 'added', command: after.command, ...unionDiff([...was.values()], after, 'after') })
      continue
    }
    free.delete(best)
    out.push({ kind: 'changed', command: after.command, ...pairDiff(removed[best], after) })
  }
  for (const i of free) {
    const before = removed[i]
    out.push({ kind: 'removed', command: before.command, ...unionDiff([...now.values()], before, 'before') })
  }
  return out
}

/**
 * The env diff of a launch with no partner, against every launch of the same
 * command on the other side taken together. `side` says which side `launch`
 * is on, so added and removed keys keep their direction.
 */
function unionDiff(
  others: RawLaunch[],
  launch: RawLaunch,
  side: 'before' | 'after',
): Omit<LaunchChange, 'kind' | 'command'> {
  const key = JSON.stringify(launch.command)
  const same = others.filter(o => JSON.stringify(o.command) === key)
  const drawn = { command: launch.command, env: drawnEnv(launch.env) }
  if (same.length === 0) return { envAdded: [], envRemoved: [], values: [], [side]: drawn }
  const otherNames = new Set(same.flatMap(o => [...names(o.env)]))
  const own = names(launch.env)
  const fresh = [...own].filter(n => !otherNames.has(n))
  const otherEnvs = same.map(o => o.env)
  const values = envValueChanges(
    side === 'after'
      ? { reason: 'changed_needs_reapproval', approved_commands: same.map(o => o.command), approved_envs: otherEnvs, commands: [launch.command], envs: [launch.env] }
      : { reason: 'changed_needs_reapproval', approved_commands: [launch.command], approved_envs: [launch.env], commands: same.map(o => o.command), envs: otherEnvs },
  )
  // A launch with no partner only ADDS (or only takes away) its own launch:
  // the other launches keep running unchanged, so an added launch reports only
  // what it brings, and a removed one only what goes with it. A hidden value
  // (both lists empty) still names its key.
  const keep = side === 'after' ? 'added' : 'removed'
  // A hidden value the other side already carries is not a change.
  const reused = (name: string) => {
    const own = keyValue(launch.env, name)
    return own !== null && same.some(o => keyValue(o.env, name) === own)
  }
  // A value of its own with no entry on this launch's side (a reorder that
  // drops an entry) still changed: it names its key like a hidden value.
  const oneWay = values
    .filter(v => v[keep].length > 0 || !reused(v.name))
    .map(v =>
      v[keep].length > 0
        ? side === 'after'
          ? { ...v, removed: [] }
          : { ...v, added: [] }
        : { ...v, added: [], removed: [] },
    )
  return {
    envAdded: side === 'after' ? fresh : [],
    envRemoved: side === 'after' ? [] : fresh,
    values: oneWay,
    [side]: drawn,
  }
}
