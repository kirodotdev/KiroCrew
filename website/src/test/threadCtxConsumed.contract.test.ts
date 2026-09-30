/**
 * A surface that SUPPLIES `ctx.threads` must also CONSUME it.
 *
 * `ChatPage` passed `threads: threadHooks` into every render context and listed
 * the hooks in its memo dependencies, while its own bubble renderer called
 * neither `replyInThreadFor` nor `threadFooterFor`. The result: no thread opener
 * and no thread badge anywhere on the dashboard's main chat surface, with every
 * unit test green -- because the component tests hand `AssistantMessage` the
 * `onReplyInThread` prop directly, so none of them can observe a host that
 * forgets to pass it.
 *
 * Source-level on purpose. The bug is one host not wiring a prop, which is a
 * fact about that file rather than about any rendered tree, and a render test
 * for it would need the whole page mounted with a live query client to assert
 * the absence of a control. This reads each supplier and checks it also names a
 * consumer, which is the exact invariant and costs nothing.
 */

import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

import { describe, expect, it } from 'vitest'

const WEBSITE = join(__dirname, '..', '..')
const SRC = join(WEBSITE, 'src')

/** Files that hand a `MessageRenderContext` its `threads` member. */
function suppliers(): string[] {
  const hits: string[] = []
  const walk = (dir: string) => {
    for (const name of readdirSync(dir)) {
      const p = join(dir, name)
      if (statSync(p).isDirectory()) {
        walk(p)
        continue
      }
      if (!/\.(ts|tsx)$/.test(name)) continue
      if (/\.test\.(ts|tsx)$/.test(name)) continue
      const text = readFileSync(p, 'utf8')
      // Both conditions, because `threads:` alone is a common member name --
      // comment threads on an artifact, a speech setting -- and none of those
      // are a message render context. Naming the context type is what makes a
      // file a supplier of THIS `threads`.
      if (!text.includes('MessageRenderContext')) continue
      if (/\bthreads:\s*[A-Za-z_$]/.test(text)) {
        hits.push(relative(WEBSITE, p).split('\\').join('/'))
      }
    }
  }
  walk(SRC)
  return hits.sort()
}

describe('ctx.threads', () => {
  it('is consumed by every surface that supplies it', () => {
    const unconsumed: string[] = []
    for (const rel of suppliers()) {
      const text = readFileSync(join(WEBSITE, rel), 'utf8')
      // The opener is what makes a thread openable; the footer is the way back
      // into one. A supplier naming neither has wired the hooks to nothing.
      const opens = text.includes('replyInThreadFor') || text.includes('onReplyInThread')
      const returns = text.includes('threadFooterFor')
      if (!opens || !returns) unconsumed.push(`${rel} (opener: ${opens}, footer: ${returns})`)
    }
    expect(unconsumed).toEqual([])
  })

  it('has at least one supplier, so the walk above can fail', () => {
    // Without this the test passes trivially the day the walk breaks: an empty
    // supplier list satisfies "every supplier consumes it".
    expect(suppliers().length).toBeGreaterThan(0)
  })
})
