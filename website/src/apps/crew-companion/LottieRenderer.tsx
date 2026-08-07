/**
 * LottieRenderer — renders Lottie JSON animations via lottie-web.
 * Manages animation lifecycle: destroys old animation and loads new
 * when animationData changes. Notifies parent via onReady callback.
 */
import React, { useEffect, useRef } from 'react'
/*
 * The LIGHT player, deliberately.
 *
 * `lottie-web`'s default build evaluates animation expressions, and the JSON reaching
 * `loadAnimation` here is attacker-authored: an appearance pack imported from a
 * third-party gallery carries its own `.json`, and it runs in the gateway's origin.
 * Nothing this app draws needs expressions, so the smaller player removes the sink
 * rather than trying to sanitise it.
 */
import lottie from 'lottie-web/build/player/lottie_light'
import type { AnimationItem } from 'lottie-web'

interface LottieRendererProps {
  animationData: string // Lottie JSON string
  width: number
  height: number
  loop?: boolean
  onReady?: () => void // animation loaded callback
}

const LottieRendererInner: React.FC<LottieRendererProps> = ({
  animationData,
  width,
  height,
  loop = true,
  onReady,
}) => {
  const containerRef = useRef<HTMLDivElement>(null)
  const animRef = useRef<AnimationItem | null>(null)

  useEffect(() => {
    // Destroy any previous animation
    if (animRef.current) {
      animRef.current.destroy()
      animRef.current = null
    }

    if (!containerRef.current || !animationData) return

    let parsed: unknown
    try {
      parsed = JSON.parse(animationData)
    } catch {
      // Invalid JSON — skip loading
      return
    }

    const anim = lottie.loadAnimation({
      container: containerRef.current,
      renderer: 'svg',
      loop,
      autoplay: true,
      animationData: parsed,
    })

    animRef.current = anim

    const handleReady = () => onReady?.()
    anim.addEventListener('DOMLoaded', handleReady)

    return () => {
      anim.removeEventListener('DOMLoaded', handleReady)
      anim.destroy()
      animRef.current = null
    }
  }, [animationData, loop, onReady])

  return (
    <div
      ref={containerRef}
      style={{ width, height }}
    />
  )
}

export const LottieRenderer = React.memo(LottieRendererInner)
