/**
 * `planTreeStateRows` decides which folders of a listing get the tree's state
 * row and what it says; `PIERRE_TREE_STATE_ROW_CSS` selects those rows in
 * Pierre's shadow root by the decoration the wrapper emits for a planned row
 * (never by the marker the synthetic segment ends in, which a real name can
 * share). Both are pure, so the edge cases the component tests would only
 * reach through a payload are pinned here.
 */
import { describe, it, expect } from 'vitest'
import { planTreeStateRows, stateRowsMatchingFilter, STATE_ROW_DECORATION, STATE_ROW_ICON, STATE_ROW_MARKER } from '../pierre/treeStateRows'
import { PIERRE_TREE_STATE_ROW_CSS } from '../pierre/config'

const LABELS = {
  empty: 'Empty folder',
  'hidden-only': 'Contains only hidden items (dotfiles, caches)',
  truncated: 'Files not shown',
} as const
const M = STATE_ROW_MARKER

describe('planTreeStateRows', () => {
  it('names one row per childless folder, with the kind the payload proves, in folder order', () => {
    const plan = planTreeStateRows(
      {
        paths: ['src/a.ts'],
        directories: ['src', 'empty', 'onlyhidden', 'locked', 'cut'],
        hiddenOnlyDirectories: ['onlyhidden'],
        unreadableDirectories: ['locked'],
        truncatedDirectories: ['cut'],
      },
      LABELS,
    )
    // A Set keeps insertion order, so spreading it into the model's path list
    // puts each row right after the folders it belongs to. `locked` gets none:
    // a failed read is the notice's to report, not a status line's.
    expect([...plan.paths]).toEqual([
      `empty/Empty folder${M}`,
      `onlyhidden/Contains only hidden items (dotfiles, caches)${M}`,
      `cut/Files not shown${M}`,
    ])
    expect(plan.paths.has(`cut/Files not shown${M}`)).toBe(true)
    expect(plan.paths.has('src/a.ts')).toBe(false)
    // The folders those rows sit under, for the decoration that must not repeat
    // what the row beneath already says.
    expect([...plan.folders]).toEqual(['empty', 'onlyhidden', 'cut'])
    expect(plan.folders.has('src')).toBe(false)
    expect(plan.folders.has('locked')).toBe(false)
  })

  it('plans no row under an unreadable folder, and none for its parent either', () => {
    // The server lists the folder it could not read as a directory row of its
    // own, so `vault` -- whose only entry it is -- has a child and gets no row.
    // The unreadable folder itself gets none: nothing beneath it is known, so
    // no row may make a claim, and the failure reaches the user through the
    // wrapper's `ErrorNotice`, never through a label in the tree.
    const plan = planTreeStateRows(
      { paths: [], directories: ['vault', 'vault/locked'], unreadableDirectories: ['vault/locked'] },
      LABELS,
    )
    expect(plan.paths.size).toBe(0)
    expect(plan.folders.size).toBe(0)
  })

  it('never calls an unreadable folder empty, even when the payload lists it in no other set', () => {
    // A malformed or partial payload that names a folder both childless and
    // unreadable still gets no "Empty folder" row: unreadable wins over empty.
    const plan = planTreeStateRows(
      { paths: [], directories: ['locked'], unreadableDirectories: ['locked'], truncatedDirectories: ['locked'] },
      LABELS,
    )
    expect(plan.paths.size).toBe(0)
  })

  it('plans no row for the root the server names as unreadable', () => {
    // `.` in `unreadableDirectories` is the project root itself: there is no
    // folder row to hang a state row under, and the component shows the
    // not-readable state in the tree's place instead.
    const plan = planTreeStateRows({ paths: [], directories: [], unreadableDirectories: ['.'] }, LABELS)
    expect(plan.paths.size).toBe(0)
    expect(plan.folders.size).toBe(0)
  })

  it('treats a folder with a subfolder as populated, down to the childless leaf', () => {
    const plan = planTreeStateRows({ paths: [], directories: ['a/b/c'] }, LABELS)
    expect([...plan.paths]).toEqual([`a/b/c/Empty folder${M}`])
    expect([...plan.folders]).toEqual(['a/b/c'])
  })

  it('reads implicit ancestors off file paths and explicit rows alike', () => {
    // `pkg` is named only as an ancestor of a file; `docs/` arrives with the
    // server's trailing slash. Neither is childless.
    const plan = planTreeStateRows({ paths: ['pkg/lib/x.ts'], directories: ['docs/', 'docs/api'] }, LABELS)
    expect([...plan.paths]).toEqual([`docs/api/Empty folder${M}`])
  })

  it('puts nothing under a listing with no folders', () => {
    const plan = planTreeStateRows({ paths: ['a.ts', 'b.ts'] }, LABELS)
    expect(plan.paths.size).toBe(0)
    expect(plan.folders.size).toBe(0)
  })

  it('never lets a label mint a subfolder', () => {
    // A translation written with a slash would otherwise become two segments
    // and the row would land one level too deep, under a folder that does not
    // exist.
    const plan = planTreeStateRows(
      { paths: [], directories: ['d'] },
      { ...LABELS, empty: 'vide / rien' },
    )
    expect([...plan.paths]).toEqual([`d/vide \u2215 rien${M}`])
  })

  it('ends every segment in the marker the stylesheet selects, and paints nothing for it', () => {
    // The marker is what tells a state row from a real file that happens to be
    // named like a label; it must be invisible and must not be whitespace the
    // widget could trim away.
    const plan = planTreeStateRows(
      { paths: [], directories: ['a', 'b', 'c', 'd'], hiddenOnlyDirectories: ['b'], unreadableDirectories: ['c'], truncatedDirectories: ['d'] },
      LABELS,
    )
    for (const path of plan.paths) expect(path.endsWith(M)).toBe(true)
    expect(M).toBe('\u200b')
    expect(M.trim()).toBe(M)
  })
})

