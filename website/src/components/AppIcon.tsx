/**
 * AppIcon — shared icon component for app cards and detail pages.
 *
 * Renders an image from iconUrl (with fallback on error) or a lucide-react
 * icon from the ICON_MAP.  Falls back to the Package icon.
 *
 * Builtin apps declare `iconUrl` in their manifest pointing to static SVGs
 * in /app-assets/ — no special mapping needed here.
 */
import { useState } from 'react'
import {
  Shield, Bot, Search, Tag, Users, Zap, Star, Package, Cat,
} from 'lucide-react'

const ICON_MAP: Record<string, typeof Shield> = {
  Shield, Bot, Search, Tag, Users, Zap, Star, Package, Cat,
}

export default function AppIcon({ icon, iconUrl, size = 20 }: { icon?: string; iconUrl?: string; size?: number }) {
  const [imgFailed, setImgFailed] = useState(false)
  if (iconUrl && !imgFailed) {
    return <img src={iconUrl} alt="" className="rounded-lg object-contain" style={{ width: size, height: size }} onError={() => setImgFailed(true)} />
  }
  const Icon = icon && ICON_MAP[icon] ? ICON_MAP[icon] : Package
  return <Icon size={size} />
}
