// Agent Worlds — Standalone KiroCrew App
// A pixel art office scene showing agents working at desks.
// Uses the host's React and app-sdk via window.__kirocrew_modules.

const React = window.__kirocrew_modules.react
const { useAppApi, useAppEvents } = window.__kirocrew_modules['@kirocrew/app-sdk']
const { Sparkles, RefreshCw } = window.__kirocrew_modules['lucide-react']

const { useState, useEffect, useRef, useCallback, createElement: h } = React

// ── Constants ──
const W = 440, H = 300, S = 3
const COLORS = ['#e74c3c', '#3498db', '#f39c12', '#9b59b6', '#1abc9c', '#e67e22', '#2ecc71', '#e84393']
const DESK_POS = [
  { x: 40, y: 120 }, { x: 120, y: 120 }, { x: 200, y: 120 }, { x: 280, y: 120 },
  { x: 40, y: 200 }, { x: 120, y: 200 }, { x: 200, y: 200 }, { x: 280, y: 200 },
]

// ── Pixel drawing helper ──
function d(ctx, x, y, w, h, color) {
  ctx.fillStyle = color
  ctx.fillRect(x * S, y * S, w * S, h * S)
}

// ── Scene renderer ──
function drawScene(ctx, agents, tick) {
  // Background
  d(ctx, 0, 0, W, 80, '#1a1209')
  for (let i = 0; i < W; i += 16) {
    for (let j = 80; j < H; j += 16) {
      d(ctx, i, j, 16, 16, ((i / 16 + j / 16) & 1) ? '#33261a' : '#2a1f14')
    }
  }
  d(ctx, 0, 78, W, 3, '#4a3520')

  // Title
  ctx.fillStyle = '#f90'
  ctx.font = `bold ${7 * S}px monospace`
  ctx.fillText('Agent Office', (W / 2 - 30) * S, 35 * S)
  d(ctx, W / 2 - 32, 37, 64, 1, '#f90')

  // Window
  d(ctx, 59, 9, 52, 42, '#666')
  d(ctx, 60, 10, 50, 40, '#2a4a6a')
  d(ctx, 85, 10, 1, 40, '#666')
  d(ctx, 60, 30, 50, 1, '#666')
  // Stars
  for (let i = 0; i < 5; i++) {
    const sx = 63 + ((i * 11 + tick * 0.01) % 44)
    const sy = 13 + (i * 7) % 34
    if (Math.sin(tick * 0.04 + i * 2.5) > 0.3) d(ctx, sx, sy, 1, 1, '#fff')
  }

  // Clock
  const cx = 170, cy = 22
  d(ctx, cx - 8, cy - 8, 16, 16, '#444')
  d(ctx, cx - 7, cy - 7, 14, 14, '#222')
  const now = new Date()
  const ha = ((now.getHours() % 12) / 12 + now.getMinutes() / 720) * Math.PI * 2 - Math.PI / 2
  const ma = (now.getMinutes() / 60) * Math.PI * 2 - Math.PI / 2
  ctx.strokeStyle = '#f90'; ctx.lineWidth = S
  ctx.beginPath(); ctx.moveTo(cx * S, cy * S); ctx.lineTo((cx + Math.cos(ha) * 3.5) * S, (cy + Math.sin(ha) * 3.5) * S); ctx.stroke()
  ctx.strokeStyle = '#ccc'
  ctx.beginPath(); ctx.moveTo(cx * S, cy * S); ctx.lineTo((cx + Math.cos(ma) * 5) * S, (cy + Math.sin(ma) * 5) * S); ctx.stroke()

  // Desks
  DESK_POS.forEach((pos, i) => {
    const occupied = i < agents.length
    const accent = COLORS[i % COLORS.length]
    // Cubicle walls
    d(ctx, pos.x - 2, pos.y - 2, 1, 32, '#4a4a4a')
    d(ctx, pos.x - 2, pos.y - 2, 34, 1, '#4a4a4a')
    d(ctx, pos.x + 31, pos.y - 2, 1, 32, '#4a4a4a')
    // Desk surface
    d(ctx, pos.x, pos.y + 16, 28, 3, '#7a5c47')
    d(ctx, pos.x, pos.y + 15, 28, 1, accent)
    // Legs
    d(ctx, pos.x + 2, pos.y + 19, 2, 10, '#5c4033')
    d(ctx, pos.x + 24, pos.y + 19, 2, 10, '#5c4033')
    // Monitor
    d(ctx, pos.x + 9, pos.y + 4, 10, 10, '#333')
    d(ctx, pos.x + 10, pos.y + 5, 8, 8, occupied ? '#0a2a0a' : '#1a1a1a')
    d(ctx, pos.x + 13, pos.y + 14, 2, 1, '#333')
    // Screen text
    if (occupied) {
      for (let l = 0; l < 4; l++) {
        const lw = 2 + ((tick + l * 7) % 5)
        d(ctx, pos.x + 11, pos.y + 6 + l * 1.8, lw, 0.8, '#33ff33')
      }
      if ((tick >> 3) & 1) d(ctx, pos.x + 11 + ((tick >> 2) % 5), pos.y + 6 + ((tick >> 4) % 4) * 1.8, 1, 1, '#33ff33')
    }
    // Chair
    d(ctx, pos.x + 10, pos.y + 22, 8, 3, '#3a2a1a')
    // Nameplate
    ctx.fillStyle = occupied ? '#fff' : '#555'
    ctx.font = `${4 * S}px monospace`
    const label = occupied ? agents[i].name.slice(0, 8) : 'empty'
    ctx.fillText(label, (pos.x + 5) * S, (pos.y + 0) * S)
  })

  // Agents
  agents.slice(0, 8).forEach((agent, i) => {
    const pos = DESK_POS[i]
    const color = COLORS[i % COLORS.length]
    const bx = pos.x + 10, by = pos.y + 20
    const bob = agent.running ? (Math.sin(tick * 0.08 + i) | 0) : 0

    // Shadow
    ctx.fillStyle = 'rgba(0,0,0,0.12)'
    ctx.fillRect((bx + 1) * S, (by + 10) * S, 6 * S, 2 * S)
    // Body
    d(ctx, bx, by + bob, 8, 8, color)
    // Head
    d(ctx, bx + 1, by - 6 + bob, 6, 6, '#fdd')
    // Hair
    d(ctx, bx + 1, by - 7 + bob, 6, 1, '#333')
    // Eyes
    const blink = (tick % 120) < 3
    if (!blink) {
      d(ctx, bx + 3, by - 4 + bob, 1, 1, '#333')
      d(ctx, bx + 5, by - 4 + bob, 1, 1, '#333')
    }
    // Smile when active
    if (agent.running) d(ctx, bx + 3, by - 2 + bob, 2, 0.5, '#c88')
    // Legs
    d(ctx, bx + 1, by + 8, 2, 3, color)
    d(ctx, bx + 5, by + 8, 2, 3, color)
    // Typing arms
    if (agent.running) {
      const armBob = (tick >> 2) & 1
      d(ctx, bx - 1, by + 2 + bob + armBob, 1, 3, color)
      d(ctx, bx + 8, by + 2 + bob + (1 - armBob), 1, 3, color)
    }
    // Status badge
    ctx.fillStyle = agent.running ? '#4f4' : '#888'
    ctx.font = `${3.5 * S}px monospace`
    ctx.fillText(agent.running ? 'active' : 'idle', (bx - 1) * S, (by - 10 + bob) * S)
    // Kind emoji
    const emoji = agent.kind === 'cron' ? '⏰' : agent.kind === 'spawn' ? '🔀' : '💬'
    ctx.font = `${2 * S}px monospace`
    ctx.fillText(emoji, (bx + 8) * S, (by - 5 + bob) * S)
  })

  // Agent counter
  ctx.fillStyle = '#999'
  ctx.font = `${4 * S}px monospace`
  ctx.fillText(`${agents.length}/8 agents`, 4 * S, (H - 4) * S)
}

