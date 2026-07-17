import { useState, useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import { Plug } from 'lucide-react'
import { api } from '../../api/client'

export default function McpInfoButton({ agent }: { agent?: string }) {
  const [open, setOpen] = useState(false)
  const [servers, setServers] = useState<{ name: string; enabled?: boolean }[]>([])
  const btnRef = useRef<HTMLButtonElement>(null)
  const popoverRef = useRef<HTMLDivElement>(null)
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    if (open) api.mcpActive(agent || undefined).then(setServers).catch(() => {})
  }, [open, agent])

  useEffect(() => {
    return () => { if (closeTimer.current) clearTimeout(closeTimer.current) }
  }, [])

  useEffect(() => {
    if (!open) return
    const onPointerDown = (e: PointerEvent) => {
      if (btnRef.current?.contains(e.target as Node) || popoverRef.current?.contains(e.target as Node)) return
      setOpen(false)
    }
    const onKeyDown = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => { document.removeEventListener('pointerdown', onPointerDown); document.removeEventListener('keydown', onKeyDown) }
  }, [open])

  const handleEnter = () => {
    if (closeTimer.current) clearTimeout(closeTimer.current)
    setOpen(true)
  }
  const handleLeave = () => {
    closeTimer.current = setTimeout(() => setOpen(false), 250)
  }

  return (
    <>
      <button ref={btnRef} onClick={() => setOpen(o => !o)} onMouseEnter={handleEnter} onMouseLeave={handleLeave} className="shrink-0 w-5 h-5 rounded flex items-center justify-center text-muted hover:text-text hover:bg-bg-hover transition-all" title="Session MCP servers"><Plug size={15} /></button>
      {open && btnRef.current && createPortal(
        // The mouse handlers only keep this hover popover open while the pointer
        // is over it; the popover is presentational (its trigger button owns all
        // interaction), so it is not a keyboard/interactive target itself.
        // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
        <div ref={popoverRef} role="tooltip" onMouseEnter={handleEnter} onMouseLeave={handleLeave} className="fixed z-[9999] bg-card border border-border rounded-lg shadow-lg p-3 min-w-[240px] max-w-[300px] max-h-[320px] overflow-y-auto" style={(() => { const r = btnRef.current!.getBoundingClientRect(); const top = r.bottom + 4 + 320 > window.innerHeight ? r.top - 320 - 4 : r.bottom + 4; const left = Math.max(8, Math.min(r.left, window.innerWidth - 308)); return { top, left } })()}>
          <div className="text-[12px] uppercase tracking-wider text-muted font-semibold mb-2">MCP Servers ({servers.filter(s => s.enabled !== false).length}/{servers.length})</div>
          {servers.length === 0 ? <div className="text-muted text-[13px] italic">None loaded</div> : servers.map(s => (
            <div key={s.name} className={`flex items-center gap-2 py-1 text-[13px] ${s.enabled === false ? 'opacity-40' : ''}`}>
              <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${s.enabled === false ? 'bg-muted' : 'bg-ok'}`} />
              <code className="text-text">{s.name}</code>
              {s.enabled === false && <span className="text-[11px] text-muted">disabled</span>}
            </div>
          ))}
          <div className="mt-2 pt-2 border-t border-border text-[11px] text-muted leading-snug">
            {agent && agent !== 'kirocrew'
              ? `Agent "${agent}" loads only its own MCP servers.`
              : 'kirocrew loads all configured MCP servers — manage from Overview → MCP tab.'}
          </div>
        </div>,
        document.body
      )}
    </>
  )
}
