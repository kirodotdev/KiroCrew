/**
 * Three sentences around a styled name as one translated message each (#33210).
 *
 * The Dev Fleet how-to paragraph wove the bold action names (Pull + Build,
 * Pod, Rebase, Prune) between catalog fragments, and the Issue Radar issue and
 * pull-request timelines built "<who> changed the title <old> → <new>" from a
 * catalog fragment placed between styled spans. No language could order those
 * sentences its own way: Japanese, Korean, Hindi and Bengali read them in
 * English word order. Each sentence is now one message that carries the styled
 * parts as `<Trans>` markup.
 */
import fs from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

// `/all` registers every authored catalog; `../i18n` registers English only.
import { i18next } from '../i18n/all'

const SRC = path.resolve(__dirname, '..')
const LANGS = ['de', 'es', 'fr', 'it', 'pt', 'ru', 'ja', 'ko', 'zh-CN', 'hi', 'bn']
const HOW = 'pages.devFleetPage.row_actions_how_to'
const TITLE_KEYS = [
  'apps.issueRadar.components.issueDetail.changed_the_title_from_to',
  'apps.issueRadar.components.prDetail.changed_the_title_from_to',
]
const t = (lng: string, key: string) => i18next.getFixedT(lng)(key) as string

describe('styled-name whole sentences', () => {
  it('keeps the English text the same', () => {
    expect(t('en', HOW)).toBe(
      'Each row below is a git worktree discovered from the main checkout. Use <pull/> on the main row to fast-forward it from origin and rebuild (then restart the gateway to apply). <pod/> boots any worktree as an isolated throwaway gateway so you can QA a feature branch without touching your live instance. <rebase/> moves a feature branch onto the latest main, and <prune/> safely removes worktrees whose PR has already merged.',
    )
    for (const key of TITLE_KEYS) expect(t('en', key)).toBe('<who/> changed the title <from/> → <to/>')
  })

  it('carries every styled part exactly once in every shipped language', () => {
    for (const lng of LANGS) {
      for (const tag of ['<pull/>', '<pod/>', '<rebase/>', '<prune/>']) {
        expect(t(lng, HOW).split(tag), `${lng} ${tag}`).toHaveLength(2)
      }
      expect(t(lng, HOW)).not.toBe(t('en', HOW))
      for (const key of TITLE_KEYS) {
        for (const tag of ['<who/>', '<from/>', '<to/>']) {
          expect(t(lng, key).split(tag), `${lng} ${key} ${tag}`).toHaveLength(2)
        }
      }
    }
  })

  it('puts the verb after the new title in Japanese, Korean, Hindi and Bengali', () => {
    for (const lng of ['ja', 'ko', 'hi', 'bn']) {
      for (const key of TITLE_KEYS) {
        const value = t(lng, key)
        expect(value.indexOf('<to/>'), `${lng} ${key}`).toBeLessThan(value.length - '<to/>'.length)
        expect(value.endsWith('<to/>'), `${lng} ${key}`).toBe(false)
      }
    }
  })

  it('no longer glues these sentences from parts', () => {
    const notices = fs.readFileSync(path.join(SRC, 'apps/dev-fleet/DevFleetPageNotices.tsx'), 'utf8')
    expect(notices).not.toContain("devFleetPage.each_row_below_is_a_git_worktree_discovered_from'")
    expect(notices).not.toContain("devFleetPage.safely_removes_worktrees_whose_pr_has_already_me'")
    for (const file of ['IssueDetail.tsx', 'PrDetail.tsx']) {
      const src = fs.readFileSync(path.join(SRC, 'apps/issue-radar/components', file), 'utf8')
      expect(src).not.toContain(".changed_the_title')")
    }
  })
})
