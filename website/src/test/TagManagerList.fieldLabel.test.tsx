import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import TagManagerList from '../components/TagManagerList'
import { i18next } from '../i18n/all'
import zh from '../i18n/locales/zh-CN.json'

// The new-tag box has an accessible name. A placeholder is not one: it
// vanishes once text is typed, and assistive tech may skip it.

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: {
    chatTags: vi.fn().mockResolvedValue([]),
    createChatTag: vi.fn().mockResolvedValue({ ok: true }),
    updateChatTag: vi.fn().mockResolvedValue({ ok: true }),
    adoptChatTag: vi.fn().mockResolvedValue({ ok: true }),
    deleteChatTag: vi.fn().mockResolvedValue({ ok: true }),
    chatFolders: vi.fn().mockResolvedValue([]),
    slackChannels: vi.fn().mockResolvedValue([]),
    setSlotColor: vi.fn().mockResolvedValue({}),
    mcpActive: vi.fn().mockResolvedValue([]),
  },
}))

vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

function renderList(mode: 'manage' | 'column-filter') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <TagManagerList mode={mode} selectedIds={[]} onToggleTag={vi.fn()} />
    </QueryClientProvider>,
  )
}

describe('TagManagerList new-tag name', () => {
  afterEach(async () => { await i18next.changeLanguage('en') })

  it.each(['manage', 'column-filter'] as const)('names the new-tag box in %s mode', mode => {
    renderList(mode)
    expect(screen.getByRole('textbox', { name: 'New tag name' })).toBeInTheDocument()
  })

  it('takes the name from the catalog in another language', async () => {
    await i18next.changeLanguage('zh-CN')
    renderList('manage')
    expect(screen.getByRole('textbox', { name: zh.components.tagManagerList.new_tag_label })).toBeInTheDocument()
  })
})
