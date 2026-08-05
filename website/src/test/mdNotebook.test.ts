/**
 * Tests for the Notes app's editor logic.
 *
 * In the original single-file app this code lived inside a module no test runner
 * could import, so indent measurement, list continuation and caret placement
 * were only ever verified by hand. Extracting them into `utils.ts` during the
 * port is what makes these assertions possible.
 */
import { describe, it, expect } from 'vitest'

import {
  buildTree,
  carriedMarker,
  formatShortcut,
  indentPx,
  isEmptyListItem,
  matchesShortcut,
  neighborAfterDelete,
  noteBasename,
  relTime,
  targetsSameNote,
  rowBadge,
  shiftListItem,
  vaultContentPath,
} from '../apps/md-notebook/utils'
import { inline } from '../apps/md-notebook/Preview'
import { flattenVisibleNotes, orderNotes } from '../apps/md-notebook/NoteRow'
import type { Note } from '../apps/md-notebook/types'

describe('md-notebook/indentPx', () => {
  it('expands a tab to a full 4-column stop', () => {
    // The bug this replaced counted characters, scoring a tab as one space.
    expect(indentPx('')).toBe(0)
    expect(indentPx('\t')).toBe(32)
    expect(indentPx('\t\t')).toBe(64)
  })

  it('measures spaces proportionally', () => {
    expect(indentPx('  ')).toBe(16)
    expect(indentPx('    ')).toBe(32)
    expect(indentPx('   ')).toBe(24)
  })

  it('advances a tab to the NEXT stop rather than adding a fixed width', () => {
    // Two spaces then a tab lands on column 4, not 2 + 4.
    expect(indentPx('  \t')).toBe(32)
    expect(indentPx('\t  ')).toBe(48)
  })

  it('ranks a tab-indented child deeper than a stray two-space indent', () => {
    // This ordering is the whole point: real nesting must not render shallower
    // than an incidental indent.
    expect(indentPx('\t')).toBeGreaterThan(indentPx('  '))
  })
})

describe('md-notebook/shiftListItem', () => {
  it('indents a list item by one tab and moves the caret with it', () => {
    expect(shiftListItem('- a', 3, false)).toEqual({ text: '\t- a', pos: 4 })
    expect(shiftListItem('* a', 3, false)).toEqual({ text: '\t* a', pos: 4 })
    expect(shiftListItem('- [ ] a', 7, false)).toEqual({ text: '\t- [ ] a', pos: 8 })
    expect(shiftListItem('1. a', 4, false)).toEqual({ text: '\t1. a', pos: 5 })
  })

  it('outdents a tab or up to a tab stop of spaces', () => {
    expect(shiftListItem('\t- a', 4, true)).toEqual({ text: '- a', pos: 3 })
    expect(shiftListItem('    - a', 7, true)).toEqual({ text: '- a', pos: 3 })
    expect(shiftListItem('  - a', 5, true)).toEqual({ text: '- a', pos: 3 })
  })

  it('refuses to outdent an item already at the outermost level', () => {
    expect(shiftListItem('- a', 3, true)).toBeNull()
  })

  it('leaves non-list lines alone so Tab can still move focus', () => {
    expect(shiftListItem('plain text', 4, false)).toBeNull()
    expect(shiftListItem('# heading', 4, false)).toBeNull()
  })

  it('operates on the caret line only, inside multi-line text', () => {
    expect(shiftListItem('a\n- b', 5, false)).toEqual({ text: 'a\n\t- b', pos: 6 })
    // The caret is on the non-list first line, so nothing happens.
    expect(shiftListItem('a\n- b', 1, false)).toBeNull()
  })
})

