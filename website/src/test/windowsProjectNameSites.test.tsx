/**
 * The composer and the Spec Builder project picker name a project by its folder,
 * keeping the full path for tooltips and the review dialog. A Windows gateway
 * reports native paths (`C:\Users\u\work\myrepo`), which hold no `/`, so
 * a `/`-only split showed the whole path where the folder name belongs. Each
 * site derives the name through `pathBasename` (utils/pathBasename.ts), and a
 * POSIX path names the same folder as before.
 */
import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, within } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import ProjectPicker from '../apps/spec-builder/components/ProjectPicker'
import { TerminalHostContext } from '../hooks/useTerminalCommand'

const browse = vi.hoisted(() => vi.fn())
vi.mock('../apps/spec-builder/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../apps/spec-builder/api')>()
  return { ...actual, specApi: { ...actual.specApi, browse } }
})
vi.mock('../utils/terminalRegistry', () => ({
  isTerminalEnabled: () => true,
  useTerminalEnabled: () => true,
  onTerminalReady: () => () => {},
  sendToTerminalSession: () => true,
}))

const WIN = 'C:\\Users\\u\\work\\myrepo'

// Path -> the folder name every site shows for it.
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

const base = { value: '', onChange: vi.fn(), onSend: vi.fn() }

beforeEach(() => {
  localStorage.clear()
  browse.mockReset()
})

describe('the composer names a project by its folder', () => {
  it.each(CASES)('project chip for %s shows %s, tooltip keeps the path', (project, name) => {
    renderWithProviders(<ChatInput {...base} onProjectClick={vi.fn()} project={project} />)
    const chip = screen.getByRole('button', { name: /Project: / })
    expect(chip).toHaveTextContent(new RegExp(`^${name.replace(/[\\/]/g, '\\$&')}$`))
    expect(chip.getAttribute('title')).toBe(`Project: ${project}`)
  })

  it.each(CASES)('read-only peer chip for %s shows %s', (project, name) => {
    renderWithProviders(<ChatInput {...base} sessionOnPeer project={project} />)
    const chip = screen.getByTestId('composer-project-readonly')
    expect(chip.textContent).toBe(name)
    expect(chip.getAttribute('title')).toContain(project)
  })

  it.each(CASES)('terminal hint for %s names %s', (project, name) => {
    renderWithProviders(
      <TerminalHostContext.Provider value="docked">
        <ChatInput {...base} value="! pwd" terminalCommands="local" project={project} />
      </TerminalHostContext.Provider>,
    )
    const hint = within(screen.getByRole('group', { name: 'Review and run in terminal' })).getByRole('status')
    expect(hint.textContent).toBe(`Terminal panel · ${name}`)
    fireEvent.click(screen.getByRole('button', { name: 'Review terminal command' }))
    expect(screen.getByRole('dialog')).toHaveTextContent(project)
  })

  it.each(CASES)('mid-turn note for %s names %s', (project, name) => {
    renderWithProviders(<ChatInput {...base} onProjectClick={vi.fn()} project={project} isRunning onStop={vi.fn()} />)
    const chip = screen.getByRole('button', { name: /Project: / })
    expect(chip.getAttribute('title')).toContain(`This reply finishes in ${name}.`)
  })
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
})

describe('no project-name site brings the `/`-only split back', () => {
  it.each([
    'components/ChatInput.tsx',
    'apps/spec-builder/components/ProjectPicker.tsx',
  ])('%s has no `/`-only folder-name split', (rel) => {
    const src = readFileSync(resolve(__dirname, '..', rel), 'utf8')
    expect(src).not.toMatch(/\.split\('\/'\)\.filter\(Boolean\)\.pop\(\)/)
  })
})
