import { afterAll, describe, expect, it, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import { SubmitBar } from '../components/ArtifactSubmitBar'
import { i18next } from '../i18n/all'

// The label is one translated sentence with the count inside it, so a language
// can place the number where its grammar needs it. Two parts joined with an
// English space read in English word order in every locale.
afterAll(async () => {
  await i18next.changeLanguage('en')
})

describe('SubmitBar pending-comments label', () => {
  it('is one sentence per count in English', () => {
    const { unmount } = render(<SubmitBar count={1} submitting={false} onSubmit={vi.fn()} />)
    expect(screen.getByText('1 comment to send to this chat')).toBeInTheDocument()
    unmount()
    render(<SubmitBar count={3} submitting={false} onSubmit={vi.fn()} />)
    expect(screen.getByText('3 comments to send to this chat')).toBeInTheDocument()
  })

  it('follows the target language word order in Japanese', async () => {
    await act(async () => {
      await i18next.changeLanguage('ja')
    })
    render(<SubmitBar count={3} submitting={false} onSubmit={vi.fn()} />)
    expect(screen.getByText('このチャットに送信するコメント 3 件')).toBeInTheDocument()
  })
})
