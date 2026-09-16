// Platform / topic filter chips above the entry list. Single-select per axis;
// clicking the active chip clears it. Selection drives the server-side
// /entries?platform=&topic= query.
import { useTranslation } from 'react-i18next'
import { X } from 'lucide-react'

const PLATFORM_KEY: Record<string, string> = {
  macos: 'apps.guide.platform.macos',
  windows: 'apps.guide.platform.windows',
  linux: 'apps.guide.platform.linux',
  'cloud-desktop': 'apps.guide.platform.cloudDesktop',
  docker: 'apps.guide.platform.docker',
  phone: 'apps.guide.platform.phone',
}

function titleCase(slug: string): string {
  return slug.replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

function Chip({
  label,
  active,
  onClick,
}: {
  label: string
  active: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className="text-xs rounded-full px-2.5 py-1 focus-visible:ring-1 focus-visible:ring-[var(--accent)]"
      style={{
        border: '1px solid ' + (active ? 'var(--accent)' : 'var(--border)'),
        background: active ? 'color-mix(in srgb, var(--accent) 15%, transparent)' : 'transparent',
        color: active ? 'var(--accent)' : 'var(--text)',
      }}
    >
      {label}
    </button>
  )
}

export default function FilterChips({
  platforms,
  topics,
  platform,
  topic,
  onPlatform,
  onTopic,
  onClear,
}: {
  platforms: string[]
  topics: string[]
  platform: string
  topic: string
  onPlatform: (p: string) => void
  onTopic: (t: string) => void
  onClear: () => void
}) {
  const { t } = useTranslation()
  const label = (p: string) => (PLATFORM_KEY[p] ? t(PLATFORM_KEY[p]) : titleCase(p))
  if (!platforms.length && !topics.length) return null
  return (
    <div className="flex flex-col gap-2 px-4 py-3" style={{ borderBottom: '1px solid var(--border)' }}>
      {platforms.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-xs font-medium mr-1" style={{ color: 'var(--muted)' }}>
            {t('apps.guide.filterPlatform')}
          </span>
          {platforms.map((p) => (
            <Chip key={p} label={label(p)} active={platform === p} onClick={() => onPlatform(p)} />
          ))}
        </div>
      )}
      {topics.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-xs font-medium mr-1" style={{ color: 'var(--muted)' }}>
            {t('apps.guide.filterTopic')}
          </span>
          {topics.map((tp) => (
            <Chip key={tp} label={titleCase(tp)} active={topic === tp} onClick={() => onTopic(tp)} />
          ))}
        </div>
      )}
      {(platform || topic) && (
        <button
          type="button"
          onClick={onClear}
          className="self-start inline-flex items-center gap-1 text-xs focus-visible:ring-1 focus-visible:ring-[var(--accent)]"
          style={{ color: 'var(--muted)' }}
        >
          <X size={12} />
          {t('apps.guide.filterClear')}
        </button>
      )}
    </div>
  )
}
