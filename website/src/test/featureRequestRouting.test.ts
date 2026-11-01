import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import type { List, Nodes } from 'mdast'
import remarkParse from 'remark-parse'
import { unified } from 'unified'
import { parse } from 'yaml'

import { FEATURE_REQUEST_PROMPT_FALLBACK } from '../prompts/featureRequest'

// The `feature-request` skill tells the agent where a mid-session problem report
// goes: a vulnerability to SECURITY.md, a broken install to the Report a Problem
// dialog, a question to Discussions, the rest to the public issue forms. Every
// one of those destinations is owned by another file, so each destination is
// checked against the file that owns it, and the `kirocrew` command against the
// CLI parser in test/test_feature_request_skill_routing.py. A renamed template, a
// moved contact link or a relabelled button then fails here instead of leaving
// the skill sending users somewhere that no longer exists. The "Request a
// Feature" button seeds the prompt fallback instead of the skill, so both copies
// are held to the same routes. Wording outside the security route, the other
// routes' prose, the repository link and the plain and prefilled new-issue links
// are not checked.

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

// A route is a line plus the indented lines that continue it: one line of the
// fallback, one wrapped list item of the skill.
const routes = (text: string) => text.split(/\n(?=\S)/)

// The skill as CommonMark blocks, and the skill's own text from one block to
// another. A heading's depth is 1 for `#`; every other block is deeper than any
// heading.
const skillBlocks = unified().use(remarkParse).parse(skill).children
const spelt = (from: Nodes, to: Nodes = from) => skill.slice(from.position?.start.offset, to.position?.end.offset)
const depth = (node: Nodes) => (node.type === 'heading' ? node.depth : Infinity)
const isHeading = (node: Nodes, title: string) =>
  node.type === 'heading' && node.children.map((child) => ('value' in child ? child.value : '')).join('') === title

// Bold and line wrapping are layout, not wording: a `**…**` pair is dropped unless
// it sits inside inline code or a fence, and each run of whitespace becomes one
// space. Every other `*` is kept.
const CODE = /(```[\s\S]*?```|`[^`\n]*`)/
const flat = (text: string) =>
  text
    .split(CODE)
    .map((part, i) => (i % 2 ? part : part.replace(/\*\*(?=\S)([\s\S]*?\S)\*\*/g, '$1')))
    .join('')
    .replace(/\s+/g, ' ')
    .trim()

// Each copy's security route, word for word. An edit that `flat` does not undo
// fails here until this text changes with it. This compares text; it does not
// judge meaning.
const SKILL_SECURITY_ROUTE = (url: string) =>
  '1. A security vulnerability. Do not draft it, search for it or file it. Give the user ' +
  'the security policy and tell them to report it privately by the email address that ' +
  'file lists, with the `[SECURITY]` subject prefix — never in a public issue: ``` ' +
  url +
  ' ```'
const FALLBACK_SECURITY_ROUTE = (url: string) =>
  'If the user describes a security vulnerability, do not draft or file it: give them ' +
  url +
  ' and tell them to report it privately by email as that file says — never in a public issue.'

// A link counts only where it stands whole: after a space, tab, line break, "("
// or the start, and before a space, tab, line break or the end, with only
// closing punctuation between. So ".../discussions-archive" or
// ".../SECURITY.md.bak" does not cite it. The classes are spelt out because `\s`
// also matches characters such as U+FEFF that do not end a link.
function expectCitesWhole(where: string, text: string, url: string) {
  const parts = text.split(url)
  expect(parts.length, `${where} does not cite ${url}`).toBeGreaterThan(1)
  for (let i = 1; i < parts.length; i++) {
    expect(parts[i - 1], `${where}: ${url} ends a longer link`).toMatch(/(?:^|[ \t\r\n(])$/)
    expect(parts[i], `${where}: ${url} runs on into a longer link`).toMatch(/^[.,;:!?)]*(?:[ \t\r\n]|$)/)
  }
}

// Each anchor must be present and occur once, so a dropped or repeated route
// fails as such rather than as out of order; then each must precede the next.
function expectInOrder(where: string, text: string, anchors: string[]) {
  const at = anchors.map((anchor) => text.indexOf(anchor))
  anchors.forEach((anchor, i) => {
    expect(at[i], `${where} is missing ${anchor}`).toBeGreaterThan(-1)
    expect(text.indexOf(anchor, at[i] + 1), `${where} repeats ${anchor}`).toBe(-1)
  })
  for (let i = 1; i < at.length; i++) {
    expect(at[i - 1], `${where}: ${anchors[i - 1]} must come before ${anchors[i]}`).toBeLessThan(at[i])
  }
}

