/**
 * Role-parity contract between ChatPage and the transcript renderer registry.
 *
 * Every chat surface renders rows through `app-sdk/messageRenderers`. Until
 * chat-core P5-a, ChatPage was the exception: an inline `renderMessage`
 * if-chain that had to be kept in step with the registry by hand, and the
 * defect class that bought this test -- `mcp_oauth` wired in app-sdk and
 * rendered as raw text in the main chat -- lived in that gap. ChatPage now
 * DISPATCHES through the registry (`resolveRenderer` over
 * `mergeRenderers(chatPageRenderers)`), so a role registered once renders on
 * every surface by construction. What is left to guard:
 *
 * 1. The dispatch stays registry-driven: the renderer block in ChatPage
 *    contains no `if (m.role === '…')` dispatch of its own.
 * 2. ChatPage's host entries either OVERRIDE a default (same id, page chrome
 *    layered on the shared row) or are one of the documented page-only shape
 *    entries below -- an undocumented id is a fork of the registry in disguise.
 * 3. Role literals ChatPage still uses OUTSIDE the renderer (chrome logic:
 *    footer rules, queue rail, last-error lookup, permission grouping) name
 *    roles the registry claims, or are allowlisted as chrome with a reason.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { defaultMessageRenderers } from '../app-sdk/messageRenderers'

/**
 * Roles ChatPage's CHROME logic names that are deliberately NOT a registry
 * row. Each entry must say why -- an entry without a reason is a parity gap
 * hiding behind the allowlist. (`queued` and `streaming` left this list with
 * P5-a: the registry claims both -- `undrawn` and the assistant entry.)
 */
const CHROME_ONLY_ROLES: Record<string, string> = {
  // Approval flow: resolved inline into grouped tool rows (GROUPED_ROLES);
  // the standalone role is chrome that the permission cards own. ChatPage's
  // own `permission` entry draws nothing for the same reason.
  permission: 'approval cards own it; grouped, never a standalone row',
}

/**
 * Host entries that do not override a default. ChatPage's host list is the
 * dashboard's shared row set (`createTranscriptRenderers`, which ChatPane
 * also uses) plus the page-only entries, so both are listed: each is a SHAPE
 * entry (`roles: ['*']` + `match`), a role the SDK has no row for, or a
 * deliberately undrawn role, with the reason it is not a registry default.
 */
const PAGE_ONLY_ENTRY_IDS: Record<string, string> = {
  // -- shared dashboard set (pages/chat/transcriptRenderers.tsx) --
  thinking_block: 'ThinkingBlock with disclosure state; the registry folds reasoning into the group summary',
  recovery_inject: 'RecoveryCard for gateway-authored inject rows; the registry renders inject as prose (resolveInjectCard decides, shared)',
  workflow_completion: 'WorkflowCompletionCard needs session/folder/panel hand-offs the SDK has no seam for',
  workflow_run_tool: 'launch card refining the tool line; store-connected',
  subagent_run_tool: 'launch card refining the tool line; store-connected',
  tool_completion: 'the ✅/🚫 completion sibling draws nothing, claimed so no surface\'s unclaimed-role fallback prints it',
  // -- page-only --
  permission: 'undrawn here; the registry leaves it to GROUPED_ROLES',
  hidden_invisible_assistant: 'zero-width-space quiet-cycle rows; the registry applies the same skip inside its assistant entry',
  bubble: 'the page\'s user / inject / assistant row, with fork, pin, footer, regenerate and search-scope chrome',
}

const src = readFileSync(resolve(__dirname, '../pages/ChatPage.tsx'), 'utf8')

function rendererBlock(): string {
  const start = src.indexOf('fallback: bubbleRenderer } = useMemo')
  const end = src.indexOf('const renderMessage = useCallback', start)
  if (start < 0 || end < 0) throw new Error('ChatPage renderer block not found -- did the P5-a dispatch move?')
  return src.slice(start, end)
}

