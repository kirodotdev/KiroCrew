// Test fixture (website/playwright/link-preview-behind-auth-proxy.spec.ts): a card that
// shows the hovered URL. It exports activate, as every provider does, and registers it,
// which is what lets the frame load it when its own request for this file is refused.
export function activate(ctx) {
  const card = document.createElement('div')
  card.id = 'probe-card'
  card.style.cssText = 'font: 13px system-ui; padding: 10px 12px; border-radius: 8px;'
  document.body.append(card)
  const paint = (theme) => {
    card.style.background = theme === 'dark' ? '#1f2330' : '#ffffff'
    card.style.color = theme === 'dark' ? '#e8eaf0' : '#1d1f24'
    card.style.border = theme === 'dark' ? '1px solid #3a3f4f' : '1px solid #d8dbe2'
  }
  paint(ctx.theme())
  return {
    show(link) {
      paint(link.theme)
      card.textContent = 'Probe card: ' + (link.url || link.text || '')
      link.card.resize({ width: 320, height: 56 })
      link.card.open()
    },
    theme(t) { paint(t) },
  }
}

globalThis.kirocrew?.register(activate)
