/** Issue Radar's agent output language.
 *
 * Three properties are pinned here, because each one silently degrades in a way
 * a screenshot would not show:
 *
 *  1. English resolves to NO directive, so an English user's seed prompt stays
 *     byte-identical — the feature costs nothing when it is not being used.
 *  2. The directive names the prose fields only. `suggested_labels` values are
 *     matched against the repo's real labels downstream, so a translated label
 *     name stops matching and the suggestion is dropped instead of applied.
 *  3. Both hooks read the preference at CLICK time. They live outside the
 *     settings page's tree, so a stale read here would mean the setting appears
 *     to do nothing until a reload.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook } from '@testing-library/react'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

import type { Issue, InvestigationRecord, PullRequest, RepoRef } from '../apps/issue-radar/api'

let locale = 'en'
vi.mock('../i18n/format', async () => {
  const actual = await vi.importActual<typeof import('../i18n/format')>('../i18n/format')
  return { ...actual, activeLocale: () => locale }
})

const openSession = vi.fn()
vi.mock('../apps/issue-radar/lib/agentSession', async () => {
  const actual = await vi.importActual<typeof import('../apps/issue-radar/lib/agentSession')>(
    '../apps/issue-radar/lib/agentSession',
  )
  return { ...actual, useAgentSession: () => ({ openSession, busy: false, error: null }) }
})

// The live selection the provider would hold. Kept separate from the persisted
// value on purpose: the prompt must follow THIS even when the two disagree.
let livePref = ''
vi.mock('../apps/issue-radar/context', async () => {
  const actual = await vi.importActual<typeof import('../apps/issue-radar/context')>(
    '../apps/issue-radar/context',
  )
  return { ...actual, useIssueRadar: () => ({ aiLanguage: livePref }) }
})

const {
  AI_LANGUAGE_CHOICES, AI_LANGUAGE_FOLLOW, UI_STATE_KEY,
  coerceAiLanguage, patchUiState, resolveAiLanguage, storedAiLanguage,
} = await import('../apps/issue-radar/lib/format')
const { buildInvestigationPrompt } = await import('../apps/issue-radar/lib/investigate.prompt')
const { useInvestigate } = await import('../apps/issue-radar/lib/investigate')
const { useReviewPr } = await import('../apps/issue-radar/lib/review')

const REPO: RepoRef = { owner: 'acme', repo: 'widget', provider: 'github', host: 'github.com' }

const ISSUE = {
  number: 7,
  title: 'Crash on save',
  labels: ['bug'],
  state: 'open',
  author: 'someone',
  url: 'https://example.invalid/acme/widget/issues/7',
} as unknown as Issue

const PULL = {
  number: 8,
  title: 'Fix the crash',
  labels: [],
  state: 'open',
  draft: false,
  merged_at: null,
  base: 'main',
  head: 'fix/crash',
  author: 'someone',
  url: 'https://example.invalid/acme/widget/pull/8',
} as unknown as PullRequest

function storePref(code: string) {
  localStorage.setItem(UI_STATE_KEY, JSON.stringify({ aiLanguage: code }))
}

beforeEach(() => {
  locale = 'en'
  localStorage.clear()
  openSession.mockReset()
  openSession.mockResolvedValue(null as unknown as InvestigationRecord)
})

describe('coerceAiLanguage', () => {
  it('keeps a language this build ships', () => {
    expect(coerceAiLanguage('zh-CN')).toBe('zh-CN')
    expect(coerceAiLanguage('en')).toBe('en')
  })

  it('falls back to follow for anything unrecognized', () => {
    // A hand-edited value, or a language dropped from the build. Follow is the
    // one choice that cannot be wrong, so it is the only safe fallback.
    expect(coerceAiLanguage('kl')).toBe(AI_LANGUAGE_FOLLOW)
    expect(coerceAiLanguage(42)).toBe(AI_LANGUAGE_FOLLOW)
    expect(coerceAiLanguage(undefined)).toBe(AI_LANGUAGE_FOLLOW)
  })

  it('refuses the pseudolocale, which is a rendering device and not a language', () => {
    expect(AI_LANGUAGE_CHOICES.some(l => l.code === 'en-XA')).toBe(false)
    expect(coerceAiLanguage('en-XA')).toBe(AI_LANGUAGE_FOLLOW)
  })
})

describe('resolveAiLanguage', () => {
  it('follows the dashboard language when unset', () => {
    locale = 'ja'
    expect(resolveAiLanguage(AI_LANGUAGE_FOLLOW)).toBe('ja')
  })

  it('overrides the dashboard language, in either direction', () => {
    // The combination the setting exists for: an English interface whose agent
    // writes Chinese, and its mirror image.
    locale = 'en'
    expect(resolveAiLanguage('zh-CN')).toBe('zh-CN')
    locale = 'zh-CN'
    expect(resolveAiLanguage('en')).toBe('')
  })

  it('resolves English to no directive at all', () => {
    locale = 'en'
    expect(resolveAiLanguage(AI_LANGUAGE_FOLLOW)).toBe('')
  })

  it('resolves a pseudolocale dashboard to no directive', () => {
    locale = 'en-XA'
    expect(resolveAiLanguage(AI_LANGUAGE_FOLLOW)).toBe('')
  })
})

describe('the investigation prompt directive', () => {
  it('is absent for English, leaving the prompt byte-identical', () => {
    expect(buildInvestigationPrompt(REPO, REPO.owner, REPO.repo, ISSUE, ''))
      .toBe(buildInvestigationPrompt(REPO, REPO.owner, REPO.repo, ISSUE))
    expect(buildInvestigationPrompt(REPO, REPO.owner, REPO.repo, ISSUE)).not.toContain('BCP-47')
  })

  it('names the tag and keeps label values out of the translation', () => {
    const p = buildInvestigationPrompt(REPO, REPO.owner, REPO.repo, ISSUE, 'zh-CN')
    expect(p).toContain('BCP-47 tag zh-CN')
    expect(p).toContain('"suggested_labels" values')
  })

  it('comes after the instructions, so it cannot displace the findings write', () => {
    const p = buildInvestigationPrompt(REPO, REPO.owner, REPO.repo, ISSUE, 'ko')
    expect(p.indexOf('BCP-47')).toBeGreaterThan(p.indexOf('issue_radar_record_investigation'))
  })
})

describe('the hooks read the live selection, not the stored one', () => {
  it('seeds an investigation in the chosen language', async () => {
    livePref = 'zh-CN'
    const { result } = renderHook(() => useInvestigate())
    await result.current.investigate(REPO, ISSUE, null)
    expect(openSession.mock.calls[0][0].prompt).toContain('BCP-47 tag zh-CN')
  })

  it('seeds a review in the chosen language, with the verdict word kept verbatim', () => {
    livePref = 'ja'
    const { result } = renderHook(() => useReviewPr())
    void result.current.reviewPr(REPO, PULL, null)
    const prompt = openSession.mock.calls[0][0].prompt
    expect(prompt).toContain('BCP-47 tag ja')
    // Not a bare `request-changes` check: the base instructions already offer that
    // verdict, so only the directive's own clause proves the enum is protected.
    expect(prompt).toContain('Keep verbatim: the verdict word itself')
  })

  it('honours the selection even when persisting it failed', () => {
    // A browser that refuses the write (private mode, quota) leaves the stored
    // value behind. Without this, the prompt ships the language the user just
    // stopped using while the picker shows the one they chose.
    livePref = 'zh-CN'
    storePref('de')
    const { result } = renderHook(() => useReviewPr())
    void result.current.reviewPr(REPO, PULL, null)
    const prompt = openSession.mock.calls[0][0].prompt
    expect(prompt).toContain('BCP-47 tag zh-CN')
    expect(prompt).not.toContain('BCP-47 tag de')
  })

  it('follows the dashboard language when the preference is unset', async () => {
    // The default the settings row advertises. The hook has to RESOLVE the follow
    // case: passing the raw preference through emits no directive at all, which
    // silently ships English findings to a non-English dashboard.
    livePref = ''
    locale = 'zh-CN'
    const { result } = renderHook(() => useInvestigate())
    await result.current.investigate(REPO, ISSUE, null)
    expect(openSession.mock.calls[0][0].prompt).toContain('BCP-47 tag zh-CN')
  })

  it('adds nothing when the preference is unset and the dashboard is English', () => {
    livePref = ''
    const { result } = renderHook(() => useReviewPr())
    void result.current.reviewPr(REPO, PULL, null)
    expect(openSession.mock.calls[0][0].prompt).not.toContain('BCP-47')
  })
})

describe('the settings row survives a narrow viewport', () => {
  it('stacks the label above the picker until sm', async () => {
    // A single horizontal row at 320px puts a long endonym trigger beside the
    // hint and squeezes the hint into a sliver. The interval and toggle rows
    // above do not need this because their controls are a short duration or a
    // fixed-width switch.
    const src = await readFile(
      join(__dirname, '..', 'apps/issue-radar/views/settings/GeneralSettings.tsx'),
      'utf8',
    )
    const row = src.slice(src.indexOf('generalSettings.agent_section'))
    const wrapper = row.slice(0, row.indexOf('agent_language_hint'))
    expect(wrapper).toMatch(/flex flex-col[^"]*sm:flex-row/)
    expect(wrapper).not.toMatch(/"flex items-start justify-between gap-4"/)
  })

  it('names the language the follow option currently resolves to', async () => {
    // "Same as dashboard" alone does not say WHICH language that is, so the
    // default's effect is invisible without leaving the picker.
    const src = await readFile(
      join(__dirname, '..', 'apps/issue-radar/views/settings/GeneralSettings.tsx'),
      'utf8',
    )
    expect(src).toMatch(/agent_language_follow',\s*\{\s*\n?\s*language: languageLabel\(activeLocale\(\)\)/)
  })
})

describe('a second tab cannot erase the choice', () => {
  it('keeps the newest stored value when a stale whole-document save lands', () => {
    // The UI state is persisted as ONE document per tab. Tab A picks a language;
    // tab B then saves an unrelated change from React state it read at mount. If
    // that save carried tab B's copy of the field, the choice would be gone and
    // the agents would quietly go back to English.
    localStorage.setItem(UI_STATE_KEY, JSON.stringify({ query: 'crash', aiLanguage: '' }))
    patchUiState({ aiLanguage: 'ja' })
    expect(storedAiLanguage()).toBe('ja')

    // What a stale tab's whole-document save now writes for this field.
    const staleTabWrites = storedAiLanguage()
    localStorage.setItem(UI_STATE_KEY, JSON.stringify({ query: 'other', aiLanguage: staleTabWrites }))
    expect(storedAiLanguage()).toBe('ja')
  })

  it('patches only the language, leaving the rest of the document intact', () => {
    localStorage.setItem(UI_STATE_KEY, JSON.stringify({ query: 'crash', selectedIssue: 42 }))
    patchUiState({ aiLanguage: 'de' })
    const doc = JSON.parse(localStorage.getItem(UI_STATE_KEY) || '{}')
    expect(doc).toMatchObject({ query: 'crash', selectedIssue: 42, aiLanguage: 'de' })
  })

  it('writes the language from its own setter and never from the whole-state save', async () => {
    const src = await readFile(join(__dirname, '..', 'apps/issue-radar/context.tsx'), 'utf8')
    expect(src).toMatch(/patchUiState\(\{ aiLanguage: next \}\)/)
    expect(src).toMatch(/aiLanguage: storedAiLanguage\(\)/)
    // The bare state variable in the saved document is the defect itself.
    expect(src).not.toMatch(/\n      aiLanguage,\n/)
  })
})
