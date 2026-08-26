import anime from './anime.es'

/**
 * Thanos-snap "disintegrate" effect for destructive actions.
 *
 * Erodes a DOM element with an animated mask wipe while a layer of theme-tinted
 * particles lifts off and drifts away, staggered by column so the dust flies off
 * in a wave that tracks the erosion front. No pixel capture (no html2canvas) and
 * no new dependency -- abstract directional dust, cross-browser safe.
 *
 * Call it from a delete/dismiss handler BEFORE the state mutation, then remove
 * the item when it resolves:
 *
 *   const row = (e.currentTarget as HTMLElement).closest('[data-row]')
 *   if (row) await disintegrate(row as HTMLElement)
 *   dispatch(deleteThing(id))
 *
 * Resolves when the element has vanished (~duration). Particles finish and clean
 * themselves up shortly after, independent of the promise. Respects
 * prefers-reduced-motion (plain quick fade).
 */

interface DisintegrateOptions {
  /** Base duration in ms for the erosion (default 520). */
  duration?: number
  /** Sweep direction the dust blows toward (default 'right'). */
  direction?: 'right' | 'up'
  /** Override particle color; defaults to sampling theme tokens. */
  particleColor?: string
}

export function disintegrate(el: HTMLElement | null, opts: DisintegrateOptions = {}): Promise<void> {
  return new Promise<void>(resolve => {
    if (!el) { resolve(); return }
    const rect = el.getBoundingClientRect()
    const duration = opts.duration ?? 520
    const direction = opts.direction ?? 'right'
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches

    if (reduce || rect.width < 2 || rect.height < 2) {
      anime({ targets: el, opacity: [1, 0], duration: 180, easing: 'easeOutQuad', complete: () => resolve() })
      return
    }

    // Derive dust colors from the element itself (its own text + background)
    // plus a couple of neutral greys, so it reads as the row turning to ash --
    // theme-agnostic, no forced accent color.
    const elCs = getComputedStyle(el)
    const opaque = (c: string) => !!c && c !== 'transparent' && !/rgba?\([^)]*,\s*0(\.0+)?\s*\)/.test(c)
    const palette = (opts.particleColor
      ? [opts.particleColor]
      : [elCs.color, elCs.backgroundColor, 'rgb(190,190,196)', 'rgb(224,224,228)']
    ).filter(opaque)
    const colors = palette.length ? palette : ['rgb(200,200,205)']

    // Particle layer in viewport coordinates (fixed), above everything.
    const layer = document.createElement('div')
    layer.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:9999;'
    document.body.appendChild(layer)

    const cols = Math.max(12, Math.round(rect.width / 9))
    const count = Math.max(80, Math.min(320, Math.round((rect.width * rect.height) / 110)))
    const particles: HTMLElement[] = []
    for (let i = 0; i < count; i++) {
      const col = i % cols
      const x = rect.left + (col + 0.5) * (rect.width / cols) + (Math.random() - 0.5) * 6
      const y = rect.top + Math.random() * rect.height
      const size = 1.5 + Math.random() * 2.5
      const p = document.createElement('div')
      p.style.cssText =
        `position:absolute;left:${x}px;top:${y}px;width:${size}px;height:${size}px;` +
        `border-radius:50%;background:${colors[i % colors.length]};opacity:0;will-change:transform,opacity;`
      layer.appendChild(p)
      particles.push(p)
    }

    // Erosion mask front grows from the sweep side; a soft 12% edge feathers it.
    const setMask = (pct: number) => {
      const side = direction === 'up' ? 'to top' : 'to right'
      const mask = `linear-gradient(${side}, transparent ${pct * 100}%, #000 ${pct * 100 + 12}%)`
      el.style.webkitMaskImage = mask
      el.style.maskImage = mask
    }
    el.style.willChange = 'mask, filter, opacity'

    // Element: erode + blur + fade in place. Resolve when it's gone.
    const erosion = { p: 0 }
    anime({
      targets: erosion, p: [0, 1], duration, easing: 'easeInOutQuad',
      update: () => {
        setMask(erosion.p)
        el.style.filter = `blur(${erosion.p * 1.6}px)`
        el.style.opacity = String(1 - erosion.p)
      },
      complete: () => resolve(),
    })

    // Dust: lift, drift, shrink, fade -- staggered by column toward the sweep side.
    anime({
      targets: particles,
      translateX: () => (Math.random() - 0.4) * 28,
      translateY: () => -22 - Math.random() * 52,
      rotate: () => (Math.random() - 0.5) * 70,
      scale: [{ value: 1, duration: 80 }, { value: 0, duration }],
      opacity: [{ value: () => 0.6 + Math.random() * 0.4, duration: 60 }, { value: 0, duration }],
      duration,
      easing: 'easeOutQuad',
      delay: (t: HTMLElement, i: number) => {
        if (direction === 'up') {
          // Erosion sweeps bottom-to-top, so dust at the bottom lifts off first.
          const relY = (parseFloat(t.style.top) - rect.top) / rect.height
          return (1 - relY) * (duration * 0.5) + Math.random() * 40
        }
        // 'right': erosion sweeps left-to-right, so left columns lift off first.
        const col = i % cols
        return col * ((duration * 0.5) / cols) + Math.random() * 40
      },
      complete: () => { layer.remove() },
    })
  })
}
