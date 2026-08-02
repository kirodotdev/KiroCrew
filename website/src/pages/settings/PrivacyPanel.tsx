import { EyeOff, HardDrive, Radio, SlidersHorizontal } from 'lucide-react'
import { Card, CardTitle } from '../../components/ui'
import { i18nT } from '../../i18n/t'

const COMMANDS = [
  'kirocrew telemetry status',
  'kirocrew telemetry disable',
] as const

/**
 * Catalog key per shell, as a literal map indexed at the `i18nT()` call site.
 *
 * The rows below used to carry the key themselves and pass the destructured
 * `labelKey` into `i18nT()`. `scripts/check-i18n-keys.mjs` resolves only
 * file-scope bindings, so the call site was unresolvable and the gate failed on
 * its dynamic-site ratchet. The three keys were still checked even then, via
 * that gate's `labelKey` data-field rule — what was exempt was the call site,
 * not the keys. Indexing an `as const` map with a non-literal index resolves to
 * the union of all three values, so each is verified at the point of use. Same
 * shape as `STATUS_LABEL_KEY` in `pages/chat/McpToolsPanel.tsx`.
 */
const SHELL_LABEL_KEY = {
  macosLinux: 'privacyDisclosure.shellMacOSLinuxLabel',
  powerShell: 'privacyDisclosure.shellPowerShellLabel',
  windowsCmd: 'privacyDisclosure.shellWindowsCmdLabel',
} as const

const SHELL_COMMANDS = [
  {
    kind: 'macosLinux',
    command: 'export KIROCREW_TELEMETRY_DISABLED=1',
  },
  {
    kind: 'powerShell',
    command: "$env:KIROCREW_TELEMETRY_DISABLED = '1'",
  },
  {
    kind: 'windowsCmd',
    command: 'set KIROCREW_TELEMETRY_DISABLED=1',
  },
] as const satisfies readonly { kind: keyof typeof SHELL_LABEL_KEY, command: string }[]

/** Durable disclosure surface. This page explains controls but does not ask for
 * consent or gate use of the application. */
export function PrivacyPanel() {
  return (
    <div aria-label={i18nT('privacyDisclosure.settingsLabel')}>
      <Card>
        <CardTitle>
          <Radio className="lucide-inline" aria-hidden="true" />
          {i18nT('privacyDisclosure.anonymousHeartbeatTitle')}
        </CardTitle>
        <p className="text-sm text-muted leading-relaxed">
          {i18nT('privacyDisclosure.anonymousHeartbeatBody')}
        </p>
      </Card>

      <Card>
        <CardTitle>
          <EyeOff className="lucide-inline" aria-hidden="true" />
          {i18nT('privacyDisclosure.dataNeverSentTitle')}
        </CardTitle>
        <p className="text-sm text-muted leading-relaxed">
          {i18nT('privacyDisclosure.dataNeverSentBody')}
        </p>
      </Card>

      <Card>
        <CardTitle>
          <HardDrive className="lucide-inline" aria-hidden="true" />
          {i18nT('privacyDisclosure.localDataTitle')}
        </CardTitle>
        <p className="text-sm text-muted leading-relaxed">
          {i18nT('privacyDisclosure.localDataBody')}
        </p>
      </Card>

      <Card>
        <CardTitle>
          <SlidersHorizontal className="lucide-inline" aria-hidden="true" />
          {i18nT('privacyDisclosure.controlsTitle')}
        </CardTitle>
        <p className="text-sm text-muted leading-relaxed mb-3">
          {i18nT('privacyDisclosure.controlsBody')}
        </p>
        <div className="flex flex-col items-start gap-2" aria-label={i18nT('privacyDisclosure.controlsTitle')}>
          {COMMANDS.map(command => (
            <code key={command} className="text-[13px] text-text bg-bg border border-border rounded-md px-2.5 py-1.5 select-all">
              {command}
            </code>
          ))}
          {SHELL_COMMANDS.map(({ kind, command }) => (
            <div key={command} className="flex max-w-full flex-col items-start gap-1">
              <span className="text-[12px] font-medium text-muted">{i18nT(SHELL_LABEL_KEY[kind])}</span>
              <code className="max-w-full overflow-x-auto text-[13px] text-text bg-bg border border-border rounded-md px-2.5 py-1.5 select-all">
                {command}
              </code>
            </div>
          ))}
        </div>
      </Card>
    </div>
  )
}
