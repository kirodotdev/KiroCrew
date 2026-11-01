/**
 * The Shared Crew memory-change notice: a newer lesson replaced an older one,
 * and every viewer of the session is told who changed what, in their language.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient } from '@tanstack/react-query'
import i18next from 'i18next'
import '../i18n/all'
import type { MemoryChange } from '../api/sharedCrew'
import { applySharedCrewFrame, MEMORY_CHANGES_KEPT, sharedCrewKeys } from '../hooks/sharedCrew/sharedCrewSocket'
import { isSystemNoticeKind } from '../lib/systemNotice'
import { SystemNoticeRow, isSystemNoticeRow, memoryChangeNoticeText } from '../pages/chat/CompactionCard'
import type { ChatMessage } from '../types'

const change: MemoryChange = {
  actor: { id: 'p_b', kind: 'human', name: 'Bob' },
  session_key: 'dashboard:chat-1',
  category: 'preference',
  new: 'Always reply in English',
  old: ['Always reply in Chinese'],
}

const row = (meta: Record<string, unknown>): ChatMessage =>
  ({ role: 'assistant', content: 'Bob changed a Crew preference: Always reply in English', meta }) as ChatMessage

describe('memory change notice', () => {
  it('is a system notice, so last-real-message scans skip it', () => {
    expect(isSystemNoticeKind('memory_change')).toBe(true)
    expect(isSystemNoticeRow(row({ kind: 'memory_change', memory_change: change }))).toBe(true)
  })

  it('names the person and the new preference', () => {
    render(<SystemNoticeRow message={row({ kind: 'memory_change', memory_change: change })} />)
    expect(screen.getByText('Bob changed a Crew preference: Always reply in English')).toBeTruthy()
  })

  it('uses the lesson wording for other categories and a stand-in for no name', () => {
    const text = memoryChangeNoticeText(row({ kind: 'memory_change', memory_change: { ...change, category: 'tool', actor: { id: 'p', kind: 'human', name: '' } } }))
    expect(text).toBe('Someone changed a Crew lesson: Always reply in English')
  })

  it('falls back to the gateway text when the payload does not validate', () => {
    expect(memoryChangeNoticeText(row({ kind: 'memory_change' }))).toBe('Bob changed a Crew preference: Always reply in English')
  })

  it('renders in the reader language', async () => {
    await i18next.changeLanguage('zh-CN')
    try {
      expect(memoryChangeNoticeText(row({ kind: 'memory_change', memory_change: change }))).toBe('Bob 更改了 Crew 偏好：Always reply in English')
    } finally {
      await i18next.changeLanguage('en')
    }
  })
})

describe('memory change frame', () => {
  it('keeps the newest changes first, bounded', () => {
    const qc = new QueryClient()
    for (let i = 0; i < MEMORY_CHANGES_KEPT + 3; i++) {
      applySharedCrewFrame(qc, { type: 'shared_crew.memory_change', schema_version: 1, change: { ...change, new: `rule ${i}` } })
    }
    const kept = qc.getQueryData<MemoryChange[]>(sharedCrewKeys.memoryChanges) ?? []
    expect(kept).toHaveLength(MEMORY_CHANGES_KEPT)
    expect(kept[0].new).toBe(`rule ${MEMORY_CHANGES_KEPT + 2}`)
  })

  it('ignores a frame with no change', () => {
    const qc = new QueryClient()
    applySharedCrewFrame(qc, { type: 'shared_crew.memory_change', schema_version: 1 })
    expect(qc.getQueryData(sharedCrewKeys.memoryChanges)).toBeUndefined()
  })
})
