import { BookOpen } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import Card from './Card'
import { calcCompanionDays, memoryRows } from './memories'
import type { StatsPayload } from './types'


export default function MemoriesSection({ mem, offline }: {
  mem: StatsPayload | null
  offline: boolean
}) {
  const rows = mem ? memoryRows(mem.stats, mem.petName) : []

  return (
    <Card
      title={i18nT('apps.crewCompanion.memories.title')}
      icon={BookOpen}
      right={mem
        ? <span className="cc-muted">{i18nT('apps.crewCompanion.memories.days_together', { days: calcCompanionDays(mem.stats.firstLaunch) })}</span>
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
