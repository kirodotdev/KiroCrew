/**
 * LottieRenderer — renders Lottie JSON animations via lottie-web.
 * Manages animation lifecycle: destroys old animation and loads new
 * when animationData changes. Notifies parent via onReady callback.
 *
 * Core, not app-owned: a crew can wear an appearance pack whose art is a Lottie
 * document, so the dashboard's own avatar needs this player while Crew
 * Companion — an optional app — may be absent. Core never imports from
 * `apps/`, so the player lives here and the Companion reads it from core.
 */
import React, { useEffect, useRef } from 'react'
/*
 * The LIGHT player, deliberately.
 *
 * `lottie-web`'s default build evaluates animation expressions, and the JSON reaching
 * `loadAnimation` here is attacker-authored: an appearance pack imported from a
 * third-party gallery carries its own `.json`, and it runs in the gateway's origin.
 * Nothing that draws a pack needs expressions, so the smaller player removes the sink
 * rather than trying to sanitise it.
 *
 * The light build closes expressions ONLY. It still resolves a document's `assets`
 * and `fonts` by requesting them, so `referencesRemoteAsset` refuses such a clip
 * below — the light player is half the fence, not the whole of it.
 */
import lottie from 'lottie-web/build/player/lottie_light'
import type { AnimationItem } from 'lottie-web'

import { referencesRemoteAsset } from '../../lib/appearancePacks/lottieSafety'
import { isValidLottie } from '../../lib/appearancePacks/types'

interface LottieRendererProps {
  animationData: string // Lottie JSON string
  width: number
  height: number
  loop?: boolean
  /**
   * Run the animation, or hold it on its first frame.
   *
   * `false` still builds the document — a Lottie clip has no cheaper "poster
   * frame" to draw — but starts no timeline, so a paused instance costs no
   * per-frame work. That is what a dense roster renders: see `PackAvatar`,
   * which pauses every avatar the user cannot currently see.
   */
  autoplay?: boolean
  onReady?: () => void // animation loaded callback
  /**
   * The clip could not be loaded at all.
   *
   * A pack's `.json` is authored by somebody else and the importer only checks
   * that the file is non-empty, so malformed art reaches this player. Without
   * this callback the failure was a console line and an empty box: the caller
   * believed it had drawn a face, so no fallback and no error notice appeared.
   *
   * Must be STABLE across renders (`useCallback`), like `onReady`: both are
   * effect dependencies, so a fresh identity per render destroys and reloads the
   * animation on every render.
   */
  onError?: () => void
}

const LottieRendererInner: React.FC<LottieRendererProps> = ({
  animationData,
  width,
  height,
  loop = true,
  autoplay = true,
  onReady,
  onError,
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
      // A malformed imported clip renders as an empty slot, so leave enough
      // context to distinguish bad pack data from a renderer failure.
      // eslint-disable-next-line no-console
      console.error('[appearance-pack] lottie JSON parse failed', {
        bytes: animationData.length,
      })
      onError?.()
      return
    }

    // A pack's clip may name images or fonts the player would REQUEST, and the
    // pack is third-party art on the gateway's own authenticated origin — so a
    // document that fetches is refused outright rather than drawn. See
    // `lottieSafety.ts` for why refusing beats stripping.
    // Valid JSON is not a valid document: `{}` parses, carries no asset, and then
    // fails inside the player with no event anyone is listening to. The importer
    // only checks that the file is non-empty, so the shape is checked here with
    // the same predicate the format module already defines.
    if (!isValidLottie(animationData)) {
      // eslint-disable-next-line no-console
      console.error('[appearance-pack] lottie clip refused: not a Lottie document', {
        bytes: animationData.length,
      })
      onError?.()
      return
    }

    if (referencesRemoteAsset(parsed)) {
      // eslint-disable-next-line no-console
      console.error('[appearance-pack] lottie clip refused: it references a remote asset', {
        bytes: animationData.length,
      })
      onError?.()
      return
    }

    // The player's own build step is the LAST fence, and it catches whatever the
    // three checks above let through: `isValidLottie` tests key presence, so
    // `{"v":"5","layers":null,...}` passes it and then throws synchronously inside
    // `loadAnimation` — before the failure listeners below could be attached. A
    // throw here is the same answer as a refusal: report, draw nothing, fall back.
    let anim: AnimationItem
    try {
      anim = lottie.loadAnimation({
        container: containerRef.current,
        renderer: 'svg',
        loop,
        autoplay,
        animationData: parsed,
      })
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error('[appearance-pack] lottie clip refused: the player could not build it', {
        bytes: animationData.length,
        error: err instanceof Error ? err.message : String(err),
      })
      onError?.()
      return
    }

    animRef.current = anim

    const handleReady = () => onReady?.()
    // The player reports its own failures as EVENTS, not exceptions: a document
    // that passes the shape check but that lottie-web still cannot build fires
    // `error` or `data_failed` and otherwise stays silent.
    const handleFail = () => onError?.()
    anim.addEventListener('DOMLoaded', handleReady)
    anim.addEventListener('error', handleFail)
    anim.addEventListener('data_failed', handleFail)

    return () => {
      anim.removeEventListener('DOMLoaded', handleReady)
      anim.removeEventListener('error', handleFail)
      anim.removeEventListener('data_failed', handleFail)
      anim.destroy()
      animRef.current = null
    }
  }, [animationData, loop, autoplay, onReady, onError])

  return (
    <div
      ref={containerRef}
      style={{ width, height }}
    />
  )
}

export const LottieRenderer = React.memo(LottieRendererInner)
