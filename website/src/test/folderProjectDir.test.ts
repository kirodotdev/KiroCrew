import { describe, it, expect } from 'vitest'
import { resolveFolderProjectDir } from '../utils/folderAgent'
import type { ChatFolder } from '../types'

const folder = (id: string, opts: Partial<ChatFolder> = {}): ChatFolder => ({
  id, name: id, order: 0, collapsed: false, parent_id: '', ...opts,
})

describe('resolveFolderProjectDir', () => {
  it('returns project_dir from the folder itself', () => {
    const folders = [folder('a', { project_dir: '/projects/foo' })]
    expect(resolveFolderProjectDir(folders, 'a')).toBe('/projects/foo')
  })

  it('walks up to nearest ancestor with project_dir', () => {
    const folders = [
      folder('a', { project_dir: '/projects/root' }),
      folder('b', { parent_id: 'a' }),
      folder('c', { parent_id: 'b' }),
    ]
    expect(resolveFolderProjectDir(folders, 'c')).toBe('/projects/root')
  })

  it('returns undefined when no ancestor has project_dir', () => {
    const folders = [folder('a'), folder('b', { parent_id: 'a' })]
    expect(resolveFolderProjectDir(folders, 'b')).toBeUndefined()
  })

  it('terminates on cyclic parent_id', () => {
    const folders = [
      folder('a', { parent_id: 'b' }),
      folder('b', { parent_id: 'a' }),
    ]
    expect(resolveFolderProjectDir(folders, 'a')).toBeUndefined()
  })

  it('returns undefined for unknown folderId', () => {
    expect(resolveFolderProjectDir([], 'nope')).toBeUndefined()
  })
})
