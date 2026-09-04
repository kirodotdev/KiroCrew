"use strict";

/**
 * Map the dashboard's dark/light MODE PREFERENCE onto `nativeTheme.themeSource`.
 *
 * `themeSource` is not just a native-chrome switch: setting it to `dark` or
 * `light` also overrides `prefers-color-scheme` in every renderer. That is the
 * media query the dashboard's Auto mode resolves through, so feeding the
 * *resolved* mode back in pins the query to whatever Auto resolved to at first
 * load and OS appearance changes stop propagating — Auto freezes. Electron's
 * docs prescribe the mapping this function implements:
 *
 *   Follow OS -> "system" · Dark -> "dark" · Light -> "light"
 *
 * @param {unknown} pref  `data-mode-pref`: "system" | "dark" | "light", or ""
 *   on a dashboard build predating that attribute.
 * @param {unknown} resolvedMode  `data-mode`: "dark" | "light". Used only as the
 *   fallback when `pref` is missing/unrecognised, which keeps an older bundle at
 *   its previous behaviour instead of forcing it to "system".
 * @returns {"system" | "dark" | "light"}
 */
function resolveThemeSource(pref, resolvedMode) {
  if (pref === "system" || pref === "dark" || pref === "light") return pref;
  if (resolvedMode === "dark" || resolvedMode === "light") return resolvedMode;
  return "system";
}

module.exports = { resolveThemeSource };
