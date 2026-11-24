/**
 * The XR entry in the top bar. It shows only where it can do something: on a
 * browser that offers an immersive session (XR glasses, a headset), or when
 * the preview switch is set for a desk browser.
 */
import { RectangleGoggles } from 'lucide-react'
import { useLocation, useNavigate } from 'react-router-dom'
import { i18nT } from '../../i18n/t'
import { useXrSupport, xrPreviewForced } from '../../pages/xr/xrSupport'

export function XrEntryButton() {
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const support = useXrSupport()
  if (support === 'none' && !xrPreviewForced()) return null
  const here = pathname === '/xr'
  return (
    <button
      type="button"
      data-testid="xr-entry"
      onClick={() => navigate(here ? '/chat' : '/xr')}
      className={`flex items-center justify-center w-7 h-7 rounded-md hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0 ${here ? 'text-accent' : 'text-muted hover:text-text'}`}
      aria-label={i18nT('xr.icon_label')}
      aria-pressed={here}
      title={i18nT('xr.icon_label')}
    >
      <RectangleGoggles size={15} />
    </button>
  )
}
