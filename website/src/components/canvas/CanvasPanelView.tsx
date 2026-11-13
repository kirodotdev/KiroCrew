/**
 * A canvas shown in a chat's artifact side panel: the editor, read-only, so it
 * pans and zooms like the detail page's past versions. The panel shows every
 * kind read-only; editing happens on the detail page, which owns the saves.
 */
import { useMemo, type CSSProperties } from 'react'
import { useTranslation } from 'react-i18next'
import ErrorNotice from '../ErrorNotice'
import CanvasEditor from './CanvasEditor'
import { parseCanvas } from './canvasModel'

const ignore = () => undefined

export default function CanvasPanelView({ content, heightStyle }: { content: string; heightStyle: CSSProperties }) {
  const { t } = useTranslation()
  const parsed = useMemo(() => parseCanvas(content), [content])
  if (!parsed.ok) {
    return <ErrorNotice title={t('components.canvas.unreadable_title')} message={t('components.canvas.unreadable_message')} askAgent />
  }
  return (
    <div data-testid="canvas-panel-view" className="relative overflow-hidden rounded-lg border border-border" style={heightStyle}>
      <CanvasEditor canvas={parsed.canvas} onChange={ignore} readOnly />
    </div>
  )
}
