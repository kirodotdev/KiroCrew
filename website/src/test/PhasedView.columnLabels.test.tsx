// The phased board's three column headings are user-facing words, so they must
// follow the active language like the Failed / Skipped / Cancelled groups below
// them, and must re-resolve after a language switch rather than freeze at import.
import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import PhasedView from '../pages/aidlc/PhasedView'
import type { TaskDetail } from '../types'
// `/all` registers every catalog; `../i18n` registers English only.
import { i18next } from '../i18n/all'

const previous = i18next.language
afterEach(async () => { await i18next.changeLanguage(previous) })

const task = (index: number, status: string): TaskDetail => ({
  index, title: `Step ${index}`, description: '', status, error: '', result: '', attempts: 1,
  depends_on: [], requires_approval: false,
})
const TASKS = [task(1, 'pending'), task(2, 'in_progress'), task(3, 'passed')]

describe('PhasedView — column headings follow the locale', () => {
  it.each([
    ['en', 'To do (1)', 'In progress (1)', 'Done (1)'],
    ['de', 'Zu erledigen (1)', 'In Bearbeitung (1)', 'Erledigt (1)'],
    ['ja', '未着手 (1)', '進行中 (1)', '完了 (1)'],
  ])('names the columns in %s', async (lng, todo, doing, done) => {
    await i18next.changeLanguage(lng)
    render(<PhasedView tasks={TASKS} />)
    for (const heading of [todo, doing, done]) {
      expect(screen.getByText(heading, { exact: false })).toBeInTheDocument()
    }
  })

  it('re-resolves the headings after a language switch', async () => {
    await i18next.changeLanguage('en')
    const { rerender } = render(<PhasedView tasks={TASKS} />)
    expect(screen.getByText('To do (1)', { exact: false })).toBeInTheDocument()
    await act(async () => { await i18next.changeLanguage('de') })
    rerender(<PhasedView tasks={[...TASKS]} />)
    expect(screen.getByText('Zu erledigen (1)', { exact: false })).toBeInTheDocument()
  })
})
