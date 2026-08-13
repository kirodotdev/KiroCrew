import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'

/**
 * The terminal is reachable from two places — the docked bottom panel and a chat
 * side-panel tab — and they must offer the SAME completion experience.
 *
 * Today they do by construction rather than by coincidence: both render
 * `CliPanel`, which is the only component that constructs an xterm, and it mounts
 * `TerminalCompletion` once for every surface. That is a stronger guarantee than
 * two call sites kept in sync by hand, but it is only a guarantee while it stays
 * true — a second surface that builds its own `new Terminal(...)` would silently
 * ship without completions, and no behavioural test would notice, because each
 * surface's own tests would still pass.
 *
 * So this pins the SHAPE. It is a structural test on purpose: the thing worth
 * asserting is "there is exactly one terminal implementation", which is a fact
 * about the module graph, not about any rendered output.
 */

const SRC = join(__dirname, '..')

function walk(dir: string): string[] {
  const out: string[] = []
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) {
      // `test/` holds xterm stand-ins, which are the point of those files.
      if (name === 'test' || name === 'node_modules') continue
      out.push(...walk(path))
    } else if (/\.tsx?$/.test(name)) {
      out.push(path)
    }
  }
  return out
}

const files = walk(SRC)
const read = (rel: string) => readFileSync(join(SRC, rel), 'utf8')

describe('terminal surface parity', () => {
  it('constructs an xterm in exactly one place', () => {
    // The anti-divergence assertion. A second construction site is a second
    // terminal implementation, and the completion menu would not follow it.
    const sites = files
      .filter(f => /new Terminal\s*\(/.test(readFileSync(f, 'utf8')))
      .map(f => f.slice(SRC.length + 1))
    expect(sites).toEqual(['components/CliPanel.tsx'])
  })

  it('mounts the completion menu in that one place', () => {
    expect(read('components/CliPanel.tsx')).toContain('<TerminalCompletion')
  })

  it('reaches the terminal through CliPanel from both surfaces', () => {
    // Docked bottom panel and chat side-panel tab. Both render the shared host,
    // so both get path completion AND the command tier with no per-surface wiring.
    expect(read('components/BottomTerminalPanel.tsx')).toMatch(/CliPanel/)
    expect(read('pages/chat/SidePanel.tsx')).toMatch(/<CliPanel\b/)
  })

  it('passes the session id and visibility through to the completion menu', () => {
    // `sessionId` keys the completion request and `active` is what stops a hidden
    // pane from polling; a surface that dropped either would render a menu that
    // completes against the wrong shell or one that never closes.
    const src = read('components/CliPanel.tsx')
    expect(src).toMatch(/<TerminalCompletion\s+term=\{term\}\s+sessionId=\{sessionId\}\s+active=\{visible\}/)
  })
})