describe('stateRowsMatchingFilter', () => {
  const plan = planTreeStateRows(
    { paths: ['src/app.ts'], directories: ['empty', 'Notes/Drafts', 'big'], truncatedDirectories: ['big'] },
    LABELS,
  )

  it('feeds the row of a folder the query matches, so the folder Pierre opens is not open over nothing', () => {
    // "emp" matches `empty/`; `big/` and `Notes/Drafts/` do not.
    expect(stateRowsMatchingFilter(plan, 'emp')).toEqual({
      paths: new Set([`empty/${LABELS.empty}${M}`]),
      folders: new Set(['empty']),
    })
  })

  it('withholds a row whose label alone matches, so a status line is never a search result', () => {
    // Every label carries "folder" or "files"; none of the folders does.
    expect(stateRowsMatchingFilter(plan, 'folder')).toEqual({ paths: new Set(), folders: new Set() })
    expect(stateRowsMatchingFilter(plan, 'not shown')).toEqual({ paths: new Set(), folders: new Set() })
  })

  it('matches the way Pierre does: trimmed, case-folded, backslashes read as slashes, against the listed `dir/` path', () => {
    const drafts = { paths: new Set([`Notes/Drafts/${LABELS.empty}${M}`]), folders: new Set(['Notes/Drafts']) }
    expect(stateRowsMatchingFilter(plan, ' DRAFTS ')).toEqual(drafts)
    expect(stateRowsMatchingFilter(plan, 'notes\\dra')).toEqual(drafts)
    // The trailing slash is part of what Pierre matches.
    expect(stateRowsMatchingFilter(plan, 'drafts/')).toEqual(drafts)
  })

  it('treats a blank query as no filter and hands the whole plan back', () => {
    expect(stateRowsMatchingFilter(plan, '')).toBe(plan)
    expect(stateRowsMatchingFilter(plan, '   ')).toBe(plan)
  })
})

describe('PIERRE_TREE_STATE_ROW_CSS', () => {
  it('selects the decoration a planned row carries -- never the path marker or a label -- and keeps the pointer off the row', () => {
    // A real file can end in the marker character too, so the path is not the
    // hook: the wrapper emits STATE_ROW_ICON for planned rows only, and the
    // sheet selects the row that carries it.
    expect(PIERRE_TREE_STATE_ROW_CSS).toContain(
      `[data-type="item"]:has([data-item-section="decoration"] [data-icon-name="${STATE_ROW_ICON}"])`,
    )
    expect(PIERRE_TREE_STATE_ROW_CSS).not.toContain('data-item-path')
    expect(PIERRE_TREE_STATE_ROW_CSS).not.toContain(M)
    expect(PIERRE_TREE_STATE_ROW_CSS).not.toContain('Empty folder')
    expect(PIERRE_TREE_STATE_ROW_CSS).toContain('pointer-events:none')
    expect(PIERRE_TREE_STATE_ROW_CSS).toContain('[data-item-section="icon"]')
    expect(PIERRE_TREE_STATE_ROW_CSS).toContain('[data-item-section="action"]')
    // The token svg itself paints nothing: no symbol behind it, and hidden.
    expect(PIERRE_TREE_STATE_ROW_CSS).toContain(`[data-icon-name="${STATE_ROW_ICON}"]{display:none}`)
    expect(STATE_ROW_DECORATION).toEqual({ icon: { name: STATE_ROW_ICON, width: 0, height: 0 } })
  })
})
