/**
 * Mochi - Chat window app entry
 * Combines ChatPanel, SettingsPanel, WatchlistPanel
 */
import React, { useEffect, useRef, useState } from 'react'
import { ChatPanel, PinnedSidePanel } from './ChatPanel'
import { SettingsPanel } from './SettingsPanel'
import { WatchlistPanel } from './WatchlistPanel'

import { api } from '../mochiApi'
import { DEFAULT_PET_NAME, resolvePetName } from '../../builtinPacks'

/** Local re-declaration of PinnedFileEntry (matches src/main/pinnedFilesService.ts) */
interface PinnedFileEntry {
  path: string
  label: string
  pinnedAt: number
  updatedAt?: number
}

export const ChatApp: React.FC = () => {
  const [view, setView] = useState<'chat' | 'settings'>('chat')
  const [watchPanelVisible, setWatchPanelVisible] = useState(false)
  const [pinnedPanelVisible, setPinnedPanelVisible] = useState(false)
  // ADDED (not upstream): the watchlist empty hint names the renameable pet.
  const [petName, setPetName] = useState(DEFAULT_PET_NAME)

  // Pinned files state (managed at app level for panel rendering)
  const [pinnedFiles, setPinnedFiles] = useState<PinnedFileEntry[]>([])
  const [updatedPaths, setUpdatedPaths] = useState<Set<string>>(new Set())
  const [deletedPaths, setDeletedPaths] = useState<Set<string>>(new Set())

  const prevView = useRef(view)

  useEffect(() => {
    api?.onNavigate?.((route: string) => {
      if (route === '/settings') setView('settings')
    })
    const handleNav = (e: Event) => {
      const route = (e as CustomEvent).detail
      if (route === '/settings') setView('settings')
    }
    window.addEventListener('mochi-navigate', handleNav)
    return () => window.removeEventListener('mochi-navigate', handleNav)
  }, [])

  // Read initial language and listen for config updates
  useEffect(() => {
    api?.getMochiConfig?.().then((c: any) => {
      // resolvePetName, not `c.petName`: an empty stored name means "use the
      // active avatar's own name" (settings.py), so reading the raw field left
      // a ghost user being told to "Ask Mochi" to watch a page.
      setPetName(resolvePetName(c))
    })
  }, [])

  // Pinned files: load initial list and subscribe to events
  useEffect(() => {
    api?.getPinnedFiles?.().then((pins: PinnedFileEntry[]) => {
      if (pins) setPinnedFiles(pins)
    })
    const unsubChanged = api?.onPinnedFilesChanged?.((pins: PinnedFileEntry[]) => {
      setPinnedFiles(pins)
      const currentPaths = new Set(pins.map((p: PinnedFileEntry) => p.path))
      setUpdatedPaths(prev => {
        const newSet = new Set(prev)
        for (const p of newSet) { if (!currentPaths.has(p)) newSet.delete(p) }
        return newSet
      })
      setDeletedPaths(prev => {
        const newSet = new Set(prev)
        for (const p of newSet) { if (!currentPaths.has(p)) newSet.delete(p) }
        return newSet
      })
    })
    const unsubUpdated = api?.onPinnedFileUpdated?.((data: { path: string; updatedAt: number }) => {
      setUpdatedPaths(prev => new Set(prev).add(data.path))
    })
    const unsubDeleted = api?.onPinnedFileDeleted?.((data: { path: string }) => {
      setDeletedPaths(prev => new Set(prev).add(data.path))
    })
    return () => { unsubChanged?.(); unsubUpdated?.(); unsubDeleted?.() }
  }, [])

  // Auto-hide panels when leaving chat view, restore when returning
  useEffect(() => {
    if (prevView.current === 'chat' && view !== 'chat') {
      if (watchPanelVisible) api?.toggleWatchPanel?.(false)
      if (pinnedPanelVisible) api?.togglePinnedPanel?.(false)
    } else if (prevView.current !== 'chat' && view === 'chat') {
      if (watchPanelVisible) api?.toggleWatchPanel?.(true)
      if (pinnedPanelVisible) api?.togglePinnedPanel?.(true)
    }
    prevView.current = view
  }, [view, watchPanelVisible, pinnedPanelVisible])

  const toggleWatch = () => {
    const next = !watchPanelVisible
    setWatchPanelVisible(next)
    api?.toggleWatchPanel?.(next)
  }

  const togglePinned = () => {
    const next = !pinnedPanelVisible
    setPinnedPanelVisible(next)
    api?.togglePinnedPanel?.(next)
  }

  return (
    <div style={{ display: 'flex', height: '100vh' }}>
      {/* minWidth is a FLOOR, not 0. The rail mounts on the same tick as the
          toggle, but the WINDOW only widens once the main process handles the
          resize -- with minWidth:0 the chat column collapsed to a sliver for
          that frame and sprang back, which is the flicker on open/close. */}
      <div style={{ flex: 1, minWidth: 280 }}>
        {view === 'settings' ? (
          <SettingsPanel onClose={() => setView('chat')} />
        ) : (
          <ChatPanel onToggleWatch={toggleWatch} watchPanelVisible={watchPanelVisible} onTogglePinned={togglePinned} pinnedPanelVisible={pinnedPanelVisible} pinnedFileCount={pinnedFiles.length} />
        )}

        {/* NO panel notification bubble. Upstream rendered one here, but the
            builtin surfaces notifications on the PET overlay only (PetWidget's
            BubbleOverlay) -- two bubbles for one notify action read as a bug.
            Do NOT let an upstream sync re-add this. */}
      </div>
      {view === 'chat' && (
        <PinnedSidePanel
          pins={pinnedFiles}
          updatedPaths={updatedPaths}
          deletedPaths={deletedPaths}
          visible={pinnedPanelVisible}

          petName={petName}
          onMarkSeen={(path) => {
            setUpdatedPaths(prev => { const s = new Set(prev); s.delete(path); return s })
          }}
        />
      )}
      {view === 'chat' && (
        <WatchlistPanel
          visible={watchPanelVisible}
          onClose={toggleWatch}

          petName={petName}
        />
      )}
    </div>
  )
}
