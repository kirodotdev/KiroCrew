// The pre-share warning names every sensitive kind it found as one list. That
// list must join the way the active language joins items (de: "A, B und C",
// zh-CN: "A、B和C"), not with a hard-coded English comma.
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import ShareMessageModal from './ShareMessageModal'
// `/all` registers the de and zh-CN catalogs; `../i18n` registers English only.
import { i18next } from '../../../i18n/all'

vi.mock('html-to-image', () => ({ toBlob: vi.fn() }))
vi.mock('../../../api/dashboardConfigQuery', () => ({
  fetchDashboardConfig: async () => ({ social_share_sites: ['x', 'linkedin'] }),
}))

const previous = i18next.language
afterEach(async () => { await i18next.changeLanguage(previous) })

// One match each for aws_key, local_path and internal_url, in that pattern order.
const THREE_KINDS = 'set AKIAIOSFODNN7EXAMPLE in /home/alice/app and open http://localhost:3000'

async function renderModal(messageText: string) {
  render(<ShareMessageModal onClose={() => {}} messageText={messageText} shareEnabled />)
  // The site list is read on mount; let that answer land inside act.
  await act(async () => {})
}

describe('ShareMessageModal — the sensitive-kind list follows the locale', () => {
  it.each([
    ['en', 'an AWS access key, a local file path, and an internal URL'],
    ['de', 'einen AWS-Zugriffsschlüssel, einen lokalen Dateipfad und eine interne URL'],
    ['zh-CN', 'AWS 访问密钥、本地文件路径和内部链接'],
  ])('joins the card findings the way %s does', async (lng, expected) => {
    await i18next.changeLanguage(lng)
    await renderModal(THREE_KINDS)
    expect(screen.getByRole('alert')).toHaveTextContent(expected)
  })

  it('joins the caption findings the way the active language does', async () => {
    await i18next.changeLanguage('de')
    await renderModal('Triaged 47 issues overnight.')
    fireEvent.change(screen.getByRole('textbox', { name: i18next.t('pages.chat.share.caption_label') }), {
      target: { value: 'token ghp_abcdefghijklmnopqrstu0123456789 at /home/alice' },
    })
    expect(screen.getByRole('alert')).toHaveTextContent('ein API-Token und einen lokalen Dateipfad')
  })
})
