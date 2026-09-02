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
