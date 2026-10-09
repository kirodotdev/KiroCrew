/**
 * The dashboard's look: bot branding, custom themes (list, create, install,
 * update, delete, detail), and the server-authoritative theme boot and
 * display config.
 */

import type { ClientTransport } from './transport'

export function createThemesEndpoints({ post, put, del, j }: ClientTransport) {
  const branding = {
    branding: () => fetch('/api/dashboard/branding').then(j) as Promise<{ bot_name: string; avatar: string; direct_local?: boolean }>,
  }

  const themeList = {
    // Custom Themes
    themes: () => fetch('/api/themes').then(j),
  }

  const themeEditing = {
    createTheme: (body: object) => post('/api/themes', body).then(j),
    installTheme: (source: { type: 'local'; path: string } | { type: 'github'; url: string }) =>
      post('/api/themes/install', { source }).then(j),
    updateTheme: (slug: string, body: object) => put('/api/themes/' + encodeURIComponent(slug), body).then(j),
    deleteTheme: (slug: string) => del('/api/themes/' + encodeURIComponent(slug)).then(j),
    themeDetail: (slug: string) => fetch('/api/themes/' + encodeURIComponent(slug)).then(j),
    // Workspace theme config (server-authoritative)
    themeBoot: () => fetch('/api/theme/boot').then(j),
    updateThemeConfig: (body: {
      mode?: string
      color?: string
      /** BCP-47 UI language tag; '' means follow the browser. */
      language?: string
      onboarded?: boolean
      import_onboarded?: boolean
      /** Gates the gateway's first heartbeat; see `beacon.telemetry_permitted`. */
      privacy_acked?: boolean
    }) =>
      put('/api/config/theme', body).then(j),
  }

  return { branding, themeList, themeEditing }
}
