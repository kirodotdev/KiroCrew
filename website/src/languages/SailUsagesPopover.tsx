import { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'
import type { SailUsage } from './sailNavigation'

/** A test-only usage: a `test`/`tests` path segment or name part (src/test/...,
 *  test_vqd/..., SYSTEM_SYSRULES_test_foo.xml, FooTest.java). */
export function isTestUsage(path: string): boolean {
  return /(^|[\/_.-])tests?([\/_.-]|$)/i.test(path) || /Tests?\.java$/.test(path)
}

/** "Find usages" result list for a SAIL declaration, anchored at the click.
 *  Portaled to <body> so no clipping/transformed ancestor in the side panel can
 *  trap the fixed position. Esc or a click outside closes it. */
export default function SailUsagesPopover({ name, x, y, loading, usages, truncated, scope, prefix, root, onOpen, onClose }: {
  name: string
  x: number
  y: number
  loading: boolean
  usages: SailUsage[]
  truncated: boolean
  scope: 'repo' | 'file'
  /** Domain shown before the name in the header (`a`, `ri`, `local`). */
  prefix: string
  /** Common prefix stripped from displayed paths. */
  root?: string
  onOpen: (u: SailUsage) => void
  onClose: () => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    const onDown = (e: PointerEvent) => { if (ref.current && !e.composedPath().includes(ref.current)) onClose() }
    window.addEventListener('keydown', onKey)
    window.addEventListener('pointerdown', onDown, true)
    return () => { window.removeEventListener('keydown', onKey); window.removeEventListener('pointerdown', onDown, true) }
  }, [onClose])
  const W = 560
  const left = Math.max(8, Math.min(x, window.innerWidth - W - 8))
  const top = Math.min(y + 12, window.innerHeight - 200)
  const shown = (p: string) => (root && p.startsWith(root + '/') ? p.slice(root.length + 1) : p)
  // Production usages first; test usages after them, tinted green, so the ones
  // that matter stand out.
  const prod = usages.filter(u => !isTestUsage(shown(u.path)))
  const tests = usages.filter(u => isTestUsage(shown(u.path)))
  const row = (u: SailUsage, test: boolean) => (
    <li key={`${u.path}:${u.line}`}>
      <button
        className={`w-full text-left px-2 py-1.5 rounded border-none cursor-pointer text-text ${test ? 'bg-ok-subtle hover:bg-bg-hover' : 'bg-transparent hover:bg-bg-hover'}`}
        style={test ? { borderLeft: '2px solid var(--ok, currentColor)' } : undefined}
        onClick={() => onOpen(u)}
      >
        <div className={`text-[11px] truncate ${test ? 'text-ok-text' : 'text-muted'}`}>{shown(u.path).split('/').pop()}:{u.line} <span className="opacity-70">{shown(u.path)}</span></div>
        <div className="text-[12px] font-mono truncate">{u.text}</div>
      </button>
    </li>
  )
  return createPortal(
    <div
      ref={ref}
      role="dialog"
      aria-label={`Usages of ${name}`}
      className="fixed z-[1000] flex flex-col rounded-lg border border-border bg-bg-elevated text-text shadow-xl"
      style={{ left, top, width: W, maxHeight: Math.max(160, window.innerHeight - top - 12) }}
    >
      <div className="flex items-center gap-2 px-3 py-2 border-b border-border text-[12px] shrink-0">
        <span className="font-medium font-mono truncate">{prefix ? `${prefix}!${name}` : name}</span>
        <span className="text-muted">
          {loading ? 'searching…' : `${prod.length} usage${prod.length === 1 ? '' : 's'}${scope === 'file' ? ' in this file' : ''}`}
        </span>
        {!loading && tests.length > 0 && <span className="text-ok-text">· {tests.length} in tests</span>}
        {!loading && truncated && <span className="text-muted">(first {usages.length})</span>}
        <button className="ml-auto p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer bg-transparent border-none" onClick={onClose} aria-label="Close">
          <X size={13} />
        </button>
      </div>
      <ul className="overflow-y-auto m-0 p-1 list-none">
        {!loading && usages.length === 0 && <li className="px-2 py-2 text-[12px] text-muted">No usages found</li>}
        {prod.map(u => row(u, false))}
        {tests.length > 0 && (
          <li className="px-2 pt-2 pb-1 text-[10px] uppercase tracking-wider font-medium text-ok-text" aria-hidden="true">Tests ({tests.length})</li>
        )}
        {tests.map(u => row(u, true))}
      </ul>
    </div>,
    document.body,
  )
}
