/**
 * Issue Radar sentences around a name, chip or markup as one message (#32029).
 *
 * The issue and pull-request timelines built "<who> added the <chip> label" and
 * "<who> added this to the <name> milestone" from catalog fragments placed
 * around the inserted part, the linked-refs heading glued "Linked" + the
 * provider's change-request plural + "& issues", and the repo settings glued
 * the new-issue toggle and the disconnect warning around a `<strong>`, the
 * repo name, the provider and a `<code>` CLI. No language could order those
 * sentences its own way. Each one is now a single message with placeholders,
 * using `<Trans>` where markup sits inside.
 */
import fs from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

// `/all` registers every authored catalog; `../i18n` registers English only.
import { i18next } from '../i18n/all'

const SRC = path.resolve(__dirname, '..')
const LANGS = ['de', 'es', 'fr', 'it', 'pt', 'ru', 'ja', 'ko', 'zh-CN', 'hi', 'bn']
const ISSUE = 'apps.issueRadar.components.issueDetail'
const PR = 'apps.issueRadar.components.prDetail'
const REPO = 'apps.issueRadar.views.settings.repoSettings'

// key -> [English text, parts every language must carry exactly once]
const SENTENCES: Record<string, [string, string[]]> = {
  [`${ISSUE}.added_the_label_chip`]: ['<who/> added the <label/> label', ['<who/>', '<label/>']],
  [`${ISSUE}.removed_the_label_chip`]: ['<who/> removed the <label/> label', ['<who/>', '<label/>']],
  [`${ISSUE}.added_this_to_the_milestone_name`]: ['<who/> added this to the <milestone/> milestone', ['<who/>', '<milestone/>']],
  [`${ISSUE}.removed_this_from_the_milestone_name`]: ['<who/> removed this from the <milestone/> milestone', ['<who/>', '<milestone/>']],
  [`${ISSUE}.linked_change_requests_and_issues`]: ['Linked {{changeRequests}} & issues', ['{{changeRequests}}']],
  [`${PR}.added_the_label_chip`]: ['<who/> added the <label/> label', ['<who/>', '<label/>']],
  [`${PR}.removed_the_label_chip`]: ['<who/> removed the <label/> label', ['<who/>', '<label/>']],
  [`${PR}.added_this_to_the_milestone_name`]: ['<who/> added this to the <milestone/> milestone', ['<who/>', '<milestone/>']],
  [`${REPO}.notify_me_when_a_new_issue_is_opened_in`]: [
    'Notify me when a <newIssue/> is opened in {{repo}}',
    ['<newIssue/>', '{{repo}}'],
  ],
  [`${REPO}.removes_repo_from_issue_radar`]: [
    'Removes {{repo}} from Issue Radar and deletes its local cache. Your {{provider}} data and <cli/> auth are untouched — you can reconnect anytime.',
    ['{{repo}}', '{{provider}}', '<cli/>'],
  ],
}
const raw = (lng: string, key: string) => i18next.getResource(lng, 'translation', key) as string | undefined

describe('Issue Radar markup sentences', () => {
  it('keeps the English text the same', () => {
    for (const [key, [en]] of Object.entries(SENTENCES)) expect(raw('en', key), key).toBe(en)
  })

  it('carries every inserted part exactly once in every shipped language', () => {
    for (const lng of LANGS) {
      for (const [key, [, parts]] of Object.entries(SENTENCES)) {
        const value = raw(lng, key)
        expect(value, `${lng} ${key}`).toBeTypeOf('string')
        for (const part of parts) expect(value!.split(part), `${lng} ${key} ${part}`).toHaveLength(2)
      }
    }
  })

  it('no longer glues these sentences from parts', () => {
    const gone: Record<string, string[]> = {
      'components/IssueDetail.tsx': [
        'issueDetail.added_the',
        'issueDetail.removed_the',
        'issueDetail.label',
        'issueDetail.added_this_to_the',
        'issueDetail.removed_this_from_the',
        'issueDetail.milestone',
        'issueDetail.linked',
        'issueDetail.issues',
      ],
      'components/PrDetail.tsx': ['prDetail.added_the', 'prDetail.removed_the', 'prDetail.label', 'prDetail.added_this_to_the', 'prDetail.milestone'],
      'views/settings/RepoSettings.tsx': [
        'repoSettings.notify_me_when_a',
        'repoSettings.is_opened_in',
        'repoSettings.removes',
        'repoSettings.data_and',
        'repoSettings.auth_are_untouched_you_can_reconnect_anytime',
      ],
    }
    for (const [file, keys] of Object.entries(gone)) {
      const lines = fs.readFileSync(path.join(SRC, 'apps/issue-radar', file), 'utf8').split('\n')
      // The closing quote keeps a longer key that shares the prefix from matching.
      const left = lines.filter((line) => keys.some((key) => line.includes(`${key}')`))).map((line) => line.trim())
      expect(left, file).toEqual([])
    }
  })
})
