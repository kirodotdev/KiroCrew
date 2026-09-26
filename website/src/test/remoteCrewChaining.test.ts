import { describe, it, expect, vi, afterEach } from 'vitest'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import type { InstanceView } from '../api/client'
import { chainRows, rowStateLabel, visibleInstanceTabs } from '../components/InstanceTabBar'
import { parseHostModel } from '../components/EmbeddedHostBridge'
import {
  announceChainedCrew,
  chainAdoptionPlan,
  chainRefusalCode,
  CHAIN_REFUSAL_MAX,
  CHAINED_CREW_MESSAGE,
  CHAINED_CREW_REFUSED_MESSAGE,
  CHAINED_HOST_MAX,
  CHAINED_NAME_MAX,
  clearChainRefusal,
  readChainRefusal,
  readChainedCrewNotice,
  subscribeChainRefusal,
} from '../lib/chainAnnounce'

/** Minimal InstanceView; only the fields the chain rules read matter. */
function inst(id: string, extra: Partial<InstanceView> = {}): InstanceView {
  return {
    id,
    name: id,
    ssh_host: `${id}-host`,
    remote_port: 5476,
    local_port: 0,
    ttl: '20h',
    remote_bin: '',
    connection_method: 'ssh',
    ssm_target: '',
    aws_profile: '',
    aws_region: '',
    ssm_run_as: '',
    was_connected: true,
    status: { instance_id: id, state: 'connected' },
    ...extra,
  }
}

function chained(id: string, parent: string, extra: Partial<InstanceView> = {}): InstanceView {
  return inst(id, { via_instance_id: parent, via_remote_port: 53999, ...extra })
}

describe('readChainedCrewNotice', () => {
  const good = { id: 'c-2', name: 'C', sshHost: 'c-host', remotePort: 5476, port: 53999 }

  it('keeps the announcing gateway\u2019s own id for the crew', () => {
    // This id is the whole point of the notice: the parent mints the token and
    // looks the crew up by ITS id, so an id derived on the host side equals it
    // only by luck and the parent then answers 404 for a crew it holds.
    expect(readChainedCrewNotice(good)?.id).toBe('c-2')
  })

  it('drops a notice with no id rather than adopting one we would have to invent', () => {
    const { id: _dropped, ...noId } = good
    expect(readChainedCrewNotice(noId)).toBeNull()
  })

  it('drops an id outside the registry grammar', () => {
    // It ends up in a request path on the announcing gateway, so a path-shaped
    // id would address a different route there.
    expect(readChainedCrewNotice({ ...good, id: 'victim/disconnect?x=' })).toBeNull()
    expect(readChainedCrewNotice({ ...good, id: 'C-2' })).toBeNull()
    expect(readChainedCrewNotice({ ...good, id: '-leading' })).toBeNull()
  })

  it('refuses a hop port outside the range but tolerates an unusable remote port', () => {
    // The hop port is dialled, so a bad one is fatal. The crew's own gateway port
    // is only a record here, so the row is still usable without it.
    expect(readChainedCrewNotice({ ...good, port: 0 })).toBeNull()
    expect(readChainedCrewNotice({ ...good, port: 70000 })).toBeNull()
    expect(readChainedCrewNotice({ ...good, remotePort: 999999 })?.remotePort).toBe(0)
  })

  it('caps the two free-text fields instead of trusting their length', () => {
    const long = readChainedCrewNotice({ ...good, name: 'n'.repeat(500), sshHost: 'h'.repeat(500) })
    expect(long?.name).toHaveLength(CHAINED_NAME_MAX)
    expect(long?.sshHost).toHaveLength(CHAINED_HOST_MAX)
  })

  it('drops a notice that is not an object at all', () => {
    expect(readChainedCrewNotice(null)).toBeNull()
    expect(readChainedCrewNotice('mc-instance-ready')).toBeNull()
  })
})

