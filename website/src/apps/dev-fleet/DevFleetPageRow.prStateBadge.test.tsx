import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { DetailPanel } from './DevFleetPageRow'
import type { Worktree } from './devFleetPageModel'
import { i18next } from '../../i18n/all'

/* The PR state badge in a worktree's detail panel printed GitHub's state word
 * lowercased (`open`, `merged`, `closed`), so every locale read English there
 * while the rest of the panel was translated. */

const w = { name: 'wt-a' } as Worktree
const detail = (state: string) => ({ branch: 'fix/x', pr: { number: 7, state, url: 'https://example.test/pr/7', title: 'Fix' } })

function panel(state: string) {
  return render(<DetailPanel w={w} d={detail(state)} busy={{}} onRemove={vi.fn()} onLoadLogs={vi.fn()} />)
}

afterEach(async () => { await i18next.changeLanguage('en') })

describe('DetailPanel PR state badge', () => {
  it.each([
    ['OPEN', 'オープン'],
    ['MERGED', 'マージ済み'],
    ['CLOSED', 'クローズ'],
  ])('names a %s pull request in the reader\'s language', async (state, word) => {
    await i18next.changeLanguage('ja')
    panel(state)
    expect(screen.getByText(word)).toBeInTheDocument()
  })

  it('keeps the English words in English', () => {
    panel('MERGED')
    expect(screen.getByText('merged')).toBeInTheDocument()
  })

  it('still shows a state the catalog does not know, as GitHub sent it', async () => {
    await i18next.changeLanguage('ja')
    panel('LOCKED')
    expect(screen.getByText('locked')).toBeInTheDocument()
  })
})
