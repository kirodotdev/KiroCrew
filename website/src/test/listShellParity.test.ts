/**
 * Parity pin: the Sessions sidebar and the Crew Members roster are built from
 * the SAME list-shell recipes (`components/listShell.ts`) — card, header line,
 * title, list body, row box, row states, row type scale.
 *
 * The roster drifted from the sidebar once by copied value: its own header
 * padding (16/16/4 vs 8/2/0 on a 40px line), row title weight (500 vs 600),
 * unset line-heights (20.15/17.05 vs 20/16), and — the one a user saw — its own
 * `bg-bg-elevated` card that stayed white in kiro-light while the sessions list
 * beside it stepped back to `--panel`. None of that was red anywhere. This test
 * makes the recipes the only way to spell those surfaces on either page.
 *
 * Mutation-verified by construction: re-inlining any listed literal in either
 * page fails the negative pin; dropping a constant's use fails the positive one.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import * as shell from '../components/listShell'

const read = (...p: string[]) => readFileSync(join(__dirname, '..', ...p), 'utf8')

const PAGES: ReadonlyArray<readonly [label: string, file: readonly string[]]> = [
  ['ChatSidebar', ['pages', 'ChatSidebar.tsx']],
  ['MembersPage', ['pages', 'members', 'MembersPage.tsx']],
]

/** Every recipe both pages must consume. */
const SHARED = ['LIST_SHELL_CLS', 'LIST_HEADER_CLS', 'LIST_TITLE_CLS', 'LIST_BODY_CLS', 'ROW_BOX_CLS', 'ROW_IDLE_CLS', 'ROW_ACTIVE_CLS', 'ROW_TITLE_CLS', 'ROW_STATUS_CLS'] as const

/** The literals those recipes replace. Spelling one of these out again in a
 *  page is the drift this file exists to catch. */
const FORBIDDEN_LITERALS = [
  shell.LIST_SHELL_CLS,
  'bg-bg-elevated border border-border rounded-xl shadow-sm',
  shell.LIST_HEADER_CLS,
  'sessions-panel-title text-sm',
  shell.LIST_BODY_CLS,
  "'text-text-strong bg-accent-subtle'",
  'text-[13px] font-medium truncate',
  'text-[13px] leading-[20px]',
  'text-[11px] leading-[16px]',
  'text-[10px] leading-[12px]',
]

describe('list shell parity — sessions sidebar and members roster share one recipe set', () => {
  it.each(PAGES)('%s imports every shared recipe from components/listShell', (_label, file) => {
    const src = read(...file)
    const m = src.match(/import \{([^}]*)\} from '(?:\.\.\/)+components\/listShell'/)
    expect(m, `${file.join('/')} must import from components/listShell`).not.toBeNull()
    const names = m![1].split(',').map(s => s.trim()).filter(Boolean)
    for (const name of SHARED) expect(names, `${file.join('/')} must import ${name}`).toContain(name)
  })

  it.each(PAGES)('%s uses each imported recipe at least once', (_label, file) => {
    const src = read(...file)
    for (const name of SHARED) {
      // A use is the identifier outside the import line — inside a template,
      // a cn() call, or as a bare className value.
      const uses = src.split('\n').filter(l => !l.startsWith('import ') && new RegExp(`\\b${name}\\b`).test(l))
      expect(uses.length, `${file.join('/')} imports ${name} but never uses it`).toBeGreaterThan(0)
    }
  })

  it.each(PAGES)('%s spells none of the recipes out by hand', (_label, file) => {
    const src = read(...file)
    for (const lit of FORBIDDEN_LITERALS) {
      expect(src.includes(lit), `${file.join('/')} re-inlines "${lit}" — use the components/listShell recipe`).toBe(false)
    }
  })

  it('the recipes carry the tokens the sidebar was built on', () => {
    // Values, not just names: a recipe that quietly changed would pass the
    // structural pins above while moving both pages together off the sidebar's
    // documented look. These are the sidebar's own classes as of extraction.
    expect(shell.LIST_SHELL_CLS).toBe('sidebar sidebar-inner bg-bg-elevated border border-border rounded-xl shadow-sm')
    expect(shell.LIST_HEADER_CLS).toBe('flex justify-between items-center px-2 mt-0.5 h-10')
    expect(shell.LIST_TITLE_CLS).toBe('sessions-panel-title text-sm font-semibold text-text-strong tracking-[.04em] truncate')
    expect(shell.LIST_BODY_CLS).toBe('flex-1 min-h-0 overflow-y-auto scrollbar-none p-2')
    expect(shell.ROW_BOX_CLS).toBe('pl-3.5 pr-3 py-2 rounded-md')
    expect(shell.ROW_IDLE_CLS).toBe('text-muted hover:text-text hover:bg-bg-hover')
    expect(shell.ROW_ACTIVE_CLS).toBe('text-text-strong bg-accent-subtle')
    expect(shell.ROW_TITLE_CLS).toBe('text-[13px] leading-[20px]')
    expect(shell.ROW_STATUS_CLS).toBe('text-[11px] leading-[16px]')
    expect(shell.ROW_META_CLS).toBe('text-[10px] leading-[12px]')
  })

  it('the Notes rail mirrors the row type scale by value (it is styled inline)', () => {
    // apps/md-notebook cannot import class strings; its RAIL_TYPE copies the
    // three sizes. Pin the copy so a change here is a change there too.
    const rail = read('apps', 'md-notebook', 'constants.ts')
    for (const [px, lh] of [['13px', '20px'], ['11px', '16px'], ['10px', '12px']]) {
      expect(rail, `RAIL_TYPE must carry ${px}/${lh}`).toMatch(new RegExp(`fontSize: '${px}', lineHeight: '${lh}'`))
    }
  })
})
