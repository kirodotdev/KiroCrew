// ProjectPicker — one clean field + dropdown panel backed by GET /browse.
// Shows recent projects (on the initial, empty-path browse) and a navigable
// folder browser with a "Use this folder" action. Raw path typing is a
// collapsed fallback, not the default (system-picker-style selection).
import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Folder, Clock, ArrowUp, Check, ChevronDown, ChevronUp, ChevronRight } from 'lucide-react'
import { specApi, type BrowseEntry } from '../api'
import { ACCENT, Btn } from './shared'
import { Input } from '../../../components/ui'
import Clickable from '../../../components/Clickable'

import { i18nT } from '../../../i18n/t'
import { PICKER_CSS } from '../inlineStyles'
const base = (p: string) => p.split('/').filter(Boolean).pop() || p

export default function ProjectPicker({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const [open, setOpen] = useState(false)
  const [recents, setRecents] = useState<string[]>([])
  const [cwd, setCwd] = useState('')
  const [parent, setParent] = useState('')
  const [dirs, setDirs] = useState<BrowseEntry[]>([])
  const [manual, setManual] = useState(false)
  const [atStart, setAtStart] = useState(true)

  // Prime the recents list once, through React Query (repo `use-react-query`
  // rule). Later navigation still updates `recents` locally, because each
  // browse response carries a fresh list for the folder being viewed.
  const recentsQuery = useQuery({
    queryKey: ['spec-builder', 'browse', ''],
    queryFn: ({ signal }) => specApi.browse('', signal),
  })
  useEffect(() => {
    if (recentsQuery.data) setRecents(recentsQuery.data.recents || [])
  }, [recentsQuery.data])

  // Navigation goes through a PATH-KEYED query rather than a manual fetch: with
  // the manual version, clicking quickly through folders could let an earlier
  // response land last and repaint the listing of a folder the user had already
  // left. React Query keys the result by path and discards superseded requests.
  const [navPath, setNavPath] = useState<string | null>(null)
  const navQuery = useQuery({
    queryKey: ['spec-builder', 'browse', navPath ?? ''],
    queryFn: ({ signal }) => specApi.browse(navPath ?? '', signal),
    enabled: navPath !== null,
  })
  useEffect(() => {
    const d = navQuery.data
    if (!d || navPath === null) return
    setCwd(d.path); setParent(d.parent); setDirs(d.dirs || [])
    if (d.recents) setRecents(d.recents)
  }, [navQuery.data, navPath])
  const loading = navQuery.isFetching

  const browse = (path: string, fromStart?: boolean) => {
    setNavPath(path || '')
    setAtStart(!!fromStart)
    setOpen(true)
  }

  const pick = (path: string) => { onChange(path); setOpen(false) }
  const canGoUp = !!parent && parent !== cwd

  const rowCls = 'sb-row flex gap-2.5 items-center px-4 py-2.5 text-[13px] cursor-pointer text-text focus-ring'
  const sectionLabel = (t: string) => (
    <div className="px-4 pt-2 pb-1 text-[11px] font-bold uppercase text-muted" style={{ letterSpacing: '.06em' }}>{t}</div>
  )

  return (
    <div className="mb-6 max-w-[680px]">
      <style>{PICKER_CSS}</style>

      {/* Field — looks like an input, shows the selection */}
      <Clickable
        className="sb-field flex items-center gap-2.5 px-3.5 py-3 rounded-lg bg-bg cursor-pointer focus-ring"
        style={{ border: '1px solid ' + (value ? 'color-mix(in srgb, var(--accent) 50%, transparent)' : 'var(--border)') }}
        onClick={() => (open ? setOpen(false) : browse(value || '', !value))}
        aria-expanded={open}
        aria-label={value ? i18nT('apps.specBuilder.components.projectPicker.project_folder_change', { path: value }) : i18nT('apps.specBuilder.components.projectPicker.choose_a_project_folder_aria')}
      >
        <Folder className="lucide-inline text-accent shrink-0" />
        {value ? (
          <>
            <span className="text-[13px] font-semibold text-text whitespace-nowrap">{base(value)}</span>
            <span className="text-[12px] text-muted overflow-hidden text-ellipsis whitespace-nowrap flex-1" style={{ direction: 'rtl', textAlign: 'left' }}>{value}</span>
            <span className="text-[12px] font-semibold whitespace-nowrap" style={{ color: ACCENT }}>{i18nT('apps.specBuilder.components.projectPicker.change')}</span>
          </>
        ) : (
          <>
            <span className="text-[13px] text-muted flex-1">{i18nT('apps.specBuilder.components.projectPicker.choose_a_project_folder')}</span>
            {open ? <ChevronUp className="lucide-inline text-accent" /> : <ChevronDown className="lucide-inline text-accent" />}
          </>
        )}
      </Clickable>

      {/* Panel — recents (at start) + folder navigation */}
      {open && (
        <div className="mt-2 border border-border rounded-lg bg-bg max-h-[320px] overflow-y-auto shadow-xl">
          <div className="flex items-center gap-2.5 px-3.5 py-2.5 border-b border-border sticky top-0 bg-bg z-[1]">
            <Btn
              onClick={() => { if (canGoUp) browse(parent) }}
              disabled={!canGoUp}
              ariaLabel={i18nT('apps.specBuilder.components.projectPicker.go_to_parent_folder')}
              label={<><ArrowUp className="lucide-inline" /> {i18nT('apps.specBuilder.components.projectPicker.up')}</>}
            />
            <span className="text-[12px] text-text/80 overflow-hidden text-ellipsis whitespace-nowrap flex-1" style={{ direction: 'rtl', textAlign: 'left' }} aria-live="polite">
              {loading ? i18nT('apps.specBuilder.components.projectPicker.loading') : cwd}
            </span>
            <Btn
              primary
              // Disabled while a browse is in flight: `cwd` still holds the
              // PREVIOUS folder until the response lands, so clicking mid-load
              // selected the folder the user had just navigated out of — and the
              // agent would then edit the wrong project.
              disabled={loading || !cwd}
              onClick={() => pick(cwd)}
              ariaLabel={i18nT('apps.specBuilder.components.projectPicker.use_this_folder_path', { path: cwd })}
              label={<><Check className="lucide-inline" /> {i18nT('apps.specBuilder.components.projectPicker.use_this_folder')}</>}
            />
          </div>

          {atStart && recents.length > 0 && (
            <>
              {sectionLabel(i18nT('apps.specBuilder.components.projectPicker.recent_projects'))}
              {recents.slice(0, 6).map((p) => (
                <Clickable key={p} className={rowCls} onClick={() => pick(p)} aria-label={i18nT('apps.specBuilder.components.projectPicker.use_recent_project', { path: p })}>
                  <Clock className="lucide-inline text-muted" />
                  <span className="font-semibold">{base(p)}</span>
                  <span className="text-[11px] text-muted overflow-hidden text-ellipsis whitespace-nowrap flex-1" style={{ direction: 'rtl', textAlign: 'left' }}>{p}</span>
                </Clickable>
              ))}
              {sectionLabel(i18nT('apps.specBuilder.components.projectPicker.browse'))}
            </>
          )}

          {dirs.length === 0 && !loading && (
            <div className="px-4 py-3.5 text-[12px] text-muted">{i18nT('apps.specBuilder.components.projectPicker.no_subfolders_here_use_use_this_folder_above_to')}</div>
          )}
          {dirs.map((d) => (
            <Clickable key={d.path} className={rowCls} onClick={() => browse(d.path)} aria-label={i18nT('apps.specBuilder.components.projectPicker.open_folder', { name: d.name })}>
              <Folder className="lucide-inline text-muted" />
              <span className="flex-1">{d.name}</span>
              <ChevronRight className="lucide-inline text-muted" />
            </Clickable>
          ))}

          <Clickable
            onClick={() => setManual(!manual)}
            className="px-4 py-2.5 text-[12px] text-muted cursor-pointer border-t border-border underline focus-ring"
            aria-expanded={manual}
          >
            {manual ? i18nT('apps.specBuilder.components.projectPicker.hide_path_input') : i18nT('apps.specBuilder.components.projectPicker.type_a_path_instead')}
          </Clickable>
        </div>
      )}

      {manual && (
        <Input
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={i18nT('apps.specBuilder.components.projectPicker.home_you_projects_my_app')}
          aria-label={i18nT('apps.specBuilder.components.projectPicker.project_folder_path')}
          className="mt-2 w-full"
        />
      )}
    </div>
  )
}
