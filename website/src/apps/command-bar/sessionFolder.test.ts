import { beforeEach, describe, expect, it, vi } from 'vitest'

import { COMMAND_SESSION_FOLDER, commandFolderName, fileSessionInCommandFolder } from './sessionFolder'
import { api } from '../../api/client'

vi.mock('../../api/client', () => ({
  api: {
    chatFolders: vi.fn(),
    createChatFolder: vi.fn(),
    setSlotFolder: vi.fn(),
  },
}))

const chatFolders = api.chatFolders as unknown as ReturnType<typeof vi.fn>
const createChatFolder = api.createChatFolder as unknown as ReturnType<typeof vi.fn>
const setSlotFolder = api.setSlotFolder as unknown as ReturnType<typeof vi.fn>

/** Fix the listing and hand back sequential ids for whatever gets created. */
function serve(list: unknown, opts: { createFails?: boolean; assignFails?: boolean } = {}) {
  let n = 0
  chatFolders.mockResolvedValue(list)
  createChatFolder.mockImplementation(async (name: string, parentId?: string) => {
    if (opts.createFails) throw new Error('refused')
    return { id: 'new-' + ++n, name, parent_id: parentId ?? '' }
  })
  setSlotFolder.mockImplementation(async () => {
    if (opts.assignFails) throw new Error('refused')
    return { ok: true }
  })
}

/** Every `createChatFolder` call as `[name, parentId]`. */
const created = () => createChatFolder.mock.calls.map(c => [c[0], c[1]])
/** Every `setSlotFolder` call as `[slotKey, folderId]`. */
const assigned = () => setSlotFolder.mock.calls.map(c => [c[0], c[1]])