function chatPageRoleLiterals(): Set<string> {
  const roles = new Set<string>()
  // Both dispatch shapes used in the file: `m.role === 'x'` and
  // `messages[i].role === 'x'` (and their !== variants -- a negative dispatch
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

const factorySrc = readFileSync(resolve(__dirname, '../pages/chat/transcriptRenderers.tsx'), 'utf8')

function hostEntryIds(): string[] {
  const page = [...rendererBlock().matchAll(/^\s+id: '([a-z_]+)',?$/gm)].map(m => m[1])
  // The page spreads the shared dashboard set into its list; its entries are
  // host entries here too.
  const spreads = /\.\.\.shared,/.test(rendererBlock()) && /const shared = createTranscriptRenderers\(/.test(rendererBlock())
  const shared = spreads ? [...factorySrc.matchAll(/^\s+id: '([a-z_]+)',?$/gm)].map(m => m[1]) : []
  return [...page, ...shared]
}

describe('chat role parity (ChatPage consumes the app-sdk registry)', () => {
  it('ChatPage dispatches rows through the registry, not an if-chain of its own', () => {
    expect(src).toMatch(/import \{[^}]*\bmergeRenderers\b[^}]*\} from '\.\.\/app-sdk\/messageRenderers'/)
    expect(src).toMatch(/import \{[^}]*\bresolveRenderer\b[^}]*\} from '\.\.\/app-sdk\/messageRenderers'/)
    expect(src).toContain('resolveRenderer(m, chatPageRenderers)')
    // Variant flags INSIDE one entry (`const isUser = m.role === 'user'`) are
    // fine; a dispatch statement is not -- it would select a row the registry
    // never sees. Both spellings of a dispatch are rejected, so a chain cannot
    // come back as a `switch`.
    expect(rendererBlock()).not.toMatch(/if \(m\.role\s*[!=]==\s*'[a-z_]+'\)/)
    expect(rendererBlock()).not.toMatch(/switch \(m\.role\)/)
  })

  it('the SDK defaults that render through ctx.wrapper are unreachable on this page', () => {
    // `ctx.wrapper` is the SDK's conversational-row layout; the page passes a
    // keyed Fragment for it, which is only sound if no row reaches those
    // defaults. Host entries resolve first, so it holds iff the page's bubble
    // claims every role the wrapper-using defaults claim.
    const wrapperUsers = defaultMessageRenderers.filter(r => /ctx\.wrapper\(/.test(r.render.toString()))
    expect(wrapperUsers.map(r => r.id).sort()).toEqual(['assistant', 'inject', 'user'])
    const bubbleRoles = rendererBlock().match(/id: 'bubble',\s*\n\s*roles: \[([^\]]*)\]/)
    expect(bubbleRoles).not.toBeNull()
    for (const r of wrapperUsers) for (const role of r.roles) expect(bubbleRoles![1]).toContain(`'${role}'`)
  })

  it('a role nobody claims falls back to the BUBBLE by reference, never to an SDK default by position', () => {
    // mergeRenderers returns [...shapeMatched, ...hostEntries, ...roleKeyedDefaults],
    // so the merged list's tail is the SDK `undrawn` default (render: () => null);
    // indexing it would make an unregistered role -- the drift this contract
    // exists to catch -- vanish from the main chat instead of rendering as text.
    expect(rendererBlock()).toContain('return { renderers, fallback: bubble }')
    expect(src).toContain('return (entry ?? bubbleRenderer).render(m, ctx)')
    expect(src).not.toMatch(/chatPageRenderers\[chatPageRenderers\.length - 1\]/)
    // And the page's own `undrawn` override leaves `system` / `done` unclaimed,
    // so they keep the if-chain's fall-through instead of the SDK's null.
    const undrawn = rendererBlock().match(/id: 'undrawn',\s*\n\s*roles: \[([^\]]*)\]/)
    expect(undrawn).not.toBeNull()
    expect(undrawn![1]).not.toMatch(/'system'|'done'/)
  })

  it('every ChatPage host entry overrides a default or is a documented page-only entry', () => {
    const defaults = new Set(defaultMessageRenderers.map(r => r.id))
    const ids = hostEntryIds()
    expect(ids.length).toBeGreaterThan(5)
    const undocumented = ids.filter(id => !defaults.has(id) && !(id in PAGE_ONLY_ENTRY_IDS))
    // An entry with a NEW id that also claims a role the defaults render would
    // shadow the shared row on this page only -- the fork this contract exists
    // to stop. Reuse the default's id to override it, or document why the
    // entry is page-only.
    expect(undocumented).toEqual([])
    // And the documentation cannot outlive the entry.
    const stale = Object.keys(PAGE_ONLY_ENTRY_IDS).filter(id => !ids.includes(id))
    expect(stale).toEqual([])
  })

  it('every role ChatPage still names is claimed by the registry or allowlisted as chrome', () => {
    const claimed = registryClaimedRoles()
    const missing = [...chatPageRoleLiterals()].filter(
      role => !claimed.has(role) && !(role in CHROME_ONLY_ROLES),
    )
    // A role ChatPage's chrome reasons about but no renderer claims would be
    // rendered by the page's bubble fallback and by nothing on the other
    // surfaces -- register it, or add it to CHROME_ONLY_ROLES with a reason.
    expect(missing).toEqual([])
  })

  it('the chrome allowlist carries no stale entries', () => {
    const known = chatPageRoleLiterals()
    const stale = Object.keys(CHROME_ONLY_ROLES).filter(role => !known.has(role))
    expect(stale).toEqual([])
  })
})
