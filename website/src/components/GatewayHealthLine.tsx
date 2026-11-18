/**
 * The gateway's health in one line: a status dot, the connection wording and
 * the uptime (with the version when the status report carries one). Settings >
 * Overview draws it as its hero; Home draws the compact form as its footer.
 * One component, so the two surfaces cannot disagree about what "running" means.
 */
import { Trans } from 'react-i18next'
import { useAppSelector } from '../store'
import { i18nT } from '../i18n/t'
import { Uptime } from './Uptime'

export default function GatewayHealthLine({ compact = false }: { compact?: boolean }) {
  const status = useAppSelector(s => s.dashboard.status)
  const connected = useAppSelector(s => s.dashboard.connected)
  const wording = connected && status
    ? i18nT('pages.overviewPage.all_systems_running')
    : status ? i18nT('pages.overviewPage.reconnecting') : i18nT('pages.overviewPage.connecting')
  const uptime = status?.version
    ? <Trans i18nKey="pages.overviewPage.uptime_line_version" values={{ version: status.version }} components={{ uptime: <Uptime /> }} />
    : <Trans i18nKey="pages.overviewPage.uptime_line" components={{ uptime: <Uptime /> }} />
  const dot = <span className={`w-2 h-2 rounded-full shrink-0 ${connected && status ? 'bg-ok' : 'bg-warn'}`} />
  if (compact) {
    return (
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[12.5px] text-muted" data-testid="gateway-health-line">
        {dot}
        <span className="text-text font-medium">{wording}</span>
        <span aria-hidden="true">·</span>
        <span>{uptime}</span>
      </div>
    )
  }
  return (
    <div data-testid="gateway-health-line">
      <div className="text-lg font-bold text-text-strong flex items-center gap-2">
        {dot}
        {wording}
      </div>
      <div className="text-[12.5px] text-muted mt-0.5">{uptime}</div>
    </div>
  )
}
