/**
 * MigrationBanner — Amber warning banner shown on builtin app pages
 * when the app has a `migratedTo` field set (Phase 1 deprecation).
 *
 * Displays the app name, migration target, and a button to navigate
 * to the App Store detail page for the standalone replacement.
 */
import { useNavigate } from 'react-router-dom'
import { AlertTriangle, ArrowRight } from 'lucide-react'
import { Btn } from './ui'

interface MigrationBannerProps {
  appName: string
  migratedTo: string // format "registry:{name}" or "standalone:{name}"
}

export default function MigrationBanner({ appName, migratedTo }: MigrationBannerProps) {
  const navigate = useNavigate()
  const targetName = migratedTo.includes(':') ? migratedTo.split(':').slice(1).join(':') : migratedTo

  return (
    <div className="mx-6 mt-4 mb-2 bg-warn/10 border border-warn/30 rounded-lg p-4 flex items-start gap-3 animate-rise">
      <AlertTriangle size={18} className="text-warn shrink-0 mt-0.5" />
      <div className="flex-1 min-w-0">
        <div className="text-[13px] font-medium text-text">
          This feature is moving to a standalone app
        </div>
        <div className="text-[13px] text-muted mt-1">
          Install "{appName}" from Apps before the next KiroCrew update to keep using it.
        </div>
      </div>
      <Btn
        primary
        onClick={() => navigate(`/apps/detail/${encodeURIComponent(targetName)}`)}
        className="shrink-0"
      >
        Install from Apps <ArrowRight size={14} />
      </Btn>
    </div>
  )
}
