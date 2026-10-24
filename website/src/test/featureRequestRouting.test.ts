import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { parse } from 'yaml'

import { FEATURE_REQUEST_PROMPT_FALLBACK } from '../prompts/featureRequest'

// The `feature-request` skill tells the agent where a mid-session problem report
// goes: a vulnerability to SECURITY.md, a broken install to the Report a Problem
// dialog, a question to Discussions, the rest to the public issue forms. Every
// one of those destinations is owned by another file, so each route is checked
// against the file it cites rather than against a copy of it. A renamed template,
// a moved contact link or a relabelled button then fails here instead of leaving
// the skill sending users somewhere that no longer exists.

const REPO_ROOT = resolve(__dirname, '../../..')
const read = (rel: string) => readFileSync(resolve(REPO_ROOT, rel), 'utf-8')

const skill = read('src/kiro_crew/builtin_skills/feature-request/SKILL.md')
const securityPolicy = read('SECURITY.md')
const templates = readdirSync(resolve(REPO_ROOT, '.github/ISSUE_TEMPLATE'))
const contactLinks: { name: string; url: string }[] = parse(
  read('.github/ISSUE_TEMPLATE/config.yml'),
).contact_links
const enManual = JSON.parse(read('website/src/i18n/locales/en.manual.json'))

function contactUrl(name: RegExp): string {
  const link = contactLinks.find((l) => name.test(l.name))
  expect(link, `no contact link named ${name} in .github/ISSUE_TEMPLATE/config.yml`).toBeDefined()
  return link?.url ?? ''
}

describe('feature-request routing', () => {
  describe.each([
    ['skill', skill],
    ['prompt fallback', FEATURE_REQUEST_PROMPT_FALLBACK],
  ])('%s', (_name, text) => {
    it('names only issue templates that exist', () => {
      const cited = [...text.matchAll(/issues\/new\?template=([\w.-]+)/g)].map((m) => m[1])
      expect(cited.length).toBeGreaterThan(0)
      for (const file of cited) expect(templates).toContain(file)
    })

    // A vulnerability must never be pointed at a public form, so both copies
    // carry the private route the issue chooser itself offers.
    it('sends a vulnerability to the security policy config.yml links', () => {
      expect(text).toContain(contactUrl(/security/i))
    })
  })

  it('offers the bug-report form beside the feature-request form', () => {
    expect(skill).toContain('issues/new?template=bug_report.yml')
  })

  // The URL above is only a private route if the file it names says so. The
  // emphasis is stripped first so moving the bold does not read as a change.
  it('cites a security policy that reports privately by email', () => {
    expect(contactUrl(/security/i)).toMatch(/\/blob\/main\/SECURITY\.md$/)
    expect(securityPolicy.replace(/\*/g, '')).toMatch(/do not create a public/i)
    expect(securityPolicy).toContain('mailto:')
  })

  it('sends a question to the Discussions link config.yml offers', () => {
    expect(skill).toContain(contactUrl(/question/i))
  })

  // The labels are the shipped English strings, so renaming the button or the
  // rail link fails here until the skill names the new label.
  it('names the Report a Problem dialog by its shipped labels', () => {
    const card = enManual.pages.settings.reportProblemCard
    for (const label of [card.support, card.report_a_problem, enManual.app.report_issue]) {
      expect(typeof label).toBe('string')
      expect(skill).toContain(label)
    }
    expect(skill).toContain('kirocrew doctor --bundle')
  })

  it('routes before the filing workflow starts', () => {
    const route = skill.indexOf('## Route it first')
    expect(route).toBeGreaterThan(-1)
    expect(route).toBeLessThan(skill.indexOf('## Workflow'))
  })
})
