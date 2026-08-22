import type { CSSProperties } from 'react'

import { fileExplorerApi } from './api'
import type { PptxParagraph, PptxShape, PptxSlide } from './types'

/** Perceived luminance (0..1) of a #rrggbb colour, for text auto-contrast. */
export function luminance(hex: string | null | undefined): number {
  try {
    const n = parseInt((hex || '#ffffff').slice(1), 16)
    return (0.299 * ((n >> 16) & 255) + 0.587 * ((n >> 8) & 255) + 0.114 * (n & 255)) / 255
  } catch {
    return 1
  }
}

const ALIGN: Record<string, CSSProperties['textAlign']> = {
  l: 'left', ctr: 'center', r: 'right', just: 'justify',
}

interface SlideCanvasProps {
  slide: PptxSlide
  path: string
  /** Slide width in points (EMU / 12700) — the base for font scaling. */
  widthPt: number
  /** slideW / slideH, preserved so the canvas keeps the deck's true shape. */
  ratio: number
}

/**
 * One slide as a true-aspect-ratio canvas: the deck's background colour,
 * shapes absolutely positioned at their real slide coordinates, text runs in
 * their actual colour/weight/size, and embedded images streamed from the
 * document. Font sizes scale with the rendered width via container-query
 * units (`cqw`), so a run's point size maps to the same fraction of the
 * slide it occupies in PowerPoint.
 */
export default function SlideCanvas({ slide, path, widthPt, ratio }: SlideCanvasProps) {
  const bg = slide.bg || '#ffffff'
  const defaultText = luminance(bg) < 0.45 ? '#f0f0f0' : '#1a1a1a'
  const fontSize = (pt?: number) => `${(((pt || 18) / widthPt) * 100).toFixed(3)}cqw`

  const renderParagraph = (para: PptxParagraph, key: number) => (
    <div key={key} style={{ textAlign: ALIGN[para.algn] || 'left', paddingLeft: para.lvl ? `${para.lvl * 2}cqw` : undefined, minHeight: '0.5em' }}>
      {para.bullet && <span style={{ fontSize: fontSize(para.runs[0]?.sz) }}>{'\u2022 '}</span>}
      {para.runs.length === 0
        ? '\u00a0'
        : para.runs.map((run, i) => run.t === '\n'
          ? <br key={i} />
          : (
            <span key={i} style={{ color: run.c || defaultText, fontWeight: run.b ? 600 : 400, fontStyle: run.i ? 'italic' : undefined, fontSize: fontSize(run.sz) }}>
              {run.t}
            </span>
          ))}
    </div>
  )

  const renderShape = (shape: PptxShape, key: number) => {
    const positioned = shape.x != null
    const box: CSSProperties = positioned
      ? { position: 'absolute', left: `${shape.x}%`, top: `${shape.y}%`, width: `${shape.w}%` }
      : { position: 'relative', margin: '2cqw 4cqw' }
    if (shape.kind === 'image' && shape.member) {
      return (
        <img
          key={key}
          src={fileExplorerApi.extractMemberUrl(path, shape.member)}
          alt=""
          style={{ ...box, objectFit: 'contain', height: positioned ? `${shape.h}%` : undefined, maxWidth: positioned ? undefined : '100%' }}
        />
      )
    }
    if (shape.kind === 'table' && shape.rows) {
      return (
        <div key={key} style={{ ...box, minHeight: positioned ? `${shape.h}%` : undefined, overflow: 'auto', fontSize: '1.6cqw', background: 'rgba(255,255,255,.92)', color: '#1a1a1a', borderRadius: 4 }}>
          <table style={{ borderCollapse: 'collapse', width: '100%' }}>
            <tbody>
              {shape.rows.map((row, r) => (
                <tr key={r}>
                  {row.map((cell, c) => (
                    <td key={c} style={{ border: '1px solid rgba(0,0,0,.2)', padding: '0.4cqw 0.8cqw' }}>{cell}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )
    }
    return (
      <div key={key} style={{ ...box, minHeight: positioned ? `${shape.h}%` : undefined, background: shape.fill || undefined, borderRadius: shape.fill ? '.5cqw' : undefined, padding: shape.fill ? '.6cqw' : undefined, lineHeight: 1.25, overflowWrap: 'break-word' }}>
        {(shape.paras || []).map(renderParagraph)}
      </div>
    )
  }

  return (
    <div
      data-testid="fe-slide-canvas"
      style={{ containerType: 'inline-size', aspectRatio: String(ratio || 16 / 9), background: bg, position: 'relative', overflow: 'hidden', borderRadius: 8, border: '1px solid var(--border)', boxShadow: '0 1px 4px rgba(0,0,0,.25)' }}
    >
      {slide.shapes.map(renderShape)}
    </div>
  )
}
