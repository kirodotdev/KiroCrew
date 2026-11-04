import fs from 'node:fs'
import path from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'

import { i18next } from './index'
import './all'
import { i18nT } from './t'

const SRC = path.resolve(__dirname, '..')

afterEach(async () => {
  if (i18next.language !== 'en') await i18next.changeLanguage('en')
})

describe('counts on comment threads, file chips, the queue, approvals and subagents', () => {
  it('picks the English singular and plural forms', () => {
    expect(i18nT('components.commentThreads.open_count', { count: 2 })).toBe('2 open')
    expect(i18nT('components.commentThreads.resolved_count', { count: 1 })).toBe('1 resolved')
    expect(i18nT('components.commentThreads.messages_count', { count: 3 })).toBe('3 messages')
    expect(i18nT('components.fileChangeChips.show_more_count', { count: 4 })).toBe('Show 4 more')
    expect(i18nT('components.queueStack.queued_count', { count: 3 })).toBe('3 queued')
    expect(i18nT('pages.chat.collapsibleToolGroup.approvals_pending_count', { count: 1 })).toBe('1 approval pending')
    expect(i18nT('pages.chat.collapsibleToolGroup.approvals_pending_count', { count: 2 })).toBe('2 approvals pending')
    expect(i18nT('pages.chat.subagentProgressBar.more_running_normally_count', { count: 5 })).toBe('5 more running normally…')
  })

  it('places the number where German puts it', async () => {
    await i18next.changeLanguage('de')
    expect(i18nT('components.fileChangeChips.show_more_count', { count: 4 })).toBe('4 weitere anzeigen')
    expect(i18nT('pages.chat.subagentProgressBar.more_running_normally_count', { count: 1 })).toBe('1 weiterer läuft normal…')
  })

  it('uses every Russian plural form', async () => {
    await i18next.changeLanguage('ru')
    const key = 'pages.chat.collapsibleToolGroup.approvals_pending_count'
    expect(i18nT(key, { count: 21 })).toBe('21 подтверждение ожидает')
    expect(i18nT(key, { count: 3 })).toBe('3 подтверждения ожидают')
    expect(i18nT(key, { count: 5 })).toBe('5 подтверждений ожидают')
    expect(i18nT('components.fileChangeChips.show_more_count', { count: 2 })).toBe('Показать ещё 2')
  })

  it.each([
    ['components/CommentThreads.tsx', '{resolvedCount} {i18nT('],
    ['components/CommentThreads.tsx', '{t.replies.length + 1} {i18nT('],
    ['components/FileChangeChips.tsx', '{hiddenCount} {i18nT('],
    ['components/QueueStack.tsx', '{messages.length} {i18nT('],
    ['pages/chat/CollapsibleToolGroup.tsx', '{pendingPermCount} {i18nT('],
    ['pages/chat/SubagentProgressBar.tsx', '{hiddenCount} {i18nT('],
  ])('%s passes the count into the catalog', (file, glued) => {
    expect(fs.readFileSync(path.join(SRC, file), 'utf8')).not.toContain(glued)
  })
})
