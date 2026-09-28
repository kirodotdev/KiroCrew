import { describe, expect, it, vi } from 'vitest'

import { SERVER_NAME_LIMIT, ensureChatFolder, fnv1a8, folderRows, storedName } from './ensureChatFolder'
import { sha256Hex } from './sha256Hex'

/** A `list`/`create` pair over a fixed listing, handing back sequential ids. */
function transport(listing: unknown, opts: { createReturns?: unknown } = {}) {
  let n = 0
  const list = vi.fn(async () => listing)
  const create = vi.fn(async (name: string, parentId: string) =>
    'createReturns' in opts ? opts.createReturns : { id: 'new-' + ++n, name, parent_id: parentId },
  )
  /** Every `create` call as `[name, parentId]`. */
  const created = () => create.mock.calls.map(c => [c[0], c[1]])
  return { list, create, created }
}

const TOP = { id: 'top', name: 'Reviews', parent_id: '' }
const NESTED = { id: 'nested', name: 'Reviews', parent_id: 'top' }

describe('ensureChatFolder — matching', () => {
  it('returns the existing folder without creating', async () => {
    const t = transport([TOP])
    expect(await ensureChatFolder({ ...t, name: 'Reviews' })).toBe('top')
    expect(t.list).toHaveBeenCalledTimes(1)
    expect(t.created()).toEqual([])
  })

  it('creates on a miss and returns the new id', async () => {
    const t = transport([TOP])
    expect(await ensureChatFolder({ ...t, name: 'Triage' })).toBe('new-1')
    expect(t.created()).toEqual([['Triage', '']])
  })

  it('matches under the named parent only, never elsewhere in the tree', async () => {
    // A same-named folder somewhere else is not ours: a caller naming the top level
    // gets the top-level one, a caller naming `top` gets the nested one, and a caller
    // naming a parent with no such child creates rather than borrowing.
    expect(await ensureChatFolder({ ...transport([NESTED, TOP]), name: 'Reviews', parentId: '' })).toBe('top')
    expect(await ensureChatFolder({ ...transport([TOP, NESTED]), name: 'Reviews', parentId: 'top' })).toBe('nested')
    const t = transport([TOP, NESTED])
    expect(await ensureChatFolder({ ...t, name: 'Reviews', parentId: 'other' })).toBe('new-1')
    expect(t.created()).toEqual([['Reviews', 'other']])
  })

  it('matches the name anywhere in the tree when no parent is named', async () => {
    // A caller with no tree of its own must keep finding a folder the reader has
    // moved under another one, rather than making a top-level duplicate.
    const t = transport([NESTED])
    expect(await ensureChatFolder({ ...t, name: 'Reviews' })).toBe('nested')
    expect(t.created()).toEqual([])
    const miss = transport([{ id: 'x', name: 'Other', parent_id: 'top' }])
    expect(await ensureChatFolder({ ...miss, name: 'Reviews' })).toBe('new-1')
    expect(miss.created()).toEqual([['Reviews', '']])
  })

  it('treats a missing parent_id as the top level', async () => {
    const t = transport([NESTED, { id: 'bare', name: 'Reviews' }])
    expect(await ensureChatFolder({ ...t, name: 'Reviews', parentId: '' })).toBe('bare')
    expect(t.created()).toEqual([])
  })

  it('matches the name the server actually stored for an over-long name', async () => {
    // The server keeps `name.strip()[:100]`. A lookup for the full name can never
    // match what an earlier create left behind, so the name is cut to fit here, with
    // a fingerprint tail — and the create asks for that same name, so the server
    // stores it verbatim and the next run finds it.
    const long = 'x'.repeat(SERVER_NAME_LIMIT + 1)
    const stored = storedName(long)
    // The tail is the first 128 bits of the SHA-256 of the whole trimmed name.
    const tag = sha256Hex(long).slice(0, 32)
    expect(stored).toBe('x'.repeat(65) + ` (${tag})`)
    const hit = transport([{ id: 'kept', name: stored, parent_id: '' }])
    expect(await ensureChatFolder({ ...hit, name: long })).toBe('kept')
    expect(hit.created()).toEqual([])
    const miss = transport([])
    expect(await ensureChatFolder({ ...miss, name: long })).toBe('new-1')
    expect(miss.created()).toEqual([[stored, '']])
  })

  it('keeps two long names apart when they agree on their first 100 characters', async () => {
    // The server alone would cut both back to the same 100 characters and file two
    // repositories into one folder. The fingerprint in the tail tells them apart, and
    // both results fit the limit so the server stores each verbatim.
    const shared = 'Issue Radar - ' + 'a'.repeat(86)
    const one = storedName(shared + '/first')
    const two = storedName(shared + '/second')
    expect(one).not.toBe(two)
    expect([...one].length).toBe(SERVER_NAME_LIMIT)
    expect([...two].length).toBe(SERVER_NAME_LIMIT)
    const t = transport([{ id: 'f-one', name: one, parent_id: '' }])
    expect(await ensureChatFolder({ ...t, name: shared + '/first' })).toBe('f-one')
    expect(await ensureChatFolder({ ...t, name: shared + '/second' })).toBe('new-1')
    expect(t.created()).toEqual([[two, '']])
  })

  it('leaves a name within the limit untouched', () => {
    const exact = 'y'.repeat(SERVER_NAME_LIMIT)
    expect(storedName(exact)).toBe(exact)
    expect(storedName('  Reviews ')).toBe('Reviews')
    expect(fnv1a8('a')).toMatch(/^[0-9a-f]{8}$/)
    expect(fnv1a8('a')).not.toBe(fnv1a8('b'))
  })

  it('trims the name the way the server does before matching', async () => {
    const t = transport([TOP])
    expect(await ensureChatFolder({ ...t, name: '  Reviews  ' })).toBe('top')
    expect(t.created()).toEqual([])
  })

  it('never splits a surrogate pair when clamping', () => {
    const emoji = '😀'
    // The cut lands inside the emoji if it counts UTF-16 units: 64 units of x, then a
    // two-unit pair straddling the 65th code point.
    const name = 'x'.repeat(64) + emoji + 'y'.repeat(40)
    const stored = storedName(name)
    expect([...stored].length).toBe(SERVER_NAME_LIMIT)
    expect(stored.startsWith('x'.repeat(64) + emoji)).toBe(true)
    expect(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(stored)).toBe(false)
  })

  it('does nothing for a blank name', async () => {
    const t = transport([TOP])
    expect(await ensureChatFolder({ ...t, name: '   ' })).toBeNull()
    expect(t.list).not.toHaveBeenCalled()
    expect(t.created()).toEqual([])
  })
})

