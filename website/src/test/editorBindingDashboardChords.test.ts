/**
 * The editor sees a key before the dashboard's own shortcut handler does, so an
 * editor binding on a dashboard shortcut's chord would take that shortcut away
 * while the editor has focus. These pin which chords the dashboard holds, that
 * code intel's defaults avoid them, and that the Python validator's copy stays the
 * part every platform shares.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { beforeEach, describe, expect, it } from 'vitest'

import { SHORTCUT_OVERRIDES_KEY } from '../lib/shortcutRegistry'
import { canonicalTrigger, dashboardChordTriggers, dashboardShortcutFor } from '../apps/editorBindingTriggers'

const ROOT = join(__dirname, '..', '..', '..') + '/'

beforeEach(() => localStorage.clear())

describe('dashboard shortcut chords', () => {
  it('names the shortcut that holds a chord, per platform', () => {
    for (const mac of [true, false]) {
      expect(dashboardShortcutFor('alt+k', mac)).toBe('shortcuts-modal')
      expect(dashboardShortcutFor('primary+n', mac)).toBe('new-chat')
      expect(dashboardShortcutFor('alt+shift+a', mac)).toBe('cycle-agent')
      expect(dashboardShortcutFor('f12', mac)).toBeNull()
    }
    // Alt+letter jumps to a chat off macOS; on a Mac the jump uses Control.
    expect(dashboardShortcutFor('alt+i', false)).toBe('chat-letters')
    expect(dashboardShortcutFor('alt+i', true)).toBeNull()
    // Literal Control: the primary modifier off macOS, no editor modifier on it.
    expect(dashboardShortcutFor('primary+g', false)).toBe('agent-monitor')
    expect(dashboardShortcutFor('primary+g', true)).toBeNull()
  })

  it('follows the reader moving a dashboard shortcut', () => {
    localStorage.setItem(SHORTCUT_OVERRIDES_KEY, JSON.stringify({ 'shortcuts-modal': { key: 'j', mod: true, shift: true } }))
    // The override replaces the chord and its Alt+K alias.
    expect(dashboardShortcutFor('primary+shift+j', false)).toBe('shortcuts-modal')
    expect(dashboardShortcutFor('alt+k', true)).toBeNull()
  })

  it("leaves code intel's own defaults free on every platform", () => {
    const manifest = JSON.parse(readFileSync(`${ROOT}src/kiro_crew/apps/builtins/code_intel/app.json`, 'utf8'))
    for (const b of manifest.contributes.editorBindings) {
      const t = b.trigger as { key?: string; click?: boolean; modifiers?: string[] }
      const canonical = canonicalTrigger((t.key ?? '').toLowerCase(), t.click === true, t.modifiers ?? [])
      for (const mac of [true, false]) expect(dashboardShortcutFor(canonical, mac), `${b.id} ${canonical}`).toBeNull()
    }
  })

  it("matches the Python validator's reserved set: the shared part plus save and quick search", () => {
    const source = readFileSync(`${ROOT}src/kiro_crew/apps/manifest_contributes.py`, 'utf8')
    const block = source.slice(source.indexOf('RESERVED_EDITOR_CHORDS = frozenset('))
    const python = [...block.slice(0, block.indexOf('\n)\n')).matchAll(/"([^"]+)"/g)].map(m => m[1]).sort()
    const mac = dashboardChordTriggers(true)
    const other = dashboardChordTriggers(false)
    const shared = [...mac.keys()].filter(k => other.has(k))
    expect(python).toEqual(['primary+k', 'primary+s', ...shared].sort())
  })
})
