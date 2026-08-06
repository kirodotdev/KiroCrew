import { useState, useEffect, useMemo, useRef, useCallback } from 'react'
import { ClipboardList, ScrollText } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useAppDispatch, useAppSelector } from '../../store'
import { setPendingInput, switchSlot } from '../../store/chatSlice'
import { api } from '../../api/client'
import { Card, CardTitle, Btn, Badge, SearchInput } from '../../components/ui'
import InfoTip from '../../components/InfoTip'
import { useProvider } from '../../providers'

import { i18nT } from '../../i18n/t'
interface Prompt {
  name: string
  fullName: string
  description: string
  path: string
  package: string
  source: string
}

function SlotPicker({ prompt, onClose }: { prompt: Prompt; onClose: () => void }) {
  const slots = useAppSelector(s => s.dashboard.slots)
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const ref = useRef<HTMLDivElement>(null)
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose

  useEffect(() => {
    const handler = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) onCloseRef.current() }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  const send = (slotKey?: string) => {
    dispatch(setPendingInput(`@${prompt.fullName}`))
    if (slotKey) {
      dispatch(switchSlot(slotKey))
      navigate('/chat?autoSend=1')
    } else {
      navigate('/chat?autoSend=1&newSession=1')
    }
    onClose()
  }

  return (
    <div ref={ref} className="absolute right-0 top-full mt-1 z-50 bg-bg-elevated border border-border rounded-lg shadow-lg min-w-[220px] max-h-[240px] overflow-y-auto py-1 animate-slide-in-left">
      <div className="px-3 py-1.5 text-[11px] text-muted uppercase tracking-wider font-semibold">{i18nT('pages.overview.promptsTab.send_to')}</div>
      <div role="button" tabIndex={0} className="px-2 py-1 mx-1 rounded-md cursor-pointer text-[13px] text-accent font-medium hover:bg-bg-hover transition-colors" onClick={() => send()} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); send() } }}>{i18nT('pages.overview.promptsTab.new_chat')}</div>
      {slots.map(s => (
        <div key={s.key} role="button" tabIndex={0} className="px-2 py-1.5 mx-1 rounded-md cursor-pointer text-[13px] hover:bg-bg-hover transition-colors flex items-center gap-2" onClick={() => send(s.key)} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); send(s.key) } }}>
          {s.running && <span className="w-1.5 h-1.5 rounded-full bg-green-400 shrink-0" />}
          <span className="truncate">{s.title && s.title !== s.key ? s.title : s.agent || s.key}</span>
        </div>
      ))}
      {slots.length === 0 && <div className="px-3 py-1.5 text-[12px] text-muted italic">{i18nT('pages.overview.promptsTab.no_active_chats')}</div>}
    </div>
  )
}

