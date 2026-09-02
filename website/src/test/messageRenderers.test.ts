/**
 * Registry contract for transcript rendering.
 *
 * Three things are pinned here that a type checker cannot see:
 *  - every role the old hardcoded chain handled still resolves to an entry, and
 *    the deliberately-undrawn ones resolve to an entry that draws nothing (as
 *    opposed to falling off the end of the registry, which would look identical
 *    on screen and hide the regression);
 *  - a host can replace a row by id and claim an undrawn role, which is the
 *    whole reason the mapping became data;
 *  - the registry module takes no store or router dependency, because the
 *    consumers that most need a shared transcript run outside the dashboard's
 *    React root and have no store to select from.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import type { ReactElement } from 'react'
import type { ChatMessage } from '../types'
import {
  defaultMessageRenderers,
  GROUPED_ROLES,
  mergeRenderers,
  resolveRenderer,
  type MessageRenderer,
  type MessageRenderContext,
} from '../app-sdk/messageRenderers'

const msg = (role: string, over: Partial<ChatMessage> = {}): ChatMessage =>
  ({ role, content: '', cls: '', ...over }) as ChatMessage

/** The registry entry id the old chain's branch for this message became. */
function idFor(m: ChatMessage): string | undefined {
  return resolveRenderer(m, defaultMessageRenderers)?.id
}

describe('default registry reproduces the old role chain', () => {
  it('routes each role to the branch that used to handle it', () => {
    expect(idFor(msg('user'))).toBe('user')
    expect(idFor(msg('assistant'))).toBe('assistant')
    expect(idFor(msg('streaming'))).toBe('assistant')
    expect(idFor(msg('tool_call'))).toBe('tool_lifecycle')
    expect(idFor(msg('tool_result'))).toBe('tool_lifecycle')
    expect(idFor(msg('inject'))).toBe('inject')
    expect(idFor(msg('error'))).toBe('error')
    expect(idFor(msg('notice'))).toBe('notice')
    expect(idFor(msg('mcp_oauth'))).toBe('mcp_oauth')
  })

  it('claims a tool row only when it is the visible 🔧 message', () => {
    // The hidden 🚫 sibling is read for the auto-denied flag and never drawn, so
    // it must NOT resolve to the tool row.
    expect(idFor(msg('tool', { content: '🔧 grep' }))).toBe('tool')
    expect(idFor(msg('tool', { content: '🚫 denied by policy' }))).toBeUndefined()
    expect(idFor(msg('tool', { content: 'plain text' }))).toBeUndefined()
  })

  it('recognises a stop event by shape, whatever role carries it', () => {
    expect(idFor(msg('assistant', { kind: 'stop_event' }))).toBe('stop_event')
    expect(idFor(msg('notice', { meta: { kind: 'stop_event' } }))).toBe('stop_event')
    // Shape-based entries are searched first, so a stop event never falls
    // through to the row its role would otherwise pick.
    expect(idFor(msg('notice'))).toBe('notice')
  })

  it('draws nothing for the undrawn roles, but still CLAIMS them', () => {
    // A grouped or lifecycle-only role has no row of its own. Both facts matter:
    // an entry exists (so deleting it is caught here), and it renders null.
    for (const role of ['thinking', 'system', 'done', 'queued']) {
      const entry = resolveRenderer(msg(role), defaultMessageRenderers)
      expect(entry?.id, role).toBe('undrawn')
      expect(entry!.render(msg(role), {} as never), role).toBeNull()
    }
    const file = resolveRenderer(msg('file'), defaultMessageRenderers)
    expect(file?.id).toBe('file')
    expect(file!.render(msg('file'), {} as never)).toBeNull()
  })

  it('leaves an unknown role unclaimed', () => {
    expect(idFor(msg('some_future_role'))).toBeUndefined()
  })

  it('gives every entry a unique id, so an override cannot be ambiguous', () => {
    const ids = defaultMessageRenderers.map(r => r.id)
    expect(new Set(ids).size).toBe(ids.length)
  })
})

