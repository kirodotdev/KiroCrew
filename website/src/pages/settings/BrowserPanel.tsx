/**
 * Settings -> Browser (RFC browser-live-harness, section 8).
 *
 * One card and two controls: the main card (the browser, a status dot, and ONE
 * button that follows the state -- see `primaryState`), "Which browser", and
 * "Show the live view in chat". Everything else is under More, collapsed by
 * default: Quality, Max frame rate, the Preview tab's local addresses, Engine,
 * Run with no window. A grey line at the foot states the privacy fence.
 *
 * Explanations are info tips beside each label (hover, focus or tap), never a
 * line of prose under a control.
 *
 * There is no first-run step list: the gateway installs the engine on its own and
 * the main card shows it installing.
 */
import { Lock, ShieldOff } from 'lucide-react'

import ErrorNotice from '../../components/ErrorNotice'
import { SettingsCard, SettingsSection } from '../../components/settings'
import { FormSkeleton } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import { BrowserMainControls, BrowserMoreSection } from './BrowserLiveSection'
import { BrowserStatusCard } from './BrowserStatusCard'
import { failText, primaryState } from './browserStatus'
import { useBrowserSettings } from './useBrowserSettings'

export function BrowserPanel() {
  const s = useBrowserSettings()
  const { status, config, view, setConfig, patch, install, test, disconnect } = s

  if (s.policyOff) {
    // A policy turned browsing off: no install, connect or setting row to offer.
    return (
      <SettingsCard>
        <p className="flex items-center gap-1.5 m-0 text-[13px] text-muted" data-testid="browser-policy-off">
          <ShieldOff size={14} className="shrink-0" aria-hidden />
          {i18nT('pages.settings.browserPanel.policy_off')}
        </p>
      </SettingsCard>
    )
  }

  if (s.policyPending || status.isLoading || config.isLoading) {
    return (
      <SettingsSection title={i18nT('pages.settings.browserPanel.title')}>
        <SettingsCard>
          <FormSkeleton rows={['info', 'field']} />
        </SettingsCard>
      </SettingsSection>
    )
  }
  if (!status.data) {
    return (
      <SettingsSection title={i18nT('pages.settings.browserPanel.title')}>
        {/* Nothing to lose: no draft has mounted. */}
        <ErrorNotice
          message={failText(status.error, i18nT('pages.settings.browserPanel.cannot_load'))}
          askAgent
        />
      </SettingsSection>
    )
  }

  const state = primaryState(status.data, install.isPending)
  const installError = install.isError
    ? failText(install.error, i18nT('pages.settings.browserPanel.install_request_failed'))
    : null
  const testError = test.isError
    ? failText(test.error, i18nT('pages.settings.browserPanel.test_request_failed'))
    : null
  // An HTTP 200 whose body says the attach did not come up is a failure too; the
  // gateway's detail names why. In the error state the card's own ErrorNotice
  // carries it, so it is not repeated here.
  const attachDetail = test.data && !test.data.ok
    ? (test.data.detail || i18nT('pages.settings.browserPanel.test_failed'))
    : null
  const testFailure = testError ?? (state !== 'error' ? attachDetail : null)
  const disconnectError = disconnect.isError
    ? failText(disconnect.error, i18nT('pages.settings.browserPanel.disconnect_request_failed'))
    : null
  const saveError = patch.isError
    ? failText(patch.error, i18nT('pages.settings.browserPanel.save_failed'))
    : null
  // The last apply into Kiro's Chromium failed (hand-off on: a status read, next
  // to no draft).
  const cred = status.data.credentials
  const credentialError = cred?.provider && cred.state === 'error'
    ? (cred.reason
      ? i18nT('pages.settings.browserPanel.credentials_error_reason', { reason: cred.reason })
      : i18nT('pages.settings.browserPanel.credentials_error'))
    : null
  const configUnavailable = config.isError
  const disabled = configUnavailable || patch.isPending
  const installAll = () => install.mutate({})

  // No title or subtitle of its own: the Settings shell already renders the page
  // title and description (`pages.settings.sections.browser.*`) above it.
  return (
    <>
      <div className="flex flex-col gap-2" data-testid="browser-primary-card">
        <BrowserStatusCard
          status={status.data}
          state={state}
          connecting={test.isPending}
          attachDetail={attachDetail}
          disconnecting={disconnect.isPending}
          onInstall={installAll}
          onConnect={() => test.mutate()}
          onDisconnect={() => disconnect.mutate()}
        />
        {/* Hand-off on: nothing on the card is an unsaved draft. */}
        {testFailure && <ErrorNotice variant="inline" message={testFailure} askAgent testId="browser-test-result" />}
        {disconnectError && <ErrorNotice variant="inline" message={disconnectError} askAgent />}
        {installError && !status.data.installed && <ErrorNotice variant="inline" message={installError} askAgent />}
        {/* The status read failed after a first answer: what is shown may be stale. */}
        {status.isRefetchError && (
          <ErrorNotice
            variant="inline"
            message={failText(status.error, i18nT('pages.settings.browserPanel.status_stale'))}
            askAgent
          />
        )}
      </div>

      {configUnavailable && (
        // Hand-off on: no draft on this page yet (the origins field is under More).
        <ErrorNotice
          variant="inline"
          className="mt-3"
          message={failText(config.error, i18nT('pages.settings.browserPanel.config_unavailable'))}
          askAgent
        />
      )}

      <div className="mt-3">
        <BrowserMainControls view={view} credentials={cred} disabled={disabled} setConfig={setConfig} />
        {credentialError && (
          <ErrorNotice variant="inline" className="mt-2" message={credentialError} askAgent testId="browser-credentials-error" />
        )}
        {/* Outside More, so a rejected save of ANY row shows while More is closed.
            No hand-off: the origins add field under More may hold an unsaved draft. */}
        {saveError && <ErrorNotice variant="inline" className="mt-2" message={saveError} testId="browser-save-error" />}
      </div>

      <BrowserMoreSection
        view={view}
        status={status.data}
        disabled={disabled}
        setConfig={setConfig}
        installing={install.isPending}
        installError={status.data.installed ? installError : null}
        onInstall={installAll}
        onRedownload={() => install.mutate({ components: ['chromium'], refresh: true })}
      />

      <p className="flex items-center gap-1.5 mt-4 mb-0 text-[12px] text-muted" data-testid="browser-privacy-footer">
        <Lock size={12} className="shrink-0" aria-hidden />
        {i18nT('pages.settings.browserPanel.privacy_footer')}
      </p>
    </>
  )
}
