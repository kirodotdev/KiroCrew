/**
 * The status-tile HUD above the composer and the agent-published Overview are
 * retired. The side panel's Overview is the root session's own Dynamic Dashboard,
 * so nothing in the app may import, render or name the retired parts again.
 */
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative, resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const SRC = resolve(__dirname, '..')

/** Names that only the retired surfaces carried. */
const RETIRED = [
  'CommandCenterDock',
  'StatusTiles',
  'TileList',
  'TaskDashboardFrame',
  'REQUEST_PUBLISHED_VIEW',
  'commandCenter.prompt',
  'TASK_DASHBOARD_TAG',
  'mc-task-dashboard-hidden',
  'onOpenCommandCenter',
]

function sources(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    if (name === 'node_modules' || name === 'locales') continue
    const path = join(dir, name)
    if (statSync(path).isDirectory()) sources(path, out)
    else if (/\.(ts|tsx|js|jsx|mjs)$/.test(name)) out.push(path)
  }
  return out
}

describe('retired dashboard surfaces', () => {
  it('leaves no reference to the HUD or the agent-published Overview', () => {
    const self = resolve(__filename)
    const hits = sources(SRC)
      .filter(path => path !== self)
      .flatMap(path => {
        const text = readFileSync(path, 'utf8')
        return RETIRED.filter(name => text.includes(name)).map(name => `${relative(SRC, path)}: ${name}`)
      })
    expect(hits).toEqual([])
  })
})
