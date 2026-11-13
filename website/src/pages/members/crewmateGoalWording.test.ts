/**
 * One word for a mate's goal (#19229).
 *
 * The New mate card's optional field is stored as the crew record's
 * `description`, and the new mate's first message asks the user to confirm it
 * as its goal. The field's label must therefore be the word the Goals tab uses,
 * in every shipped language, so a reader never has to guess that two names
 * mean the same thing.
 */
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, it, expect } from 'vitest'

const LOCALES_DIR = join(__dirname, '..', '..', 'i18n', 'locales')

/** Shipped translated catalogs: not the generated pseudo-locale or the manual overlay. */
const CATALOGS = readdirSync(LOCALES_DIR)
  .filter(name => name.endsWith('.json') && name !== 'en-XA.json' && name !== 'en.manual.json')
  .sort()

function membersPage(file: string): Record<string, string> {
  const doc = JSON.parse(readFileSync(join(LOCALES_DIR, file), 'utf8'))
  return doc.pages.membersPage
}

describe('the New mate card names its goal with the Goals tab word', () => {
  it('covers every shipped language', () => {
    expect(CATALOGS).toContain('en.json')
    expect(CATALOGS.length).toBeGreaterThan(1)
  })

  it.each(CATALOGS)('%s: create_job is the singular goal word of goals_goal_number', file => {
    const page = membersPage(file)
    const goalWord = page.goals_goal_number.replace('{{number}}', '').trim()
    expect(goalWord).not.toBe('')
    expect(page.create_job).toBe(goalWord)
  })

  it('en: the card label reads "Goal"', () => {
    expect(membersPage('en.json').create_job).toBe('Goal')
  })
})
