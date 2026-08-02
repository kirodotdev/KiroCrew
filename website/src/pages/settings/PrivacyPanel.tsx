import { EyeOff, HardDrive, Radio, SlidersHorizontal } from 'lucide-react'
import { Card, CardTitle } from '../../components/ui'
import { i18nT } from '../../i18n/t'

const COMMANDS = [
  'kirocrew telemetry status',
  'kirocrew telemetry disable',
] as const

const SHELL_COMMANDS = [
  {
    labelKey: 'privacyDisclosure.shellMacOSLinuxLabel',
    command: 'export KIROCREW_TELEMETRY_DISABLED=1',
  },
  {
    labelKey: 'privacyDisclosure.shellPowerShellLabel',
    command: "$env:KIROCREW_TELEMETRY_DISABLED = '1'",
  },
  {
    labelKey: 'privacyDisclosure.shellWindowsCmdLabel',
    command: 'set KIROCREW_TELEMETRY_DISABLED=1',
  },
] as const

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
          {SHELL_COMMANDS.map(({ labelKey, command }) => (
            <div key={command} className="flex max-w-full flex-col items-start gap-1">
              <span className="text-[12px] font-medium text-muted">{i18nT(labelKey)}</span>
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
