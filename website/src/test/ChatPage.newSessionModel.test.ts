// A new chat session must NOT be created carrying a model the user did not pick.
//
// A session's model is a permanent pin: the runtime reads `slot.model or
// agent_model`, so once slot.model is set it wins for every later turn. ChatPage
// used to seed `pendingModel` at mount from the backend resolver -- which answers
// "what would run", correct for the composer chip and wrong as a create value --
// so every new chat was pinned to whatever the four-tier chain resolved at page
// load. An agent left on Auto never re-resolved, and later changes to the agent or
// the global default never reached the session (#2035).
//
// Omitting `model` is what preserves the chain: `SessionManager.get_or_create`
// documents that a `None` model falls back to the global `agent.model` config,
// only when the named agent does not pin its own, and stays `None` for a sentinel
// like "auto" so the backend resolves from the agent's own JSON config.
//
// WHY A SOURCE GUARD RATHER THAN A RENDER TEST: the observable contract is the
// absence of a field in the `POST /api/chat/slots` body, and the paths that build
// it (first send, and the empty-state "Start a new chat" button) sit behind a
// ChatPage render harness that mocks ChatInput away -- so the send path is not
// reachable and a render test could not actually observe the payload. This repo
// already uses structural guards for invariants of exactly this shape (see the
// `str()`-coercion guard in test/test_agent_default_model.py and the conftest
// audit in test/test_mcp_gateway_pool_integ.py). The invariant here is "no
// resolver result ever reaches setPendingModel", which is a property of the
// source and is checked exactly.

import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const CHAT_PAGE = join(__dirname, '..', 'pages', 'ChatPage.tsx')
const source = readFileSync(CHAT_PAGE, 'utf-8')

/** Arguments that are legitimate for a create-time model: nothing, or an
 *  explicit user pick. `v` is the setter's own parameter; `modelName` is the
 *  argument of switchModel, i.e. the model the user clicked. */
const ALLOWED_ARGS = new Set(["''", '', 'v', 'modelName'])

describe('ChatPage — a new session carries no model the user did not pick', () => {
  it('never passes a resolver result to setPendingModel', () => {
    const calls = [...source.matchAll(/setPendingModel\(([^)]*)\)/g)].map(m => m[1].trim())
    expect(calls.length).toBeGreaterThan(0)
    const offenders = calls.filter(arg => !ALLOWED_ARGS.has(arg))
    expect(offenders).toEqual([])
  })

  it('resolves a model for DISPLAY only -- one call site, not two', () => {
    // Two call sites is the seeding coming back: one for the chip, one feeding
    // pendingModel. One call site means display only.
    const hits = [...source.matchAll(/provider\.resolveModel\(/g)]
    expect(hits).toHaveLength(1)
  })

  it('has no mount-time query that seeds the next session\'s model', () => {
    // The removed shapes, named explicitly so a revert is caught by name rather
    // than only by the argument check above.
    expect(source).not.toMatch(/new-session-model/)
    expect(source).not.toMatch(/resolveNewSessionModel/)
    expect(source).not.toMatch(/\.then\(m => setPendingModel\(/)
  })

  it('clears the previous pick when the agent changes on a slotless page', () => {
    // Switching agent must not carry the old agent's explicit pick forward, and
    // must not re-seed a resolved one either -- it clears, so createSlot omits
    // `model` and the backend resolves the NEW agent's chain.
    const switchAgent = source.slice(source.indexOf('const switchAgent = useCallback'))
    const body = switchAgent.slice(0, switchAgent.indexOf('}, ['))
    expect(body).toMatch(/setPendingModel\(''\)/)
    expect(body).not.toMatch(/resolveModel/)
  })
})
