/**
 * Folder listings folded into the whole-project tree payload: a folder
 * past the 10,000-row cap becomes reachable once its own listing is merged.
 */
import { describe, it, expect } from 'vitest'
import { MAX_FOLDER_LISTINGS, MAX_FOLDER_LISTING_ROWS, foldersOverRowBudget, mergeFolderListings, withFolder, type TreePayload } from '../pierre/treeFolderListings'

const base: TreePayload = {
  root: '/p',
  paths: ['README.md', 'big/a.txt'],
  directories: ['big', 'small'],
  repo: false,
  truncated: true,
  truncatedDirectories: ['', 'big'],
  hiddenOnlyDirectories: [],
  unreadableDirectories: [],
  linkedDirectories: [],
}

describe('mergeFolderListings', () => {
  it('returns the payload itself when no folder is loaded', () => {
    expect(mergeFolderListings(base, [])).toBe(base)
  })

  it('adds the folder rows and files the cap hid and un-cuts the folder', () => {
    const merged = mergeFolderListings(base, [{
      root: '/p', dir: 'big', repo: false,
      paths: ['big/a.txt', 'big/late/deep.txt'],
      directories: ['big/late'],
      truncatedDirectories: [],
    }])
    expect(merged.paths).toEqual(['README.md', 'big/a.txt', 'big/late/deep.txt'])
    expect(merged.directories).toEqual(['big', 'small', 'big/late'])
    expect(merged.truncatedDirectories).toEqual([''])
    expect(merged.truncated).toBe(true)
  })

  it('keeps a folder cut when its own listing is cut too', () => {
    const merged = mergeFolderListings(base, [{
      root: '/p', dir: 'big', repo: false, paths: [], directories: ['big/x'], truncatedDirectories: ['big'],
    }])
    expect(merged.truncatedDirectories).toEqual(['', 'big'])
  })

  it('un-cuts a folder beneath a listed folder unless that listing cuts it again', () => {
    const nested = { ...base, truncatedDirectories: ['big', 'big/inner'] }
    const whole = mergeFolderListings(nested, [{ root: '/p', dir: 'big', repo: false, paths: ['big/inner/x'], truncatedDirectories: [] }])
    expect(whole.truncatedDirectories).toEqual([])
    const still = mergeFolderListings(nested, [{ root: '/p', dir: 'big', repo: false, paths: [], truncatedDirectories: ['big/inner'] }])
    expect(still.truncatedDirectories).toEqual(['big/inner'])
  })

  it('lets a deeper loaded listing replace a cut its parent folder\'s listing names', () => {
    const merged = mergeFolderListings({ ...base, truncatedDirectories: ['big'] }, [
      { root: '/p', dir: 'big', repo: false, paths: [], directories: ['big/late'], truncatedDirectories: ['big/late'] },
      { root: '/p', dir: 'big/late', repo: false, paths: ['big/late/x'], truncatedDirectories: [] },
    ])
    expect(merged.truncatedDirectories).toEqual([])
    expect(merged.truncated).toBe(false)
  })

  it('reports the tree whole once every cut folder is loaded whole', () => {
    const merged = mergeFolderListings({ ...base, truncatedDirectories: ['big'] }, [{
      root: '/p', dir: 'big', repo: false, paths: ['big/b.txt'], truncatedDirectories: [],
    }])
    expect(merged.truncated).toBe(false)
  })

  it('adds no row for a folder an empty git listing names (an ignored folder)', () => {
    const merged = mergeFolderListings(base, [{ root: '/p', dir: 'coverage/x', repo: true, paths: [], directories: [] }])
    expect(merged.directories).not.toContain('coverage/x')
  })

  it('stops naming a nested repository once its own listing is loaded, and names the ones inside it', () => {
    const withRepo: TreePayload = {
      ...base,
      paths: ['README.md'],
      directories: ['libs', 'libs/sub', 'libs/other'],
      truncated: false,
      truncatedDirectories: [],
      nestedRepoDirectories: ['libs/other', 'libs/sub'],
    }
    const merged = mergeFolderListings(withRepo, [{
      root: '/p', dir: 'libs/sub', repo: true,
      paths: ['libs/sub/a.ts'],
      directories: ['libs/sub/vendor'],
      truncatedDirectories: [],
      nestedRepoDirectories: ['libs/sub/vendor'],
    }])
    expect(merged.paths).toEqual(['README.md', 'libs/sub/a.ts'])
    expect(merged.nestedRepoDirectories).toEqual(['libs/other', 'libs/sub/vendor'])
    // Not a cut: loading a repository never makes the tree "truncated".
    expect(merged.truncated).toBe(false)
  })

  it('ignores a listing for another project root', () => {
    expect(mergeFolderListings(base, [{ root: '/other', dir: 'big', repo: false, paths: ['big/z'] }])).toBe(base)
  })
})

describe('withFolder', () => {
  it('adds once and drops the oldest past the bound', () => {
    expect(withFolder(['a'], 'a')).toEqual(['a'])
    let folders: string[] = []
    for (let i = 0; i <= MAX_FOLDER_LISTINGS; i++) folders = withFolder(folders, `f${i}`)
    expect(folders).toHaveLength(MAX_FOLDER_LISTINGS)
    expect(folders[0]).toBe('f1')
  })
})

describe('foldersOverRowBudget', () => {
  const listing = (dir: string, rows: number) => ({
    root: '/p', dir, repo: false, paths: Array.from({ length: rows }, (_, i) => `${dir}/f${i}`),
  })
  it('drops the oldest listings past the row budget and always keeps the newest', () => {
    const half = MAX_FOLDER_LISTING_ROWS / 2
    expect(foldersOverRowBudget([listing('a', half), listing('b', half)])).toEqual([])
    expect(foldersOverRowBudget([listing('a', half), listing('b', half), listing('c', 1)])).toEqual(['a'])
    expect(foldersOverRowBudget([listing('a', 1), listing('huge', MAX_FOLDER_LISTING_ROWS + 1)])).toEqual(['a'])
  })
})
