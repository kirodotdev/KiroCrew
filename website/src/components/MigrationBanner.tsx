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

import { i18nT } from '../i18n/t'
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
          {i18nT('components.migrationBanner.this_feature_is_moving_to_a_standalone_app')}
        </div>
        <div className="text-[13px] text-muted mt-1">
          {i18nT('components.migrationBanner.install')}{appName}{i18nT('components.migrationBanner.from_apps_before_the_next_kirocrew_update_to_kee')}
        </div>
      </div>
      <Btn
        primary
        onClick={() => navigate(`/apps/detail/${encodeURIComponent(targetName)}`)}
        className="shrink-0"
      >
        {i18nT('components.migrationBanner.install_from_apps')} <ArrowRight size={14} />
      </Btn>
    </div>
  )
}