describe('fileSessionInCommandFolder', () => {
  beforeEach(() => {
    chatFolders.mockReset()
    createChatFolder.mockReset()
    setSlotFolder.mockReset()
  })

  it('names the parent folder for the launcher that opened the session', () => {
    // Pinned because the name is DURABLE: it is matched by name on every later run, so
    // changing it orphans every session already filed under the old one.
    expect(COMMAND_SESSION_FOLDER).toBe('Command Bar Sessions')
  })

  it('creates both folders on first use and files the slot into the leaf', async () => {
    serve([])
    const leaf = await fileSessionInCommandFolder('slot-1', 'Approve and merge all PRs')
    expect(created()).toEqual([
      [COMMAND_SESSION_FOLDER, undefined],
      ['Approve and merge all PRs', 'new-1'],
    ])
    expect(assigned()).toEqual([['slot-1', 'new-2']])
    expect(leaf).toBe('new-2')
  })

  it('reuses both folders on a later run', async () => {
    serve([
      { id: 'p', name: COMMAND_SESSION_FOLDER, parent_id: '' },
      { id: 'l', name: 'Approve and merge all PRs', parent_id: 'p' },
    ])
    const leaf = await fileSessionInCommandFolder('slot-2', 'Approve and merge all PRs')
    expect(created()).toEqual([])
    expect(assigned()).toEqual([['slot-2', 'l']])
    expect(leaf).toBe('l')
  })

  it('gives a second command its own leaf under the shared parent', async () => {
    serve([
      { id: 'p', name: COMMAND_SESSION_FOLDER, parent_id: '' },
      { id: 'l', name: 'Approve and merge all PRs', parent_id: 'p' },
    ])
    await fileSessionInCommandFolder('slot-3', 'Review all PRs')
    expect(created()).toEqual([['Review all PRs', 'p']])
    expect(assigned()).toEqual([['slot-3', 'new-1']])
  })

  it('matches the name the server actually stored for an over-long title', async () => {
    // The server keeps `name.strip()[:100]`, and a manifest title may be 120. Asking
    // for the full title and then looking for the full title never matches what came
    // back, so every run would make another folder -- the one failure mode here that
    // compounds instead of staying cosmetic.
    const long = 'A'.repeat(120)
    const stored = 'A'.repeat(100)
    serve([
      { id: 'p', name: COMMAND_SESSION_FOLDER, parent_id: '' },
      { id: 'l', name: stored, parent_id: 'p' },
    ])
    const leaf = await fileSessionInCommandFolder('slot-4', long)
    expect(created()).toEqual([])
    expect(assigned()).toEqual([['slot-4', 'l']])
    expect(leaf).toBe('l')
  })

  it('creates an over-long title under the name the server will keep', async () => {
    serve([])
    await fileSessionInCommandFolder('slot-5', 'B'.repeat(140))
    expect(created()[1]?.[0]).toBe('B'.repeat(100))
  })

  it('does not mistake a same-named folder elsewhere in the tree for ours', async () => {
    serve([
      { id: 'p', name: COMMAND_SESSION_FOLDER, parent_id: '' },
      { id: 'other', name: 'Review all PRs', parent_id: 'unrelated' },
    ])
    await fileSessionInCommandFolder('slot-6', 'Review all PRs')
    expect(created()).toEqual([['Review all PRs', 'p']])
  })

  it('treats a missing parent_id as the top level', async () => {
    serve([{ id: 'p', name: COMMAND_SESSION_FOLDER }])
    await fileSessionInCommandFolder('slot-7', 'Review all PRs')
    expect(created()).toEqual([['Review all PRs', 'p']])
    expect(assigned()).toEqual([['slot-7', 'new-1']])
  })

  it('returns null and never throws when the folder API refuses', async () => {
    serve([], { createFails: true })
    await expect(fileSessionInCommandFolder('slot-8', 'Review all PRs')).resolves.toBeNull()
    expect(assigned()).toEqual([])
  })

  it('returns null and never throws when the assign refuses', async () => {
    serve(
      [
        { id: 'p', name: COMMAND_SESSION_FOLDER, parent_id: '' },
        { id: 'l', name: 'Review all PRs', parent_id: 'p' },
      ],
      { assignFails: true },
    )
    await expect(fileSessionInCommandFolder('slot-9', 'Review all PRs')).resolves.toBeNull()
  })

  it('tolerates an error envelope where the folder list was expected', async () => {
    serve({ error: 'nope' })
    await fileSessionInCommandFolder('slot-10', 'Review all PRs')
    expect(created()).toEqual([
      [COMMAND_SESSION_FOLDER, undefined],
      ['Review all PRs', 'new-1'],
    ])
  })

  it('reads the passed folder cache instead of fetching', async () => {
    // `GET /api/chat/folders` walks the on-disk session list to count archived
    // sessions, so a fetch per command run pays for a filesystem scan to learn what
    // the sidebar's cache already holds.
    serve([])
    const cached = [
      { id: 'p', name: COMMAND_SESSION_FOLDER, parent_id: '' },
      { id: 'l', name: 'Review all PRs', parent_id: 'p' },
    ]
    const leaf = await fileSessionInCommandFolder('slot-cache', 'Review all PRs', cached)
    expect(chatFolders).not.toHaveBeenCalled()
    expect(created()).toEqual([])
    expect(leaf).toBe('l')
  })

  it('falls back to one fetch when the cache is cold', async () => {
    serve([{ id: 'p', name: COMMAND_SESSION_FOLDER, parent_id: '' }])
    await fileSessionInCommandFolder('slot-cold', 'Review all PRs', [])
    expect(chatFolders).toHaveBeenCalledTimes(1)
    expect(created()).toEqual([['Review all PRs', 'p']])
  })

  it('re-reads once and reuses the server folder when the cache is stale about it', async () => {
    // A warm cache that has not heard about a folder an earlier run created would
    // otherwise make a duplicate of it on every run.
    serve([
      { id: 'p', name: COMMAND_SESSION_FOLDER, parent_id: '' },
      { id: 'l', name: 'Review all PRs', parent_id: 'p' },
    ])
    const stale = [{ id: 'p', name: COMMAND_SESSION_FOLDER, parent_id: '' }]
    const leaf = await fileSessionInCommandFolder('slot-stale', 'Review all PRs', stale)
    expect(chatFolders).toHaveBeenCalledTimes(1)
    expect(created()).toEqual([])
    expect(leaf).toBe('l')
  })

  it('spends only one re-read even when both folders miss', async () => {
    serve([])
    const stale = [{ id: 'x', name: 'Something else', parent_id: '' }]
    await fileSessionInCommandFolder('slot-two-miss', 'Review all PRs', stale)
    expect(chatFolders).toHaveBeenCalledTimes(1)
    expect(created()).toEqual([
      [COMMAND_SESSION_FOLDER, undefined],
      ['Review all PRs', 'new-1'],
    ])
  })

  it('reports a create so the caller can invalidate, and stays quiet when it reused', async () => {
    serve([])
    const onCreated = vi.fn()
    await fileSessionInCommandFolder('slot-new-folder', 'Review all PRs', undefined, onCreated)
    expect(onCreated).toHaveBeenCalledTimes(1)

    onCreated.mockClear()
    serve([
      { id: 'p', name: COMMAND_SESSION_FOLDER, parent_id: '' },
      { id: 'l', name: 'Review all PRs', parent_id: 'p' },
    ])
    await fileSessionInCommandFolder('slot-reuse', 'Review all PRs', undefined, onCreated)
    expect(onCreated).not.toHaveBeenCalled()
  })

  it('does nothing without a slot key or a command title', async () => {
    serve([])
    expect(await fileSessionInCommandFolder('', 'Review all PRs')).toBeNull()
    expect(await fileSessionInCommandFolder('slot-11', '   ')).toBeNull()
    expect(chatFolders).not.toHaveBeenCalled()
  })
})