describe('invisible-only assistant rows', () => {
  const baseCtx = (over: Partial<MessageRenderContext> = {}): MessageRenderContext => ({
    index: 0,
    messages: [],
    running: false,
    key: 'k',
    hideCardOwnedOAuth: false,
    autoDeniedIds: new Set<string>(),
    wrapper: children => children,
    row: children => children,
    ...over,
  })

  it('draws nothing for a ZWSP-only assistant row (quiet monitor-cycle reply)', () => {
    const m = msg('assistant', { content: '\u200b' })
    const entry = resolveRenderer(m, defaultMessageRenderers)
    expect(entry?.id).toBe('assistant')
    // The skip fires before the context is read — same shape the undrawn
    // roles use above.
    expect(entry!.render(m, {} as never)).toBeNull()
  })

  it('still draws a streaming row whose text has not arrived yet', () => {
    const m = msg('streaming', { content: '\u200b' })
    const entry = resolveRenderer(m, defaultMessageRenderers)!
    expect(entry.render(m, baseCtx({ messages: [m], running: true }))).not.toBeNull()
  })

  it('moves the turn footer past a hidden trailing row to the last drawn reply', () => {
    const real = msg('assistant', { content: 'real answer' })
    const zwsp = msg('assistant', { content: '\u200b' })
    const messages = [msg('user', { content: 'q' }), real, zwsp]
    const entry = resolveRenderer(real, defaultMessageRenderers)!
    const node = entry.render(real, baseCtx({ index: 1, messages })) as ReactElement<{
      children: ReactElement<{ showFooter?: boolean }>
    }>
    // Identity wrapper: node is the bubble div, its child the AssistantMessage.
    expect(node.props.children.props.showFooter).toBe(true)
  })

  it('keeps a ZWSP-only row with file-change chips and passes the chips through', () => {
    // The chips are the row's content: hiding it would hide them, and
    // rendering it without the prop would draw the empty bubble the chips
    // were supposed to justify.
    const changes = [{ path: 'a.ts' }]
    const m = msg('assistant', { content: '\u200b', meta: { file_changes: changes } })
    const entry = resolveRenderer(m, defaultMessageRenderers)!
    const node = entry.render(m, baseCtx({ messages: [m] })) as ReactElement<{
      children: ReactElement<{ fileChanges?: unknown }>
    }>
    expect(node).not.toBeNull()
    expect(node.props.children.props.fileChanges).toBe(changes)
  })
})

describe('host entries', () => {
  const custom: MessageRenderer = { id: 'user', roles: ['user'], render: () => 'replaced' }

  it('replaces a built-in row by reusing its id', () => {
    const merged = mergeRenderers([custom])
    expect(resolveRenderer(msg('user'), merged)).toBe(custom)
    // Replacing one row must not drop the rest.
    expect(merged.filter(r => r.id === 'user')).toHaveLength(1)
    expect(merged.length).toBe(defaultMessageRenderers.length)
    expect(resolveRenderer(msg('error'), merged)?.id).toBe('error')
  })

  it('can claim a role the defaults leave undrawn', () => {
    const queued: MessageRenderer = { id: 'queued-card', roles: ['queued'], render: () => 'card' }
    const merged = mergeRenderers([queued])
    expect(resolveRenderer(msg('queued'), merged)).toBe(queued)
    // The default undrawn entry is still there for the roles it kept.
    expect(resolveRenderer(msg('thinking'), merged)?.id).toBe('undrawn')
  })

  it('is the identity for an empty or absent list', () => {
    expect(mergeRenderers(undefined)).toBe(defaultMessageRenderers)
    expect(mergeRenderers([])).toBe(defaultMessageRenderers)
  })

  it('cannot outrank a shape-matched default by claiming the role it travels on', () => {
    // A stop event reaches the transcript as role `system` (the backend appends
    // it that way) and is recognised by `kind`. `system` is also one of the
    // roles the defaults leave undrawn, so it is exactly what a host is invited
    // to claim — and claiming it must NOT swallow the stop card.
    const hostSystem: MessageRenderer = { id: 'system-note', roles: ['system'], render: () => 'host' }
    const merged = mergeRenderers([hostSystem])

    const stop = msg('system', { kind: 'stop_event' })
    expect(resolveRenderer(stop, merged)?.id).toBe('stop_event')
    // A plain system row is still the host's.
    expect(resolveRenderer(msg('system'), merged)).toBe(hostSystem)
  })

  it('still lets a host replace a shape-matched row, explicitly, by id', () => {
    const ownStop: MessageRenderer = { id: 'stop_event', roles: ['*'], match: () => true, render: () => 'mine' }
    const merged = mergeRenderers([ownStop])
    expect(resolveRenderer(msg('system', { kind: 'stop_event' }), merged)).toBe(ownStop)
  })
})

