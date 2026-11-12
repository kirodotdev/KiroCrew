import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Activity, BookOpen, Box, Cpu, FolderOpen, HardDrive, MemoryStick, Server, Timer } from 'lucide-react'
import { api, type RemoteEnvironmentHealth } from '../api/client'
import { fmtBytes, fmtDuration, fmtNumber, fmtPercent } from '../i18n/format'
import ErrorNotice from './ErrorNotice'
import { reportForError } from '../utils/errorReport'
import { errMessage } from '../utils/thunkError'

const HEALTH_POLL_MS = 15_000

/** Used fraction at or above which a meter reads as elevated. */
const WARN_AT = 0.8

/** Only a warning or a danger is coloured; a healthy meter stays neutral. */
function tone(used: number): string {
  if (used >= 0.95) return 'bg-danger'
  if (used >= WARN_AT) return 'bg-warn'
  return 'bg-muted'
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

/** Each left-out skill by its folder name, so the person can see which. */
function skillNames(items: { path: string }[] | undefined): string {
  return (items ?? []).map((s) => s.path.split(/[\\/]/).filter(Boolean).pop() ?? s.path).join(', ')
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
export default function EnvironmentHealthCard({ slot, name, icon }: {
  slot: string; name: string
  /** The provider's mark, as the chip shows it; a generic server otherwise. */
  icon?: React.ReactNode
}) {
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
  const memUsed = mem ? 1 - mem.available_bytes / mem.total_bytes : 0
  const skills = data?.skills
  const state = data?.state
  const dot = state === 'running' ? 'bg-ok' : state === 'stopped' || state === 'not_created' ? 'bg-muted' : 'bg-danger'

  return (
    <div className="w-[min(20rem,calc(100vw-2rem))] text-[12px]" data-testid="environment-health-card">
      <div className="mb-2.5 grid gap-0.5">
        {/* Names the card for what it is: a status read-out of the chip's
            environment, not a control that switches machines. */}
        <span className="text-[11px] text-muted">{t('sessionEnvironments.health.statusHeading')}</span>
        <div className="flex items-center gap-2">
          {icon ?? <Server className="lucide-inline shrink-0 text-accent" aria-hidden />}
          <span className="truncate font-semibold text-text">{name}</span>
          {data && (
            <span className="ml-auto flex shrink-0 items-center gap-1.5 text-muted">
              <span className={`h-1.5 w-1.5 rounded-full ${dot}`} aria-hidden />
              {stateLabel(t, data.state)}
            </span>
          )}
        </div>
      </div>
      {health.isError ? (
        // A read-only card: nothing to lose, so the hand-off stays on, and it
        // carries the failed request (endpoint, status, code) from its report.
        <ErrorNotice
          variant="inline"
          title={errMessage(health.error) ? t('sessionEnvironments.health.error') : undefined}
          message={errMessage(health.error) || t('sessionEnvironments.health.error')}
          report={reportForError(health.error)}
          messagePlacement="below"
          askAgent
        />
      ) : data?.state === 'unavailable' ? (
        // The guest's own reason ("the workspace agent did not answer…") is
        // jargon as a lead, so it sits under a plain title and still travels
        // with the hand-off.
        <ErrorNotice
          variant="inline"
          title={t('sessionEnvironments.health.notResponding')}
          message={data.error || t('sessionEnvironments.health.error')}
          messagePlacement="below"
          askAgent
        />
      ) : !data ? (
        <div className="text-muted" aria-busy="true">{t('sessionEnvironments.remoteFiles.loading')}</div>
      ) : (
        <dl className="m-0 grid min-w-0 gap-2">
          {data.workspace && (
            <Row icon={<Box className="lucide-inline" aria-hidden />} label={t('sessionEnvironments.health.workspaceName')} value={<span className="font-mono">{data.workspace}</span>} title={data.workspace} />
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
                  label={t('sessionEnvironments.health.busyCores')}
                  // Load averages read against the core count: "0.4 · 0.3 · 0.2 of 4".
                  value={<span className="tabular-nums">{t('sessionEnvironments.health.busyCoresValue', {
                    load: stats.load.map((v) => fmtNumber(v, { maximumFractionDigits: 2 })).join(' · '),
                    cores: fmtNumber(stats.cpus),
                  })}</span>}
                />
              )}
              {mem && (
                <div className="grid gap-1 border-t border-border pt-2">
                  <Row
                    icon={<MemoryStick className="lucide-inline" aria-hidden />}
                    label={t('sessionEnvironments.health.memory')}
                    value={fmtPercent(memUsed)}
                  />
                  <Meter
                    label={t('sessionEnvironments.health.memory')}
                    used={memUsed}
                    summary={t('sessionEnvironments.health.free', { free: fmtBytes(mem.available_bytes), total: fmtBytes(mem.total_bytes) })}
                  />
                  {memUsed >= WARN_AT && (
                    <p className="m-0 text-[11px] leading-snug text-warn">{t('sessionEnvironments.health.memoryLow')}</p>
                  )}
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
          {skills?.state === 'failed' && (
            <div className="grid gap-1 border-t border-border pt-2" data-testid="environment-skills">
              {/* The row names what failed, so the notice below reads as about skills. */}
              <Row
                icon={<BookOpen className="lucide-inline" aria-hidden />}
                label={t('sessionEnvironments.health.skills')}
                value={t('sessionEnvironments.health.skillsFailedValue')}
              />
              <ErrorNotice
                variant="inline"
                message={t('sessionEnvironments.health.skillsFailed')}
                askAgent
              />
            </div>
          )}
          {skills && skills.state !== 'failed' && (
            // Says plainly that skill files were copied onto this machine, and
            // where, so nothing lands there unannounced (Settings turns it off).
            <div className="grid gap-1 border-t border-border pt-2" data-testid="environment-skills">
              <Row
                icon={<BookOpen className="lucide-inline" aria-hidden />}
                label={t('sessionEnvironments.health.skills')}
                value={skills.state === 'copied'
                  ? t('sessionEnvironments.health.skillsCopied', { n: fmtNumber(skills.count ?? 0) })
                  : t('sessionEnvironments.health.skillsOff')}
              />
              {skills.state === 'copied' && skills.root && (
                <p className="m-0 text-[11px] leading-snug text-muted">
                  {t('sessionEnvironments.health.skillsHint', { root: skills.root })}
                </p>
              )}
              {skills.gateway_python && skills.python && (
                <p className="m-0 text-[11px] leading-snug text-warn">
                  {t('sessionEnvironments.health.skillsOldPython', { python: skills.python, gateway: skills.gateway_python })}
                </p>
              )}
              {!!skills.incompatible && skills.python && (
                // Scripts that cannot run there are an error the agent can act on.
                <ErrorNotice
                  variant="inline"
                  message={t('sessionEnvironments.health.skillsIncompatible', { n: fmtNumber(skills.incompatible), python: skills.python })}
                  askAgent
                />
              )}
              {!!skills.skipped_count && (
                <p className="m-0 text-[11px] leading-snug text-muted">
                  {t('sessionEnvironments.health.skillsLeftOut', { n: fmtNumber(skills.skipped_count) })}
                </p>
              )}
              {!!skills.outside_count && (
                // Each skill left out whole, by folder name, so the person can see which.
                <p className="m-0 text-[11px] leading-snug text-muted" data-testid="environment-skills-outside">
                  {t('sessionEnvironments.health.skillsOutside', {
                    n: fmtNumber(skills.outside_count),
                    names: skillNames(skills.outside),
                  })}
                </p>
              )}
              {!!skills.dropped_count && (
                <p className="m-0 text-[11px] leading-snug text-muted" data-testid="environment-skills-secret">
                  {t('sessionEnvironments.health.skillsSecret', {
                    n: fmtNumber(skills.dropped_count),
                    names: skillNames(skills.dropped),
                  })}
                </p>
              )}
            </div>
          )}
          {data.state === 'stopped' && (
            <p className="m-0 text-muted leading-snug">{t('sessionEnvironments.health.stoppedHint')}</p>
          )}
          {data.state === 'not_created' && (
            <p className="m-0 text-muted leading-snug">{t('sessionEnvironments.health.notCreatedHint')}</p>
          )}
        </dl>
      )}
    </div>
  )
}
