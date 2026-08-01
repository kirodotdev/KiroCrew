import { describe, it, expect } from 'vitest'

import {
  parseExcalidrawScene,
  renderExcalidrawScene,
  renderExcalidrawSource,
  sceneBounds,
  safeColor,
  safeImageDataUrl,
  cornerRadius,
  EmptySceneError,
  type ExcalidrawElement,
} from '../lib/excalidrawScene'

/** Minimal element with the fields the renderer reads. */
const el = (over: Partial<ExcalidrawElement> = {}): ExcalidrawElement => ({
  type: 'rectangle',
  x: 0, y: 0, width: 100, height: 50,
  strokeColor: '#1e1e1e', backgroundColor: 'transparent',
  fillStyle: 'hachure', strokeWidth: 1, strokeStyle: 'solid',
  roughness: 1, opacity: 100, seed: 12345,
  ...over,
})

const scene = (elements: ExcalidrawElement[], extra = {}) => ({ elements, ...extra })

describe('parseExcalidrawScene', () => {
  it('parses a well-formed scene', () => {
    const s = parseExcalidrawScene(JSON.stringify({
      type: 'excalidraw', version: 2, elements: [el()],
    }))
    expect(s).not.toBeNull()
    expect(s!.elements).toHaveLength(1)
    expect(s!.type).toBe('excalidraw')
  })

  it('accepts an empty canvas as valid', () => {
    // An empty scene is a legitimate document, distinct from a broken one — the
    // renderer signals "nothing to draw" separately via EmptySceneError.
    expect(parseExcalidrawScene('{"elements":[]}')).not.toBeNull()
  })

  it.each([
    ['malformed JSON', '{"elements": ['],
    ['truncated stream', '{"type":"excalidraw","elem'],
    ['a JSON array', '[1,2,3]'],
    ['a bare string', '"hello"'],
    ['null', 'null'],
    ['an object with no elements array', '{"type":"excalidraw"}'],
    ['elements as an object', '{"elements":{}}'],
    ['empty input', ''],
  ])('returns null for %s', (_label, input) => {
    expect(parseExcalidrawScene(input)).toBeNull()
  })

  it('drops non-object entries from elements instead of throwing', () => {
    const s = parseExcalidrawScene('{"elements":[null,1,"x",{"type":"rectangle"}]}')
    expect(s!.elements).toHaveLength(1)
  })
})

describe('safeColor', () => {
  it.each(['#fff', '#ffffff', '#ffffffcc', 'rgb(1,2,3)', 'rgba(1,2,3,0.5)', 'red', 'transparent'])(
    'accepts %s', (c) => expect(safeColor(c, 'FALLBACK')).toBe(c),
  )

  it.each([
    'url(#x)',
    'red;fill:blue',
    '#fff"onload="alert(1)',
    'expression(alert(1))',
    '</style><script>',
  ])('rejects %s', (c) => expect(safeColor(c, 'FALLBACK')).toBe('FALLBACK'))

  it('falls back for non-strings', () => {
    expect(safeColor(undefined, 'FALLBACK')).toBe('FALLBACK')
    expect(safeColor(42, 'FALLBACK')).toBe('FALLBACK')
  })
})

describe('safeImageDataUrl', () => {
  it('accepts a raster image data URL', () => {
    const png = 'data:image/png;base64,iVBORw0KGgo='
    expect(safeImageDataUrl(png)).toBe(png)
  })

  it.each([
    ['javascript:', 'javascript:alert(1)'],
    ['inline HTML', 'data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg=='],
    ['SVG (parser surface we gain nothing from)', 'data:image/svg+xml;base64,PHN2Zz48L3N2Zz4='],
    ['a remote URL', 'https://example.com/x.png'],
    ['a non-base64 image URL', 'data:image/png,rawbytes'],
  ])('rejects %s', (_label, url) => {
    expect(safeImageDataUrl(url)).toBeNull()
  })
})