describe('grouped roles are a documented exception, not an accident', () => {
  // `thinking` and `permission` are assembled into a collapsible group BEFORE
  // per-row resolution, so an entry claiming one is consulted but renders INSIDE
  // the group rather than replacing it. Pinning the set here keeps the docs and
  // the transcript honest about which roles behave that way — silently adding a
  // third would make the api-reference wrong without anything noticing.
  it('names exactly the roles the transcript groups', () => {
    expect([...GROUPED_ROLES].sort()).toEqual(['permission', 'thinking'])
  })

  it('cannot be mutated by a consumer at RUNTIME, not merely by type', () => {
    // This value crosses into apps through the vendored SDK surface, and an app is
    // plain JavaScript that never sees our types. A merely type-readonly export
    // would let one `delete`/`push` stop the host grouping permissions and take the
    // pending approval UI with it, so the guarantee has to survive type erasure.
    expect(Object.isFrozen(GROUPED_ROLES)).toBe(true)
    expect(() => {
      (GROUPED_ROLES as string[]).push('notice')
    }).toThrow()
    // Whatever a caller tried, the set of grouped roles is unchanged.
    expect([...GROUPED_ROLES].sort()).toEqual(['permission', 'thinking'])
  })

  it('leaves permission unclaimed by default, so nothing draws it per-row', () => {
    // The group's own summary and approval affordance handle it.
    expect(resolveRenderer(msg('permission'), defaultMessageRenderers)).toBeUndefined()
  })

  it('does resolve a host entry for a grouped role — it just renders in the group', () => {
    const card: MessageRenderer = { id: 'approval', roles: ['permission'], render: () => 'card' }
    expect(resolveRenderer(msg('permission'), mergeRenderers([card]))).toBe(card)
  })
})

describe('renderTool stays a supported shorthand', () => {
  it('is preferred by both tool entries when the host supplies one', () => {
    const seen: string[] = []
    const ctx = {
      running: false,
      autoDeniedIds: new Set<string>(),
      renderTool: (m: ChatMessage) => { seen.push(m.role); return 'host-tool' },
      row: (children: unknown) => children,
    }
    const tool = msg('tool', { content: '🔧 grep' })
    const call = msg('tool_call')
    expect(resolveRenderer(tool, defaultMessageRenderers)!.render(tool, ctx as never)).toBe('host-tool')
    expect(resolveRenderer(call, defaultMessageRenderers)!.render(call, ctx as never)).toBe('host-tool')
    expect(seen).toEqual(['tool', 'tool_call'])
  })
})

describe('registry stays usable without a store', () => {
  // Asserted against SOURCE TEXT: tsconfig.app.json excludes src/test, so a
  // type-level lock written here would never be checked by tsc at all.
  const src = readFileSync(
    join(__dirname, '..', 'app-sdk', 'messageRenderers.tsx'),
    'utf8',
  )

  it('imports no store and no router', () => {
    const imports = src.split('\n').filter(l => /^\s*import\b/.test(l))
    expect(imports.length).toBeGreaterThan(5)
    for (const line of imports) {
      expect(line, line).not.toMatch(/from '[^']*\/store/)
      expect(line, line).not.toMatch(/from 'react-redux'/)
      expect(line, line).not.toMatch(/from 'react-router/)
    }
  })

  it('reads live app state only through the context it is handed', () => {
    expect(src).not.toMatch(/useAppSelector|useAppDispatch|useSelector\(/)
  })
})