const kirocrewCommands = (text: string) => [...text.matchAll(/`kirocrew ([^`]+)`/g)].map((m) => m[1])

describe('feature-request routing', () => {
  describe.each([
    ['skill', skill, SKILL_SECURITY_ROUTE],
    ['prompt fallback', FEATURE_REQUEST_PROMPT_FALLBACK, FALLBACK_SECURITY_ROUTE],
  ])('%s', (name, text, securityRoute) => {
    it('names only issue templates that exist', () => {
      const cited = [...text.matchAll(/issues\/new\?template=([\w.-]+)/g)].map((m) => m[1])
      expect(cited.length).toBeGreaterThan(0)
      for (const file of cited) expect(templates).toContain(file)
    })

    // A vulnerability must never be pointed at a public form, so both copies
    // carry the private route the issue chooser itself offers, in the words
    // pinned above.
    it('sends a vulnerability to the security policy config.yml links', () => {
      const url = contactUrl(/security/i)
      expectCitesWhole(name, text, url)
      const cited = routes(text).filter((route) => route.includes(url))
      expect(cited, 'exactly one route cites the security policy').toHaveLength(1)
      expect(flat(cited[0])).toBe(securityRoute(url))
    })

    it('sends a question to the Discussions link config.yml offers', () => {
      expectCitesWhole(name, text, contactUrl(/question/i))
    })

    // The labels and the Settings path are the shipped English strings, so
    // renaming the button, the rail link or a step of the path fails here until
    // both copies name the new label.
    it('names the Report a Problem dialog by its shipped labels', () => {
      const card = enManual.pages.settings.reportProblemCard
      const path = [enManual.nav.settings, enManual.settings.tabs.about.label, card.support, card.report_a_problem]
      for (const label of [...path, enManual.app.report_issue]) {
        expect(typeof label).toBe('string')
        expect(text).toContain(label)
      }
      expect(flat(text)).toContain(path.join(' › '))
      expect(text).toContain('kirocrew doctor --bundle')
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

  // "Take the first case that fits": the skill is parsed as CommonMark, and its
  // section "Route it first" (the blocks up to the next heading of the same or a
  // higher level) must be the intro paragraph and then one list, so only the
  // intro comes before the list. The list's first item must be the route that
  // cites the security policy, and the other routes must follow in order.
  it('routes the skill in order before the filing workflow starts', () => {
    const at = skillBlocks.findIndex((node) => isHeading(node, 'Route it first'))
    expect(at, 'the skill has a "Route it first" heading').toBeGreaterThan(-1)
    const workflow = skillBlocks.findIndex((node) => isHeading(node, 'Workflow'))
    expect(workflow, '"Route it first" precedes "Workflow"').toBeGreaterThan(at)
    const end = skillBlocks.findIndex((node, i) => i > at && depth(node) <= depth(skillBlocks[at]))
    const section = skillBlocks.slice(at + 1, end < 0 ? undefined : end)
    expect(
      section.map((node) => node.type),
      '"Route it first" is the intro paragraph and then the list of routes, nothing else',
    ).toEqual(['paragraph', 'list'])
    const [intro, list] = section as [Nodes, List]
    const security = contactUrl(/security/i)
    expect(spelt(list.children[0]), 'the first list item of "Route it first" is the security route').toContain(security)
    expectInOrder('skill', flat(spelt(intro, list)), [
      security,
      'Report a Problem dialog',
      contactUrl(/question/i),
      'Anything else',
    ])
  })

  // The fallback has no headings, so each route line is found by a phrase only
  // it carries. Of its non-empty lines, only the opening line and the greeting
  // come before the security route.
  it('routes the prompt fallback in order before its filing workflow starts', () => {
    const security = contactUrl(/security/i)
    const lines = FEATURE_REQUEST_PROMPT_FALLBACK.split('\n').filter((line) => line.trim())
    const greeting = lines.findIndex((line) => line.includes('Greet the user'))
    expect(greeting, 'the greeting follows the opening line').toBe(1)
    expect(lines[greeting + 1], 'the first line after the greeting is the security route').toContain(security)
    expectInOrder('fallback', FEATURE_REQUEST_PROMPT_FALLBACK, [
      security,
      'Report a Problem dialog',
      contactUrl(/question/i),
      'Guide them conversationally',
    ])
  })

  it('cites the same kirocrew commands in the fallback as in the skill', () => {
    expect(kirocrewCommands(FEATURE_REQUEST_PROMPT_FALLBACK)).toEqual(kirocrewCommands(skill))
  })
})
