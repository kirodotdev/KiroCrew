/**
 * InstalledAppCard — management row for the Library tab.
 *
 * Expandable row: a 16:9 hero capsule leading slot (``useHeroArt`` +
 * gradient/icon fallback, matching AppListRow), then Open / Enable / Disable /
 * Update / Sync / Uninstall actions and a details drawer.
 */
import { useState } from 'react'
import {
  Package, Power, PowerOff, Trash2, RefreshCw,
  Bot, Tag, Users, Zap, ChevronRight,
  ExternalLink, Clock, X, ArrowUp,
} from 'lucide-react'
import { api } from '../../api/client'
import { Badge, Btn } from '../ui'
import HeroCapsule from './HeroCapsule'
import type { InstalledApp } from './types'
import { appDisplayName, appDescription } from './appManifest'

import { i18nT } from '../../i18n/t'
import { fmtDateNumeric } from '../../i18n/format'
export default function InstalledAppCard({
  app,
  actionLoading,
  onAction,
  onOpen,
  onDetail,
}: {
  app: InstalledApp & { _newVersion?: string }
  actionLoading: string | null
  onAction: (name: string, action: 'enable' | 'disable' | 'uninstall' | 'update') => void
  onOpen: () => void
  onDetail: () => void
}) {
  const [expanded, setExpanded] = useState(false)
  const [remoteCmd, setRemoteCmd] = useState('')
  const m = app.manifest
  const agentCount = m?.agents?.length || 0
  const skillCount = m?.skills?.length || 0
  const cronCount = m?.crons?.length || 0
  const sopCount = m?.sops?.length || 0
  const hasUI = !!(m?.ui?.entry) || (m?.ui?.pages?.length || 0) > 0
  const pageIcon = m?.ui?.pages?.[0]?.icon || ''
  const isSelfManaged = app.resources === 'app'
  const isBuiltin = app.origin === 'builtin'
  const canUpdate = app.lifecycle === 'gateway'
  const canUninstall = app.lifecycle !== 'locked'
  const hasOpenCommand = !!m?.openCommand
  // Derive icon URL: prefer manifest iconUrl (builtins), fallback to blob proxy (registry)
  const iconUrl = m?.iconUrl || (m?.iconPath && m?.repo
    ? `/api/apps/blob?repo=${encodeURIComponent(m.repo)}&path=${encodeURIComponent(m.iconPath)}`
    : undefined)

  return (
    <div className="border border-border rounded-lg hover:border-accent/30 transition-colors overflow-hidden">
      {remoteCmd && (
        <div className="px-4 pt-3 pb-2">
          <div className="bg-accent/10 border border-accent/20 rounded-lg p-3 text-[13px]">
            <div className="flex items-start justify-between gap-2">
              <div>
                <span className="text-text font-medium">{i18nT('components.appstore.installedAppCard.remote_environment_detected')}</span>
                <p className="text-muted mt-1">{i18nT('components.appstore.installedAppCard.run_this_on_your_local_machine')}</p>
                <code className="block mt-1.5 bg-bg-elevated px-2 py-1 rounded text-[12px] font-mono select-all">{remoteCmd}</code>
              </div>
              <button aria-label={i18nT('components.appstore.installedAppCard.dismiss')} className="text-muted hover:text-text text-sm shrink-0" onClick={() => setRemoteCmd('')}><X className="lucide-inline" /></button>
            </div>
          </div>
        </div>
      )}
      <div className="p-4">
        <div className="flex items-start justify-between gap-4">
          <div className="flex items-start gap-3 flex-1 min-w-0">
            {/* Hero capsule — same art and fallback chain as Discover's rows,
                so one app looks like itself on both tabs. */}
            <HeroCapsule
              name={app.name}
              art={{ heroImage: m?.heroImage, heroImageDark: m?.heroImageDark, screenshots: m?.screenshots }}
              icon={pageIcon}
              iconUrl={iconUrl}
              className="w-24 h-[54px] mt-0.5"
            />
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 mb-1 flex-wrap">
                <button type="button" className="font-medium text-text cursor-pointer hover:text-accent transition-colors bg-transparent border-0 p-0 text-left" onClick={onDetail}>{appDisplayName(app)}</button>
                <span className="text-[11px] text-muted bg-bg-elevated px-1.5 py-0.5 rounded">{i18nT('components.appstore.installedAppCard.v')}{app.version}{app.updateAvailable && ` (v${app._newVersion} available)`}</span>
                {isBuiltin ? (
                  <>
                    <Badge variant="aim">{i18nT('components.appstore.installedAppCard.built_in')}</Badge>
                    <Badge variant={app.enabled ? 'ok' : 'warn'}>
                      {app.enabled ? i18nT('components.appstore.installedAppCard.enabled') : i18nT('components.appstore.installedAppCard.disabled')}
                    </Badge>
                  </>
                ) : isSelfManaged ? (
                  <Badge variant="ok">{i18nT('components.appstore.installedAppCard.self_managed')}</Badge>
                ) : (
                  <Badge variant={app.enabled ? 'ok' : 'warn'}>
                    {app.enabled ? i18nT('components.appstore.installedAppCard.enabled') : i18nT('components.appstore.installedAppCard.disabled')}
                  </Badge>
                )}
                {app.migratedTo && (
                  <Badge variant="warn">{i18nT('components.appstore.installedAppCard.migrating')}</Badge>
                )}
                {!isBuiltin && app.origin === 'registry' && (
                  <Badge variant="aim">{i18nT('components.appstore.installedAppCard.registry')}</Badge>
                )}
                {app.origin === 'local' && (
                  <Badge variant="warn">{i18nT('components.appstore.installedAppCard.local')}</Badge>
                )}
                {app.origin === 'external' && !isSelfManaged && (
                  <Badge variant="ok">{i18nT('components.appstore.installedAppCard.external')}</Badge>
                )}
              </div>
              <p className="text-sm text-muted mb-2 line-clamp-2">{appDescription({ name: app.name, description: m?.description })}</p>
              <div className="flex items-center gap-3 text-[12px] text-muted flex-wrap">
                {m?.author && <span className="flex items-center gap-1"><Users size={11} /> {m.author}</span>}
                {agentCount > 0 && <span className="flex items-center gap-1"><Bot size={11} /> {i18nT('components.appstore.installedAppCard.agent', { count: agentCount })}</span>}
                {skillCount > 0 && <span className="flex items-center gap-1"><Zap size={11} /> {i18nT('components.appstore.installedAppCard.skill', { count: skillCount })}</span>}
                {cronCount > 0 && <span className="flex items-center gap-1"><Clock size={11} /> {i18nT('components.appstore.installedAppCard.cron', { count: cronCount })}</span>}
                {hasUI && <span className="flex items-center gap-1"><Package size={11} /> {i18nT('components.appstore.installedAppCard.page', { count: m.ui!.pages!.length })}</span>}
              </div>
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {/* Open button — all app types */}
            {hasOpenCommand && (
              <Btn primary onClick={() => api.openApp(app.name).then((res: { remote?: boolean; command?: string; message?: string } | null) => {
                if (res?.remote) setRemoteCmd(res.command || res.message || i18nT('components.appstore.installedAppCard.app_cannot_be_opened_kirocrew_is_running_in_a_he'))
              }).catch(() => {})}>
                <ExternalLink size={14} /> {i18nT('components.appstore.installedAppCard.open')}
              </Btn>
            )}
            {app.enabled && hasUI && !hasOpenCommand && (
              <Btn primary onClick={onOpen}>
                <ExternalLink size={14} /> {i18nT('components.appstore.installedAppCard.open')}
              </Btn>
            )}

            {/* Enable/Disable */}
            {app.enabled ? (
              <Btn
                onClick={() => onAction(app.name, 'disable')}
                disabled={actionLoading === `${app.name}:disable`}
              >
                <PowerOff size={14} /> {i18nT('components.appstore.installedAppCard.disable')}
              </Btn>
            ) : (
              <Btn
                onClick={() => onAction(app.name, 'enable')}
                disabled={actionLoading === `${app.name}:enable`}
              >
                <Power size={14} /> {i18nT('components.appstore.installedAppCard.enable')}
              </Btn>
            )}

            {/* Update — show accent button when new version available (any installed app) */}
            {app.updateAvailable && (
              <Btn
                onClick={() => onAction(app.name, 'update')}
                disabled={actionLoading === `${app.name}:update`}
                title={i18nT('components.appstore.installedAppCard.update_to', { version: app._newVersion || app.version })}
                className="!bg-[var(--info)] !text-white hover:!opacity-80"
              >
                <ArrowUp size={14} /> {i18nT('components.appstore.installedAppCard.update')}
              </Btn>
            )}
            {/* Sync — always available for gateway apps */}
            {canUpdate && !app.updateAvailable && (
              <Btn
                onClick={() => onAction(app.name, 'update')}
                disabled={actionLoading === `${app.name}:update`}
                title={i18nT('components.appstore.installedAppCard.sync_app_from_its_source_directory')}
              >
                <RefreshCw size={14} /> {i18nT('components.appstore.installedAppCard.sync')}
              </Btn>
            )}

            {/* Uninstall — only for lifecycle != locked */}
            {canUninstall && (
              <Btn
                danger
                onClick={() => onAction(app.name, 'uninstall')}
                disabled={actionLoading === `${app.name}:uninstall`}
              >
                <Trash2 size={14} /> {i18nT('components.appstore.installedAppCard.uninstall')}
              </Btn>
            )}

            <button
              aria-label={expanded ? i18nT('components.appstore.installedAppCard.collapse_details') : i18nT('components.appstore.installedAppCard.expand_details')}
              className="text-muted hover:text-text transition-colors p-1 bg-transparent border-0 cursor-pointer"
              onClick={() => setExpanded(!expanded)}
            >
              <ChevronRight size={16} className={`transition-transform ${expanded ? 'rotate-90' : ''}`} />
            </button>
          </div>
        </div>
      </div>

      {/* Expanded details */}
      {expanded && (
        <div className="border-t border-border bg-bg-elevated/50 p-4 space-y-3 text-[13px]">
          {(m?.tags || []).length > 0 && (
            <div className="flex items-center gap-2 flex-wrap">
              <Tag size={12} className="text-muted" />
              {m!.tags!.map(t => (
                <span key={t} className="bg-bg-elevated border border-border px-2 py-0.5 rounded text-[11px] text-muted">{t}</span>
              ))}
            </div>
          )}
          {(m?.permissions?.mcpTools || []).length > 0 && (
            <div>
              <span className="text-muted">{i18nT('components.appstore.installedAppCard.mcp_tools')} </span>
              <span className="text-text">{m!.permissions!.mcpTools!.join(', ')}</span>
            </div>
          )}
          {hasUI && m?.ui?.pages && (
            <div>
              <span className="text-muted">{i18nT('components.appstore.installedAppCard.ui_pages')} </span>
              {m.ui.pages.map(p => (
                <span key={p.route} className="text-text mr-3">{p.label} ({p.route})</span>
              ))}
            </div>
          )}
          {sopCount > 0 && (
            <div>
              <span className="text-muted">{i18nT('components.appstore.installedAppCard.sops')} </span>
              <span className="text-text">{i18nT('components.appstore.installedAppCard.standard_operating_procedure', { count: sopCount })}</span>
            </div>
          )}
          <div className="text-[11px] text-muted">
            {i18nT('components.appstore.installedAppCard.installed')} {fmtDateNumeric(app.installedAt)}
            {m?.minKiroCrewVersion && <span className="ml-3">{i18nT('components.appstore.installedAppCard.min_version')} {m.minKiroCrewVersion}</span>}
            {isSelfManaged && <div className="mt-1">{i18nT('components.appstore.installedAppCard.management_app_handles_its_own_agent_skill_mcp_r')}</div>}
            {isBuiltin && <div className="mt-1">{i18nT('components.appstore.installedAppCard.built_in_this_feature_is_part_of_the_kirocrew_da')}</div>}
            {app.source && !isBuiltin && <div className="mt-1 truncate" title={app.source}>{i18nT('components.appstore.installedAppCard.source')} {app.source}</div>}
            {app.origin && <div className="mt-1">{i18nT('components.appstore.installedAppCard.origin')} {app.origin} {i18nT('components.appstore.installedAppCard.resources')} {app.resources || 'gateway'} {i18nT('components.appstore.installedAppCard.lifecycle')} {app.lifecycle || 'gateway'}</div>}
          </div>
        </div>
      )}
    </div>
  )
}