// ── Main App Component ──
function AgentWorldsApp() {
  const api = useAppApi()
  const canvasRef = useRef(null)
  const tickRef = useRef(0)
  const agentsRef = useRef([])
  const [agentCount, setAgentCount] = useState(0)
  const animRef = useRef(null)

  const fetchAgents = useCallback(async () => {
    try {
      // Fetch from multiple sources like the built-in useAgentSync
      const sources = []

      // Chat slots (via status endpoint which includes slot info)
      try {
        const status = await api.get('/api/status')
        if (status.slots) {
          status.slots.forEach(sl => {
            sources.push({
              id: 'slot-' + sl.key,
              name: (sl.title || sl.key || '').slice(0, 10),
              kind: 'slot',
              running: sl.running,
            })
          })
        }
      } catch {}

      // Cron jobs
      try {
        const crons = await api.get('/api/crons')
        if (Array.isArray(crons)) {
          crons.filter(c => c.enabled).slice(0, 3).forEach(cr => {
            sources.push({
              id: 'cron-' + cr.id,
              name: (cr.name || cr.id || '').slice(0, 10),
              kind: 'cron',
              running: cr.last_status === 'running',
            })
          })
        }
      } catch {}

      // Subagents
      try {
        const spawns = await api.get('/api/spawn')
        if (Array.isArray(spawns)) {
          spawns.filter(s => !s.done).slice(0, 3).forEach(sp => {
            sources.push({
              id: 'spawn-' + sp.id,
              name: (sp.task || '').slice(0, 8),
              kind: 'spawn',
              running: !sp.done,
            })
          })
        }
      } catch {}

      agentsRef.current = sources.slice(0, 8)
      setAgentCount(sources.length)
    } catch {}
  }, [api])

  useEffect(() => { fetchAgents() }, [fetchAgents])
  useEffect(() => {
    const iv = setInterval(fetchAgents, 5000)
    return () => clearInterval(iv)
  }, [fetchAgents])

  useAppEvents('agent:status', fetchAgents)

  // Animation loop
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    canvas.width = W * S
    canvas.height = H * S
    const ctx = canvas.getContext('2d')

    const loop = () => {
      tickRef.current++
      drawScene(ctx, agentsRef.current, tickRef.current)
      animRef.current = requestAnimationFrame(loop)
    }
    animRef.current = requestAnimationFrame(loop)
    return () => { if (animRef.current) cancelAnimationFrame(animRef.current) }
  }, [])

  return h('div', { style: { display: 'flex', flexDirection: 'column', height: '100%' } },
    // Header
    h('div', { className: 'px-5 pt-4 pb-2 flex items-center justify-between' },
      h('div', null,
        h('div', { className: 'text-lg font-semibold text-text-strong flex items-center gap-2' },
          h(Sparkles, { size: 18 }), 'Agent Worlds'),
        h('div', { className: 'text-sm text-muted' },
          `${agentCount} agent${agentCount !== 1 ? 's' : ''} present`),
      ),
      h('button', {
        className: 'px-2.5 py-1 rounded-md border border-border bg-transparent text-muted hover:text-text text-[13px] cursor-pointer transition-all inline-flex items-center gap-1.5',
        onClick: fetchAgents,
      }, h(RefreshCw, { size: 14 }), 'Refresh'),
    ),
    // Canvas
    h('div', { style: { flex: 1, display: 'flex', justifyContent: 'center', alignItems: 'start', padding: '0 20px 20px', minHeight: 0 } },
      h('canvas', {
        ref: canvasRef,
        style: { width: W * S + 'px', height: H * S + 'px', imageRendering: 'pixelated', borderRadius: 8, border: '1px solid var(--border, #333)' },
      }),
    ),
    // Footer
    h('div', { className: 'text-[12px] text-muted/60 text-center pb-3' },
      'Standalone federated app — loaded dynamically via AppHost'),
  )
}

export default AgentWorldsApp
