import { describe, it, expect } from 'vitest'
import { resolveFolderRepos } from '../utils/folderAgent'
import type { ChatFolder } from '../types'

const folder = (id: string, opts: Partial<ChatFolder> = {}): ChatFolder => ({
  id, name: id, order: 0, collapsed: false, parent_id: '', ...opts,
})

describe('resolveFolderRepos', () => {
  it('returns repos from the folder that declares project_dir', () => {
    const folders = [folder('a', { project_dir: '/p/foo', repos: ['/p/bar', '/p/baz'] })]
    expect(resolveFolderRepos(folders, 'a')).toEqual(['/p/bar', '/p/baz'])
  })

  it('resolves repos from the nearest ancestor that declares project_dir', () => {
    const folders = [
      folder('a', { project_dir: '/p/root', repos: ['/p/extra'] }),
      folder('b', { parent_id: 'a' }),
      folder('c', { parent_id: 'b' }),
    ]
    // The project_dir resolves from `a`, so its repos travel with it.
    expect(resolveFolderRepos(folders, 'c')).toEqual(['/p/extra'])
  })

  it('does not accumulate repos up the chain (nearest-wins)', () => {
    const folders = [
      folder('a', { project_dir: '/p/root', repos: ['/p/root-extra'] }),
      // `b` declares its OWN project_dir, so it is the nearest project folder
      // and only ITS repos apply — `a`'s are not added on top.
      folder('b', { parent_id: 'a', project_dir: '/p/sub', repos: ['/p/sub-extra'] }),
    ]
    expect(resolveFolderRepos(folders, 'b')).toEqual(['/p/sub-extra'])
  })

  it('returns [] when the nearest project folder declares no repos', () => {
    const folders = [folder('a', { project_dir: '/p/foo' })]
    expect(resolveFolderRepos(folders, 'a')).toEqual([])
  })

  it('returns [] when no folder in the chain declares a project', () => {
    const folders = [folder('a', { repos: ['/p/orphan'] }), folder('b', { parent_id: 'a' })]
    // `a` lists repos but no project_dir, so there is no project folder and no
    // repos are resolved — repos belong to a project folder only.
    expect(resolveFolderRepos(folders, 'b')).toEqual([])
  })

  it('terminates on a cyclic parent_id', () => {
    const folders = [
      folder('a', { parent_id: 'b' }),
      folder('b', { parent_id: 'a' }),
    ]
    expect(resolveFolderRepos(folders, 'a')).toEqual([])
  })

  it('filters out empty-string entries', () => {
    const folders = [folder('a', { project_dir: '/p/foo', repos: ['/p/bar', ''] })]
    expect(resolveFolderRepos(folders, 'a')).toEqual(['/p/bar'])
  })

  it('returns [] for an unknown folderId', () => {
    expect(resolveFolderRepos([], 'nope')).toEqual([])
  })
})
