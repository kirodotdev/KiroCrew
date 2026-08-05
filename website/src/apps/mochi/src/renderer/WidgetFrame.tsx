/**
 * WidgetFrame — renders <mcwidget> HTML content in a sandboxed iframe.
 * Ported from the KiroCrew website/src/components/WidgetFrame.tsx.
 *
 * Security: iframe uses sandbox="allow-scripts" with srcdoc (null origin).
 * The LLM content cannot access parent DOM, cookies, or localStorage.
 */
import React, { useState, useRef, useEffect, useMemo, useCallback } from 'react'
import { api } from '../mochiApi'
import { buildSrcdoc, THEME_VAR_NAMES } from '../../../../lib/widgetSrcdoc'

import { i18nT } from '../../../../i18n/t'
import { ArrowUpRight } from 'lucide-react'
const MIN_HEIGHT = 60
const MAX_HEIGHT = 600

/**
 * Theme variables for the widget document, read off this window.
 *
 * Mochi's windows receive the core CSS *variables* (not component styles), so
 * the same names resolve here and the widget inherits the live theme.
 */
function readThemeVars(): Record<string, string> {
  const cs = getComputedStyle(document.documentElement)
  const out: Record<string, string> = {}
  for (const name of THEME_VAR_NAMES) {
    const v = cs.getPropertyValue(name).trim()
    if (v) out[name] = v
  }
  return out
}

/** 'light' | 'dark', matching the attribute the dashboard and Mochi both set. */
function currentMode(): 'light' | 'dark' {
  return (document.documentElement.dataset.theme || '').includes('light') ? 'light' : 'dark'
}

interface WidgetFrameProps {
  html: string
  title?: string
}

export const WidgetFrame: React.FC<WidgetFrameProps> = ({ html, title = 'Widget' }) => {
  const iframeRef = useRef<HTMLIFrameElement>(null)
  const [height, setHeight] = useState(160)
  // The core builder, NOT a local one. The port's copy pulled Tailwind from
  // public cdn.tailwindcss.com (and had to allow that origin in the iframe CSP),
  // so widget styling broke with no network and the desktop app fetched a
  // third-party script at render time; the core replaced that with a
  // same-origin runtime and has a test asserting the CDN is gone. It also
  // assembles the document through typed DOM APIs instead of concatenating the
  // model's HTML into a template literal.
  const srcdoc = useMemo(
    () => buildSrcdoc({
      html,
      themeVars: readThemeVars(),
      mode: currentMode(),
      includeHeightReporter: true,
    }),
    [html],
  )

  useEffect(() => {
    const handler = (e: MessageEvent) => {
      if (!iframeRef.current || e.source !== iframeRef.current.contentWindow) return
      if (e.data?.type === 'mc-widget-height' && typeof e.data.height === 'number') {
        const h = Math.min(Math.max(e.data.height, MIN_HEIGHT), MAX_HEIGHT)
        setHeight(h)
      }
    }
    window.addEventListener('message', handler)
    return () => window.removeEventListener('message', handler)
  }, [])

  const openExternal = useCallback(() => {
    // Write to temp file and open in system browser (blob: URLs don't work with shell.openExternal)
    ;api?.openWidgetExternal?.(srcdoc, title)
  }, [srcdoc, title])

  return (
    <div style={{
      borderRadius: 8, border: '1px solid var(--border)',
      overflow: 'hidden', margin: '6px 0', background: 'var(--bg-elevated)',
    }}>
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '4px 8px', borderBottom: '1px solid var(--border)',
        fontSize: 11, color: 'var(--text-muted)',
      }}>
        <span style={{ fontWeight: 500 }}>{title}</span>
        <button onClick={openExternal} style={{
          background: 'none', border: 'none', color: 'var(--text-muted)',
          fontSize: 10, cursor: 'pointer', padding: '2px 4px', borderRadius: 3,
        }}
          title={i18nT('apps.mochi.widget.open_in_browser')}
          aria-label={i18nT('apps.mochi.widget.open_in_browser')}
        ><ArrowUpRight size={11} /></button>
      </div>
      <iframe
        ref={iframeRef}
        srcDoc={srcdoc}
        sandbox="allow-scripts"
        style={{ width: '100%', height, border: 'none', background: '#fff', display: 'block' }}
        title={title}
      />
    </div>
  )
}

/**
 * Extract <mcwidget> blocks from raw message text.
 * Returns segments: either plain text or widget objects.
 */
export interface WidgetSegment {
  type: 'text' | 'widget'
  content: string
  title?: string
}

const WIDGET_RE = /<mcwidget(?:\s+title="([^"]*)")?>([\s\S]*?)<\/mcwidget>/g

export function parseWidgets(raw: string): WidgetSegment[] {
  const segments: WidgetSegment[] = []
  let lastIdx = 0
  WIDGET_RE.lastIndex = 0
  let match: RegExpExecArray | null
  while ((match = WIDGET_RE.exec(raw)) !== null) {
    const before = raw.slice(lastIdx, match.index)
    if (before.trim()) segments.push({ type: 'text', content: before })
    segments.push({ type: 'widget', content: match[2].trim(), title: match[1] || i18nT('apps.mochi.widget.untitled') })
    lastIdx = match.index + match[0].length
  }
  const after = raw.slice(lastIdx)
  if (after.trim()) segments.push({ type: 'text', content: after })
  return segments
}

/** Check if text contains any complete mcwidget tag. */
export function hasWidgets(raw: string): boolean {
  WIDGET_RE.lastIndex = 0
  return WIDGET_RE.test(raw)
}
