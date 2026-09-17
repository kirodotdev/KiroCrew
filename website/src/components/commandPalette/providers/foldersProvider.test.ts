/**
 * Folders command-palette provider.
 *
 * Exercises `createFoldersProvider` directly (no hooks) with a mock fetch + reveal
 * callback, which is the whole reason the provider is split from its hook.
 *
 * Assertions are on ORDER and MEMBERSHIP, never on a literal fuzzy score: the
 * scores come from the shared `fuzzyMatch`, so pinning a number here would make
 * this file fail whenever that scorer is retuned, for a reason that has nothing to
 * do with folders. The one score fact worth asserting is the RELATION the provider
 * itself creates — a name match must outrank a path-only match.
 */
import { describe, it, expect, vi } from 'vitest'
import { createFoldersProvider } from './foldersProvider'
import type { ChatFolder } from '../../../types'

/**
 * `oss` appears twice: once as a root folder and once nested under `kirocrew`.
 * That duplication is the point — it is what makes the breadcrumb load-bearing
 * rather than decorative, and it is the real shape of the sidebar this backs.
 */
const FOLDERS: ChatFolder[] = [
  { id: 'f-kirocrew', name: 'kirocrew', order: 0 },
  { id: 'f-oss-root', name: 'oss', order: 1 },
  { id: 'f-oss-nested', name: 'oss', order: 0, parent_id: 'f-kirocrew' },
  { id: 'f-travel', name: 'Travel Desk', order: 2 },
]

function build(folders: ChatFolder[] = FOLDERS) {
  const revealFolder = vi.fn()
  const provider = createFoldersProvider({
    fetchFolders: async () => folders,
    revealFolder,
  })
  return { provider, revealFolder }
}

describe('createFoldersProvider — matching', () => {
  it('finds a folder by its own name', async () => {
    const { provider } = build()
    const results = await provider.search('travel')
    expect(results.map(r => r.title)).toEqual(['Travel Desk'])
    expect(results[0].id).toBe('folders:f-travel')
    // Highlight indices are positions in the TITLE, which is what the palette
    // splits on to emit <mark> nodes.
    expect(results[0].indices.length).toBeGreaterThan(0)
  })

  it('renders the ancestor breadcrumb as the subtitle, and nothing for a root folder', async () => {
    const { provider } = build()
    const results = await provider.search('oss')
    const nested = results.find(r => r.id === 'folders:f-oss-nested')
    const root = results.find(r => r.id === 'folders:f-oss-root')
    expect(nested?.subtitle).toBe('kirocrew')
    expect(root?.subtitle).toBeUndefined()
  })

  it('reaches a folder through its ancestry path, and ranks it below a name match', async () => {
    const { provider } = build()
    const results = await provider.search('kirocrew')
    const ids = results.map(r => r.id)
    // The parent matched by name; its child matched only through the path.
    expect(ids).toContain('folders:f-kirocrew')
    expect(ids).toContain('folders:f-oss-nested')
    // A name match must come first — typing a parent's name lists the parent, not
    // one of its children.
    expect(ids[0]).toBe('folders:f-kirocrew')
    const parent = results.find(r => r.id === 'folders:f-kirocrew')!
    const child = results.find(r => r.id === 'folders:f-oss-nested')!
    expect(parent.score).toBeGreaterThan(child.score)
    // The child surfaced BECAUSE of its path, so that is what gets highlighted…
    expect(child.subtitleIndices?.length).toBeGreaterThan(0)
    // …while the parent's own breadcrumb is not marked (its name is the match).
    expect(parent.subtitleIndices).toBeUndefined()
  })

  it('matches a query spanning two path segments', async () => {
    const { provider } = build()
    const results = await provider.search('kirocrew oss')
    expect(results.map(r => r.id)).toContain('folders:f-oss-nested')
  })

  it('drops folders that match neither name nor path', async () => {
    const { provider } = build()
    expect(await provider.search('zzzzz-no-such-folder')).toEqual([])
  })

  it('lists every folder in the sidebar tree order for an empty query', async () => {
    const { provider } = build()
    const results = await provider.search('')
    // Pre-order: roots by (order, name), each parent immediately followed by its
    // children. Mirrors what the sidebar draws rather than an alphabetical view.
    expect(results.map(r => r.id)).toEqual([
      'folders:f-kirocrew',
      'folders:f-oss-nested',
      'folders:f-oss-root',
      'folders:f-travel',
    ])
  })

  it('survives a parent_id cycle instead of hanging', async () => {
    // A hand-edited folders.json can contain a loop; the palette must still answer.
    const { provider } = build([
      { id: 'a', name: 'Ay', order: 0, parent_id: 'b' },
      { id: 'b', name: 'Bee', order: 1, parent_id: 'a' },
    ])
    const results = await provider.search('')
    expect(results.map(r => r.title).sort()).toEqual(['Ay', 'Bee'])
  })
})