describe('sceneBounds', () => {
  it('covers width/height for shapes', () => {
    expect(sceneBounds(scene([el({ x: 10, y: 20, width: 30, height: 40 })])))
      .toEqual({ minX: 10, minY: 20, maxX: 40, maxY: 60 })
  })

  it('extends to negative relative points on linear elements', () => {
    // Arrow/line points are relative to x/y and routinely go negative; bounds
    // taken from width/height alone would clip the diagram.
    const b = sceneBounds(scene([el({
      type: 'arrow', x: 100, y: 100, width: 0, height: 0,
      points: [[0, 0], [-50, -80]],
    })]))
    expect(b).toEqual({ minX: 50, minY: 20, maxX: 100, maxY: 100 })
  })

  it('returns null when nothing is drawable', () => {
    expect(sceneBounds(scene([]))).toBeNull()
    expect(sceneBounds(scene([el({ isDeleted: true })]))).toBeNull()
    expect(sceneBounds(scene([el({ type: 'embeddable' })]))).toBeNull()
  })

  it('covers the ROTATED extent, not the unrotated box', () => {
    // Regression: bounds were measured from the unrotated x/y/width/height, so a
    // rotated element swept outside its own box and was clipped by the viewBox.
    // A 100x50 rectangle turned 90 degrees occupies 50x100.
    const b = sceneBounds(scene([
      el({ x: 0, y: 0, width: 100, height: 50, angle: Math.PI / 2 }),
    ]))!
    expect(b.minX).toBeCloseTo(25)
    expect(b.maxX).toBeCloseTo(75)
    expect(b.minY).toBeCloseTo(-25)
    expect(b.maxY).toBeCloseTo(75)
  })

  it('leaves an unrotated element unchanged', () => {
    // Guards the rotation branch against regressing the common case.
    expect(sceneBounds(scene([el({ x: 0, y: 0, width: 100, height: 50, angle: 0 })])))
      .toEqual({ minX: 0, minY: 0, maxX: 100, maxY: 50 })
  })

  it('rotates relative points too', () => {
    const b = sceneBounds(scene([el({
      type: 'line', x: 0, y: 0, width: 100, height: 0,
      points: [[0, 0], [100, 0]], angle: Math.PI / 2,
    })]))!
    // A horizontal 100-long line rotated 90 degrees becomes vertical.
    expect(b.maxY - b.minY).toBeCloseTo(100)
    expect(b.maxX - b.minX).toBeCloseTo(0)
  })
})

describe('cornerRadius', () => {
  it('is zero without roundness', () => {
    expect(cornerRadius(100, el({ roundness: null }))).toBe(0)
  })

  it('is a flat quarter of the shorter side when proportional', () => {
    expect(cornerRadius(100, el({ roundness: { type: 2 } }))).toBe(25)
  })

  it('caps at the fixed radius once adaptive shapes pass the cutoff', () => {
    // Below the 128 cutoff it stays proportional; above it clamps to 32 so a
    // large rectangle does not degenerate into a stadium.
    expect(cornerRadius(100, el({ roundness: { type: 3 } }))).toBe(25)
    expect(cornerRadius(400, el({ roundness: { type: 3 } }))).toBe(32)
  })
})

