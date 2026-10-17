// Vendor stub: re-exports lucide-react from the host.
const m = window.__kirocrew_modules?.['lucide-react']
if (!m) throw new Error('[vendor/lucide-react] Host modules not initialized.')
// Re-export everything — lucide-react has hundreds of icon components
const handler = { get: (_, prop) => m[prop] }
export default new Proxy(m, handler)
// Named exports for the most common icons (apps can use any via the default export)
export const {
  AlertTriangle, ArrowLeft, ArrowRight, ArrowUp, Bell, Bot, Brain, Building2,
  Calendar, Check, ChevronRight, Clock, Code, Download, ExternalLink,
  Gamepad2, Heart, Home, Loader2, Menu, MessageSquare, Moon, Package,
  Plug, Plus, Power, RefreshCw, Rocket, Search, Settings, Shield, Sparkles,
  Star, Sun, Tag, Trash2, Users, Wand2, Waves, X, Zap,
} = m
