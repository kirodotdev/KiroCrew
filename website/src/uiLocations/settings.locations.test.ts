/**
 * Goldens for the `settings` area (controls inside Settings tabs): the exact
 * parent chain, entry, route, label key and requirements each location carries
 * in the committed index, and the marker sitting in the file it claims.
 */
import { describe, expect, it } from 'vitest'
import * as fs from 'node:fs'
import * as path from 'node:path'

interface Placement { surface_id: string; route: string; parent_ids: string[]; entry_kind: string; requires: unknown[] }
interface Loc { id: string; kind: string; label_key: string; alias_keys?: string[]; terms?: Record<string, string[]>; placements: Placement[] }

const INDEX_FILE = path.resolve(__dirname, '../../../src/kiro_crew/docs/ui-index.generated.json')
const index = JSON.parse(fs.readFileSync(INDEX_FILE, 'utf-8')) as { locations: Loc[]; labels: Record<string, Record<string, string>> }
const byId = new Map(index.locations.map(l => [l.id, l]))
const src = (rel: string) => fs.readFileSync(path.resolve(__dirname, rel), 'utf-8')
const OVERVIEW_SRC = src('../pages/OverviewPage.tsx')
const PORTABILITY_SRC = src('../pages/overview/PortabilityTab.tsx')

type Expect = { labelKey: string; aliasKeys?: string[]; source: string; en: string; zh: string; tab: string; route: string }

const EXPECTED: Record<string, Expect> = {
  'backup.export': {
    labelKey: 'pages.overview.portabilityTab.download_export_zip', source: PORTABILITY_SRC,
    en: 'Download Export (.zip)', zh: '下载导出文件（.zip）', tab: 'settings.tab.imports', route: '/settings/imports',
  },
  'backup.import-file': {
    labelKey: 'pages.overview.portabilityTab.choose_file', source: PORTABILITY_SRC,
    en: 'Choose file', zh: '选择文件', tab: 'settings.tab.imports', route: '/settings/imports',
  },
}

describe('settings area locations', () => {
  it('indexes exactly the expected settings-area ids', () => {
    const ids = index.locations.map(l => l.id).filter(id => id.startsWith('overview.') || id.startsWith('backup.')).sort()
    expect(ids).toEqual(Object.keys(EXPECTED).sort())
  })

  for (const [id, e] of Object.entries(EXPECTED)) {
    it(`${id}: label, aliases, placement and render site`, () => {
      const loc = byId.get(id)
      expect(loc, id).toBeDefined()
      expect(loc!.kind).toBe('button')
      expect(loc!.label_key).toBe(e.labelKey)
      expect(index.labels.en[e.labelKey]).toBe(e.en)
      expect(index.labels['zh-CN'][e.labelKey]).toBe(e.zh)
      expect(loc!.alias_keys ?? []).toEqual(e.aliasKeys ?? [])
      expect(loc!.placements).toEqual([{
        surface_id: 'settings', route: e.route, parent_ids: ['page.settings', e.tab], entry_kind: 'content', requires: [],
      }])
      const marker = `uiLocation('${id}')`
      expect(e.source.split(marker).length - 1, id).toBe(1)
      for (const other of [OVERVIEW_SRC, PORTABILITY_SRC].filter(s => s !== e.source)) {
        expect(other.includes(marker), id).toBe(false)
      }
      expect(loc!.terms?.en?.length, id).toBeGreaterThan(0)
      expect(loc!.terms?.['zh-CN']?.length, id).toBeGreaterThan(0)
    })
  }

  it('finds the memory page as its own Settings entry, not an Overview card', () => {
    const tab = byId.get('settings.tab.memory')
    expect(tab?.placements.map(p => p.route)).toEqual(['/settings/memory'])
    expect(tab?.terms?.en).toContain('manage memory')
    expect(tab?.terms?.['zh-CN']).toContain('管理记忆')
    expect(byId.has('overview.memory-details')).toBe(false)
    expect(OVERVIEW_SRC).not.toContain('MemorySummaryCard')
  })
})
