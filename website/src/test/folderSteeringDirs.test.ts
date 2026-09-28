import { describe, it, expect } from 'vitest'
import { resolveFolderSteeringDirs } from '../utils/folderAgent'
import { ChatFolder } from '../types'

const folder = (id: string, extra: Partial<ChatFolder> = {}): ChatFolder =>
  ({ id, name: id, order: 0, ...extra }) as ChatFolder

describe('resolveFolderSteeringDirs', () => {
  it('returns an empty array when the folder is unknown', () => {
    expect(resolveFolderSteeringDirs([], 'missing')).toEqual([])
  })

  it('returns an empty array when nothing on the chain sets steering_dirs', () => {
    const folders = [folder('a'), folder('b', { parent_id: 'a' })]
    expect(resolveFolderSteeringDirs(folders, 'b')).toEqual([])
  })

  it('returns a folder’s own dirs when it has no ancestors', () => {
    const folders = [folder('a', { steering_dirs: ['/std/a', '/std/b'] })]
    expect(resolveFolderSteeringDirs(folders, 'a')).toEqual(['/std/a', '/std/b'])
  })

  it('accumulates ancestor dirs ROOT-FIRST, own dirs last', () => {
    // Unlike project_dir (nearest-wins), steering dirs from EVERY level in the
    // chain are in effect. Root ancestor's dirs precede the folder's own.
    const folders = [
      folder('org', { steering_dirs: ['/org/standards'] }),
      folder('team', { parent_id: 'org', steering_dirs: ['/team/conventions'] }),
      folder('repo', { parent_id: 'team', steering_dirs: ['/repo/steering'] }),
    ]
    expect(resolveFolderSteeringDirs(folders, 'repo')).toEqual([
      '/org/standards',
      '/team/conventions',
      '/repo/steering',
    ])
  })

  it('skips levels that set no dirs while still accumulating the rest', () => {
    const folders = [
      folder('org', { steering_dirs: ['/org/standards'] }),
      folder('team', { parent_id: 'org' }), // no dirs
      folder('repo', { parent_id: 'team', steering_dirs: ['/repo/steering'] }),
    ]
    expect(resolveFolderSteeringDirs(folders, 'repo')).toEqual([
      '/org/standards',
      '/repo/steering',
    ])
  })

  it('de-duplicates by keeping the FIRST (root-most) occurrence', () => {
    // A dir listed at both an ancestor and the folder appears once, in its
    // root-most position.
    const folders = [
      folder('org', { steering_dirs: ['/shared', '/org/only'] }),
      folder('repo', { parent_id: 'org', steering_dirs: ['/shared', '/repo/only'] }),
    ]
    expect(resolveFolderSteeringDirs(folders, 'repo')).toEqual([
      '/shared',
      '/org/only',
      '/repo/only',
    ])
  })

  it('de-duplicates within a single folder’s list too', () => {
    const folders = [folder('a', { steering_dirs: ['/dup', '/dup', '/x'] })]
    expect(resolveFolderSteeringDirs(folders, 'a')).toEqual(['/dup', '/x'])
  })

  it('is cycle-guarded: a corrupt parent_id loop ends the walk', () => {
    // a -> b -> a. The walk must terminate and still return both levels' dirs
    // exactly once, rather than spinning.
    const folders = [
      folder('a', { parent_id: 'b', steering_dirs: ['/a'] }),
      folder('b', { parent_id: 'a', steering_dirs: ['/b'] }),
    ]
    const out = resolveFolderSteeringDirs(folders, 'a')
    // Walk order is a -> b (b revisiting a stops it); reversed root-first = b, a.
    expect(out).toEqual(['/b', '/a'])
    expect(new Set(out).size).toBe(out.length) // no duplicates from the cycle
  })

  it('ends the walk on a parent_id naming a folder that no longer exists', () => {
    const folders = [folder('leaf', { parent_id: 'gone', steering_dirs: ['/leaf'] })]
    expect(resolveFolderSteeringDirs(folders, 'leaf')).toEqual(['/leaf'])
  })

  describe('principal filtering mirrors the backend delivery gate', () => {
    // The backend skips an ancestor whose owner_app is non-empty and differs
    // from the chat's principal, so a person-owned child under an app-owned
    // parent never receives the parent's dirs. The resolver applies the same
    // rule so the modal does not list inert steering as inherited.
    const tree = [
      folder('person-root', { steering_dirs: ['/person/root'] }),
      folder('app-mid', { parent_id: 'person-root', owner_app: 'radar', steering_dirs: ['/app/mid'] }),
      folder('member-mid', { parent_id: 'app-mid', owner_app: 'member:ops', steering_dirs: ['/member/mid'] }),
      folder('leaf', { parent_id: 'member-mid', steering_dirs: ['/leaf'] }),
    ]

    it('a person-owned folder sees only person-owned ancestors (and itself)', () => {
      expect(resolveFolderSteeringDirs(tree, 'leaf', '')).toEqual(['/person/root', '/leaf'])
    })

    it('omitting the principal reads as the person', () => {
      expect(resolveFolderSteeringDirs(tree, 'leaf')).toEqual(['/person/root', '/leaf'])
    })

    it('an app-owned folder sees person-owned ancestors and its own app\u2019s, not another principal\u2019s', () => {
      expect(resolveFolderSteeringDirs(tree, 'leaf', 'radar')).toEqual(['/person/root', '/app/mid', '/leaf'])
    })

    it('a member-owned folder sees person-owned ancestors and its own store\u2019s only', () => {
      expect(resolveFolderSteeringDirs(tree, 'leaf', 'member:ops')).toEqual([
        '/person/root',
        '/member/mid',
        '/leaf',
      ])
    })

    it('an empty owner_app string is the person, same as absent', () => {
      const folders = [
        folder('root', { owner_app: '', steering_dirs: ['/root'] }),
        folder('leaf', { parent_id: 'root' }),
      ]
      expect(resolveFolderSteeringDirs(folders, 'leaf', 'radar')).toEqual(['/root'])
    })
  })
})
