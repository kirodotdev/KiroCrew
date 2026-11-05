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

/** The edge-challenge banner's reload button, and the session banner's scan button. */
export const AUTH_BANNER_BUTTON_CSS =
  'margin-left:12px;padding:4px 12px;border-radius:4px;border:1px solid #fff;'
  + 'background:transparent;color:#fff;font-size:13px;cursor:pointer;'

/**
 * The full-screen camera overlay the session banner's QR scan opens (`api/qrScan.ts`).
 * Above the banner's own z-index so the viewfinder covers it while the camera is on.
 */
export const QR_SCAN_OVERLAY_CSS =
  'position:fixed;inset:0;z-index:100000;display:flex;flex-direction:column;'
  + 'align-items:center;justify-content:center;gap:12px;background:#000;color:#fff;'
  + 'font:14px/1.5 system-ui;text-align:center;padding:16px;'

/** The live camera preview inside the overlay: fills the width, keeps the aspect. */
export const QR_SCAN_VIDEO_CSS =
  'width:100%;max-width:480px;max-height:60vh;border-radius:8px;background:#111;object-fit:cover;'

/** The overlay's cancel button: the banner button without its inline left margin. */
export const QR_SCAN_CANCEL_CSS =
  'padding:6px 16px;border-radius:4px;border:1px solid #fff;'
  + 'background:transparent;color:#fff;font-size:14px;cursor:pointer;'
