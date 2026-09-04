import { safeSetItem } from '../utils/safeStorage'
import { useEffect, useRef, useState, useCallback } from 'react'
import { SCENE_STORAGE_KEY, POPOUT_CHANNEL } from '../pages/scenes/config'

type PopoutMsg =
  | { type: 'scene'; scene: string }
  | { type: 'popout-open' }
  | { type: 'popout-close' }
  | { type: 'ping' }
  | { type: 'pong' }

const HEARTBEAT_MS = 5_000

export function usePopoutSync(isPopout: boolean, onSceneChange?: (scene: string) => void) {
  const [popoutActive, setPopoutActive] = useState(false)
  const channelRef = useRef<BroadcastChannel | null>(null)
  const popoutRef = useRef<Window | null>(null)
  const pongReceived = useRef(false)
  const onSceneChangeRef = useRef(onSceneChange)
  onSceneChangeRef.current = onSceneChange

  useEffect(() => {
    if (typeof BroadcastChannel === 'undefined') return
    const ch = new BroadcastChannel(POPOUT_CHANNEL)
    channelRef.current = ch

    ch.onmessage = (e: MessageEvent<PopoutMsg>) => {
      const msg = e.data
      if (!isPopout) {
        if (msg.type === 'popout-open') { pongReceived.current = true; setPopoutActive(true) }
        if (msg.type === 'popout-close') setPopoutActive(false)
        if (msg.type === 'scene') { safeSetItem(SCENE_STORAGE_KEY, msg.scene); onSceneChangeRef.current?.(msg.scene) }
        if (msg.type === 'pong') { pongReceived.current = true; setPopoutActive(true) }
      } else {
        if (msg.type === 'scene') { safeSetItem(SCENE_STORAGE_KEY, msg.scene); onSceneChangeRef.current?.(msg.scene) }
        if (msg.type === 'ping') ch.postMessage({ type: 'pong' } satisfies PopoutMsg)
      }
    }

    let heartbeat: ReturnType<typeof setInterval> | undefined
    if (isPopout) {
      ch.postMessage({ type: 'popout-open' } satisfies PopoutMsg)
    } else {
      ch.postMessage({ type: 'ping' } satisfies PopoutMsg)
      heartbeat = setInterval(() => {
        if (!pongReceived.current) setPopoutActive(false)
        pongReceived.current = false
        ch.postMessage({ type: 'ping' } satisfies PopoutMsg)
      }, HEARTBEAT_MS)
    }

    return () => {
      if (heartbeat) clearInterval(heartbeat)
      if (isPopout) ch.postMessage({ type: 'popout-close' } satisfies PopoutMsg)
      ch.close()
      channelRef.current = null
    }
  }, [isPopout])

  useEffect(() => {
    if (!isPopout) return
    const onUnload = () => channelRef.current?.postMessage({ type: 'popout-close' } satisfies PopoutMsg)
    window.addEventListener('beforeunload', onUnload)
    return () => window.removeEventListener('beforeunload', onUnload)
  }, [isPopout])

  const broadcastScene = useCallback((scene: string) => {
    channelRef.current?.postMessage({ type: 'scene', scene } satisfies PopoutMsg)
  }, [])

  const openPopout = useCallback(() => {
    if (popoutRef.current && !popoutRef.current.closed) {
      popoutRef.current.focus()
      return
    }
    const w = Math.min(960, screen.availWidth * 0.6)
    const h = Math.round(w / 1.5) + 80
    // availLeft/availTop are non-standard (Firefox / legacy) Screen fields.
    const multiScreen = screen as Screen & { availLeft?: number; availTop?: number }
    const left = (multiScreen.availLeft ?? 0) + (screen.availWidth - w) / 2
    const top = (multiScreen.availTop ?? 0) + (screen.availHeight - h) / 2
    popoutRef.current = window.open(
      '/worlds-popout',
      'kirocrew-worlds',
      `width=${w},height=${h},left=${left},top=${top},resizable=yes`,
    )
  }, [])

  return { popoutActive, broadcastScene, openPopout }
}
