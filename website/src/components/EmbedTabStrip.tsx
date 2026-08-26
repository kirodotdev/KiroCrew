import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation } from '@tanstack/react-query'
import { useAppSelector, useAppDispatch } from '../store'
import { createSlot } from '../store/chatSlice'
import { X, Plus } from 'lucide-react'
import { useScrollEdges } from '../hooks/useScrollEdges'
import type { ChatSlot } from '../types'

import { i18nT } from '../i18n/t'
interface Tab { slug: string }

/** Tab state injected by the host embedding plugin onto `window`. */
interface EmbedTabsWindow extends Window {
  __kirocrewTabs?: string[]
  __kirocrewActiveTabIndex?: number
}

const STORAGE_KEY = 'kirocrew-embed-tabs'
const STORAGE_INDEX_KEY = 'kirocrew-embed-active-index'

function loadTabs(activeSlot: string | null): { tabs: Tab[]; index: number } {
  // Priority: sessionStorage > window.__kirocrewTabs > activeSlot > empty
  try {
    const stored = sessionStorage.getItem(STORAGE_KEY)
    const storedIndex = sessionStorage.getItem(STORAGE_INDEX_KEY)
    if (stored) {
      const parsed = JSON.parse(stored) as string[]
      if (parsed.length) return { tabs: parsed.map(s => ({ slug: s })), index: Number(storedIndex) || 0 }
    }
  } catch {}
  const w = window as EmbedTabsWindow
  const injected = w.__kirocrewTabs
  if (injected?.length) return { tabs: injected.map(s => ({ slug: s })), index: w.__kirocrewActiveTabIndex ?? 0 }
  if (activeSlot) return { tabs: [{ slug: activeSlot }], index: 0 }
  return { tabs: [{ slug: '' }], index: 0 }
}

