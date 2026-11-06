import { afterEach, describe, expect, it } from 'vitest'

import { i18next } from './index'
import './all'
import { i18nT } from './t'

const KEY = 'pages.devFleetPage.force_remove_confirm_desc'

afterEach(async () => {
  if (i18next.language !== 'en') await i18next.changeLanguage('en')
})

describe('the Dev Fleet force-remove confirm counts its worktrees', () => {
  it('picks the English singular and plural forms', () => {
    expect(i18nT(KEY, { count: 1 })).toBe(
      '1 worktree has unmerged or uncommitted work. Removing it permanently deletes all local changes. This cannot be undone.',
    )
    expect(i18nT(KEY, { count: 3 })).toBe(
      '3 worktrees have unmerged or uncommitted work. Removing them permanently deletes all local changes. This cannot be undone.',
    )
  })

  it('drops the (s) shortcut in German and French', async () => {
    await i18next.changeLanguage('de')
    expect(i18nT(KEY, { count: 1 })).toMatch(/^1 Worktree hat /)
    expect(i18nT(KEY, { count: 2 })).toMatch(/^2 Worktrees haben /)
    await i18next.changeLanguage('fr')
    expect(i18nT(KEY, { count: 1 })).toMatch(/^1 worktree a du travail/)
    expect(i18nT(KEY, { count: 2 })).not.toContain('(s)')
  })

  it('agrees the Russian verb with one worktree', async () => {
    await i18next.changeLanguage('ru')
    expect(i18nT(KEY, { count: 1 })).toMatch(/^1 worktree содержит /)
    expect(i18nT(KEY, { count: 21 })).toMatch(/^21 worktree содержит /)
    expect(i18nT(KEY, { count: 5 })).toMatch(/^5 worktree содержат /)
  })
})