describe('ensureChatFolder — reads', () => {
  it('reads the passed cache instead of fetching on a hit', async () => {
    const t = transport([])
    expect(await ensureChatFolder({ ...t, name: 'Reviews', cached: [TOP] })).toBe('top')
    expect(t.list).not.toHaveBeenCalled()
  })

  it('re-reads once on a cache miss and reuses the server folder', async () => {
    // A warm cache that has not heard about a folder an earlier run created would
    // otherwise make a duplicate of it on every run.
    const t = transport([TOP])
    const stale = [{ id: 'x', name: 'Something else', parent_id: '' }]
    expect(await ensureChatFolder({ ...t, name: 'Reviews', cached: stale })).toBe('top')
    expect(t.list).toHaveBeenCalledTimes(1)
    expect(t.created()).toEqual([])
  })

  it('creates after the re-read still misses, and spends no second read', async () => {
    const t = transport([])
    const stale = [{ id: 'x', name: 'Something else', parent_id: '' }]
    expect(await ensureChatFolder({ ...t, name: 'Reviews', cached: stale })).toBe('new-1')
    expect(t.list).toHaveBeenCalledTimes(1)
  })

  it('fetches once when the cache is empty and does not re-read before creating', async () => {
    const t = transport([])
    expect(await ensureChatFolder({ ...t, name: 'Reviews', cached: [] })).toBe('new-1')
    expect(t.list).toHaveBeenCalledTimes(1)
  })

  it('tolerates an error envelope where the folder list was expected', async () => {
    const t = transport({ error: 'nope' })
    expect(await ensureChatFolder({ ...t, name: 'Reviews' })).toBe('new-1')
    expect(t.created()).toEqual([['Reviews', '']])
  })
})

describe('ensureChatFolder — failure stays at the call site', () => {
  it('returns null when the create answers without an id', async () => {
    expect(await ensureChatFolder({ ...transport([], { createReturns: {} }), name: 'Reviews' })).toBeNull()
    expect(await ensureChatFolder({ ...transport([], { createReturns: null }), name: 'Reviews' })).toBeNull()
  })

  it('propagates a rejected list untouched', async () => {
    const boom = new Error('refused')
    const t = { list: vi.fn(async () => { throw boom }), create: vi.fn() }
    await expect(ensureChatFolder({ ...t, name: 'Reviews' })).rejects.toBe(boom)
    expect(t.create).not.toHaveBeenCalled()
  })

  it('propagates a rejected create untouched', async () => {
    const boom = new Error('refused')
    const t = { list: vi.fn(async () => []), create: vi.fn(async () => { throw boom }) }
    await expect(ensureChatFolder({ ...t, name: 'Reviews' })).rejects.toBe(boom)
  })
})

describe('folder row helpers', () => {
  it('folderRows yields nothing for a non-array', () => {
    expect(folderRows(undefined)).toEqual([])
    expect(folderRows({ error: 'x' })).toEqual([])
    expect(folderRows([TOP])).toEqual([TOP])
  })

  it('skips a null row while matching', async () => {
    const t = transport([null, TOP])
    expect(await ensureChatFolder({ ...t, name: 'Reviews', parentId: '' })).toBe('top')
    expect(await ensureChatFolder({ ...transport([null, TOP]), name: 'Reviews' })).toBe('top')
  })
})
