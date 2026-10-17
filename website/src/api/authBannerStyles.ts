/**
 * Inline stylesheet text for the fixed auth banners `api/client.ts` injects.
 *
 * Kept out of `client.ts` so it can be exempted from the i18n literal gate by path:
 * every export is CSS declaration text handed to `style.cssText`. The module imports
 * nothing and touches no DOM, so nothing here has a path to the screen as words.
 */

/** The fixed red strip both auth banners share. */
export const AUTH_BANNER_CSS =
  'position:fixed;top:0;left:0;right:0;z-index:99999;background:#b91c1c;color:#fff;'
  + 'padding:12px 20px;text-align:center;font:14px/1.5 system-ui;'

/** The edge-challenge banner's reload button. */
export const AUTH_BANNER_BUTTON_CSS =
  'margin-left:12px;padding:4px 12px;border-radius:4px;border:1px solid #fff;'
  + 'background:transparent;color:#fff;font-size:13px;cursor:pointer;'
