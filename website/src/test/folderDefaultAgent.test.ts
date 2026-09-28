import { describe, it, expect } from 'vitest'
import { resolveFolderAgent } from '../utils/folderAgent'
import type { ChatFolder } from '../types'

describe('resolveFolderAgent', () => {
  const folders: ChatFolder[] = [
    { id: 'f-msad', name: 'MS&AD', order: 0, default_agent: 'msad' },
    { id: 'f-nissay', name: 'Nissay', order: 1, default_agent: 'nissay' },
    { id: 'f-common', name: 'Common', order: 2 },
  ]

  it('uses folder default_agent when set', () => {
    expect(resolveFolderAgent(folders, 'f-msad', 'default')).toBe('msad')
    expect(resolveFolderAgent(folders, 'f-nissay', 'default')).toBe('nissay')
  })

  it('falls back to global default when folder has no default_agent', () => {
    expect(resolveFolderAgent(folders, 'f-common', 'default')).toBe('default')
  })

  it('returns undefined when neither folder nor global has agent', () => {
    expect(resolveFolderAgent(folders, 'f-common', '')).toBeUndefined()
  })

  it('falls back to global when folder not found', () => {
    expect(resolveFolderAgent(folders, 'nonexistent', 'default')).toBe('default')
  })
})

describe('resolveFolderAgent inheritance', () => {
  // A subfolder with no agent of its own is "inherit", the same way an empty
  // project_dir is — the agent has to come from the nearest ancestor that
  // pinned one, not from the global default.
  const nested: ChatFolder[] = [
    { id: 'root', name: 'root', order: 0, default_agent: 'msad' },
    { id: 'mid', name: 'mid', order: 1, parent_id: 'root' },
    { id: 'leaf', name: 'leaf', order: 2, parent_id: 'mid' },
    { id: 'pinned', name: 'pinned', order: 3, parent_id: 'root', default_agent: 'nissay' },
    { id: 'bare-root', name: 'bare root', order: 4 },
    { id: 'bare-child', name: 'bare child', order: 5, parent_id: 'bare-root' },
    { id: 'orphan', name: 'orphan', order: 6, parent_id: 'gone' },
    { id: 'cycle-a', name: 'cycle a', order: 7, parent_id: 'cycle-b' },
    { id: 'cycle-b', name: 'cycle b', order: 8, parent_id: 'cycle-a' },
  ]

  it('inherits default_agent from the parent folder', () => {
    expect(resolveFolderAgent(nested, 'mid', 'default')).toBe('msad')
  })

  it('inherits default_agent from a grandparent folder', () => {
    expect(resolveFolderAgent(nested, 'leaf', 'default')).toBe('msad')
  })

  it('prefers the folder own default_agent over an ancestor one', () => {
    expect(resolveFolderAgent(nested, 'pinned', 'default')).toBe('nissay')
  })

  it('falls back to global when no ancestor has a default_agent', () => {
    expect(resolveFolderAgent(nested, 'bare-child', 'default')).toBe('default')
  })

  it('falls back to global when parent_id names a missing folder', () => {
    expect(resolveFolderAgent(nested, 'orphan', 'default')).toBe('default')
  })

  it('terminates on cyclic parent_id and falls back to global', () => {
    expect(resolveFolderAgent(nested, 'cycle-a', 'default')).toBe('default')
  })
})
