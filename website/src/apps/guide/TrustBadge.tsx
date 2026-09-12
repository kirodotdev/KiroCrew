// Trust badge — confirmed / known-bug / unverified, three colors.
import { useTranslation } from 'react-i18next'
import { ShieldCheck, Bug, HelpCircle, type LucideIcon } from 'lucide-react'

const MAP: Record<string, { key: string; icon: LucideIcon; color: string }> = {
  confirmed: { key: 'apps.guide.trustConfirmed', icon: ShieldCheck, color: 'var(--ok)' },
  'known-bug': { key: 'apps.guide.trustKnownBug', icon: Bug, color: 'var(--warn)' },
  unverified: { key: 'apps.guide.trustUnverified', icon: HelpCircle, color: 'var(--muted)' },
}

export default function TrustBadge({ trust }: { trust?: string }) {
  const { t } = useTranslation()
  const spec = MAP[trust || ''] || MAP.unverified
  const Icon = spec.icon
  return (
    <span
      className="inline-flex items-center gap-1 text-xs font-medium rounded-full px-2 py-0.5"
      style={{
        color: spec.color,
        border: `1px solid color-mix(in srgb, ${spec.color} 45%, transparent)`,
        background: `color-mix(in srgb, ${spec.color} 10%, transparent)`,
      }}
    >
      <Icon size={12} />
      {t(spec.key)}
    </span>
  )
}
