import { BookOpen } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import Card from './Card'
import { calcCompanionDays, memoryRows } from './memories'
import type { StatsPayload } from './types'


export default function MemoriesSection({ mem, offline, stale }: {
  mem: StatsPayload | null
  offline: boolean
  /** When the pet is off we show cached stats; label them as a look-back. */
  stale?: boolean
}) {
  const rows = mem ? memoryRows(mem.stats, mem.petName) : []

  return (
    <Card
      title={i18nT('apps.crewCompanion.memories.title')}
      icon={BookOpen}
      right={mem
        ? <span className="cc-muted">{stale
            ? i18nT('apps.crewCompanion.memories.from_last_session')
            /*
             * `count` (not `days`) — i18next picks the singular/plural form from a
             * variable named `count`. The mainline version used a single key and read
             * "1 days together" on the first day; the catalogue now carries proper
             * per-language plural forms, so this stayed fixed rather than reverting
             * with the rest of the file.
             */
            : i18nT('apps.crewCompanion.memories.days_together', {
                count: calcCompanionDays(mem.stats.firstLaunch),
              })}</span>
        : undefined}
    >
      {offline ? (
        <div className="cc-muted">{i18nT('apps.crewCompanion.memories.offline')}</div>
      ) : mem === null ? (
        <div className="cc-muted">{i18nT('apps.crewCompanion.memories.loading')}</div>
      ) : rows.length === 0 ? (
        <div className="cc-muted">{i18nT('apps.crewCompanion.memories.empty')}</div>
      ) : (
        <div>
          {rows.map((r, i) => (
            <div key={i} className={`cc-row${i === 0 ? ' is-first' : ''}`}>
              <r.icon className="cc-mem-icon lucide-inline" aria-hidden />
              <span>{r.text}</span>
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}
