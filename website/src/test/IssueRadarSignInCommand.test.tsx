import { describe, it, expect, vi, afterAll, beforeEach } from 'vitest'
import { render, cleanup } from '@testing-library/react'

import { REFRESH_DEFAULTS, AI_LANGUAGE_FOLLOW } from '../apps/issue-radar/lib/format'
// `/all` for every catalog: `../i18n` registers English only.
import { i18next } from '../i18n/all'
import { SUPPORTED_CODES } from '../i18n/languages'

// The signed-out hint tells the user which command signs the provider's CLI in.
// It is typed into a terminal as written, so the <code> span must hold exactly that
// command in every language: no translated fragment (ja once rendered
// `gh 認証ログイン`), and Azure DevOps' own `az login` rather than `az auth login`.
const ctx = { value: {} as Record<string, unknown> }
vi.mock('../apps/issue-radar/context', () => ({ useIssueRadar: () => ctx.value }))

const GeneralSettings = (await import('../apps/issue-radar/views/settings/GeneralSettings')).default

const PROVIDERS = [
  { ref: { owner: 'acme', repo: 'widget', provider: 'github' as const, host: 'github.com' }, command: 'gh auth login' },
  { ref: { owner: 'group/sub', repo: 'proj', provider: 'gitlab' as const, host: 'gitlab.com' }, command: 'glab auth login' },
  { ref: { owner: 'contoso/Payments', repo: 'ledger', provider: 'azure' as const, host: 'dev.azure.com' }, command: 'az login' },
]

function signedOut(active: (typeof PROVIDERS)[number]['ref']) {
  ctx.value = {
    me: null,
    repos: [],
    active,
    onAddRepo: () => {},
    openSettings: () => {},
    refreshPrefs: REFRESH_DEFAULTS,
    setRefreshPrefs: () => {},
    aiLanguage: AI_LANGUAGE_FOLLOW,
    setAiLanguage: () => {},
  }
}

beforeEach(() => {
  Element.prototype.scrollIntoView = () => {}
})

afterAll(async () => {
  await i18next.changeLanguage('en')
})

describe('Issue Radar sign-in hint', () => {
  it('shows each provider its own sign-in command, verbatim, in every language', async () => {
    for (const lng of SUPPORTED_CODES) {
      await i18next.changeLanguage(lng)
      for (const { ref, command } of PROVIDERS) {
        signedOut(ref)
        const { container } = render(<GeneralSettings anchor="account" />)
        const codes = [...container.querySelectorAll('code')].map(c => c.textContent)
        expect(codes, `${ref.provider} in ${lng}`).toContain(command)
        cleanup()
      }
    }
  })
})