describe('md-notebook/carriedMarker', () => {
  it('carries a bullet into the next block', () => {
    expect(carriedMarker('- foo')).toBe('- ')
    expect(carriedMarker('* foo')).toBe('* ')
  })

  it('increments an ordered list', () => {
    expect(carriedMarker('2. foo')).toBe('3. ')
    expect(carriedMarker('  3. foo')).toBe('  4. ')
  })

  it('starts a new task unchecked even from a checked one', () => {
    expect(carriedMarker('- [ ] foo')).toBe('- [ ] ')
    expect(carriedMarker('- [x] done')).toBe('- [ ] ')
  })

  it('preserves the indent so the new item stays at the same level', () => {
    expect(carriedMarker('  - nested')).toBe('  - ')
    expect(carriedMarker('\t- nested')).toBe('\t- ')
  })

  it('carries nothing from a paragraph or heading', () => {
    expect(carriedMarker('plain paragraph')).toBe('')
    expect(carriedMarker('# Heading')).toBe('')
  })

  it('places the caret after the marker, not before it', () => {
    // The caret column is the prefix length; 0 would land left of the bullet.
    expect(carriedMarker('- foo').length).toBe(2)
    expect(carriedMarker('- [ ] foo').length).toBe(6)
    expect(carriedMarker('  - foo').length).toBe(4)
  })
})

describe('md-notebook/isEmptyListItem', () => {
  it('is true for a marker with nothing after it', () => {
    expect(isEmptyListItem('- ', '')).toBe(true)
    expect(isEmptyListItem('- [ ] ', '')).toBe(true)
    expect(isEmptyListItem('  1. ', '')).toBe(true)
  })

  it('is false when the item has content, so Enter continues the list', () => {
    expect(isEmptyListItem('- foo', '')).toBe(false)
  })

  it('is false when text would move down, so nothing is lost', () => {
    expect(isEmptyListItem('- ', 'tail')).toBe(false)
  })

  it('is false for a plain line', () => {
    expect(isEmptyListItem('', '')).toBe(false)
  })
})

describe('md-notebook/buildTree', () => {
  const note = (path: string): Note => ({
    path,
    title: path,
    modifiedAt: 0,
    syncStatus: 'synced',
  })

  it('nests notes under their folders', () => {
    const tree = buildTree([note('a.md'), note('sub/b.md'), note('sub/deep/c.md')])
    expect(tree.notes.map(n => n.path)).toEqual(['a.md'])
    expect(tree.folders.get('sub')?.notes.map(n => n.path)).toEqual(['sub/b.md'])
    expect(
      tree.folders.get('sub')?.folders.get('deep')?.notes.map(n => n.path),
    ).toEqual(['sub/deep/c.md'])
  })

  it('returns an empty tree for no notes', () => {
    const tree = buildTree([])
    expect(tree.notes).toEqual([])
    expect(tree.folders.size).toBe(0)
  })
})

describe('md-notebook/misc helpers', () => {
  it('derives a basename without directories or extension', () => {
    expect(noteBasename('sub/My Note.md')).toBe('My Note')
    expect(noteBasename('Plain.MD')).toBe('Plain')
    expect(noteBasename('no-extension')).toBe('no-extension')
  })

  it('scopes a knowledge source to the subfolder when set', () => {
    expect(vaultContentPath({ localPath: '/v' })).toBe('/v')
    expect(vaultContentPath({ localPath: '/v', subfolder: 'notes' })).toBe('/v/notes')
  })

  it('formats a shortcut in macOS modifier order', () => {
    expect(formatShortcut({ key: 's', meta: true, ctrl: false, alt: false, shift: false })).toBe(
      '⌘ S',
    )
    expect(formatShortcut({ key: 's', meta: true, ctrl: true, alt: true, shift: true })).toBe(
      '⌃ ⌥ ⇧ ⌘ S',
    )
    expect(formatShortcut(null)).toBe('—')
  })

  it('matches a shortcut only when every modifier agrees', () => {
    const sc = { key: 's', meta: true, ctrl: false, alt: false, shift: false }
    const ev = (over: Partial<KeyboardEvent>) =>
      ({
        key: 's',
        metaKey: true,
        ctrlKey: false,
        altKey: false,
        shiftKey: false,
        ...over,
      }) as KeyboardEvent
    expect(matchesShortcut(ev({}), sc)).toBe(true)
    expect(matchesShortcut(ev({ shiftKey: true }), sc)).toBe(false)
    expect(matchesShortcut(ev({ key: 'a' }), sc)).toBe(false)
  })

  it('shows only the time for a note modified today', () => {
    const now = new Date()
    now.setHours(9, 5, 0, 0)
    // Not asserting the exact locale string, only that it omits a date part.
    expect(relTime(now.getTime())).not.toMatch(/\d{4}/)
  })
})


