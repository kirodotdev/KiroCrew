import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Activity, Box, Cpu, FolderOpen, HardDrive, MemoryStick, Server, Timer } from 'lucide-react'
import { api, type RemoteEnvironmentHealth } from '../api/client'
import { fmtBytes, fmtDuration, fmtNumber, fmtPercent } from '../i18n/format'
import ErrorNotice from './ErrorNotice'

const HEALTH_POLL_MS = 15_000

/** Used fraction at or above which a meter reads as elevated / critical. */
function tone(used: number): string {
  if (used >= 0.95) return 'bg-danger'
  if (used >= 0.8) return 'bg-warn'
  return 'bg-ok'
}

function Meter({ label, used, summary }: { label: string; used: number; summary: string }) {
  const pct = Math.max(0, Math.min(1, used))
  return (
    <div className="grid gap-1">
      <div
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(pct * 100)}
        className="h-1.5 overflow-hidden rounded-full bg-border"
      >
        <div className={`h-full rounded-full ${tone(pct)}`} style={{ width: `${pct * 100}%` }} />
      </div>
      <span className="text-right text-[11px] text-muted tabular-nums">{summary}</span>
    </div>
  )
}

function Row({ icon, label, value, title }: { icon: React.ReactNode; label: string; value: React.ReactNode; title?: string }) {
  return (
    <div className="grid grid-cols-[auto_minmax(0,1fr)] items-center gap-x-3">
      <dt className="flex items-center gap-1.5 text-muted">{icon}{label}</dt>
      <dd className="m-0 truncate text-right font-medium text-text" title={title}>{value}</dd>
    </div>
  )
}

function stateLabel(t: (k: string) => string, state: RemoteEnvironmentHealth['state']): string {
  switch (state) {
    case 'running': return t('sessionEnvironments.health.running')
    case 'stopped': return t('sessionEnvironments.health.stopped')
    case 'not_created': return t('sessionEnvironments.health.notCreated')
    default: return t('sessionEnvironments.health.unavailable')
  }
}

function uptime(seconds: number): string {
  const d = Math.floor(seconds / 86400)
  const h = Math.floor((seconds % 86400) / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  if (d > 0) return fmtDuration([[d, 'day'], [h, 'hour']], { dropZero: true })
  if (h > 0) return fmtDuration([[h, 'hour'], [m, 'minute']], { dropZero: true })
  return fmtDuration([[m, 'minute']])
}

/** The remote environment's identity, state and machine metrics: the body of
 *  the composer chip's hover card. Polls only while mounted (the card is open),
 *  and the endpoint never starts a stopped workspace to measure it. */
export default function EnvironmentHealthCard({ slot, name }: { slot: string; name: string }) {
  const { t } = useTranslation()
  const health = useQuery({
    queryKey: ['slot-environment-health', slot],
    queryFn: () => api.chatSlotEnvironmentHealth(slot),
    refetchInterval: HEALTH_POLL_MS,
    refetchIntervalInBackground: false,
    staleTime: 10_000,
    retry: false,
  })
  const data = health.data
  const stats = data?.stats
  const mem = stats?.memory
  const disk = stats?.disk
  const state = data?.state
  const dot = state === 'running' ? 'bg-ok' : state === 'stopped' || state === 'not_created' ? 'bg-muted' : 'bg-warn'

  return (
    <div className="w-[min(20rem,calc(100vw-2rem))] text-[12px]" data-testid="environment-health-card">
      <div className="mb-2.5 flex items-center gap-2">
        <Server className="lucide-inline shrink-0 text-accent" aria-hidden />
        <span className="truncate font-semibold text-text">{name}</span>
        {data && (
          <span className="ml-auto flex shrink-0 items-center gap-1.5 text-muted">
            <span className={`h-1.5 w-1.5 rounded-full ${dot}`} aria-hidden />
            {stateLabel(t, data.state)}
          </span>
        )}
      </div>
      {health.isError || data?.state === 'unavailable' ? (
        // A read-only card: nothing to lose, so the hand-off stays on.
        <ErrorNotice variant="inline" message={data?.error || t('sessionEnvironments.health.error')} askAgent />
      ) : !data ? (
        <div className="text-muted" aria-busy="true">{t('sessionEnvironments.remoteFiles.loading')}</div>
      ) : (
        <dl className="m-0 grid min-w-0 gap-2">
          {data.workspace && (
            <Row icon={<Box className="lucide-inline" aria-hidden />} label={t('sessionEnvironments.health.workspace')} value={<span className="font-mono">{data.workspace}</span>} title={data.workspace} />
          )}
          {data.workdir && (
            <Row icon={<FolderOpen className="lucide-inline" aria-hidden />} label={t('sessionEnvironments.health.directory')} value={<span className="font-mono">{data.workdir}</span>} title={data.workdir} />
          )}
          {stats && (
            <>
              <Row
                icon={<Cpu className="lucide-inline" aria-hidden />}
                label={t('sessionEnvironments.health.cpu')}
                value={t('sessionEnvironments.health.cpuValue', { cpus: fmtNumber(stats.cpus), arch: stats.arch })}
              />
              {stats.load && (
                <Row
                  icon={<Activity className="lucide-inline" aria-hidden />}
                  label={t('sessionEnvironments.health.load')}
                  value={<span className="tabular-nums">{stats.load.map((v) => fmtNumber(v, { maximumFractionDigits: 2 })).join(' · ')}</span>}
                />
              )}
              {mem && (
                <div className="grid gap-1 border-t border-border pt-2">
                  <Row
                    icon={<MemoryStick className="lucide-inline" aria-hidden />}
                    label={t('sessionEnvironments.health.memory')}
                    value={fmtPercent(1 - mem.available_bytes / mem.total_bytes)}
                  />
                  <Meter
                    label={t('sessionEnvironments.health.memory')}
                    used={1 - mem.available_bytes / mem.total_bytes}
                    summary={t('sessionEnvironments.health.free', { free: fmtBytes(mem.available_bytes), total: fmtBytes(mem.total_bytes) })}
                  />
                </div>
              )}
              {disk && (
                <div className="grid gap-1">
                  <Row
                    icon={<HardDrive className="lucide-inline" aria-hidden />}
                    label={t('sessionEnvironments.health.disk')}
                    value={fmtPercent(1 - disk.free_bytes / disk.total_bytes)}
                  />
                  <Meter
                    label={t('sessionEnvironments.health.disk')}
                    used={1 - disk.free_bytes / disk.total_bytes}
                    summary={t('sessionEnvironments.health.free', { free: fmtBytes(disk.free_bytes), total: fmtBytes(disk.total_bytes) })}
                  />
                </div>
              )}
              {stats.uptime_s !== undefined && (
                <Row icon={<Timer className="lucide-inline" aria-hidden />} label={t('sessionEnvironments.health.uptime')} value={uptime(stats.uptime_s)} />
              )}
            </>
          )}
          {data.state === 'stopped' && (
            <p className="m-0 text-muted leading-snug">{t('sessionEnvironments.health.stoppedHint')}</p>
          )}
        </dl>
      )}
    </div>
  )
}
