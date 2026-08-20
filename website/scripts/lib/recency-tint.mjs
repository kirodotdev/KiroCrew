/**
 * The recency tint's widest OPAQUE stripe, read from its own source.
 *
 * The session row's status marker has to clear the band this stripe paints at the
 * row's left edge — that clearance is the whole subject of
 * `capture-session-status-marker.mjs` and one of the checks in
 * `capture-session-row-grid.mjs`. Both harnesses used to hardcode `7`, copied from
 * `MAX_W` in `src/utils/recencyTint.ts`. `MAX_W` is a function-local const, so it
 * cannot be imported (and these are plain `.mjs` scripts with no TS loader), which
 * is how the copy got there — but a copy means widening the real stripe leaves both
 * harnesses asserting the OLD width, and the collision they exist to catch would
 * pass through the gap.
 *
 * So the value is read out of the source at run time and the read FAILS LOUD: a
 * renamed or restructured const stops the harness rather than silently reverting it
 * to a stale literal.
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const SRC = resolve(dirname(fileURLToPath(import.meta.url)), '../../src/utils/recencyTint.ts')

/** @returns {number} `MAX_W` from `recencyTintShadow` — px, the opaque stripe's cap. */
export function recencyTintMaxPx() {
  const src = readFileSync(SRC, 'utf8')
  const m = /\bMAX_W\s*=\s*(\d+(?:\.\d+)?)/.exec(src)
  if (!m) {
    throw new Error(`could not read MAX_W from ${SRC} — the recency tint's stripe width `
      + 'moved or was renamed; update scripts/lib/recency-tint.mjs to match it rather '
      + 'than hardcoding a width here')
  }
  return Number(m[1])
}
