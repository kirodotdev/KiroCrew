import { describe, expect, it } from 'vitest'
import { actionGrant, resolveComposerActions } from './composerActionRegistry'

const action = (over: Record<string, unknown> = {}) => ({ id: 'insert', title: 'Insert snippet', icon: 'Zap', popover: { width: 320, maxHeight: 240 }, ...over })

describe('resolveComposerActions', () => {
  it('lists an enabled app\'s approved actions with its display name', () => {
    const [item] = resolveComposerActions([{ name: 'snip', displayName: 'Snippets', composerActionGrant: { actions: [action()] } }])
    expect(item).toEqual({ key: 'snip:insert', appName: 'snip', appLabel: 'Snippets', id: 'insert', title: 'Insert snippet', icon: 'Zap', width: 320, maxHeight: 240 })
  })

  it('falls back to the manifest name, then the app name, for the label', () => {
    expect(resolveComposerActions([{ name: 'snip', manifest: { displayName: 'From manifest' }, composerActionGrant: { actions: [action()] } }])[0].appLabel).toBe('From manifest')
    expect(resolveComposerActions([{ name: 'snip', composerActionGrant: { actions: [action()] } }])[0].appLabel).toBe('snip')
  })

  it('skips disabled apps, nameless apps and apps with no approved list', () => {
    expect(resolveComposerActions([
      { name: 'off', enabled: false, composerActionGrant: { actions: [action()] } },
      { composerActionGrant: { actions: [action()] } },
      { name: 'none' },
      { name: 'bad', composerActionGrant: { actions: 'insert' } },
    ])).toEqual([])
  })

  it('drops malformed actions and keeps at most two per app', () => {
    const items = resolveComposerActions([{ name: 'snip', composerActionGrant: { actions: [
      null, 'x', action({ id: 'Bad Id' }), action({ title: '' }), action({ title: 'x'.repeat(41) }),
      action({ id: 'a' }), action({ id: 'b' }),
    ] } }])
    // Only the first two entries are read, and both of those are malformed here.
    expect(items).toEqual([])
    expect(resolveComposerActions([{ name: 'snip', composerActionGrant: { actions: [action({ id: 'a' }), action({ id: 'b' }), action({ id: 'c' })] } }]).map((i) => i.id)).toEqual(['a', 'b'])
  })

  it('clamps sizes and falls back on a bad icon', () => {
    const [item] = resolveComposerActions([{ name: 'snip', composerActionGrant: { actions: [action({ icon: 'not-an-icon', popover: { width: 9999, maxHeight: 'tall' } })] } }])
    expect(item.icon).toBe('')
    expect(item.width).toBe(360)
    expect(item.maxHeight).toBe(320)
    const [noPopover] = resolveComposerActions([{ name: 'snip', composerActionGrant: { actions: [action({ popover: null })] } }])
    expect(noPopover.width).toBe(360)
  })

  it('caps a long app label', () => {
    expect(resolveComposerActions([{ name: 'snip', displayName: 'n'.repeat(200), composerActionGrant: { actions: [action()] } }])[0].appLabel).toHaveLength(80)
  })
})

describe('actionGrant', () => {
  it('names the insert entry of an action', () => {
    expect(actionGrant('insert')).toBe('insert:insert')
  })
})