describe('renderExcalidrawScene', () => {
  it('renders an svg with a viewBox padded around the scene', () => {
    const svg = renderExcalidrawScene(scene([el({ x: 0, y: 0, width: 100, height: 50 })]))
    expect(svg.tagName.toLowerCase()).toBe('svg')
    // 16px default padding on each side.
    expect(svg.getAttribute('viewBox')).toBe('-16 -16 132 82')
    expect(svg.hasAttribute('data-excalidraw-scene')).toBe(true)
    expect(svg.getAttribute('style')).toContain('max-width:100%')
  })

  it('throws EmptySceneError when there is nothing to draw', () => {
    expect(() => renderExcalidrawScene(scene([]))).toThrow(EmptySceneError)
  })

  it.each(['rectangle', 'diamond', 'ellipse', 'line', 'arrow', 'freedraw'])(
    'emits geometry for %s', (type) => {
      const svg = renderExcalidrawScene(scene([el({
        type, points: [[0, 0], [50, 50]],
      })]))
      expect(svg.querySelectorAll('path').length).toBeGreaterThan(0)
    },
  )

  it('renders rounded rectangles through a path rather than a plain rect', () => {
    const svg = renderExcalidrawScene(scene([el({ roundness: { type: 3 } })]))
    const d = svg.querySelector('path')?.getAttribute('d') || ''
    expect(d.length).toBeGreaterThan(0)
  })

  it('renders text as textContent, never as markup', () => {
    const svg = renderExcalidrawScene(scene([el({
      type: 'text', text: '<script>alert(1)</script>', fontSize: 20,
    })]))
    const t = svg.querySelector('text')
    expect(t?.textContent).toBe('<script>alert(1)</script>')
    // The payload must not have become a real element.
    expect(svg.querySelector('script')).toBeNull()
  })

  it('splits multi-line text into one <text> per line', () => {
    const svg = renderExcalidrawScene(scene([el({ type: 'text', text: 'a\nb\nc' })]))
    expect(svg.querySelectorAll('text')).toHaveLength(3)
  })

  it('embeds a valid raster image', () => {
    const png = 'data:image/png;base64,iVBORw0KGgo='
    const svg = renderExcalidrawScene(scene(
      [el({ type: 'image', fileId: 'f1' })],
      { files: { f1: { dataURL: png, mimeType: 'image/png' } } },
    ))
    expect(svg.querySelector('image')?.getAttribute('href')).toBe(png)
  })

  it('refuses a hostile data URL on an image element', () => {
    const svg = renderExcalidrawScene(scene(
      [el({ type: 'image', fileId: 'f1' })],
      { files: { f1: { dataURL: 'data:text/html;base64,PHNjcmlwdD4=' } } },
    ))
    expect(svg.querySelector('image')).toBeNull()
  })

  it('skips deleted and unknown elements but keeps their neighbours', () => {
    // A scene from a newer Excalidraw must degrade, not fail.
    const svg = renderExcalidrawScene(scene([
      el({ isDeleted: true }),
      el({ type: 'embeddable' }),
      el({ type: 'text', text: 'kept' }),
    ]))
    expect(svg.querySelector('text')?.textContent).toBe('kept')
  })

  it('applies rotation and opacity on the element wrapper', () => {
    const svg = renderExcalidrawScene(scene([el({ angle: Math.PI / 2, opacity: 50 })]))
    const g = svg.querySelector('g[transform]')
    expect(g?.getAttribute('transform')).toContain('rotate(90')
    expect(g?.getAttribute('opacity')).toBe('0.5')
  })

  it('paints the scene background when one is set', () => {
    const svg = renderExcalidrawScene(scene([el()], {
      appState: { viewBackgroundColor: '#fff3e0' },
    }))
    expect(svg.querySelector('rect')?.getAttribute('fill')).toBe('#fff3e0')
  })

  it('paints a light canvas when the scene declares no background', () => {
    // Regression: a scene with explicit near-black strokes and no background was
    // composited straight onto the chat surface, so on the dark theme the diagram
    // was near-invisible. The author's own canvas is painted instead.
    const svg = renderExcalidrawScene(scene([el({ strokeColor: '#1e1e1e' })]))
    expect(svg.querySelector('rect')?.getAttribute('fill')).toBe('#ffffff')
  })

  it('paints a light canvas even when the scene asks for transparent', () => {
    // A transparent canvas cannot guarantee legibility — that depends on whatever
    // is behind it — so it is treated as unspecified.
    const svg = renderExcalidrawScene(scene([el()], {
      appState: { viewBackgroundColor: 'transparent' },
    }))
    expect(svg.querySelector('rect')?.getAttribute('fill')).toBe('#ffffff')
  })

  it('ignores a hostile background colour', () => {
    const svg = renderExcalidrawScene(scene([el()], {
      appState: { viewBackgroundColor: 'url(#evil)' },
    }))
    // Falls back to the safe default rather than letting the payload through.
    expect(svg.querySelector('rect')?.getAttribute('fill')).toBe('#ffffff')
  })

  it('names the graphic with its text so screen readers can read it', () => {
    // role="img" hides descendants from assistive tech, so the diagram's <text>
    // would be dropped entirely without an accessible name built from it.
    const svg = renderExcalidrawScene(scene([
      el({ type: 'text', text: 'ingest' }),
      el({ type: 'text', text: 'queue\nworker' }),
    ]))
    expect(svg.getAttribute('role')).toBe('img')
    expect(svg.getAttribute('aria-label')).toBe('ingest. queue worker')
  })

  it('omits role=img when there is no text to name it with', () => {
    // An unnamed role="img" announces as a bare graphic and hides its children;
    // dropping the role is strictly better than shipping one with no name.
    const svg = renderExcalidrawScene(scene([el({ type: 'ellipse' })]))
    expect(svg.hasAttribute('role')).toBe(false)
    expect(svg.hasAttribute('aria-label')).toBe(false)
  })

  it('is deterministic across renders so diagrams do not wobble', () => {
    // rough.js derives its jitter from the seed. If the seed were dropped, two
    // renders of the same scene would differ and every repaint would shimmer.
    const s = scene([el({ seed: 999, roughness: 2 })])
    const a = renderExcalidrawScene(s).innerHTML
    const b = renderExcalidrawScene(s).innerHTML
    expect(a).toBe(b)
    expect(a.length).toBeGreaterThan(0)
  })

  it('produces different geometry for different seeds', () => {
    const a = renderExcalidrawScene(scene([el({ seed: 1, roughness: 2 })])).innerHTML
    const b = renderExcalidrawScene(scene([el({ seed: 424242, roughness: 2 })])).innerHTML
    expect(a).not.toBe(b)
  })
})

describe('renderExcalidrawSource', () => {
  it('renders straight from JSON text', () => {
    const svg = renderExcalidrawSource(JSON.stringify({ elements: [el()] }))
    expect(svg.tagName.toLowerCase()).toBe('svg')
  })

  it('throws on unparseable input so callers can show the raw source', () => {
    expect(() => renderExcalidrawSource('{oops')).toThrow(/invalid Excalidraw scene/i)
  })
})
