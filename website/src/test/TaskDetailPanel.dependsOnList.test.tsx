// A finished task lists the tasks it depended on as one sentence. That list
// must join the way the active language joins items (de: "A, B und C",
// zh-CN: "A、B和C"), not with a hard-coded English comma.
import { describe, it, expect, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import TaskDetailPanel from '../pages/aidlc/TaskDetailPanel'
import type { TaskDetail } from '../types'
// `/all` registers the de and zh-CN catalogs; `../i18n` registers English only.
import { i18next } from '../i18n/all'

const previous = i18next.language
afterEach(async () => { await i18next.changeLanguage(previous) })

const base = { description: '', status: 'passed', error: '', result: '', attempts: 1, depends_on: [], requires_approval: false }
const allTasks: TaskDetail[] = [
  { ...base, index: 1, title: 'Setup' },
  { ...base, index: 2, title: 'Build' },
  { ...base, index: 3, title: 'Test' },
  { ...base, index: 4, title: 'Deploy', depends_on: [1, 2, 3] },
]

describe('TaskDetailPanel — the depends-on list follows the locale', () => {
  it.each([
    ['en', 'Task 1 (Setup), Task 2 (Build), and Task 3 (Test)'],
    ['de', 'Aufgabe 1 (Setup), Aufgabe 2 (Build) und Aufgabe 3 (Test)'],
    ['zh-CN', '任务 1（Setup）、任务 2（Build）和任务 3（Test）'],
  ])('joins the dependencies the way %s does', async (lng, expected) => {
    await i18next.changeLanguage(lng)
    render(<TaskDetailPanel task={allTasks[3]} allTasks={allTasks} onClose={() => {}} />)
    const line = screen.getByText(i18next.t('pages.aidlc.taskDetailPanel.depends_on_2'), { exact: false })
    expect(line).toHaveTextContent(expected)
  })
})
