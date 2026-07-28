import { useState } from 'react'
import Clickable from './Clickable'
import { Gamepad2, X } from 'lucide-react'
import PixelCanvas, { type SlotData } from './PixelCanvas'
import type { ProjectRun } from '../types'

import { i18nT } from '../i18n/t'
function slotsFromRun(run: ProjectRun): SlotData[] {
  const mapped: SlotData[] = []
  for (const t of run.task_details || []) {
    if (mapped.length >= 7) break
    if (t.status === 'pending') mapped.push({ state: 'empty', label: (t.title || '').slice(0, 20) })
    else if (t.status === 'in_progress') mapped.push({ state: 'typing', label: (t.title || '').slice(0, 20) })
    else if (t.status === 'reviewing') mapped.push({ state: 'typing', label: (t.title || '').slice(0, 20) })
  }
  while (mapped.length < 7) mapped.push({ state: 'empty' })
  return mapped
}

export default function PixelCanvasWidget({ run }: { run: ProjectRun }) {
  const [open, setOpen] = useState(false)
  const slots = slotsFromRun(run)
  const active = slots.filter(s => s.state !== 'empty').length
  const name = run.name || run.spec_name || 'Project'

  return (
    <>
      <button className="relative px-3 py-1.5 rounded-lg text-[13px] font-bold cursor-pointer border-none transition-all hover:scale-105" style={{ background: 'var(--card)', border: '2px solid var(--border)' }} onClick={() => setOpen(true)} title={i18nT('components.pixelCanvasWidget.open_workspace_animation')}>
        <span className="text-lg"><Gamepad2 className="lucide-inline" /></span>
        {active > 0 && <span className="absolute -top-1.5 -right-1.5 flex h-4 min-w-4 items-center justify-center rounded-full text-[10px] font-bold text-accent-fg" style={{ background: 'var(--accent)' }}>{active}</span>}
      </button>
      {open && (
        <Clickable className="fixed inset-0 z-[100] flex items-center justify-center" style={{ background: 'rgba(0,0,0,.4)' }} onClick={() => setOpen(false)}>
          {/* Stop clicks/keys inside the modal from bubbling to the backdrop's
              close handler — the dialog container itself is not interactive. */}
          {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
          <div role="dialog" aria-modal="true" aria-label={`${name} — Workspace`} className="w-full max-w-2xl mx-4 rounded-xl shadow-lg overflow-hidden" style={{ background: 'var(--card)', border: '1px solid var(--border)' }} onClick={e => e.stopPropagation()} onKeyDown={e => e.stopPropagation()}>
            <div className="flex items-center justify-between px-4 py-3" style={{ borderBottom: '1px solid var(--border)' }}>
              <span className="text-sm font-bold" style={{ color: 'var(--text-strong)' }}>{name} {i18nT('components.pixelCanvasWidget.workspace')}</span>
              <div className="flex items-center gap-2">
                <span className="text-[12px]" style={{ color: 'var(--muted)' }}>{active} {i18nT('components.pixelCanvasWidget.agent')}{active !== 1 ? 's' : ''}</span>
                <button aria-label={i18nT('components.pixelCanvasWidget.close')} className="text-lg cursor-pointer bg-transparent border-none font-body" style={{ color: 'var(--muted)' }} onClick={() => setOpen(false)}><X className="lucide-inline" /></button>
              </div>
            </div>
            <div className="p-4">
              <PixelCanvas slots={slots} />
            </div>
          </div>
        </Clickable>
      )}
    </>
  )
}
