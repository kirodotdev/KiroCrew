import { useMutation } from '@tanstack/react-query'
import { AlertTriangle, Check } from 'lucide-react'
import { useId } from 'react'
import { apiErrorCode } from '../../api/apiError'
import ErrorNotice from '../../components/ErrorNotice'
import { SettingsSection, SettingsCard } from '../../components/settings'
import { CopyCommandButton } from '../../components/settingRef/CopyCommandButton'
import { Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import { RestartGatewayButton } from './AboutPanelUpdateFlow'
import { useDesktopGatewayRestart } from './DesktopGatewayRestartRow'

/** The optional-SDK fields a channel's config GET reports (see
 *  ``_channel_sdk_status`` in the gateway). */
export interface OptionalSdkStatus {
  sdk_installed?: boolean
  sdk_install_supported?: boolean
  sdk_install_command?: string
  /** The desktop app installs the SDK itself (Feishu only). */
  sdk_install_in_app?: boolean
}

/**
 * The card's words for a failed in-app install, chosen by the route's `code`
 * (`api_feishu_sdk_install`): pip's own lines stay in the gateway log. Any
 * other refusal (another install running, a remote session) keeps the
 * gateway's message.
 */
function installErrorText(error: unknown, channel: string): string {
  const button = i18nT('pages.settings.botChannelPanel.sdk_install')
  switch (apiErrorCode(error)) {
    case 'sdk_download_failed':
      return i18nT('pages.settings.botChannelPanel.sdk_download_failed', { channel, button })
    case 'sdk_install_failed':
      return i18nT('pages.settings.botChannelPanel.sdk_install_failed', { channel, button })
    default:
      return error instanceof Error ? error.message : String(error)
  }
}

/**
 * "Installed, restart due", with the desktop app's own Restart gateway beside
 * it (the same hook and two-step button as Settings → About). Where the app
 * cannot restart this gateway (an adopted gateway, a remote crew) the line
 * stands alone. Its own component, so the app's bridge is asked only once an
 * install has finished.
 *
 * The app's restart repaints the window, so while the channel form around the
 * card holds unsaved edits the button is held and a line asks for a save first.
 */
function InstalledRestartRow({ channel, hasUnsavedChanges }: { channel: string; hasUnsavedChanges: boolean }) {
  const restart = useDesktopGatewayRestart()
  const saveFirstId = useId()
  const holdForSave = restart.available && hasUnsavedChanges
  return (
    <>
      <div className="flex flex-col items-start gap-2 md:flex-row md:items-center md:gap-3">
        <p className="md:flex-1 text-[13px] text-ok m-0 flex items-start gap-1.5" role="status" data-testid="sdk-installed-restart">
          <Check size={14} className="flex-none mt-0.5" />
          {i18nT('pages.settings.botChannelPanel.sdk_installed_restart', { channel })}
        </p>
        {restart.available && (
          <RestartGatewayButton
            primary
            pending={restart.pending}
            restarting={restart.restarting}
            onConfirm={() => { void restart.restart() }}
            testId="sdk-restart-gateway"
            disabled={holdForSave}
            describedBy={holdForSave ? saveFirstId : undefined}
          />
        )}
      </div>
      {holdForSave && (
        <p id={saveFirstId} className="text-[12px] text-warn mt-2 mb-0 flex items-start gap-1.5" data-testid="sdk-restart-save-first">
          <AlertTriangle size={12} className="flex-none mt-0.5" />
          {i18nT('pages.settings.botChannelPanel.sdk_restart_save_first', { channel })}
        </p>
      )}
      {/* No hand-off: the surrounding channel form may hold unsaved credential and settings drafts. */}
      <ErrorNotice message={restart.error} className="mt-2" testId="sdk-restart-error" />
    </>
  )
}

/**
 * The "install the optional SDK" card shared by every channel whose client
 * library ships as an optional extra (Feishu, Teams, WhatsApp).
 *
 * On the desktop app a channel that passes `onInstall` gets an Install button:
 * the gateway lays the SDK down outside the signed app bundle. Elsewhere the
 * command comes from the gateway and names ITS interpreter, because installing
 * into a different environment is the failure this card exists to prevent; a
 * bare `pip install` is never rendered here. Where no install channel exists
 * (bundled interpreter, no pip, PEP 668) the gateway sends no command and the
 * card explains that instead.
 *
 * Strictly `=== false`: an older gateway omits the field entirely, and treating
 * undefined as "missing" would tell every user of one to install a package they
 * may already have.
 */
export function OptionalSdkCard({ channel, packageLabel, status, onInstall, onInstalled, hasUnsavedChanges = false }: {
  /** Display name of the channel, e.g. "Feishu". */
  channel: string
  /** Distribution name as the user would type it, e.g. "lark-oapi". */
  packageLabel: string
  status: OptionalSdkStatus
  /** The gateway's own installer, shown where `sdk_install_in_app` is true. */
  onInstall?: () => Promise<{ restart_required?: boolean } | void>
  /** Called after a successful install, to re-read the channel's status. */
  onInstalled?: () => void
  /** The form around the card holds edits not saved yet: the app's restart
   *  would drop them, so the card holds its restart until they are saved. */
  hasUnsavedChanges?: boolean
}) {
  const install = useMutation({
    mutationFn: () => (onInstall ? onInstall() : Promise.resolve()),
    onSuccess: () => onInstalled?.(),
  })
  // A finished install flips `sdk_installed` on the next read, but the running
  // gateway loaded without the SDK: keep the card to say a restart is still due.
  const restartDue = install.isSuccess && install.data?.restart_required !== false
  if (status.sdk_installed !== false && !restartDue) return null
  const inApp = status.sdk_install_in_app === true && onInstall !== undefined
  return (
    <SettingsSection title={i18nT('pages.settings.botChannelPanel.sdk_missing', { channel })}>
      <SettingsCard>
        {restartDue ? (
          <InstalledRestartRow channel={channel} hasUnsavedChanges={hasUnsavedChanges} />
        ) : inApp ? (
          <>
            {/* Narrow-first: below md the button takes its own row, so its long busy label never crushes the text. */}
            <div className="flex flex-col items-start gap-2 md:flex-row md:items-center md:gap-3">
              <p className="md:flex-1 text-[13px] text-text m-0">
                {/* The order (install, restart, turn on) is in this line, so the
                    terminal path's "then restart" line is not repeated under it. */}
                {i18nT('pages.settings.botChannelPanel.sdk_install_in_app_body', { channel })}
              </p>
              <Btn primary busy={install.isPending} onClick={() => install.mutate()} data-testid="sdk-install-button">
                {install.isPending
                  ? i18nT('pages.settings.botChannelPanel.sdk_installing')
                  : i18nT('pages.settings.botChannelPanel.sdk_install')}
              </Btn>
            </div>
            {/* No hand-off: the surrounding channel form may hold unsaved credential and settings drafts. */}
            {install.isError && (
              <ErrorNotice
                message={installErrorText(install.error, channel)}
                className="mt-2"
                testId="sdk-install-error"
              />
            )}
          </>
        ) : status.sdk_install_supported && status.sdk_install_command ? (
          <>
            <p className="text-[13px] text-text m-0">
              {i18nT('pages.settings.botChannelPanel.sdk_missing_body', { channel, package: packageLabel })}
            </p>
            {/* Copyable verbatim: break-all, never truncated. */}
            <div className="flex items-start gap-2 mt-2 rounded-md border border-border bg-bg-elevated px-3 py-2">
              <code className="flex-1 text-[12px] font-mono text-text break-all" data-testid="sdk-install-command">
                {status.sdk_install_command}
              </code>
              <CopyCommandButton text={status.sdk_install_command} />
            </div>
            <p className="text-[12px] text-muted mt-2 mb-0">
              {i18nT('pages.settings.botChannelPanel.sdk_restart_after_install', { channel })}
            </p>
          </>
        ) : (
          <p className="text-[13px] text-warn m-0 flex items-start gap-1.5">
            <AlertTriangle size={13} className="flex-none mt-0.5" />
            {i18nT('pages.settings.botChannelPanel.sdk_install_unsupported', { package: packageLabel })}
          </p>
        )}
      </SettingsCard>
    </SettingsSection>
  )
}