describe('md-notebook/orderNotes', () => {
  const note = (path: string, modifiedAt = 0): Note => ({
    path,
    title: path,
    modifiedAt,
    syncStatus: 'synced',
  })
  const byName = (a: Note, b: Note) => a.title.localeCompare(b.title)

  it('floats pinned notes above the rest', () => {
    const notes = [note('a.md'), note('b.md'), note('c.md')]
    const order = orderNotes(notes, byName, p => p === 'c.md').map(n => n.path)
    expect(order).toEqual(['c.md', 'a.md', 'b.md'])
  })

  it('keeps the chosen sort inside each group', () => {
    const notes = [note('b.md'), note('a.md'), note('d.md'), note('c.md')]
    const pinned = new Set(['d.md', 'b.md'])
    const order = orderNotes(notes, byName, p => pinned.has(p)).map(n => n.path)
    expect(order).toEqual(['b.md', 'd.md', 'a.md', 'c.md'])
  })

  it('does not mutate the input list', () => {
    const notes = [note('b.md'), note('a.md')]
    orderNotes(notes, byName, () => false)
    expect(notes.map(n => n.path)).toEqual(['b.md', 'a.md'])
  })

  it('is a no-op ordering when nothing is pinned', () => {
    const notes = [note('b.md'), note('a.md')]
    expect(orderNotes(notes, byName, () => false).map(n => n.path)).toEqual(['a.md', 'b.md'])
  })
})

describe('md-notebook/neighborAfterDelete', () => {
  const visible = ['a.md', 'b.md', 'c.md']

  it('lands on the next note down', () => {
    expect(neighborAfterDelete(visible, 'a.md')).toBe('b.md')
    expect(neighborAfterDelete(visible, 'b.md')).toBe('c.md')
  })

  it('falls back to the note above when the deleted one was last', () => {
    expect(neighborAfterDelete(visible, 'c.md')).toBe('b.md')
  })

  it('returns null for the only note, so the caller shows the empty state', () => {
    expect(neighborAfterDelete(['only.md'], 'only.md')).toBeNull()
  })

  it('returns null when the note is not on screen at all', () => {
    // A note inside a collapsed folder, or filtered out by search.
    expect(neighborAfterDelete(visible, 'hidden.md')).toBeNull()
  })
})

describe('md-notebook/flattenVisibleNotes', () => {
  const note = (path: string): Note => ({
    path,
    title: path.split('/').pop() ?? path,
    modifiedAt: 0,
    syncStatus: 'synced',
  })
  const byName = (a: Note, b: Note) => a.title.localeCompare(b.title)
  const none = () => false
  const tree = buildTree([
    note('root-b.md'),
    note('root-a.md'),
    note('sub/y.md'),
    note('sub/x.md'),
    note('sub/deep/z.md'),
  ])

  it('walks folders depth-first before this level, matching renderTree', () => {
    expect(flattenVisibleNotes(tree, byName, none, new Set())).toEqual([
      'sub/deep/z.md',
      'sub/x.md',
      'sub/y.md',
      'root-a.md',
      'root-b.md',
    ])
  })

  it('omits a collapsed folder entirely — its notes are off screen', () => {
    expect(flattenVisibleNotes(tree, byName, none, new Set(['sub']))).toEqual([
      'root-a.md',
      'root-b.md',
    ])
  })

  it('omits only the collapsed level, keeping its parent visible', () => {
    expect(flattenVisibleNotes(tree, byName, none, new Set(['sub/deep']))).toEqual([
      'sub/x.md',
      'sub/y.md',
      'root-a.md',
      'root-b.md',
    ])
  })

  it('puts pinned notes first within their own folder', () => {
    const order = flattenVisibleNotes(tree, byName, p => p === 'root-b.md', new Set())
    expect(order).toEqual(['sub/deep/z.md', 'sub/x.md', 'sub/y.md', 'root-b.md', 'root-a.md'])
  })
})