describe('commandFolderName', () => {
  const cmd = (id: string, title: string, appLabel: string) => ({ id, title, appLabel })
  const map = (...list: Array<{ id: string; title: string; appLabel: string }>) =>
    new Map(list.map(c => [c.id, c]))

  it('uses the row title when nothing else carries it', () => {
    const commands = map(
      cmd('app:pr-bulk-ops:review-all', 'Review all PRs', 'PR Bulk Ops'),
      cmd('app:pr-bulk-ops:merge-all', 'Approve and merge all PRs', 'PR Bulk Ops'),
    )
    expect(commandFolderName(commands, 'app:pr-bulk-ops:review-all')).toBe('Review all PRs')
  })

  it('appends the app label when two apps contribute the same title', () => {
    // Two rows the reader cannot tell apart would otherwise share one leaf, which is
    // the interleaving this filing exists to prevent.
    const commands = map(
      cmd('app:pr-bulk-ops:review-all', 'Review all PRs', 'PR Bulk Ops'),
      cmd('app:other:review', 'Review all PRs', 'Other App'),
    )
    expect(commandFolderName(commands, 'app:pr-bulk-ops:review-all')).toBe(
      'Review all PRs (PR Bulk Ops)',
    )
    expect(commandFolderName(commands, 'app:other:review')).toBe('Review all PRs (Other App)')
  })

  it('does not collide a row with itself', () => {
    const commands = map(cmd('app:a:one', 'Only row', 'A'))
    expect(commandFolderName(commands, 'app:a:one')).toBe('Only row')
  })

  it('keeps the discriminator when both colliding titles are at the length limit', () => {
    // The clamp and the discriminator fight otherwise: append then slice(0,100) and two
    // 100-character colliding titles come back byte-identical, back in one leaf.
    const long = 'C'.repeat(120)
    const commands = map(cmd('app:a:one', long, 'App A'), cmd('app:b:two', long, 'App B'))
    const a = commandFolderName(commands, 'app:a:one')
    const b = commandFolderName(commands, 'app:b:two')
    expect(a).not.toBe(b)
    expect(a.endsWith('(App A)')).toBe(true)
    expect(b.endsWith('(App B)')).toBe(true)
    // Both fit the server's own limit, so neither is truncated back into the other.
    expect(a.length).toBeLessThanOrEqual(100)
    expect(b.length).toBeLessThanOrEqual(100)
  })

  it('bounds a long app label so the title is not crowded out', () => {
    const commands = map(
      cmd('app:a:one', 'Same title', 'L'.repeat(80)),
      cmd('app:b:two', 'Same title', 'M'.repeat(80)),
    )
    const a = commandFolderName(commands, 'app:a:one')
    expect(a.length).toBeLessThanOrEqual(100)
    expect(a.startsWith('Same title')).toBe(true)
    expect(a).not.toBe(commandFolderName(commands, 'app:b:two'))
  })

  it('uses the row id when one app contributes two rows with the same title', () => {
    // The app label separates nothing here, so both rows would get an identical suffix
    // and land back in one leaf.
    const commands = map(
      cmd('app:pr-bulk-ops:review-all', 'Review all PRs', 'PR Bulk Ops'),
      cmd('app:pr-bulk-ops:review-mine', 'Review all PRs', 'PR Bulk Ops'),
    )
    const a = commandFolderName(commands, 'app:pr-bulk-ops:review-all')
    const b = commandFolderName(commands, 'app:pr-bulk-ops:review-mine')
    expect(a).toBe('Review all PRs (review-all)')
    expect(b).toBe('Review all PRs (review-mine)')
    expect(a).not.toBe(b)
  })

  it('falls back to a hash when even the bounded readable tag collides', () => {
    // Two distinct apps whose labels agree on their first 32 characters: bounding makes
    // the readable tag identical, so it separates nothing and the id hash takes over.
    const shared = 'X'.repeat(40)
    const commands = map(
      cmd('app:a:one', 'Same title', shared + 'AAA'),
      cmd('app:b:two', 'Same title', shared + 'BBB'),
    )
    const a = commandFolderName(commands, 'app:a:one')
    const b = commandFolderName(commands, 'app:b:two')
    expect(a).not.toBe(b)
    expect(a).toMatch(/^Same title \([0-9a-f]{8}\)$/)
    expect(b).toMatch(/^Same title \([0-9a-f]{8}\)$/)
  })

  it('separates two distinct titles that agree on their first 100 characters', () => {
    // Raw-title comparison finds no collision here, adds no discriminator, and lets the
    // server file both commands into one folder -- reached without a duplicate title.
    const shared = 'D'.repeat(100)
    const commands = map(
      cmd('app:a:one', shared + 'first', 'App A'),
      cmd('app:b:two', shared + 'second', 'App B'),
    )
    const a = commandFolderName(commands, 'app:a:one')
    const b = commandFolderName(commands, 'app:b:two')
    expect(a).not.toBe(b)
    expect(a.endsWith('(App A)')).toBe(true)
    expect(b.endsWith('(App B)')).toBe(true)
  })

  it('never splits a surrogate pair when truncating', () => {
    // `slice` counts UTF-16 units, so a title whose boundary falls inside an emoji loses
    // half a character and the name carries a lone surrogate -- an invalid string, made
    // durable.
    const emoji = 'A' + '\u{1F600}'.repeat(80)
    const commands = map(cmd('app:a:one', emoji, 'App A'), cmd('app:b:two', emoji, 'App B'))
    for (const id of ['app:a:one', 'app:b:two']) {
      const name = commandFolderName(commands, id)
      // A lone surrogate is a code unit with no pair; iterating by code point and
      // re-joining is lossless only when none was split.
      expect([...name].join('')).toBe(name)
      expect(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(name)).toBe(false)
      expect([...name].length).toBeLessThanOrEqual(100)
    }
  })

  it('answers empty for an unknown id or a blank title', () => {
    const commands = map(cmd('app:a:one', '   ', 'A'))
    expect(commandFolderName(commands, 'app:a:one')).toBe('')
    expect(commandFolderName(commands, 'app:a:missing')).toBe('')
  })
})
