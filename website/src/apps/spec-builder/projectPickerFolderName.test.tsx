/**
 * The project picker names a project by its folder, keeping the full path beside
 * it. A Windows gateway reports native paths (`C:\Users\u\work\myrepo`),
 * which hold no `/`, so a `/`-only split showed the whole path where the folder
 * name belongs. The picker derives the name through `pathBasename`
 * (utils/pathBasename.ts), and a POSIX path names the same folder as before. The
 * composer's half of the rule is core's `src/test/windowsProjectNameSites.test.tsx`.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { renderWithProviders } from '../../test/helpers'
import ProjectPicker from './components/ProjectPicker'

const browse = vi.hoisted(() => vi.fn())
vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return { ...actual, specApi: { ...actual.specApi, browse } }
})

const WIN = 'C:\\Users\\u\\work\\myrepo'

// Path -> the folder name the picker shows for it.
const CASES: [string, string][] = [
  [WIN, 'myrepo'],
  [`${WIN}\\`, 'myrepo'],
  ['C:/Users/u/work/myrepo', 'myrepo'],
  ['\\\\host\\share\\myrepo', 'myrepo'],
  ['/home/u/work/myrepo', 'myrepo'],
  ['/home/u/work/myrepo/', 'myrepo'],
  ['/tmp/we\\ird', 'we\\ird'],
  ['/', '/'],
]

beforeEach(() => {
  localStorage.clear()
  browse.mockReset()
})

describe('the Spec Builder project picker names a project by its folder', () => {
  it.each(CASES)('the selected project %s shows %s beside its path', (project, name) => {
    browse.mockResolvedValue({ path: '', parent: '', dirs: [], recents: [] })
    renderWithProviders(<ProjectPicker value={project} onChange={vi.fn()} />)
    const field = screen.getByRole('button', { name: `Project folder: ${project} — change` })
    expect(field.querySelector('.font-semibold')?.textContent).toBe(name)
  })

  it.each(CASES)('the recent project %s shows %s', async (project, name) => {
    browse.mockResolvedValue({ path: '', parent: '', dirs: [], recents: [project] })
    renderWithProviders(<ProjectPicker value="" onChange={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Choose a project folder' }))
    const recent = await screen.findByRole('button', { name: `Use recent project ${project}` })
    expect(recent.querySelector('.font-semibold')?.textContent).toBe(name)
  })

  it('has no `/`-only folder-name split', () => {
    const src = readFileSync(resolve(__dirname, 'components', 'ProjectPicker.tsx'), 'utf8')
    expect(src).not.toMatch(/\.split\('\/'\)\.filter\(Boolean\)\.pop\(\)/)
  })
})
