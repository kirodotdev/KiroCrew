/**
 * Guard over website/src and website/electron: no frame scheduler may latch on a
 * pending rAF handle.
 *
 * `requestAnimationFrame` does not promise that a handle it returns will ever
 * have its callback run. A frame queued for a page the browser then moves into
 * the back/forward cache is dropped, and happy-dom returns a truthy `{}` when
 * the window is closed or a timer-loop limit trips. A scheduler written as
 *
 *     if (rafId) return
 *     rafId = requestAnimationFrame(() => { rafId = 0; work() })
 *
 * therefore latches permanently on the first such handle, and whatever it was
 * keeping in sync silently stops updating for the life of the page. That defect
 * shipped in four separate schedulers before anyone noticed, so it is guarded as
 * a class rather than patched per site: cancel-and-reschedule coalesces exactly
 * as well and cannot latch.
 *
 * This is a grep guard on purpose. A behavioural test can only cover the
 * schedulers someone remembered to write one for, and the failure mode here is a
 * NEW site copying the old shape.
 */

import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

import { describe, expect, it } from 'vitest'

const ROOTS = [join(__dirname, '..'), join(__dirname, '..', '..', 'electron')]

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) {
      if (entry === 'node_modules' || entry === 'test' || entry === 'build') continue
      walk(full, out)
    } else if (/\.(ts|tsx|js|jsx)$/.test(entry)) {
      out.push(full)
    }
  }
  return out
}

/**
 * An early return keyed on a frame handle, on the line(s) directly before a
 * requestAnimationFrame assignment to that same handle. Deliberately narrow: it
 * matches the latch, not every `if (x) return`.
 */
const LATCH =
  /if\s*\(\s*(_?\w*[rR]af\w*)\s*\)\s*(?:\{\s*)?return[\s;}]*[\r\n]+\s*\1\s*=\s*(?:window\.)?(?:requestAnimationFrame|raf)\s*\(/

describe('frame schedulers', () => {
  it('never defer to a pending frame handle that may never fire', () => {
    const offenders: string[] = []
    for (const root of ROOTS) {
      for (const file of walk(root)) {
        const source = readFileSync(file, 'utf8')
        if (LATCH.test(source)) offenders.push(relative(root, file))
      }
    }
    expect(offenders, [
      'These schedulers latch on a pending requestAnimationFrame handle.',
      'A handle whose callback never fires (bfcache-dropped frame, closed',
      'window) then blocks every later signal permanently. Use',
      'cancel-and-reschedule instead -- it coalesces identically:',
      '',
      '  if (rafId) cancelAnimationFrame(rafId)',
      '  rafId = requestAnimationFrame(() => { rafId = 0; work() })',
    ].join('\n')).toEqual([])
  })
})
