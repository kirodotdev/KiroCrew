import fs from 'node:fs'
import path from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'

import { i18next } from '../../i18n/index'
import '../../i18n/all'
import { i18nT } from '../../i18n/t'

const GENERAL = 'apps.issueRadar.views.settings.generalSettings.your_provider_identity_and_watched_repos'
const REPO = 'apps.issueRadar.views.settings.repoSettings.local_triage_settings_intro'

afterEach(async () => {
  if (i18next.language !== 'en') await i18next.changeLanguage('en')
})

describe('Issue Radar settings intro sentences', () => {
  it('keep the English text', () => {
    expect(i18nT(GENERAL, { provider: 'GitHub' })).toBe('Your GitHub identity and the repositories Issue Radar watches.')
    expect(i18nT(REPO, { repo: 'widgets', provider: 'GitLab' })).toBe(
      'Local triage settings for this repo — they teach Issue Radar how widgets organises its issues and are never written back to GitLab.',
    )
  })

  it('let each language place the provider and repo inside one sentence', async () => {
    await i18next.changeLanguage('zh-CN')
    // The old split spelled "GitHub" into the catalog, so a GitLab repo read wrong.
    expect(i18nT(REPO, { repo: 'widgets', provider: 'GitLab' })).toBe('此仓库的本地分类设置 — 它们告诉 Issue Radar widgets 如何组织其 issue，且永不写回 GitLab。')
    await i18next.changeLanguage('ja')
    expect(i18nT(GENERAL, { provider: 'GitHub' })).toBe('GitHub のアイデンティティと、Issue Radar が監視するリポジトリ。')
    await i18next.changeLanguage('it')
    expect(i18nT(GENERAL, { provider: 'GitHub' })).toBe('La tua identità GitHub e i repository monitorati da Issue Radar.')
  })

  it('no longer glue the provider or repo between catalog fragments', () => {
    const dir = path.resolve(__dirname, 'views/settings')
    const general = fs.readFileSync(path.join(dir, 'GeneralSettings.tsx'), 'utf8').split('\n')
    const repo = fs.readFileSync(path.join(dir, 'RepoSettings.tsx'), 'utf8').split('\n')
    const fragments = [
      "generalSettings.your'",
      'identity_and_the_repositories_issue_radar_watche',
      'local_triage_settings_for_this_repo_they_teach_i',
      'organises_its_issues_and_are_never_written_back',
    ]
    expect([...general, ...repo].filter((line) => fragments.some((f) => line.includes(f)))).toEqual([])
    expect(general.filter((line) => line.includes(`'${GENERAL}'`))).toHaveLength(1)
    expect(repo.filter((line) => line.includes(`'${REPO}'`))).toHaveLength(1)
  })
})
