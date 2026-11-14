/**
 * Isolated capture entry for ErrorNotice `impact` / `nested` / `actions`
 * (docs/request-for-change/rfc-error-notice-impact.md).
 *
 * Renders the REAL ErrorNotice against the real stylesheet, theme tokens and
 * i18n catalog. Each scene copies its host's container classes and the call
 * site's English strings, with mocked data; no gateway is involved. Scene 1's
 * and scene 5's error text is made up (the real one comes from a server).
 * `buttons` shows the action-row button at rest, on hover and on keyboard focus
 * for each impact; the shoot script forces those states through DevTools.
 *
 * Query: ?theme=dark|light  ?scene=<id>
 */
import { createRoot } from 'react-dom/client'
import type { ReactNode } from 'react'

import ErrorNotice from '../src/components/ErrorNotice'
import { Btn } from '../src/components/ui'
import { setGatewayConnected } from '../src/utils/errorReport'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const scene = params.get('scene') || 'balance'
document.documentElement.dataset.mode = theme
document.documentElement.dataset.theme = theme === 'light' ? 'kiro-light' : 'kiro-dark'
initI18n()
setGatewayConnected(true)

const S = {
  signinWarn: 'Kiro CLI is signed in, but Kiro Agent could not confirm which account it runs as.',
  signinErr: 'Could not read the account name: kiro-cli whoami exited with code 1.',
  forecast: "Could not load recent usage, so the run-out date can't be shown.",
  balance: 'Could not read your balance.',
  effort: "Could not verify this session's reasoning effort options. The effort control is unavailable until the connection recovers.",
  notReady: "Kiro Crew is set to use Claude Code, which isn't ready on this host yet.",
  later: 'You can switch agents at any time in Settings > Agent Harness.',
  useKiro: 'Use Kiro CLI instead',
  switchErr: 'Could not save the agent choice: the settings file is read-only.',
  replacedTitle: 'Signed in, but the previous sign-in could not be removed',
  replacedBody: 'The new account is stored, but the one you were switching away from is still there and takes precedence. Sign out of it, then check the sign-in shown here.',
  pending: "Could not check this project's settings for outside changes.",
}

const warnHost = (children: ReactNode) => (
  <div className="rounded-lg border border-warn/40 bg-warn-subtle px-3 py-2.5 text-[13px] leading-relaxed text-text">{children}</div>
)

const SCENES: Record<string, { site: string; level: string; width: number; body: ReactNode }> = {
  'signin-name': {
    site: 'pages/developer/KiroSignInSources.tsx: account name unreadable, inside the sign-in warning',
    level: 'minor, nested',
    width: 560,
    body: warnHost(<><p>{S.signinWarn}</p><div className="mt-2.5"><ErrorNotice impact="minor" nested message={S.signinErr} askAgent /></div></>),
  },
  forecast: {
    site: 'components/KiroAccountModal.tsx: run-out forecast failed',
    level: 'minor',
    width: 560,
    body: <div className="rounded-xl border border-border bg-card p-4"><div className="text-[13px] font-medium mb-2">Credits</div><ErrorNotice impact="minor" message={S.forecast} askAgent /></div>,
  },
  balance: {
    site: 'components/KiroAccountModal.tsx: credit balance read failed, Refresh in the action row',
    level: 'partial',
    width: 560,
    body: <div className="rounded-xl border border-border bg-card p-4"><ErrorNotice impact="partial" message={S.balance} askAgent actions={<Btn type="button">Refresh</Btn>} /></div>,
  },
  buttons: {
    site: 'Action-row button on each impact: rest | hover | focus',
    level: 'blocking, partial, minor',
    width: 560,
    body: (
      <div className="rounded-xl border border-border bg-card p-4 flex flex-col gap-3">
        {(['blocking', 'partial', 'minor'] as const).map((impact) => (
          <ErrorNotice
            key={impact}
            impact={impact}
            message={`${impact}: ${S.balance}`}
            askAgent
            actions={<>
              <Btn type="button" data-state="rest">Refresh</Btn>
              <Btn type="button" data-state="hover">Refresh</Btn>
              <Btn type="button" data-state="focus">Refresh</Btn>
            </>}
          />
        ))}
      </div>
    ),
  },
  'effort-inline': {
    site: 'pages/chat/crew-window/CrewChatWindow.tsx: effort options unavailable (inline)',
    level: 'partial, inline',
    width: 560,
    body: <div className="rounded-xl border border-border bg-card p-3"><ErrorNotice variant="inline" impact="partial" message={S.effort} askAgent /></div>,
  },
  'switch-nested': {
    site: "components/KiroPrerequisiteGate.tsx: 'Use Kiro CLI instead' did not save, inside the not-ready warning",
    level: 'blocking, nested',
    width: 560,
    body: warnHost(<>
      <p>{S.notReady}</p>
      <p className="mt-1">{S.later}</p>
      <Btn type="button" className="mt-2 min-h-9 rounded-lg border-border-strong bg-card px-3 text-[13px] shadow-sm">{S.useKiro}</Btn>
      <ErrorNotice className="mt-2.5" nested message={S.switchErr} noHandoffReason="capture" />
    </>),
  },
  'replaced-blocking': {
    site: 'components/KasLoginGate.tsx: previous sign-in could not be removed',
    level: 'blocking',
    width: 560,
    body: <div className="rounded-xl border border-border bg-card p-4"><ErrorNotice title={S.replacedTitle} message={S.replacedBody} askAgent onDismiss={() => {}} /></div>,
  },
  'pending-read': {
    site: 'pages/settings/SecurityPanelConfigPending.tsx: outside-change check failed, Retry in the action row',
    level: 'partial',
    width: 560,
    body: <div className="rounded-xl border border-border bg-card p-4"><ErrorNotice impact="partial" message={S.pending} askAgent actions={<Btn type="button">Retry</Btn>} /></div>,
  },
}

function Page() {
  const s = SCENES[scene]
  return (
    <div className="bg-bg text-text p-4 inline-block" data-capture-root>
      <div className="text-[11px] text-muted mb-1 italic">Component render, not the live page. Mocked data.</div>
      <div className="text-[12px] text-muted mb-1 font-mono">{s.site}</div>
      <div className="text-[12px] text-text mb-3">impact: {s.level}</div>
      <div style={{ width: s.width }} data-scene={scene}>{s.body}</div>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<Page />)
