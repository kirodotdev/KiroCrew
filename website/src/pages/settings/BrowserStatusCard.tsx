/**
 * Settings -> Browser, the main card: the browser, a status dot and line, and
 * ONE button that follows the state (`primaryState`).
 *
 * Colour carries the state the user has to act on: yellow only while Chrome
 * waits for Allow (the next move is theirs, in another window), red only on a
 * failure. Prose that explains rather than reports sits in an info tip beside
 * the title, never as a second line.
 */
import { AlertTriangle, Globe, Loader2, Monitor, Plug, RotateCw, Unplug } from 'lucide-react'

import type { BrowserKind, BrowserStatusData } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import InfoTip from '../../components/InfoTip'
import { Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import {
  BROWSER_KIND_LABEL_KEY,
  CONNECTION_LABEL_KEY,
  isUserBrowser,
  type PrimaryState,
} from './browserStatus'

function KindIcon({ kind, size = 20 }: { kind: BrowserKind | null; size?: number }) {
  return kind === 'kiro_chromium'
    ? <Monitor size={size} aria-hidden />
    : <Globe size={size} aria-hidden />
}

const DOT_CLASS: Record<PrimaryState, string> = {
  installing: 'bg-accent',
  install_failed: 'bg-danger',
  not_installed: 'bg-muted',
  waiting_allow: 'bg-warn',
  error: 'bg-danger',
  disconnected: 'bg-muted',
  connected: 'bg-ok',
}

export interface BrowserStatusCardProps {
  status: BrowserStatusData
  state: PrimaryState
  /** A connect / Allow / Retry round trip is in flight. */
  connecting: boolean
  /** Why the last attach this page asked for did not come up (a 200 `ok: false`),
   *  shown in the `error` state when the status read carries no detail. */
  attachDetail?: string | null
  disconnecting: boolean
  onInstall: () => void
  onConnect: () => void
  onDisconnect: () => void
}

export function BrowserStatusCard({
  status, state, connecting, attachDetail = null, disconnecting, onInstall, onConnect, onDisconnect,
}: BrowserStatusCardProps) {
  const kind = status.browser?.kind ?? null
  const red = state === 'error' || state === 'install_failed'
  const yellow = state === 'waiting_allow'
  const kindTitle = kind ? i18nT(BROWSER_KIND_LABEL_KEY[kind]) : i18nT('pages.settings.browserPanel.no_browser_yet')

  const title = state === 'installing'
    ? i18nT('pages.settings.browserPanel.preparing_title')
    : state === 'install_failed'
      ? i18nT('pages.settings.browserPanel.setup_failed_title')
      : state === 'not_installed'
        ? i18nT('pages.settings.browserPanel.not_installed_title')
        : kindTitle

  // The status line. Server-authored detail (a stage name, a failure) is shown
  // verbatim and never translated; the catalog line only when there is none.
  const job = status.install_job
  const kindShort = !kind
    ? null
    : isUserBrowser(kind)
      ? i18nT('pages.settings.browserPanel.kind_short_user')
      : status.browser?.headless
        ? i18nT('pages.settings.browserPanel.kind_short_headless')
        : i18nT('pages.settings.browserPanel.kind_short_kiro')
  // No status line on a failed install: the title already says it, and the
  // failure itself is the ErrorNotice below.
  const line = state === 'installing'
    ? i18nT('pages.settings.browserPanel.preparing_desc')
    : state === 'install_failed'
      ? null
      : state === 'not_installed'
        ? i18nT('pages.settings.browserPanel.not_installed_desc')
        : state === 'waiting_allow'
          ? i18nT('pages.settings.browserPanel.waiting_allow_desc')
          : state === 'error'
            ? i18nT('pages.settings.browserPanel.connection_error')
            : state === 'connected' && kindShort
              ? `${i18nT(CONNECTION_LABEL_KEY.connected)} · ${kindShort}`
              : i18nT(CONNECTION_LABEL_KEY[state === 'connected' ? 'connected' : 'disconnected'])
  // A red state's failure, verbatim from the gateway, as an ErrorNotice with the
  // Ask-agent hand-off (nothing on this card is an unsaved draft).
  const failure = state === 'install_failed'
    ? (job?.error_detail || i18nT('pages.settings.browserPanel.install_failed'))
    : state === 'error'
      ? (status.detail || attachDetail || null)
      : null
  // A hint the gateway gives while disconnected (what to click, what to turn on).
  const serverHint = state === 'disconnected' || state === 'waiting_allow' ? status.detail : null

  // What the browser IS, for the tip beside the title.
  const tip = state === 'waiting_allow'
    ? i18nT('pages.settings.browserPanel.waiting_allow_hint')
    : state === 'installing' || state === 'install_failed' || state === 'not_installed'
      ? i18nT('pages.settings.browserPanel.engine_hint')
      : kind === null
        ? i18nT('pages.settings.browserPanel.no_browser_desc')
        : isUserBrowser(kind)
          ? i18nT('pages.settings.browserPanel.user_browser_desc')
          : status.browser?.headless
            ? i18nT('pages.settings.browserPanel.kiro_chromium_headless_desc')
            : i18nT('pages.settings.browserPanel.kiro_chromium_desc')

  const frame = red
    ? 'border-danger bg-danger-subtle'
    : yellow
      ? 'border-warn bg-warn-subtle'
      : 'border-border bg-bg-elevated'
  const iconFrame = red
    ? 'bg-danger text-danger-fg'
    : yellow
      ? 'bg-warn text-warn-fg'
      : 'bg-accent-subtle text-accent'

  return (
    <div
      className={`rounded-xl border p-3 sm:p-4 flex flex-col gap-2 ${frame}`}
      data-testid="browser-status-card"
      data-state={state}
      data-connection={status.connection}
    >
      <div className="flex items-center gap-3">
        <span className={`flex items-center justify-center w-10 h-10 rounded-xl shrink-0 ${iconFrame}`} aria-hidden>
          {state === 'installing'
            ? <Loader2 size={20} className="animate-spin" />
            : red ? <AlertTriangle size={20} /> : <KindIcon kind={kind} />}
        </span>
        <div className="flex-1 min-w-0 flex flex-col gap-0.5">
          <div className="flex items-center gap-1.5 min-w-0">
            <span className="text-[14px] font-semibold text-text truncate">{title}</span>
            <InfoTip text={tip} />
          </div>
          {line !== null && (
            <div className="flex items-start gap-1.5 text-[12px] text-muted min-w-0" data-testid="browser-status-line" aria-live="polite">
              <span className={`mt-[5px] w-1.5 h-1.5 rounded-full shrink-0 ${DOT_CLASS[state]}`} aria-hidden />
              <span className={`min-w-0 break-words ${red ? 'text-danger-text' : yellow ? 'text-warn-text' : ''}`}>{line}</span>
            </div>
          )}
          {serverHint && (
            <div className="text-[12px] text-text" data-testid="browser-status-detail">{serverHint}</div>
          )}
        </div>
        <div className="shrink-0">
          <PrimaryButton
            state={state}
            connecting={connecting}
            disconnecting={disconnecting}
            onInstall={onInstall}
            onConnect={onConnect}
            onDisconnect={onDisconnect}
          />
        </div>
      </div>
      {failure && (
        <ErrorNotice variant="inline" message={failure} askAgent testId="browser-status-error" />
      )}
      {state === 'installing' && (
        <div
          className="h-1 rounded-full bg-border overflow-hidden"
          role="progressbar"
          aria-label={i18nT('pages.settings.browserPanel.preparing_title')}
          aria-valuetext={job?.stage ?? undefined}
          data-testid="browser-install-progress"
        >
          <div className="h-full w-1/3 rounded-full bg-accent motion-safe:animate-pulse" />
        </div>
      )}
    </div>
  )
}

function PrimaryButton({ state, connecting, disconnecting, onInstall, onConnect, onDisconnect }: {
  state: PrimaryState
  connecting: boolean
  disconnecting: boolean
  onInstall: () => void
  onConnect: () => void
  onDisconnect: () => void
}) {
  const spin = <Loader2 size={13} className="lucide-inline animate-spin" aria-hidden />
  switch (state) {
    case 'installing':
      return (
        <Btn disabled aria-busy data-testid="browser-primary-action">
          {spin}{i18nT('pages.settings.browserPanel.installing')}
        </Btn>
      )
    case 'install_failed':
      return (
        <Btn primary onClick={onInstall} data-testid="browser-primary-action">
          <RotateCw size={13} className="lucide-inline" aria-hidden />{i18nT('pages.settings.browserPanel.retry')}
        </Btn>
      )
    case 'not_installed':
      return (
        <Btn primary onClick={onInstall} data-testid="browser-primary-action">
          {i18nT('pages.settings.browserPanel.install_engine')}
        </Btn>
      )
    case 'waiting_allow':
      // Asking the gateway to attach again is what brings Chrome's prompt back.
      return (
        <Btn primary onClick={onConnect} disabled={connecting} aria-busy={connecting || undefined} data-testid="browser-primary-action">
          {connecting && spin}{i18nT('pages.settings.browserPanel.go_allow')}
        </Btn>
      )
    case 'error':
      return (
        <Btn primary onClick={onConnect} disabled={connecting} aria-busy={connecting || undefined} data-testid="browser-primary-action">
          {connecting ? spin : <RotateCw size={13} className="lucide-inline" aria-hidden />}
          {i18nT('pages.settings.browserPanel.retry')}
        </Btn>
      )
    case 'disconnected':
      return (
        <Btn onClick={onConnect} disabled={connecting} aria-busy={connecting || undefined} data-testid="browser-primary-action">
          {connecting ? spin : <Plug size={13} className="lucide-inline" aria-hidden />}
          {i18nT('pages.settings.browserPanel.connect')}
        </Btn>
      )
    case 'connected':
      return (
        <Btn onClick={onDisconnect} disabled={disconnecting} aria-busy={disconnecting || undefined} data-testid="browser-primary-action">
          {disconnecting ? spin : <Unplug size={13} className="lucide-inline" aria-hidden />}
          {i18nT('pages.settings.browserPanel.disconnect')}
        </Btn>
      )
  }
}
