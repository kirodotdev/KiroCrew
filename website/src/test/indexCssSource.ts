/**
 * The source text of the global stylesheet, for tests that measure it.
 *
 * `src/index.css` is the entry. It keeps the Tailwind header, the base layer, the
 * ambient backdrop and the app-icon tints, and pulls every other rule in from a
 * partial in `src/styles/` with one `@import "./styles/<name>.css";` line per
 * partial, in cascade order. Tailwind inlines each of those imports where it
 * stands, so the stylesheet the browser gets is the entry with every partial
 * spliced in at its import. (The other sheets in `src/styles/` are imported by
 * `main.tsx`, not by index.css, and are not part of this text.)
 *
 * `readIndexCss()` returns exactly that text. A test asking whether a rule exists,
 * whether something appears anywhere in the stylesheet, or which of two rules
 * comes later reads the same document the cascade does, so moving a rule between
 * partials neither hides it nor changes what the test measures. Only the
 * `./styles/` partials are spliced: the Tailwind theme, Preflight and
 * tailwind-theme.css imports stay as written. `whereInIndexCss(n)` names the file
 * and line a line of that text comes from, for a failure message.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const SRC = resolve(__dirname, '..')

/** One `@import "./styles/<name>.css";` line in index.css, and its partial's path. */
const PARTIAL_IMPORT = /^@import "\.\/(styles\/[\w-]+\.css)";\n/gm

export function readIndexCss(): string {
  return readFileSync(resolve(SRC, 'index.css'), 'utf8').replace(PARTIAL_IMPORT, (_line, partial: string) =>
    readFileSync(resolve(SRC, partial), 'utf8'),
  )
}

/** Where line `n` (1-based) of `readIndexCss()` lives, as `<file>:<line>` under `src/`. */
export function whereInIndexCss(n: number): string {
  let at = 0
  const entry = readFileSync(resolve(SRC, 'index.css'), 'utf8').split('\n').slice(0, -1)
  for (const [i, line] of entry.entries()) {
    const partial = /^@import "\.\/(styles\/[\w-]+\.css)";$/.exec(line)?.[1]
    const size = partial ? readFileSync(resolve(SRC, partial), 'utf8').split('\n').length - 1 : 1
    if (n <= at + size) return partial ? `${partial}:${n - at}` : `index.css:${i + 1}`
    at += size
  }
  return `index.css:${n}`
}
