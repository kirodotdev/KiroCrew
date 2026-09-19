/**
 * Pins the gateway-connectivity pairing invariant as a CHECK, not a comment.
 *
 * `dashboardSlice.connected` (store) and the seam in `utils/errorReport`
 * (read by AskAgentButton outside the Provider) must move together. The
 * `markGatewayConnected`/`markGatewayDisconnected` helpers in `useWebSocket.ts`
 * are the only sanctioned way to move them — a dispatch site that called
 * `dispatch(sseConnected())` directly would set the store flag while the seam
 * kept its old answer, and the hand-off button would appear or vanish out of
 * step with the rest of the dashboard. Nothing at runtime can detect that
 * split, so the invariant is pinned here at the source level: the raw
 * dispatches may appear only inside the two helpers.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const HOOK = path.join(SRC, 'hooks', 'useWebSocket.ts')

const DISPATCH_RE = /dispatch\(\s*(sseConnected|sseDisconnected)\(\)\s*\)/g

function* sourceFiles(dir: string): Generator<string> {
  for (const name of readdirSync(dir)) {
    const p = path.join(dir, name)
    if (statSync(p).isDirectory()) {
      if (name === 'node_modules' || name === 'test') continue
      yield* sourceFiles(p)
    } else if (/\.(ts|tsx)$/.test(name) && !/\.(test|cov80\.test)\.tsx?$/.test(name)) {
      yield p
    }
  }
}

/** The body text of a top-level `function <name>(...) { ... }` declaration. */
function functionBody(source: string, name: string): string {
  const start = source.indexOf(`function ${name}(`)
  expect(start, `function ${name} must exist in useWebSocket.ts`).toBeGreaterThan(-1)
  const open = source.indexOf('{', start)
  let depth = 0
  for (let i = open; i < source.length; i++) {
    if (source[i] === '{') depth++
    else if (source[i] === '}' && --depth === 0) return source.slice(open, i + 1)
  }
  throw new Error(`unbalanced braces walking function ${name}`)
}

describe('gateway connectivity pairing invariant', () => {
  it('raw sseConnected/sseDisconnected dispatches live only inside the two helpers', () => {
    const hookSource = readFileSync(HOOK, 'utf-8')
    const connectedBody = functionBody(hookSource, 'markGatewayConnected')
    const disconnectedBody = functionBody(hookSource, 'markGatewayDisconnected')

    // The helpers each hold exactly one raw dispatch, paired with the seam write.
    expect(connectedBody).toMatch(/dispatch\(sseConnected\(\)\)/)
    expect(connectedBody).toMatch(/setGatewayConnected\(true\)/)
    expect(disconnectedBody).toMatch(/dispatch\(sseDisconnected\(\)\)/)
    expect(disconnectedBody).toMatch(/setGatewayConnected\(false\)/)

    // And no raw dispatch exists anywhere outside them. Strip the helper bodies
    // from the hook's source, then demand zero matches across the whole tree.
    // Line-wise, skipping comment lines: useWebSocket documents a deliberate
    // no-op with the literal text `dispatch(sseDisconnected())` in a comment.
    const hookOutsideHelpers = hookSource.replace(connectedBody, '').replace(disconnectedBody, '')
    const offenders: string[] = []
    for (const file of sourceFiles(SRC)) {
      const text = file === HOOK ? hookOutsideHelpers : readFileSync(file, 'utf-8')
      const hit = text.split('\n').some(line => {
        const trimmed = line.trim()
        if (trimmed.startsWith('//') || trimmed.startsWith('*')) return false
        DISPATCH_RE.lastIndex = 0
        return DISPATCH_RE.test(line)
      })
      if (hit) offenders.push(path.relative(SRC, file))
    }
    expect(offenders, 'raw connectivity dispatches outside markGatewayConnected/markGatewayDisconnected split the store flag from the errorReport seam AskAgentButton reads').toEqual([])
  })
})