export default function PromptsTab() {
  const provider = useProvider()
  const queryClient = useQueryClient()
  const [activeKey, setActiveKey] = useState('')
  const [content, setContent] = useState('')
  const [filter, setFilter] = useState('')
  const [pickerPrompt, setPickerPrompt] = useState<Prompt | null>(null)

  const { data: prompts = [], isLoading: loading, error } = useQuery<Prompt[]>({
    queryKey: ['prompts'],
    queryFn: api.prompts,
  })

  const promptKey = (p: Prompt) => `${p.source}:${p.package}/${p.name}`
  const pendingRef = useRef('')

  const toggle = async (p: Prompt) => {
    const key = promptKey(p)
    if (activeKey === key) { setActiveKey(''); return }
    pendingRef.current = key
    const detailKey = p.package ? `${p.package}/${p.name}` : p.name
    try {
      const d = await queryClient.fetchQuery({
        queryKey: ['prompts', detailKey],
        queryFn: () => api.promptDetail(detailKey),
      })
      if (pendingRef.current !== key) return // stale response
      setActiveKey(key); setContent(d.content || '')
    } catch { if (pendingRef.current === key) { setActiveKey(key); setContent('(failed to load)') } }
  }

  const sf = useCallback(
    (p: Prompt) =>
      !filter ||
      (p.name + ' ' + p.fullName + ' ' + p.description + ' ' + p.package)
        .toLowerCase()
        .includes(filter.toLowerCase()),
    [filter],
  )

  const packagePrompts = useMemo(() => prompts.filter(p => p.source === 'package'), [prompts])
  const userPrompts = useMemo(() => prompts.filter(p => p.source !== 'package'), [prompts])
  const filteredUser = useMemo(() => userPrompts.filter(sf), [userPrompts, sf])
  const filteredPackage = useMemo(() => packagePrompts.filter(sf), [packagePrompts, sf])

  const grouped = useMemo(() => {
    const g: Record<string, Prompt[]> = {}
    for (const p of filteredPackage) (g[p.package || 'unknown'] ||= []).push(p)
    return Object.entries(g).sort(([a], [b]) => a.localeCompare(b))
  }, [filteredPackage])

  const renderPrompt = (p: Prompt) => {
    const pk = promptKey(p)
    const isPickerActive = pickerPrompt && promptKey(pickerPrompt) === pk
    return (
    <div key={pk} className="border-b border-border last:border-b-0">
      <div role="button" tabIndex={0} aria-expanded={activeKey === pk} className="flex items-start gap-2.5 px-3 py-2.5 cursor-pointer hover:bg-bg-hover transition-colors" onClick={() => toggle(p)} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(p) } }}>
        <span className="text-muted text-[13px] mt-0.5 w-4 shrink-0">{activeKey === pk ? '▼' : '▶'}</span>
        <span className="font-semibold text-text-strong font-mono text-[13px] whitespace-nowrap">@{p.fullName}</span>
        {p.source === 'package' ? <Badge variant="ok">{i18nT('pages.overview.promptsTab.package')}</Badge>
         : <Badge variant="warn">{p.source}</Badge>}
        <span className="text-muted text-[13px] leading-relaxed truncate">{p.description}</span>
      </div>
      {activeKey === pk && <div className="px-3 pb-3">
        <div className="flex gap-1.5 mb-2 items-center">
          <code className="text-muted text-[12px] truncate max-w-[400px]">{p.path}</code>
          <div className="relative">
            <Btn onClick={() => setPickerPrompt(isPickerActive ? null : p)}><ClipboardList className="lucide-inline" /> {i18nT('pages.overview.promptsTab.use_in_chat')}</Btn>
            {isPickerActive && <SlotPicker prompt={p} onClose={() => setPickerPrompt(null)} />}
          </div>
        </div>
        <pre className="bg-bg-elevated border border-border rounded-md p-3 font-mono text-[13px] text-text overflow-x-auto max-h-[400px] overflow-y-auto whitespace-pre-wrap leading-normal">{content}</pre>
      </div>}
    </div>
  )}

  return (<>
    <Card>
      <CardTitle><ScrollText className="lucide-inline" /> {i18nT('pages.overview.promptsTab.prompts')} <InfoTip text={i18nT('pages.overview.promptsTab.saved_prompts_from', { registry: provider.labels.pluginRegistryName.toLowerCase() })} /></CardTitle>
      <p className="text-muted text-[13px] mb-3 leading-relaxed">
        {i18nT('pages.overview.promptsTab.invoke_in_chat')} <code className="text-[12px]">{i18nT('pages.overview.promptsTab.agent_sop_name')}</code> {i18nT('pages.overview.promptsTab.or')} <code className="text-[12px]">{i18nT('pages.overview.promptsTab.prompts_get_name')}</code>{i18nT('pages.overview.promptsTab.prompts_are_loaded_on_demand_they_don_t_consume')}
      </p>
      {prompts.length > 0 && (
        <div className="mb-3 px-3">
          <SearchInput placeholder={i18nT('pages.overview.promptsTab.filter_prompts')} value={filter} onChange={e => setFilter(e.target.value)} />
        </div>
      )}
      {prompts.length === 0 && !loading && !error && <p className="text-muted italic text-sm px-3 py-4">{i18nT('pages.overview.promptsTab.no_prompts_found_install_a')} {provider.labels.pluginRegistryName.toLowerCase().replace(/s$/, '')} {i18nT('pages.overview.promptsTab.with_prompts_or_create_prompts_in_kiro_prompts')}</p>}
      {loading && <p className="text-muted italic text-sm px-3 py-4">{i18nT('pages.overview.promptsTab.loading_prompts')}</p>}
      {error && <p className="text-red-400 text-sm px-3 py-4">{error.message || i18nT('pages.overview.promptsTab.failed_to_load_prompts')}</p>}
    </Card>
    {filteredUser.length > 0 && <Card>
      <CardTitle>{filter ? i18nT('pages.overview.promptsTab.user_prompts_filtered_count', { count: filteredUser.length, total: userPrompts.length }) : i18nT('pages.overview.promptsTab.user_prompts_count', { count: userPrompts.length })}</CardTitle>
      {filteredUser.map(renderPrompt)}
    </Card>}
    {grouped.length > 0 && <Card>
      <CardTitle>{provider.labels.pluginRegistryName} {filter ? i18nT('pages.overview.promptsTab.prompts_filtered_count', { count: filteredPackage.length, total: packagePrompts.length }) : i18nT('pages.overview.promptsTab.prompts_count', { count: packagePrompts.length })}</CardTitle>
      {grouped.map(([pkg, items]) => (
          <div key={pkg} className="mb-3">
            <div className="text-muted text-[12px] uppercase tracking-[.06em] font-semibold px-3 py-1.5 bg-bg-hover rounded-t-md">{pkg}</div>
            {items.map(renderPrompt)}
          </div>
      ))}
    </Card>}
  </>)
}
