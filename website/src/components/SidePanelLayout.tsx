import React from 'react'
import { useSearchParams } from 'react-router-dom'
import { useIsMobile } from '../hooks/useIsMobile'

export interface SidePanelTab {
  key: string
  label: string
  icon: React.ReactNode
  description?: string
  /** Presence dot after the label (e.g. About while an update is available). */
  dot?: boolean
}

interface SidePanelLayoutProps {
  title: string
  tabs: readonly SidePanelTab[]
  defaultTab?: string
  footer?: React.ReactNode
  headerRight?: React.ReactNode
  /** When true, content area uses overflow-hidden + flex layout for Virtuoso/fixed-height children */
  fixedContent?: boolean
  children: (activeTab: string) => React.ReactNode
}

export default function SidePanelLayout({ title, tabs, defaultTab, footer, headerRight, fixedContent, children }: SidePanelLayoutProps) {
  const [params, setParams] = useSearchParams()
  const isMobile = useIsMobile()
  const rawTab = params.get('tab')
  const first = defaultTab || tabs[0]?.key || ''
  const tab = rawTab && tabs.some(t => t.key === rawTab) ? rawTab : first
  const setTab = (t: string) => setParams(prev => {
    const next = new URLSearchParams(prev)
    if (t === first) next.delete('tab')
    else next.set('tab', t)
    return next
  }, { replace: true })
  const meta = tabs.find(t => t.key === tab)

  return (
    <div className={`flex-1 min-h-0 flex overflow-hidden ${isMobile ? 'flex-col' : ''}`}>
      {isMobile ? (
        <div className="shrink-0 border-b border-border bg-bg px-4 pt-3 pb-0">
          <div className="flex items-center justify-between mb-2">
            <div className="text-lg font-bold text-text-strong">{title}</div>
            {headerRight}
          </div>
          <div className="flex gap-1 overflow-x-auto scrollbar-none pb-2">
            {tabs.map(t => (
              <button
                key={t.key}
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full text-[12px] font-medium cursor-pointer border-none whitespace-nowrap transition-all ${
                  tab === t.key
                    ? 'bg-accent-subtle text-accent'
                    : 'bg-transparent text-muted hover:text-text hover:bg-bg-hover'
                }`}
                onClick={() => setTab(t.key)}
              >
                <span className="w-3.5 h-3.5 shrink-0 flex items-center justify-center">{t.icon}</span>
                {t.label}
                {t.dot && <span className="w-1.5 h-1.5 bg-accent rounded-full shrink-0" role="status" aria-label="update available" />}
              </button>
            ))}
          </div>
          {footer && <div className="pt-2 pb-2">{footer}</div>}
        </div>
      ) : (
        <nav className="w-[200px] shrink-0 border-r border-border bg-bg overflow-y-auto py-3 px-3 flex flex-col gap-0.5">
          <div className="text-lg font-bold text-text-strong px-2.5 py-2 mb-1">{title}</div>
          {tabs.map(t => (
            <button
              key={t.key}
              className={`flex items-center gap-2.5 w-full px-2.5 py-2 rounded-md text-[13px] font-medium cursor-pointer border-none transition-all ${
                tab === t.key
                  ? 'bg-accent-subtle text-accent'
                  : 'bg-transparent text-muted hover:text-text hover:bg-bg-hover'
              }`}
              onClick={() => setTab(t.key)}
            >
              <span className={`w-4 h-4 shrink-0 flex items-center justify-center ${tab === t.key ? 'text-accent' : 'text-muted'}`}>
                {t.icon}
              </span>
              {t.label}
              {t.dot && <span className="ml-auto w-2 h-2 bg-accent rounded-full shrink-0" role="status" aria-label="update available" />}
            </button>
          ))}
          {footer && <div className="mt-auto pt-3 px-2.5">{footer}</div>}
        </nav>
      )}

      <div className={`flex-1 min-w-0 min-h-0 flex flex-col ${fixedContent ? 'overflow-hidden' : 'overflow-y-auto'}`}>
        {!isMobile && (
        <div className="flex items-end justify-between gap-4 px-6 pt-4 pb-3 shrink-0">
          <div>
            <div className="text-2xl font-bold tracking-tight text-text-strong">{meta?.label || ''}</div>
            {meta?.description && <div className="text-muted text-sm mt-1">{meta.description}</div>}
          </div>
          {headerRight}
        </div>
        )}
        <div className={`${isMobile ? 'px-4' : 'px-6'} ${fixedContent ? 'flex-1 min-h-0 flex flex-col' : 'flex-1 pb-8'}`}>
          {children(tab)}
        </div>
      </div>
    </div>
  )
}