describe('rowStateLabel', () => {
  it('stops claiming a state an ancestor has made false', () => {
    // The whole reason the row is dimmed: an ancestor hop is down, so nothing here
    // can reach this crew whatever its own tunnel thinks. Before, the row kept a
    // green "connected" beside the dimming and the two said opposite things for as
    // long as the outage lasted.
    expect(rowStateLabel({ state: 'connected', reachable: false, brokenAt: 'build-host' })).toBe(
      'unreachable \u2014 build-host is down',
    )
    // Nothing to name (a loop leaves its members unreachable from any root).
    expect(rowStateLabel({ state: 'connected', reachable: false })).toBe('unreachable')
  })

  it('names the ancestor that is down, not the hop that is merely unreachable', () => {
    // The depth-2 case, and the reason `brokenAt` exists at all. gpu-box rides
    // fresh-desktop, which is itself CONNECTED -- only build-host is down. Naming
    // the immediate hop would say "fresh-desktop is down", which is false, and would
    // send the reader to a row that is not the one to fix.
    const rows = chainRows([
      inst('build-host', { status: { state: 'error' } }),
      chained('fresh-desktop', 'build-host'),
      chained('gpu-box', 'fresh-desktop'),
    ])
    const at = (id: string) => rows.find(r => r.inst.id === id)!
    expect(at('fresh-desktop').brokenAt).toBe('build-host')
    expect(at('gpu-box').brokenAt).toBe('build-host')
    expect(rowStateLabel({ ...at('gpu-box'), state: 'connected' })).toContain('build-host')
    expect(rowStateLabel({ ...at('gpu-box'), state: 'connected' })).not.toContain('fresh-desktop')
    // A healthy chain names nothing.
    expect(at('build-host').brokenAt).toBe('')
  })

  it('leaves a reachable row reporting its own state', () => {
    // Asserted as the RULE rather than against the label table, which is not part of
    // the module's surface: a reachable row reports its own state, and a chained row
    // that is reachable reports exactly what an unchained one does.
    const chained2 = rowStateLabel({
      state: 'connected',
      reachable: true,
      brokenAt: 'build-host',
    })
    expect(chained2).not.toContain('unreachable')
    expect(chained2).toBe(rowStateLabel({ state: 'connected' }))
    // Undefined is the unchained case and must not read as unreachable.
    expect(rowStateLabel({ state: 'error' })).not.toContain('unreachable')
  })

  it('is what every row TITLE is built from, not the raw state', () => {
    // Structural, because the mismatch is between two labels for one row and a test of
    // either label alone passes while they disagree. `title` is also what a screen
    // reader falls back to, so a title from the raw state tells a sighted user
    // "unreachable" and a screen-reader user "connected" about the same row.
    //
    // Two sites had it: the host-side menu row and the relayed switcher's twin. Pinned
    // over the source so a third cannot be added quietly, read the way this repo
    // already reads a component from its own test.
    const src = readFileSync(path.join(__dirname, '../components/InstanceTabBar.tsx'), 'utf-8')
    const titles = src.split('\n').filter(l => /^\s*title: `/.test(l))
    expect(titles.length).toBeGreaterThan(0)
    const raw = titles.filter(l => /\bstateLabel\(/.test(l) && !/\browStateLabel\(/.test(l))
    expect(raw).toEqual([])
  })
})

describe('chainRows', () => {
  it('puts each crew before the crews reached through it, and records the depth', () => {
    const rows = chainRows([inst('b'), chained('c', 'b'), chained('d', 'c'), inst('other')])
    expect(rows.map(r => r.inst.id)).toEqual(['b', 'c', 'd', 'other'])
    expect(rows.map(r => r.depth)).toEqual([0, 1, 2, 0])
    expect(rows.map(r => r.parentName)).toEqual(['', 'b', 'c', ''])
  })

  it('keeps the incoming order among siblings', () => {
    // Tab order is a user-visible preference; the tree must reorder nothing it
    // does not have to.
    const rows = chainRows([inst('b'), inst('a'), chained('c', 'b')])
    expect(rows.map(r => r.inst.id)).toEqual(['b', 'c', 'a'])
  })

  it('treats a crew whose parent has no tab as a root rather than hiding it', () => {
    // The parent was never connected, so it has no tab. Dropping the child would
    // hide a crew that is genuinely connected.
    const rows = chainRows([chained('c', 'never-connected')])
    expect(rows).toHaveLength(1)
    expect(rows[0].depth).toBe(0)
    expect(rows[0].parentName).toBe('')
  })

  it('marks a crew unreachable when an ancestor hop is down', () => {
    // They share the ancestor's tunnel, so a child cannot outlive it however
    // recently its own state said 'connected'.
    const rows = chainRows([
      inst('b', { status: { instance_id: 'b', state: 'error' } }),
      chained('c', 'b'),
      chained('d', 'c'),
    ])
    expect(rows.map(r => [r.inst.id, r.reachable])).toEqual([
      ['b', true],
      ['c', false],
      ['d', false],
    ])
  })

  it('keeps a whole healthy chain reachable', () => {
    const rows = chainRows([inst('b'), chained('c', 'b'), chained('d', 'c')])
    expect(rows.every(r => r.reachable)).toBe(true)
  })

  it('emits every crew exactly once even when the registry holds a loop', () => {
    // A hand-edited registry can name a cycle. A row the user can see is what
    // lets them disconnect it and fix the file.
    const rows = chainRows([chained('a', 'b'), chained('b', 'a')])
    expect(rows.map(r => r.inst.id).sort()).toEqual(['a', 'b'])
    expect(new Set(rows.map(r => r.inst.id)).size).toBe(rows.length)
  })

  it('leaves a list with no chained crew exactly as it was', () => {
    const flat = [inst('a'), inst('b'), inst('c')]
    const rows = chainRows(flat)
    expect(rows.map(r => r.inst.id)).toEqual(['a', 'b', 'c'])
    expect(rows.every(r => r.depth === 0 && r.parentName === '' && r.reachable)).toBe(true)
  })
})

describe('announceChainedCrew', () => {
  const notice = { id: 'c', name: 'C', sshHost: 'c-host', remotePort: 5476, port: 53999 }

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  /** Stand in for a pane: `self !== top`, with a recording parent. */
  function asPane() {
    const posted: unknown[] = []
    const parent = { postMessage: (data: unknown) => posted.push(data) }
    vi.stubGlobal('window', { self: {}, top: {}, parent })
    return posted
  }

  it('does nothing at top level, where there is no host to tell', () => {
    const posted: unknown[] = []
    const shared = {}
    vi.stubGlobal('window', {
      self: shared,
      top: shared,
      parent: { postMessage: (d: unknown) => posted.push(d) },
    })
    expect(announceChainedCrew(notice)).toBe(false)
    expect(posted).toEqual([])
  })

  it('carries the crew and the hop port, and no credential', () => {
    const posted = asPane()
    expect(announceChainedCrew(notice)).toBe(true)
    expect(posted).toHaveLength(1)
    const msg = posted[0] as Record<string, unknown>
    expect(msg.type).toBe(CHAINED_CREW_MESSAGE)
    expect(msg.id).toBe('c')
    expect(msg.port).toBe(53999)
    // The whole reason this notice may travel through frame code: it names a
    // crew and a port, and nothing that grants access to either.
    const keys = Object.keys(msg).join(' ')
    expect(keys).not.toMatch(/token|secret|cookie|credential/i)
  })

  it('refuses a payload with no usable hop port', () => {
    const posted = asPane()
    for (const port of [0, -1, 70000, 1.5, Number.NaN]) {
      expect(announceChainedCrew({ ...notice, port })).toBe(false)
    }
    expect(announceChainedCrew({ ...notice, id: '' })).toBe(false)
    expect(posted).toEqual([])
  })

  it('stays silent when the post itself throws', () => {
    // A pane whose host predates the message, or a browser that refuses the
    // post, must not break the connect that just succeeded.
    vi.stubGlobal('window', {
      self: {},
      top: {},
      parent: {
        postMessage: () => {
          throw new Error('cross-origin')
        },
      },
    })
    expect(announceChainedCrew(notice)).toBe(false)
  })
})

describe('the relayed refusal outlives the panel', () => {
  afterEach(() => clearChainRefusal())

  const refuse = (reason: string, id = 'gpu-box') =>
    window.dispatchEvent(
      new MessageEvent('message', {
        source: window,
        data: { type: CHAINED_CREW_REFUSED_MESSAGE, v: 1, id, reason },
      }),
    )

  it('holds a refusal that arrives while nothing is subscribed', () => {
    // The host answers while the user is still watching the crew connect, which
    // can be long after Settings was closed. A listener mounted with the panel
    // drops it, and the crew then reads as connected-but-missing with no reason.
    expect(readChainRefusal()).toBeNull()
    refuse(
      'Connecting crew GPU box through build-host would put 3 machines between this dashboard and it, and 2 is the limit. Connect it from a dashboard closer to it.',
    )
    expect(readChainRefusal()?.reason).toContain('3 machines')
    expect(readChainRefusal()?.id).toBe('gpu-box')
  })

  it('notifies a subscriber and clears on dismiss', () => {
    let hits = 0
    const stop = subscribeChainRefusal(() => {
      hits += 1
    })
    refuse('loop detected')
    expect(hits).toBe(1)
    clearChainRefusal()
    expect(hits).toBe(2)
    expect(readChainRefusal()).toBeNull()
    stop()
  })

  it('caps the relayed reason and ignores a message of another type', () => {
    refuse('x'.repeat(CHAIN_REFUSAL_MAX + 50))
    expect(readChainRefusal()?.reason.length).toBe(CHAIN_REFUSAL_MAX)
    clearChainRefusal()
    window.dispatchEvent(
      new MessageEvent('message', { source: window, data: { type: 'mc-host-model', v: 1 } }),
    )
    expect(readChainRefusal()).toBeNull()
  })
})

describe('the relayed model a pane parses', () => {
  const model = (tab: Record<string, unknown>) =>
    parseHostModel({ type: 'mc-host-model', tabs: [{ id: 'c', name: 'C', ...tab }] })?.tabs[0]

  it('keeps the parent segment, so a pane chip squeezes the same half the window does', () => {
    // A pane cannot derive the chain: it never sees the host's registry. Any field
    // the chip reads that this parser drops is a field that is always undefined
    // inside every pane, which looks correct in the window and is dead in the pane.
    expect(model({ pathName: 'b \u203a C', pathParent: 'b' })?.pathParent).toBe('b')
  })

  it('carries every field the chained chip and row read', () => {
    const t = model({ depth: 1, reachable: false, pathName: 'b \u203a C', pathParent: 'b' })
    expect(t).toMatchObject({ depth: 1, reachable: false, pathName: 'b \u203a C', pathParent: 'b' })
  })

  it('degrades a malformed parent segment rather than passing it through', () => {
    expect(model({ pathParent: 42 })?.pathParent).toBeUndefined()
    expect(model({})?.pathParent).toBeUndefined()
  })
})

describe('an adopted crew and the tab filter', () => {
  it('gives no tab to a row that was only added', () => {
    // The contract the adoption path turns on. A row written by `POST
    // /api/instances` carries no connect intent, no status and no warm entry, so
    // adopting a crew WITHOUT connecting it leaves the promised top-level tab
    // missing and the crew reachable only from the Remote Crew list.
    const justAdded = chained('c', 'b', { was_connected: false, status: undefined })
    expect(visibleInstanceTabs([inst('b'), justAdded], {}).map(i => i.id)).toEqual(['b'])
  })

  it('gives it a tab once the connect intent is on the row', () => {
    const connected = chained('c', 'b', { was_connected: true, status: undefined })
    expect(visibleInstanceTabs([inst('b'), connected], {}).map(i => i.id)).toEqual(['b', 'c'])
  })
})

describe('re-announcing a crew the host already has a row for', () => {
  it('connects even when the hop port has not moved', () => {
    // The defect this closes: deciding from the port alone returned early, so a
    // crew reconnected on the parent after THIS gateway restarted kept its row
    // and its dead tab while running perfectly on the parent.
    expect(chainAdoptionPlan(53999, 53999)).toEqual({ repoint: false, connect: true })
  })

  it('repoints and connects when the parent serves it somewhere new', () => {
    expect(chainAdoptionPlan(53999, 54000)).toEqual({ repoint: true, connect: true })
  })

  it('repoints a row that never carried a port', () => {
    expect(chainAdoptionPlan(undefined, 54000)).toEqual({ repoint: true, connect: true })
    expect(chainAdoptionPlan(0, 54000)).toEqual({ repoint: true, connect: true })
  })

  it('connects on every shape of announcement', () => {
    // `connect` is unconditional by design, so state it as its own assertion
    // rather than leaving it implied by the cases above.
    for (const [had, announced] of [[53999, 53999], [53999, 1], [undefined, 65535], [0, 1]] as const) {
      expect(chainAdoptionPlan(had, announced).connect).toBe(true)
    }
  })
})

describe('reading the gateway refusal code off a failed add', () => {
  const withBody = (body: unknown) => ({ message: 'prose for a person', body })

  it('reads the code the gateway sent', () => {
    expect(chainRefusalCode(withBody('{"error":"already here","code":"chain_duplicate"}'))).toBe(
      'chain_duplicate',
    )
    expect(chainRefusalCode(withBody('{"error":"too deep","code":"chain_too_deep"}'))).toBe(
      'chain_too_deep',
    )
  })

  it('answers empty for anything it cannot read a code from', () => {
    // Empty means "no code", which every caller treats as a refusal to relay --
    // the safe direction, since silence is reserved for the one benign code.
    expect(chainRefusalCode(undefined)).toBe('')
    expect(chainRefusalCode(null)).toBe('')
    expect(chainRefusalCode(new Error('plain'))).toBe('')
    expect(chainRefusalCode(withBody(''))).toBe('')
    expect(chainRefusalCode(withBody('not json'))).toBe('')
    expect(chainRefusalCode(withBody('{"error":"no code field"}'))).toBe('')
    expect(chainRefusalCode(withBody('{"code":7}'))).toBe('')
    expect(chainRefusalCode(withBody('null'))).toBe('')
    expect(chainRefusalCode(withBody({ code: 'chain_duplicate' }))).toBe('')
  })

  it('does not branch on the human message', () => {
    // The message is prose and is the wrong thing to key behaviour off.
    expect(chainRefusalCode({ message: 'chain_duplicate', body: '{}' })).toBe('')
  })
})
