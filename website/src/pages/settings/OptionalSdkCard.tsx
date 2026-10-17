import { AlertTriangle } from 'lucide-react'
import { SettingsSection, SettingsCard } from '../../components/settings'
import { CopyCommandButton } from '../../components/settingRef/CopyCommandButton'
import { i18nT } from '../../i18n/t'

/** The optional-SDK fields a channel's config GET reports (see
 *  ``_channel_sdk_status`` in the gateway). */
export interface OptionalSdkStatus {
  sdk_installed?: boolean
  sdk_install_supported?: boolean
  sdk_install_command?: string
}

/**
 * The "install the optional SDK" card shared by every channel whose client
 * library ships as an optional extra (Feishu, Teams, WhatsApp).
 *
 * The command comes from the gateway and names ITS interpreter, because
 * installing into a different environment is the failure this card exists to
 * prevent; a bare `pip install` is never rendered here. Where no install channel
 * exists (bundled interpreter, no pip, PEP 668) the gateway sends no command and
 * the card explains that instead.
 *
 * Strictly `=== false`: an older gateway omits the field entirely, and treating
 * undefined as "missing" would tell every user of one to install a package they
 * may already have.
 */
export function OptionalSdkCard({ channel, packageLabel, status }: {
  /** Display name of the channel, e.g. "Feishu". */
  channel: string
  /** Distribution name as the user would type it, e.g. "lark-oapi". */
  packageLabel: string
  status: OptionalSdkStatus
}) {
  if (status.sdk_installed !== false) return null
  return (
    <SettingsSection title={i18nT('pages.settings.botChannelPanel.sdk_missing', { channel })}>
      <SettingsCard>
        {status.sdk_install_supported && status.sdk_install_command ? (
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