describe('markdown link sanitization', () => {
  // A note is ordinary text a user can paste or sync from a shared vault, so a
  // `javascript:` href would run script in the dashboard's own origin.
  const render = (md: string) => {
    const nodes = inline(md, 'k') as Array<{ type?: unknown; props?: { href?: string } }>
    return nodes.filter(n => typeof n === 'object' && n !== null)
  }

  it.each([
    '[open](javascript:alert(1))',
    '[open](JavaScript:alert(1))',
    '[open](\tjavascript:alert(1))',
    '[open](data:text/html,<script>alert(1)</script>)',
    '[open](vbscript:msgbox(1))',
  ])('renders %s as text, not a link', md => {
    const els = render(md)
    const hrefs = els.map(e => e.props?.href).filter(Boolean)
    expect(hrefs).toEqual([])
  })

  it.each([
    '[docs](https://example.com/a)',
    '[local](http://127.0.0.1:5476/x)',
    '[editor](vscode://file/tmp/a.md)',
  ])('keeps %s as a link', md => {
    const els = render(md)
    const hrefs = els.map(e => e.props?.href).filter(Boolean)
    expect(hrefs.length).toBe(1)
  })
})

describe('md-notebook/rowBadge', () => {
  it('shows pending only when the vault has a remote', () => {
    const modified = { deleting: false, syncStatus: 'pending' }
    expect(rowBadge({ ...modified, showSyncBadge: true })).toBe('pending')
    // A vault with no remote has nowhere for the note to be pending TO, and the
    // badge there reads as "not saved" — which is backwards, since it only
    // appears once the file has reached disk.
    expect(rowBadge({ ...modified, showSyncBadge: false })).toBeNull()
  })

  it('shows nothing for an unmodified note either way', () => {
    expect(rowBadge({ deleting: false, syncStatus: 'synced', showSyncBadge: true })).toBeNull()
    expect(rowBadge({ deleting: false, syncStatus: 'synced', showSyncBadge: false })).toBeNull()
  })

  it('lets an in-flight delete win, remote or not', () => {
    // The delete indicator shares this slot and is the newer fact about the
    // note, so suppressing the sync badge must not suppress it too.
    expect(rowBadge({ deleting: true, syncStatus: 'pending', showSyncBadge: true })).toBe(
      'deleting',
    )
    expect(rowBadge({ deleting: true, syncStatus: 'synced', showSyncBadge: false })).toBe(
      'deleting',
    )
  })
})

describe('md-notebook/targetsSameNote', () => {
  const captured = { vault: 'vault-a', path: 'One.md' }

  it('matches only the vault the captured identity came from', () => {
    expect(targetsSameNote(captured, 'vault-a', 'One.md')).toBe(true)
    // The whole point: a note path is vault-RELATIVE, so two vaults can each
    // hold `One.md`. Matching on path alone would make vault B's note look like
    // the one being deleted (keystrokes swallowed, row un-openable) or like the
    // one being saved (the conflict banner offering "use the file on disk"
    // against a buffer from a vault the save never touched).
    expect(targetsSameNote(captured, 'vault-b', 'One.md')).toBe(false)
  })

  it('does not match a different note in the same vault', () => {
    expect(targetsSameNote(captured, 'vault-a', 'Two.md')).toBe(false)
  })

  it('is false with nothing captured, and with no note open', () => {
    expect(targetsSameNote(null, 'vault-a', 'One.md')).toBe(false)
    expect(targetsSameNote(captured, 'vault-a', null)).toBe(false)
  })

  it('treats the default (null) vault as its own identity', () => {
    // `activeVaultId` is `string | null`; null means "the backend's default
    // vault", which is a real target rather than "unknown".
    expect(targetsSameNote({ vault: null, path: 'One.md' }, null, 'One.md')).toBe(true)
    expect(targetsSameNote({ vault: null, path: 'One.md' }, 'vault-a', 'One.md')).toBe(false)
  })
})
