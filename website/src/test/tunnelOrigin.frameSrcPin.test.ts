import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { isEmbeddableLoopbackOrigin } from '../lib/tunnelOrigin'

// A cross-LANGUAGE contract, so no type can hold it. `isEmbeddableLoopbackOrigin`
// claims to match `_LOOPBACK_FRAME_SRC` + `_INSTANCES_FRAME_SRC_EXTRA` in
// server.py EXACTLY. Edit either literal without the client and a remote-crew pane
// is mounted where the CSP blanks it, or refused where the CSP now allows it.
// Exact match, so both directions are checked.

/** The space-separated sources of a `NAME = (...)` or `NAME = "..."` literal in server.py. */
function serverSources(name: string): string[] {
  const server = readFileSync(
    join(__dirname, '../../../src/kiro_crew/dashboard/server.py'),
    'utf8',
  )
  const m = new RegExp(`^${name} = (\\([\\s\\S]*?\\n\\)|"[^"\\n]*")`, 'm').exec(server)
  expect(m, `${name} literal not found in server.py`).not.toBeNull()
  const text = [...m![1].matchAll(/"([^"]*)"/g)].map(s => s[1]).join('')
  return text.split(/\s+/).filter(Boolean)
}

// A wildcard host-source is probed with one single-label name, the shape the
// client documents for `*.localhost`.
const WILDCARD_PROBE = 'kirocrew'

/** The (protocol, hostname) pairs the CSP admits, as `"http:|127.0.0.1"` keys. */
function serverPairs(): Set<string> {
  const pairs = new Set<string>()
  for (const src of [
    ...serverSources('_LOOPBACK_FRAME_SRC'),
    ...serverSources('_INSTANCES_FRAME_SRC_EXTRA'),
  ]) {
    const m = /^(https?):\/\/([^/:]+):\*$/.exec(src)
    expect(m, `frame-src source ${src} is not a scheme://host:* entry`).not.toBeNull()
    const host = m![2].startsWith('*.') ? `${WILDCARD_PROBE}${m![2].slice(1)}` : m![2]
    pairs.add(`${m![1]}:|${host}`)
  }
  return pairs
}

describe('isEmbeddableLoopbackOrigin vs gateway frame-src', () => {
  it('admits exactly the (scheme, host) pairs the server literals carry', () => {
    const allowed = serverPairs()
    const hosts = new Set([
      ...[...allowed].map(k => k.split('|')[1]),
      // Hosts the CSP leaves out today: IPv6 loopback, a public name, a LAN address.
      '::1', '[::1]', 'example.com', '10.0.0.5',
    ])
    const mismatches: string[] = []
    for (const protocol of ['http:', 'https:']) {
      for (const host of hosts) {
        const want = allowed.has(`${protocol}|${host}`)
        if (isEmbeddableLoopbackOrigin(protocol, host) !== want) {
          mismatches.push(`${protocol}//${host}: client ${!want}, server ${want}`)
        }
      }
    }
    expect(mismatches).toEqual([])
  })
})
