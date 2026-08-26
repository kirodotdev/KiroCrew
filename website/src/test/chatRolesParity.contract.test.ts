/**
 * Role-parity contract between the two transcript render paths.
 *
 * The dashboard renders chat messages through TWO paths: ChatPage's inline
 * `renderMessage` if-chain, and the registry in `app-sdk/messageRenderers`
 * that every other surface (SideChat, ChatPane, ChatEmbed) consumes via
 * ChatMessageList. A role wired in only one path ships a surface where that
 * message renders as raw text or not at all — `mcp_oauth` shipped exactly
 * this way once, wired in app-sdk but not in the main chat.
 *
 * Until ChatPage consumes the registry directly (the chat-core extraction's
 * later phase), this contract is the guard: every role literal that ChatPage's
 * source dispatches on must be CLAIMED by the registry, or be explicitly
 * listed here as chrome-only with a reason. Adding a role branch to ChatPage
 * without touching either list fails this test.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { defaultMessageRenderers } from '../app-sdk/messageRenderers'

/**
 * Roles ChatPage handles that are deliberately NOT a registry row. Each entry
 * must say why it is chrome rather than a transcript row type — an entry
 * without a reason is a parity gap hiding behind the allowlist.
 */
const CHROME_ONLY_ROLES: Record<string, string> = {
  // Rendered as the QueueStack card rail above the composer, not as a
  // transcript row (the registry deliberately draws nothing for it).
  queued: 'composer rail, not a transcript row',
  // Approval flow: resolved inline into grouped tool rows; the standalone
  // role is chrome that the permission cards own.
  permission: 'approval cards own it; grouped, never a standalone row',
  // Pseudo-role: normalized to `assistant` before dispatch on both paths.
  streaming: 'alias of assistant during a live turn',
}

/**
 * Roles only the registry claims, with the reason each is legitimate. These
 * arrive on surfaces that read the raw snapshot endpoints (app embeds, side
 * sessions), whose payloads carry wire-shape roles ChatPage's normalized
 * store never sees.
 */
const REGISTRY_ONLY_ROLES: Record<string, string> = {
  tool_call: 'raw snapshot wire shape; ChatPage store normalizes to tool',
  tool_result: 'raw snapshot wire shape; ChatPage store normalizes to tool',
  system: 'lifecycle marker in raw snapshots; deliberately undrawn',
  done: 'lifecycle marker in raw snapshots; deliberately undrawn',
}

function chatPageRoleLiterals(): Set<string> {
  const src = readFileSync(resolve(__dirname, '../pages/ChatPage.tsx'), 'utf8')
  const roles = new Set<string>()
  // Both dispatch shapes used in the file: `m.role === 'x'` and
  // `messages[i].role === 'x'` (and their !== variants — a negative dispatch
  // still means the code KNOWS the role).
  for (const m of src.matchAll(/\.role\s*[!=]==\s*'([a-z_]+)'/g)) roles.add(m[1])
  return roles
}

function registryClaimedRoles(): Set<string> {
  const roles = new Set<string>()
  for (const r of defaultMessageRenderers) {
    for (const role of r.roles) if (role !== '*') roles.add(role)
  }
  return roles
}

describe('chat role parity (ChatPage renderMessage vs app-sdk registry)', () => {
  it('every role ChatPage dispatches on is claimed by the registry or allowlisted as chrome', () => {
    const claimed = registryClaimedRoles()
    const missing = [...chatPageRoleLiterals()].filter(
      role => !claimed.has(role) && !(role in CHROME_ONLY_ROLES),
    )
    // A failure here means a role renders in the main chat but is invisible
    // (or raw) in SideChat / ChatPane / ChatEmbed — register it in
    // app-sdk/messageRenderers, or add it to CHROME_ONLY_ROLES with a reason.
    expect(missing).toEqual([])
  })

  it('the chrome allowlist carries no stale entries', () => {
    const known = chatPageRoleLiterals()
    const stale = Object.keys(CHROME_ONLY_ROLES).filter(role => !known.has(role))
    // An allowlist entry for a role ChatPage no longer mentions is dead
    // weight that would silently excuse a future regression — remove it.
    expect(stale).toEqual([])
  })

  it('the registry itself only claims roles ChatPage knows (no orphaned surface-only roles)', () => {
    const known = chatPageRoleLiterals()
    const orphaned = [...registryClaimedRoles()].filter(
      role => !known.has(role) && !(role in REGISTRY_ONLY_ROLES),
    )
    // A role only the registry knows renders on app surfaces but as raw text
    // in the MAIN chat — the exact defect class this contract exists to stop
    // (that is how mcp_oauth shipped). Wire it into ChatPage too, or record
    // why it is a wire-shape role in REGISTRY_ONLY_ROLES.
    expect(orphaned).toEqual([])
  })

  it('the registry-only allowlist carries no stale entries', () => {
    const claimed = registryClaimedRoles()
    const stale = Object.keys(REGISTRY_ONLY_ROLES).filter(role => !claimed.has(role))
    expect(stale).toEqual([])
  })

  it('fails closed: every role dispatch in ChatPage uses the shape the extractor parses', () => {
    const src = readFileSync(resolve(__dirname, '../pages/ChatPage.tsx'), 'utf8')
    // The extractor only understands `<expr>.role ===/!== '<literal>'`. Any
    // other dispatch idiom — switch(m.role), a lookup map, comparison against
    // a variable — would be invisible to the parity checks above, so its mere
    // presence fails this contract. If you add one, extend the extractor in
    // this file to parse it rather than allowlisting it here.
    const comparisons = [...src.matchAll(/\.role\s*[!=]==\s*(\S)/g)]
    const nonLiteral = comparisons.filter(m => m[1] !== "'")
    expect(nonLiteral.map(m => m[0])).toEqual([])
    expect([...src.matchAll(/switch\s*\([^)]*\.role/g)].map(m => m[0])).toEqual([])
  })
})
