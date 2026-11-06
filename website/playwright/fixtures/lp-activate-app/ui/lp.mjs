// Test fixture (website/playwright/link-preview-behind-auth-proxy.spec.ts): a card that
// only exports activate, as providers written before the registration contract do.
export function activate(ctx) {
  const card = document.createElement('div')
  card.id = 'probe-card'
  card.style.cssText = 'font: 13px system-ui; padding: 10px 12px;'
  document.body.append(card)
  return {
    show(link) {
      card.textContent = 'Activate-only card: ' + (link.url || '')
      link.card.resize({ width: 320, height: 48 })
      link.card.open()
    },
  }
}
