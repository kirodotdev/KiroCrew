/**
 * Isolated capture entry for the Schedule page's vault-secrets panel.
 *
 * WHY ISOLATED: the panel lives inside the job detail Dialog, which needs the
 * whole Schedule roster, a live gateway, a vault entry, a script cron and an
 * agent-side pending request to reach. The panel itself only depends on
 * `GET /api/crons/{id}/script` (the source the approval blesses), so stubbing
 * that one response renders the real component — real classes, real Tailwind
 * output, real theme tokens — without standing up a gateway.
 *
 * Three scenes cover the states a reviewer needs to see: a pending agent
 * request with its reviewed source (Approve enabled only once the script has
 * rendered), an active grant with the two-step revoke armed, and the empty
 * state that tells a first-time user where a grant comes from.
 *
 * Theme via query string: ?theme=dark|light
 */
import { useEffect, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { initI18n } from '../src/i18n'
import { JobSecretsPanel } from '../src/pages/SchedulePage'
import type { CronJob } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SCRIPT = [
  'import os',
  'import urllib.request',
  '',
  'def run(ctx):',
  '    token = os.environ["SLACK_TOKEN"]',
  '    req = urllib.request.Request(',
  '        "https://slack.com/api/auth.test",',
  '        headers={"Authorization": f"Bearer {token}"},',
  '    )',
  '    with urllib.request.urlopen(req, timeout=10) as resp:',
  '        ctx.notify(f"slack auth: {resp.status}")',
  '',
].join('\n')

const BASE: CronJob = {
  id: 'a1b2c3d4',
  name: 'slack-daily-digest',
  message: '',
  enabled: true,
  every_secs: 86400,
  schedule: 'every 24h',
  script: '~/.kiro/crew/crons/slack_digest.py:run',
  agent: '',
  last_status: 'ok',
  last_run_ts: null,
  next_run_ts: null,
  session_key: 'dashboard:ops-slot',
} as unknown as CronJob

// Only the script endpoint is stubbed; anything else falls through so a missed
// dependency shows up as a real network error rather than silently rendering
// empty. The grant PUT is never reached: the shot is taken before any click
// that would submit.
const realFetch = window.fetch.bind(window)
window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/crons/') && url.endsWith('/script')) {
    return Promise.resolve(new Response(JSON.stringify({
      source: SCRIPT,
      file: 'slack_digest.py',
      function: 'run',
      truncated: false,
      reviewable: true,
      sha256: '3f8a1c9e5b7d2a4f6c8e0b1d3f5a7c9e1b3d5f7a9c1e3b5d7f9a1c3e5b7d9f1a',
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  return realFetch(input as RequestInfo, init)
}) as typeof window.fetch

function Frame({ label, testid, children }: { label: string; testid: string; children: ReactNode }) {
  return (
    <div data-testid={testid} className="rounded-xl border border-border-strong bg-card p-4" style={{ width: 640 }}>
      <div className="mb-3 font-mono text-[11px] text-muted-strong">{label}</div>
      {children}
    </div>
  )
}

/** Opens the collapsed "Vault secrets" disclosure and arms Revoke, so the
 * shot shows the confirm label a reviewer would otherwise have to click for. */
function OpenAndArm({ arm }: { arm: boolean }) {
  useEffect(() => {
    const t = setTimeout(() => {
      const frame = document.querySelector('[data-testid="scene-active"], [data-testid="scene-empty"]')
      const toggle = Array.from(frame?.querySelectorAll('[aria-expanded]') ?? [])[0] as HTMLElement | undefined
      toggle?.click()
      if (arm) {
        setTimeout(() => {
          const btn = Array.from(document.querySelectorAll('[data-testid="scene-active"] button')).find(
            b => b.textContent?.includes('Revoke all secrets'),
          ) as HTMLElement | undefined
          btn?.click()
        }, 50)
      }
    }, 50)
    return () => clearTimeout(t)
  }, [arm])
  return null
}

function Scenes() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const scene = params.get('scene') || 'pending'
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <div className="flex flex-col items-start gap-5 bg-bg p-6 text-text">
          {scene === 'pending' && (
            <Frame label="agent requested a secret — approve is enabled once the script has rendered" testid="scene-pending">
              <JobSecretsPanel
                job={{ ...BASE, secret_env: {}, secret_env_pending: { SLACK_TOKEN: 'slack-sandbox' }, secret_env_pending_ts: 1_757_000_000 }}
                onSaved={() => {}}
              />
            </Frame>
          )}
          {scene === 'active' && (
            <Frame label="active grant — revoke is arm-then-confirm" testid="scene-active">
              <JobSecretsPanel
                job={{ ...BASE, secret_env: { SLACK_TOKEN: 'slack-sandbox', JIRA_TOKEN: 'jira-readonly' } }}
                onSaved={() => {}}
              />
              <OpenAndArm arm />
            </Frame>
          )}
          {scene === 'empty' && (
            <Frame label="no grant yet — the empty state names the mint path" testid="scene-empty">
              <JobSecretsPanel job={{ ...BASE, secret_env: {} }} onSaved={() => {}} />
              <OpenAndArm arm={false} />
            </Frame>
          )}
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(<Scenes />)