describe('createFoldersProvider — activation', () => {
  it('reveals the folder on Enter, through both the declarative action and the closure', async () => {
    const { provider, revealFolder } = build()
    const [row] = await provider.search('travel')

    row.onActivate()
    expect(revealFolder).toHaveBeenCalledWith('f-travel')

    // The declarative `enter` contract is what the palette's central dispatcher
    // reads; it must do the same thing as the legacy closure.
    expect(row.enter).toEqual({ kind: 'invoke', run: expect.any(Function) })
    if (row.enter?.kind === 'invoke') row.enter.run()
    expect(revealFolder).toHaveBeenCalledTimes(2)
    expect(revealFolder).toHaveBeenLastCalledWith('f-travel')
  })

  it('leaves ⌘Enter unbound so the dispatcher falls back to the plain reveal', async () => {
    // A folder has no split-pane or new-session variant, so binding a second
    // action would invent a behaviour. Documented fallback, asserted so a future
    // edit cannot make ⌘Enter silently inert.
    const { provider } = build()
    const [row] = await provider.search('travel')
    expect(row.onCmdActivate).toBeUndefined()
  })

  it('does not reveal anything merely by searching', async () => {
    const { provider, revealFolder } = build()
    await provider.search('oss')
    expect(revealFolder).not.toHaveBeenCalled()
  })
})

describe('createFoldersProvider — palette contract', () => {
  it('declares no minimum query length, because the corpus is a local cache', async () => {
    const { provider } = build()
    expect(provider.minQueryChars).toBeUndefined()
    // One character must therefore actually search.
    expect((await provider.search('t')).length).toBeGreaterThan(0)
  })

  it('tags every row with its provider id and an icon', async () => {
    const { provider } = build()
    for (const row of await provider.search('')) {
      expect(row.providerId).toBe('folders')
      expect(row.icon).toBeTruthy()
    }
  })

  it('survives a stored folder whose name is not a string', async () => {
    // The value comes off disk, so a hand edit or an older writer can leave a
    // number, null, or an object in `name`. `fuzzyMatch` reads `.length` and calls
    // `.toLowerCase()` on its candidate, so an unguarded read throws and takes the
    // whole palette query with it. A malformed folder reads as empty: it does not
    // match, and it does not match `[object Object]` either.
    const folders = [
      ...FOLDERS,
      { id: 'f-num', name: 7, order: 9, collapsed: false },
      { id: 'f-null', name: null, order: 10, collapsed: false },
      { id: 'f-obj', name: { en: 'Deals' }, order: 11, collapsed: false },
    ] as unknown as ChatFolder[]
    const { provider } = build(folders)
    // The well-formed match still comes back…
    expect((await provider.search('travel')).map(r => r.title)).toEqual(['Travel Desk'])
    // …and nothing answers to a stringified name.
    expect(await provider.search('object')).toEqual([])
    // An empty query lists rows without throwing on the malformed ones.
    expect((await provider.search('')).length).toBeGreaterThan(0)
  })
})
