import { describe, expect, it } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import { join, relative, sep } from 'node:path'
import * as facadeModule from './SidePanel'
import * as addMenuModule from './SidePanelAddMenu'
import * as sizeModule from './useSidePanelSize'

/* ── SidePanel's module boundary. `pages/chat/SidePanel.tsx` is the only import
 *    path: hosts import it, and the specs that mount a host mock it by that id,
 *    so a module that imported an owner directly would slip past those mocks.
 *    Imports run one way: the facade imports its owners, and no owner imports
 *    the facade. The facade exports every value name its consumers import, each
 *    as the owning module's own binding, and calls the sizing hook where the
 *    panel's other hooks expect it: last, after the diff-view preferences. ── */

const WEBSITE = join(__dirname, '..', '..', '..')
const ROOTS = ['src', 'capture']
const CHAT = __dirname
const FACADE = 'SidePanel.tsx'
const OWNER_RE = /^(use)?SidePanel/
const OWNERS = readdirSync(CHAT)
  .filter(f => OWNER_RE.test(f) && f !== FACADE && !/\.test\.tsx?$/.test(f))
  .sort()

/** Static, side-effect and dynamic import specifiers of a module. */
function importSpecifiers(src: string): string[] {
  const out: string[] = []
  for (const m of src.matchAll(/^(?:import|export)\b[^'"]*?\bfrom\s+['"]([^'"]+)['"]/gm)) out.push(m[1])
  for (const m of src.matchAll(/^import\s+['"]([^'"]+)['"]/gm)) out.push(m[1])
  for (const m of src.matchAll(/\bimport\(\s*['"]([^'"]+)['"]\s*\)/g)) out.push(m[1])
  return out
}

/** The pages/chat module a relative or `@/` specifier names, or null for anything else. */
function chatModuleOf(spec: string, fromDir: string): string | null {
  const abs = spec.startsWith('@/') ? join(WEBSITE, 'src', spec.slice(2))
    : spec.startsWith('.') ? join(fromDir, spec) : null
  if (abs === null) return null
  const rel = relative(CHAT, abs).split(sep).join('/')
  return rel.includes('/') ? null : rel.replace(/\.tsx?$/, '')
}

/** Non-test source files under a website root, relative to the website dir. */
function sourceFiles(root: string): string[] {
  return readdirSync(join(WEBSITE, root), { recursive: true, encoding: 'utf8' })
    .map(p => `${root}/${p.split('\\').join('/')}`)
    .filter(p => /\.tsx?$/.test(p) && !/\.test\.tsx?$/.test(p) && !p.startsWith('src/test/'))
}

describe('SidePanel module boundary', () => {
  it('names its owners', () => {
    expect(OWNERS).toEqual([
      'SidePanelAddMenu.tsx',
      'SidePanelTabBodies.tsx',
      'SidePanelTabChip.tsx',
      'SidePanelTabIcons.tsx',
      'SidePanelTabNav.tsx',
      'useSidePanelSize.ts',
    ])
  })

  it('no owner imports the facade', () => {
    const offenders = OWNERS.filter(f => importSpecifiers(readFileSync(join(CHAT, f), 'utf8'))
      .some(s => chatModuleOf(s, CHAT) === 'SidePanel'))
    expect(offenders).toEqual([])
  })

  it('outside the facade and its owners, no source module imports an owner', () => {
    const owners = new Set(OWNERS.map(f => f.replace(/\.tsx?$/, '')))
    const family = new Set([FACADE, ...OWNERS].map(f => `src/pages/chat/${f}`))
    const offenders: string[] = []
    for (const root of ROOTS) {
      for (const rel of sourceFiles(root)) {
        if (family.has(rel)) continue
        const dir = join(WEBSITE, rel, '..')
        for (const spec of importSpecifiers(readFileSync(join(WEBSITE, rel), 'utf8'))) {
          const target = chatModuleOf(spec, dir)
          if (target && owners.has(target)) offenders.push(`${rel} -> ${spec}`)
        }
      }
    }
    expect(offenders).toEqual([])
  })

  it('the import scan sees every import shape and resolves from any directory', () => {
    const probe = "import a from './SidePanelTabChip'\nexport { b } from '../x'\nimport './SidePanel'\nconst c = () => import('./Other')\n"
    expect(importSpecifiers(probe)).toEqual(['./SidePanelTabChip', '../x', './SidePanel', './Other'])
    expect(chatModuleOf('./SidePanel', CHAT)).toBe('SidePanel')
    expect(chatModuleOf('../chat/useSidePanelSize', join(CHAT, '..', 'members'))).toBe('useSidePanelSize')
    expect(chatModuleOf('../SidePanelAddMenu', join(CHAT, 'page'))).toBe('SidePanelAddMenu')
    expect(chatModuleOf('@/pages/chat/SidePanelTabChip', join(WEBSITE, 'capture'))).toBe('SidePanelTabChip')
    expect(chatModuleOf('@/lib/basePath', CHAT)).toBeNull()
    expect(chatModuleOf('../x', CHAT)).toBeNull()
    expect(sourceFiles('capture').length).toBeGreaterThan(0)
  })

  it('exports every value name its consumers import, as the owner binding', () => {
    expect(Object.keys(facadeModule)).toEqual(expect.arrayContaining([
      'CHAT_PANE_MIN_W',
      'NEW_MENU_DESC_KEY',
      'NEW_MENU_LABEL_KEY',
      'SIDE_PANEL_MIN_W',
      'SIDE_PANEL_RESERVED_W',
      'default',
      'newMenuSections',
      'sidePanelEffectiveWidth',
      'sidePanelFillWidth',
    ]))
    expect(facadeModule.NEW_MENU_LABEL_KEY).toBe(addMenuModule.NEW_MENU_LABEL_KEY)
    expect(facadeModule.NEW_MENU_DESC_KEY).toBe(addMenuModule.NEW_MENU_DESC_KEY)
    expect(facadeModule.newMenuSections).toBe(addMenuModule.newMenuSections)
    expect(facadeModule.sidePanelFillWidth).toBe(sizeModule.sidePanelFillWidth)
    expect(facadeModule.sidePanelEffectiveWidth).toBe(sizeModule.sidePanelEffectiveWidth)
    expect(facadeModule.SIDE_PANEL_MIN_W).toBe(sizeModule.SIDE_PANEL_MIN_W)
    expect(facadeModule.SIDE_PANEL_RESERVED_W).toBe(sizeModule.SIDE_PANEL_RESERVED_W)
    expect(facadeModule.CHAT_PANE_MIN_W).toBe(sizeModule.CHAT_PANE_MIN_W)
  })

  it('calls the sizing hook once, as the last hook before the render', () => {
    const src = readFileSync(join(CHAT, FACADE), 'utf8').replace(/\r\n/g, '\n')
    const body = src.slice(src.indexOf('export default function SidePanel('))
    const call = body.indexOf('= useSidePanelSize({')
    expect(body.split('useSidePanelSize(').length - 1).toBe(1)
    expect(body.indexOf('= useDiffSplit()')).toBeGreaterThan(-1)
    expect(call).toBeGreaterThan(body.indexOf('= useDiffSplit()'))
    // Nothing else is called with a hook's name between the sizing hook and the render.
    const tail = body.slice(call + '= useSidePanelSize({'.length, body.indexOf('\n  return (\n'))
    expect(tail).not.toMatch(/\buse[A-Z]\w*\(/)
  })
})
