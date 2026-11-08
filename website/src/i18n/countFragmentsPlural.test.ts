import fs from 'node:fs'
import path from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'

import { i18next } from './index'
import './all'
import { i18nT } from './t'

const SRC = path.resolve(__dirname, '..')
const PR = 'apps.issueRadar.components.prDetail'

afterEach(async () => {
  if (i18next.language !== 'en') await i18next.changeLanguage('en')
})

describe('counts on PR checks, knowledge, file previews, comments and logs', () => {
  it('picks the English singular and plural forms', () => {
    expect(i18nT(`${PR}.skipped_count`, { count: 2 })).toBe('2 skipped')
    expect(i18nT('pages.knowledge.knowledgeGraph.graph_counts', {
      nodes: i18nT('pages.knowledge.knowledgeGraph.nodes_count', { count: 1 }),
      edges: i18nT('pages.knowledge.knowledgeGraph.edges_count', { count: 3 }),
    })).toBe('1 node, 3 edges')
    expect(i18nT('pages.chat.knowledgePicker.tokens_count', { count: 1 })).toBe('1 token')
    expect(i18nT('components.fileRenderers.showing_first_rows', { shown: 500, count: 812 })).toBe('Showing 500 of 812 rows')
    expect(i18nT('components.fileRenderers.items_count', { count: 1 })).toBe('1 item')
    expect(i18nT('components.commentsSidebar.show_resolved_count', { count: 2 })).toBe('Show 2 resolved')
    expect(i18nT('pages.logsPage.matches_count', { count: 1 })).toBe('1 match')
  })

  it('translates them in German', async () => {
    await i18next.changeLanguage('de')
    expect(i18nT(`${PR}.running_count`, { count: 2 })).toBe('2 laufen')
    expect(i18nT(`${PR}.skipped_count`, { count: 2 })).toBe('2 übersprungen')
    expect(i18nT('pages.chat.knowledgePicker.selected_count', { count: 3 })).toBe('3 ausgewählt')
    expect(i18nT('components.fileRenderers.showing_first_rows', { shown: 500, count: 812 })).toBe('500 von 812 Zeilen angezeigt')
    expect(i18nT('components.commentsSidebar.hide_resolved_count', { count: 2 })).toBe('Erledigte ausblenden (2)')
  })

  it('uses every Russian plural form', async () => {
    await i18next.changeLanguage('ru')
    const key = 'pages.logsPage.matches_count'
    expect(i18nT(key, { count: 1 })).toBe('1 совпадение')
    expect(i18nT(key, { count: 3 })).toBe('3 совпадения')
    expect(i18nT(key, { count: 5 })).toBe('5 совпадений')
    expect(i18nT('pages.knowledge.knowledgeGraph.edges_count', { count: 21 })).toBe('21 ребро')
    expect(i18nT('components.fileRenderers.lines_count', { count: 2 })).toBe('2 строки')
    expect(i18nT('components.fileRenderers.more_count', { count: 31 })).toBe('ещё 31')
  })

  it.each([
    ['apps/issue-radar/components/PrDetail.tsx', '} skipped`'],
    ['apps/issue-radar/components/PrDetail.tsx', "{failing.length} {i18nT("],
    ['pages/knowledge/KnowledgeGraph.tsx', "{graphData.nodes.length} {i18nT("],
    ['pages/chat/KnowledgePicker.tsx', "{selected.size} {i18nT("],
    ['pages/chat/KnowledgePicker.tsx', "{r.tokens} {i18nT("],
    ['components/FileRenderers.tsx', "{body.length} {i18nT("],
    ['components/FileRenderers.tsx', "{entries.length} {i18nT("],
    ['components/FileRenderers.tsx', "{entries.length - 200} {i18nT("],
    ['components/FileRenderers.tsx', "{lines.length} {i18nT("],
    ['components/CommentsSidebar.tsx', "{resolvedCount} {i18nT("],
    ['pages/LogsPage.tsx', "{matchCount} {i18nT("],
  ])('%s passes the count into the catalog (%s)', (file, glued) => {
    expect(fs.readFileSync(path.join(SRC, file), 'utf8')).not.toContain(glued)
  })
})
