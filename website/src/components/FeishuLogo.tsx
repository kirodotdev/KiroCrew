import feishuLogoUrl from '../assets/feishu-logo.svg'

/**
 * Feishu (飞书 / Lark) channel mark. Importing the asset lets Vite emit a hashed
 * URL under /assets, which the production gateway serves (same treatment as
 * webex-logo.svg — see vite.config.ts).
 *
 * The bundled SVG is Feishu's own artwork, taken unmodified from the developer
 * console the settings panel links to (`open.feishu.cn`); see the provenance
 * comment in `src/assets/feishu-logo.svg`. Every call site — the channel list,
 * the panel header, `ChannelBrandIcon` — renders it through this component, so
 * that one file is the only place to touch if the mark changes.
 */
export function FeishuLogo({ size = 16 }: { size?: number }) {
  return <img src={feishuLogoUrl} width={size} height={size} alt="" aria-hidden="true" />
}