export default function EmbedTabStrip() {
  const navigate = useNavigate()
  const dispatch = useAppDispatch()
  const slots = useAppSelector(s => s.dashboard.slots)
  const activeSlot = useAppSelector(s => s.chat.activeSlot)

  const [tabs, setTabs] = useState<Tab[]>(() => loadTabs(activeSlot).tabs)
  const [activeIndex, setActiveIndex] = useState(() => loadTabs(activeSlot).index)

  const tabsRef = useRef(tabs)
  tabsRef.current = tabs
  const activeIndexRef = useRef(activeIndex)
  activeIndexRef.current = activeIndex

  // On mount, navigate to the URL matching the restored active tab
  const didMountNav = useRef(false)
  useEffect(() => {
    if (didMountNav.current) return
    didMountNav.current = true
    const tab = tabs[activeIndex]
    if (!tab) return
    if (tab.slug) navigate(`/embed/chat/${tab.slug}?sid=${tab.slug}`, { replace: true })
    else navigate('/embed/sessions', { replace: true })
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const createSlotMutation = useMutation({
    mutationFn: () => dispatch(createSlot(undefined)).unwrap(),
    onSuccess: (slot: ChatSlot) => {
      const key = slot?.key
      if (!key) return
      const newTabs = [...tabsRef.current, { slug: key }]
      const newIndex = newTabs.length - 1
      setTabs(newTabs)
      setActiveIndex(newIndex)
      navigate(`/embed/chat/${key}?sid=${key}`)
      persist(newTabs, newIndex)
    },
  })

  // Persist to sessionStorage + notify plugin on every change
  const persist = useCallback((newTabs: Tab[], newIndex: number) => {
    const slugs = newTabs.map(t => t.slug)
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(slugs))
    sessionStorage.setItem(STORAGE_INDEX_KEY, String(newIndex))
    window.dispatchEvent(new CustomEvent('kirocrew-tab-update', {
      detail: { tabs: slugs, activeIndex: newIndex }
    }))
  }, [])

  // Listen for plugin pushing tab state
  useEffect(() => {
    const handler = (e: Event) => {
      const d = (e as CustomEvent).detail
      if (d?.tabs && Array.isArray(d.tabs)) {
        const newTabs = d.tabs.map((s: string) => ({ slug: s }))
        const newIndex = typeof d.activeIndex === 'number' ? d.activeIndex : 0
        setTabs(newTabs)
        setActiveIndex(newIndex)
        persist(newTabs, newIndex)
        const target = newTabs[newIndex]
        if (target?.slug) navigate(`/embed/chat/${target.slug}?sid=${target.slug}`)
        else navigate('/embed/sessions')
      }
    }
    window.addEventListener('kirocrew-tab-state', handler)
    return () => window.removeEventListener('kirocrew-tab-state', handler)
  }, [persist, navigate])

  // When activeSlot changes (user picked a session in a new/empty tab)
  const didHydrate = useRef(false)
  useEffect(() => {
    if (!activeSlot) return
    // Skip the first activeSlot change (Redux hydration from localStorage)
    if (!didHydrate.current) { didHydrate.current = true; return }
    const idx = activeIndexRef.current
    const current = tabsRef.current[idx]
    if (!current || current.slug === activeSlot) return
    // If this slug is already open in another tab, switch to it
    const existingIdx = tabsRef.current.findIndex(t => t.slug === activeSlot)
    if (existingIdx !== -1 && existingIdx !== idx) {
      setActiveIndex(existingIdx)
      if (current.slug === '') {
        const newTabs = tabsRef.current.filter((_, i) => i !== idx)
        const newIndex = existingIdx > idx ? existingIdx - 1 : existingIdx
        setTabs(newTabs)
        setActiveIndex(newIndex)
        persist(newTabs, newIndex)
      } else {
        persist(tabsRef.current, existingIdx)
      }
      return
    }
    // Sessions tab: replace it with the selected chat
    if (current.slug === '') {
      const newTabs = tabsRef.current.map((t, i) => i === idx ? { slug: activeSlot } : t)
      setTabs(newTabs)
      navigate(`/embed/chat/${activeSlot}?sid=${activeSlot}`)
      persist(newTabs, idx)
    }
  }, [activeSlot, persist, navigate])

  // Remove tabs whose slots were deleted (skip if slots not yet hydrated)
  useEffect(() => {
    if (slots.length === 0) return
    const currentTabs = tabsRef.current
    const currentIndex = activeIndexRef.current
    const slotKeys = new Set(slots.map(s => s.key))
    const filtered = currentTabs.filter(t => t.slug === '' || slotKeys.has(t.slug))
    if (filtered.length < currentTabs.length) {
      const newTabs = filtered.length > 0 ? filtered : [{ slug: '' }]
      const newIndex = Math.min(currentIndex, newTabs.length - 1)
      const activeTabRemoved = currentTabs[currentIndex]?.slug && !slotKeys.has(currentTabs[currentIndex].slug)
      setTabs(newTabs)
      setActiveIndex(newIndex)
      persist(newTabs, newIndex)
      if (activeTabRemoved) {
        const target = newTabs[newIndex]
        if (target.slug) navigate(`/embed/chat/${target.slug}?sid=${target.slug}`)
        else navigate('/embed/sessions')
      }
    }
  }, [slots, persist, navigate])

  const selectTab = (index: number) => {
    setActiveIndex(index)
    const tab = tabs[index]
    if (tab.slug) navigate(`/embed/chat/${tab.slug}?sid=${tab.slug}`)
    else navigate('/embed/sessions')
    persist(tabs, index)
  }

  const closeTab = (index: number) => {
    const newTabs = [...tabs]
    newTabs.splice(index, 1)
    if (newTabs.length === 0) newTabs.push({ slug: '' })
    let newIndex = activeIndex
    if (activeIndex > index) newIndex--
    else if (activeIndex >= newTabs.length) newIndex = newTabs.length - 1
    setTabs(newTabs)
    setActiveIndex(newIndex)
    if (activeIndex === index) {
      const tab = newTabs[newIndex]
      if (tab.slug) navigate(`/embed/chat/${tab.slug}?sid=${tab.slug}`)
      else navigate('/embed/sessions')
    }
    persist(newTabs, newIndex)
  }

  const addTab = () => {
    const newTabs = [...tabs, { slug: '' }]
    const newIndex = newTabs.length - 1
    setTabs(newTabs)
    setActiveIndex(newIndex)
    navigate('/embed/sessions')
    persist(newTabs, newIndex)
    setTimeout(() => tabRefs.current[newIndex]?.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' }), 0)
  }

  // --- Drag reorder: tab follows cursor, reorder on drop ---
  const dragRef = useRef<{ index: number; slug: string; startX: number; active: boolean } | null>(null)
  const [dragSlug, setDragSlug] = useState<string | null>(null)
  const [dragOffset, setDragOffset] = useState(0)
  const tabRefs = useRef<(HTMLDivElement | null)[]>([])
  const stripRef = useRef<HTMLDivElement | null>(null)
  const [attachEdges, edges, remeasure] = useScrollEdges<HTMLDivElement>()

  // The hook owns the node's edge measurement; this keeps a plain handle to
  // the same node for the drag auto-scroll, pointer capture, and the wheel
  // translation, which all read stripRef directly.
  const setStrip = useCallback((node: HTMLDivElement | null) => {
    stripRef.current = node
    attachEdges(node)
  }, [attachEdges])

  // Tabs opening, closing, or renaming keep the strip's own box, so neither
  // the ResizeObserver nor a scroll event reports the changed content width —
  // only this remeasure can refresh the cue. `slots` is in the deps because
  // the rendered label comes from redux, not from `tabs`: a session retitled
  // after its first turn (auto-titling) widens the strip while `tabs` stays
  // identity-stable. remeasure drops same-value writes, so the extra churn
  // from unrelated slot updates costs no re-render.
  useEffect(() => { remeasure() }, [tabs, slots, remeasure])

  const onPointerDown = (e: React.PointerEvent, index: number) => {
    if ((e.target as HTMLElement).closest('button')) return
    const strip = stripRef.current
    if (!strip) return

    dragRef.current = {
      index,
      slug: tabs[index].slug || `new-${index}`,
      startX: e.clientX,
      active: false,
    }
    strip.setPointerCapture(e.pointerId)

    // Activate the tab being dragged
    if (index !== activeIndex) {
      setActiveIndex(index)
      const tab = tabs[index]
      if (tab.slug) navigate(`/embed/chat/${tab.slug}?sid=${tab.slug}`)
      else navigate('/embed/sessions')
      persist(tabs, index)
    }
  }

  const onPointerMove = (e: React.PointerEvent) => {
    const d = dragRef.current
    if (!d) return
    const dx = e.clientX - d.startX
    if (!d.active && Math.abs(dx) > 6) {
      d.active = true
      setDragSlug(d.slug)
    }
    if (!d.active) return
    setDragOffset(dx)

    // Auto-scroll when dragging near edges
    const strip = stripRef.current
    if (strip) {
      const rect = strip.getBoundingClientRect()
      const edgeZone = 40
      const prevScroll = strip.scrollLeft
      if (e.clientX < rect.left + edgeZone) {
        strip.scrollLeft -= 8
      } else if (e.clientX > rect.right - edgeZone) {
        strip.scrollLeft += 8
      }
      // Compensate startX so tab stays under cursor
      const scrollDelta = strip.scrollLeft - prevScroll
      d.startX -= scrollDelta
    }
  }

  const onPointerCancel = () => {
    dragRef.current = null
    setDragSlug(null)
    setDragOffset(0)
  }

  const onPointerUp = (e: React.PointerEvent) => {
    const d = dragRef.current
    dragRef.current = null
    if (!d?.active) {
      setDragSlug(null)
      setDragOffset(0)
      return
    }

    // Determine drop position based on pointer location
    const pointerX = e.clientX
    let targetIndex = d.index
    for (let i = 0; i < tabRefs.current.length; i++) {
      const el = tabRefs.current[i]
      if (!el || i === d.index) continue
      const rect = el.getBoundingClientRect()
      const mid = rect.left + rect.width / 2
      if (i < d.index && pointerX < mid) {
        targetIndex = i
        break
      }
      if (i > d.index && pointerX > mid) {
        targetIndex = i
      }
    }

    if (targetIndex !== d.index) {
      setTabs(prev => {
        const next = [...prev]
        const [moved] = next.splice(d.index, 1)
        next.splice(targetIndex, 0, moved)
        return next
      })
      // Adjust activeIndex to follow
      setActiveIndex(prev => {
        if (prev === d.index) return targetIndex
        if (d.index < prev && targetIndex >= prev) return prev - 1
        if (d.index > prev && targetIndex <= prev) return prev + 1
        return prev
      })
    }

    setDragSlug(null)
    setDragOffset(0)
    // Persist after state settles
    setTimeout(() => {
      const currentTabs = tabsRef.current
      const currentIndex = activeIndexRef.current
      persist(currentTabs, currentIndex)
    }, 0)
  }

  const getTitle = (slug: string, _index: number) => {
    if (!slug) return i18nT('components.embedTabStrip.sessions')
    return slots.find(s => s.key === slug)?.title || slug
  }

  // Status dot color per tab
  const unreadSlots = useAppSelector(s => s.dashboard.unreadSlots)

  const getStatus = (slug: string): 'idle' | 'running' | 'unread' | 'permission' | 'question' => {
    if (!slug) return 'idle'
    const slot = slots.find(s => s.key === slug)
    if (!slot) return 'idle'
    if (slot.pending_approval) return 'permission'
    // Above running: a blocking question card leaves the turn parked, so the tab
    // would otherwise pulse "working" while it waits on the user.
    if (slot.needs_input) return 'question'
    if (slot.running) return 'running'
    if (unreadSlots.includes(slug)) return 'unread'
    return 'idle'
  }

  return (
    <div
      className="flex items-center shrink-0 border-b border-border px-1.5 py-1.5"
      style={{ background: 'var(--bg)' }}
    >
      {/* The wrapper exists for the edge cues: absolutely-positioned children
          of the scroller itself would travel with the scrolled content, so the
          fades anchor to this non-scrolling parent. It also owns the flex
          sizing so the scroller keeps filling the row. */}
      <div className="relative min-w-0 flex-1">
        <div
          ref={setStrip}
          className="flex items-center gap-1 overflow-x-auto relative"
          style={{ scrollbarWidth: 'none' }}
          onWheel={e => { if (stripRef.current) stripRef.current.scrollLeft += e.deltaY }}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerCancel}
        >
        {tabs.map((tab, i) => {
          const active = i === activeIndex
          const title = getTitle(tab.slug, i)
          const truncated = title.length > 24 ? title.slice(0, 24) + '…' : title
          const isDragged = dragSlug != null && (tab.slug || `new-${i}`) === dragSlug
          return (
            <div
              key={tab.slug || `new-${i}`}
              ref={el => { tabRefs.current[i] = el }}
              role="tab"
              tabIndex={0}
              aria-selected={active}
              onPointerDown={e => onPointerDown(e, i)}
              onClick={() => { if (!dragRef.current?.active) selectTab(i) }}
              onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); selectTab(i) } }}
              className={`group/tab flex items-center gap-2 pl-2 pr-2 py-1.5 rounded-md cursor-pointer select-none shrink-0 text-xs ${
                active
                  ? 'bg-bg-elevated text-text border border-border'
                  : 'text-muted hover:text-text hover:bg-bg-elevated/50 border border-transparent'
              }`}
              style={isDragged ? {
                transform: `translateX(${dragOffset}px)`,
                zIndex: 50,
                opacity: 0.85,
                transition: 'none',
                cursor: 'grabbing',
              } : dragSlug ? {
                transition: 'transform 200ms ease',
              } : undefined}
            >
              {tab.slug && (() => {
                const status = getStatus(tab.slug)
                const colors = { idle: 'var(--muted)', running: 'var(--accent)', unread: 'var(--ok)', permission: 'var(--warn)', question: 'var(--info)' }
                return (
                  <span
                    className={`shrink-0 w-1.5 h-1.5 rounded-full self-center mr-0.5 ${status === 'running' || status === 'permission' ? 'animate-pulse' : ''}`}
                    style={{ background: colors[status] }}
                  />
                )
              })()}
              <span className="whitespace-nowrap">{truncated}</span>
              <button
                onPointerDown={e => e.stopPropagation()}
                onClick={e => { e.stopPropagation(); closeTab(i) }}
                className={`transition-opacity ${
                  active ? 'opacity-60 hover:opacity-100 hover:text-text' : 'opacity-0 group-hover/tab:opacity-60 group-focus-within/tab:opacity-60 hover:!opacity-100 hover:text-text'
                }`}
                aria-label={i18nT('components.embedTabStrip.close_tab')}
              >
                <X size={11} />
              </button>
            </div>
          )
        })}
        </div>
        {/* Edge cues, same treatment as the sibling strips: this scroller hides
            its scrollbar entirely (scrollbarWidth: none), so a gradient is the
            only signal that tabs continue past the clipped edge. from-bg
            matches the bar's var(--bg) surface. The dragged tab's z-50 stays
            above the cue on purpose — mid-drag the tab is the content being
            placed, not the content being hinted at. */}
        {edges.left && (
          <div aria-hidden="true" data-testid="embed-tab-strip-cue-left" className="pointer-events-none absolute left-0 top-0 bottom-0 w-6 z-10 bg-gradient-to-r from-bg to-transparent" />
        )}
        {edges.right && (
          <div aria-hidden="true" data-testid="embed-tab-strip-cue-right" className="pointer-events-none absolute right-0 top-0 bottom-0 w-6 z-10 bg-gradient-to-l from-bg to-transparent" />
        )}
      </div>
      <button
        onClick={e => {
          if (e.shiftKey) {
            // Shift+click: create new session directly
            createSlotMutation.mutate()
          } else {
            addTab()
          }
        }}
        className="shrink-0 ml-1 p-1.5 rounded-full text-muted hover:text-text hover:bg-bg-elevated/50 transition-colors"
        aria-label={i18nT('components.embedTabStrip.new_tab')}
        title={i18nT('components.embedTabStrip.new_tab_shift_click_for_new_chat')}
      >
        <Plus size={14} />
      </button>
    </div>
  )
}
