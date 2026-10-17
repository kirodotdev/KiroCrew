/**
 * Every tool-approval decide built by hand under `src/apps` names the exact
 * request its card showed.
 *
 * A request id is minted by the caller that raised the approval and can recur,
 * so a decide that carries only the id resolves whichever request holds it at
 * that moment: a card left up for request A decides request B. The owner-bound
 * targets are `origin=coordinator` with the slot and instance on
 * `POST /api/approvals/{id}/{action}`, and `origin: 'native'` with the row's
 * `request_mid` on `POST /api/chat/slots/{slot}/approve`.
 *
 * Each hand-built POST to either route must carry `origin` at its call site. The
 * one exception is the slot route's durable trust actions, which that route takes
 * only by request id; such a call is marked `approval-target: trust-only` so the
 * exception is visible where it is made.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

const SRC = join(__dirname, '..')
const APPS = join(SRC, 'apps')

function* walk(dir: string): Generator<string> {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) {
      if (name === 'test' || name === '__tests__') continue
      yield* walk(p)
      continue
    }
    if (/\.(ts|tsx)$/.test(name) && !/\.(test|stories|spec)\./.test(name)) yield p
  }
}

/** Template-literal URLs of the two decide routes (comments name them with `{id}`). */
const DECIDE_URL = /`\/api\/approvals\/\$\{|`\/api\/chat\/slots\/\$\{[^`]*\}\/approve`/g
const TRUST_ONLY = 'approval-target: trust-only'
const WINDOW = 700
/** An `origin:` key; `credentials: 'same-origin'` is not one. */
const ORIGIN_KEY = /(^|[^-\w])origin\s*:/

interface Site { file: string; line: number; text: string }

function decideSites(): { bound: Site[]; unbound: Site[] } {
  const bound: Site[] = []
  const unbound: Site[] = []
  for (const file of walk(APPS)) {
    const src = readFileSync(file, 'utf8')
    for (const m of src.matchAll(DECIDE_URL)) {
      const at = m.index ?? 0
      const site = {
        file: relative(SRC, file),
        line: src.slice(0, at).split('\n').length,
        text: m[0],
      }
      const after = src.slice(at, at + WINDOW)
      const before = src.slice(Math.max(0, at - 300), at)
      if (ORIGIN_KEY.test(after) || before.includes(TRUST_ONLY)) bound.push(site)
      else unbound.push(site)
    }
  }
  return { bound, unbound }
}

describe('apps approval decide guard', () => {
  it('finds the hand-built decide sites it guards', () => {
    // A guard that matches nothing passes vacuously; the Mochi panel bridge is
    // known to build these requests.
    const { bound, unbound } = decideSites()
    const files = new Set([...bound, ...unbound].map(s => s.file))
    expect([...files].some(f => f.includes('mochi/panel/panelBridge.ts'))).toBe(true)
  })

  it('every decide under src/apps carries an owner-bound target', () => {
    expect(decideSites().unbound).toEqual([])
  })
})
