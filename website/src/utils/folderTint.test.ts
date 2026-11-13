import { describe, it, expect } from 'vitest'
import { resolveFolderTintColor } from './folderTint'
import { folderPaneTint } from '../components/folderColorPaint'
import type { ChatFolder } from '../types'

const folder = (id: string, extra: Partial<ChatFolder> = {}): ChatFolder => ({ id, name: id, order: 0, ...extra })

describe('resolveFolderTintColor', () => {
  it('returns null for an unfiled session', () => {
    expect(resolveFolderTintColor([folder('a', { color: '#ef4444' })], undefined)).toBeNull()
    expect(resolveFolderTintColor([folder('a', { color: '#ef4444' })], '')).toBeNull()
  })

  it('returns the folder color', () => {
    expect(resolveFolderTintColor([folder('a', { color: '#ef4444' })], 'a')).toBe('#ef4444')
  })

  it('returns null when the folder is unknown or has no color anywhere up the chain', () => {
    const folders = [folder('root'), folder('child', { parent_id: 'root' })]
    expect(resolveFolderTintColor(folders, 'child')).toBeNull()
    expect(resolveFolderTintColor(folders, 'missing')).toBeNull()
  })

  it('inherits the nearest colored ancestor', () => {
    const folders = [
      folder('root', { color: '#ef4444' }),
      folder('mid', { parent_id: 'root', color: '#3b82f6' }),
      folder('leaf', { parent_id: 'mid' }),
      folder('sibling', { parent_id: 'root' }),
    ]
    expect(resolveFolderTintColor(folders, 'leaf')).toBe('#3b82f6')
    expect(resolveFolderTintColor(folders, 'sibling')).toBe('#ef4444')
  })

  it('prefers the folder\'s own color over an ancestor\'s', () => {
    const folders = [folder('root', { color: '#ef4444' }), folder('child', { parent_id: 'root', color: '#22c55e' })]
    expect(resolveFolderTintColor(folders, 'child')).toBe('#22c55e')
  })

  it('terminates on a parent_id loop', () => {
    const folders = [folder('a', { parent_id: 'b' }), folder('b', { parent_id: 'a' })]
    expect(resolveFolderTintColor(folders, 'a')).toBeNull()
  })

  it('inherits a colored ancestor however deep the chain is', () => {
    const folders = [folder('f0', { color: '#a855f7' })]
    for (let i = 1; i <= 60; i++) folders.push(folder(`f${i}`, { parent_id: `f${i - 1}` }))
    expect(resolveFolderTintColor(folders, 'f60')).toBe('#a855f7')
  })

  it('does not tint a mate-managed folder or anything inside one', () => {
    const folders = [
      folder('home', { mate_home: 'mate-1', color: '#ef4444' }),
      folder('inside', { parent_id: 'home' }),
    ]
    expect(resolveFolderTintColor(folders, 'home')).toBeNull()
    expect(resolveFolderTintColor(folders, 'inside')).toBeNull()
  })
})

describe('folderPaneTint', () => {
  it('mixes a small share of the color into the theme background', () => {
    const paint = folderPaneTint('#ef4444')
    expect(paint).toContain('#ef4444')
    expect(paint).toContain('var(--bg)')
    expect(paint).toMatch(/^color-mix\(in srgb, #ef4444 \d+%, var\(--bg\)\)$/)
  })
})
