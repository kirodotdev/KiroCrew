/** Alpha-aware theme color backed by a CSS variable.
 *
 * Theme tokens are CSS custom properties (hex strings resolved at runtime),
 * so Tailwind cannot inline an alpha channel the way it does for static
 * palette colors. Without an `<alpha-value>` hook, every opacity-modifier
 * utility on these tokens (`border-border/30`, `text-muted/50`,
 * `bg-accent/10`, …) is silently DROPPED from the build — borders then fall
 * back to Preflight's default #e5e7eb, which renders as glaring white lines
 * in dark mode, and translucent fills/text simply lose their styling.
 *
 * The fix: when Tailwind supplies an alpha (`text-muted/50` → alpha 0.5),
 * emit a color-mix() that applies that opacity to the runtime var. Plain
 * usage (`text-muted`) keeps emitting `var(--muted)` unchanged. color-mix
 * is already a hard dependency of this config (see info-subtle below).
 */
const withAlpha = (cssVar) => ({ opacityValue }) =>
  opacityValue === undefined
    ? `var(${cssVar})`
    : `color-mix(in srgb, var(${cssVar}) calc(${opacityValue} * 100%), transparent)`

/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  darkMode: ['selector', '[data-theme="dark"]'],
  theme: {
    extend: {
      colors: {
        bg: withAlpha('--bg'),
        'bg-accent': withAlpha('--bg-accent'),
        'bg-elevated': withAlpha('--bg-elevated'),
        'bg-hover': withAlpha('--bg-hover'),
        card: withAlpha('--card'),
        'card-fg': withAlpha('--card-fg'),
        chrome: withAlpha('--chrome'),
        text: withAlpha('--text'),
        'text-strong': withAlpha('--text-strong'),
        muted: withAlpha('--muted'),
        'muted-fg': withAlpha('--muted-fg'),
        'muted-strong': withAlpha('--muted-strong'),
        border: withAlpha('--border'),
        'border-strong': withAlpha('--border-strong'),
        accent: withAlpha('--accent'),
        'accent-fg': withAlpha('--accent-fg'),
        'accent-hover': withAlpha('--accent-hover'),
        'accent-subtle': withAlpha('--accent-subtle'),
        'accent-glow': withAlpha('--accent-glow'),
        ring: withAlpha('--ring'),
        ok: withAlpha('--ok'),
        'ok-fg': withAlpha('--ok-fg'),
        'ok-subtle': withAlpha('--ok-subtle'),
        warn: withAlpha('--warn'),
        'warn-fg': withAlpha('--warn-fg'),
        'warn-subtle': withAlpha('--warn-subtle'),
        danger: withAlpha('--danger'),
        'danger-fg': withAlpha('--danger-fg'),
        'danger-subtle': withAlpha('--danger-subtle'),
        info: withAlpha('--info'),
        'info-fg': withAlpha('--info-fg'),
        // Matches the warn/danger/ok "-subtle" pattern, but derived from
        // --info via color-mix so every theme gets a translucent info fill
        // without adding a per-theme var. Consumed by _SYNC_TONES.info in
        // ArtifactDetailPage; without it `bg-info-subtle` mapped to no color
        // and the info-tone sync banner rendered a transparent background.
        'info-subtle': 'color-mix(in srgb, var(--info) 12%, transparent)',
        aim: withAlpha('--aim'),
        'aim-fg': withAlpha('--aim-fg'),
        'aim-subtle': withAlpha('--aim-subtle'),
        clarify: withAlpha('--clarify'),
        'clarify-subtle': withAlpha('--clarify-subtle'),
        'diff-add': withAlpha('--diff-add'),
        'diff-add-text': withAlpha('--diff-add-text'),
        'diff-del': withAlpha('--diff-del'),
        'diff-del-text': withAlpha('--diff-del-text'),
        'diff-hunk': withAlpha('--diff-hunk'),
        'diff-hunk-text': withAlpha('--diff-hunk-text'),
        'diff-meta-text': withAlpha('--diff-meta-text'),
      },
      fontFamily: {
        body: ['var(--font-body)'],
        mono: ['var(--mono)'],
      },
      borderRadius: {
        sm: 'var(--radius-sm)',
        md: 'var(--radius-md)',
        lg: 'var(--radius-lg)',
        xl: 'var(--radius-xl)',
      },
      boxShadow: {
        sm: 'var(--shadow-sm)',
        md: 'var(--shadow-md)',
        lg: 'var(--shadow-lg)',
      },
      keyframes: {
        rise: { from: { opacity: '0', transform: 'translateY(8px)' }, to: { opacity: '1', transform: 'translateY(0)' } },
        'fade-in': { from: { opacity: '0' }, to: { opacity: '1' } },
        'slide-up': { from: { opacity: '0', transform: 'translateY(12px)' }, to: { opacity: '1', transform: 'translateY(0)' } },
        'slide-in-right': { from: { opacity: '0', transform: 'translateX(16px)' }, to: { opacity: '1', transform: 'translateX(0)' } },
        'slide-in-left': { from: { opacity: '0', transform: 'translateX(-16px)' }, to: { opacity: '1', transform: 'translateX(0)' } },
        'scale-in': { from: { opacity: '0', transform: 'scale(.92)' }, to: { opacity: '1', transform: 'scale(1)' } },
        shimmer: { '0%': { backgroundPosition: '-200% 0' }, '100%': { backgroundPosition: '200% 0' } },
        /* Indeterminate progress: a review that is genuinely at 0% for minutes
           needs to read as working, not stalled. */
        'sage-sweep': { '0%': { transform: 'translateX(-100%)' }, '100%': { transform: 'translateX(400%)' } },
        blink: { '0%,100%': { opacity: '1' }, '50%': { opacity: '0' } },
        'dot-breathe': { '0%,100%': { opacity: '.6', transform: 'scale(.9)' }, '50%': { opacity: '1', transform: 'scale(1.1)' } },
        'brand-glow': { '0%': { opacity: '.6', transform: 'scale(1)' }, '100%': { opacity: '1', transform: 'scale(1.05)' } },
        'gradient-shift': { '0%': { backgroundPosition: '0% 50%' }, '50%': { backgroundPosition: '100% 50%' }, '100%': { backgroundPosition: '0% 50%' } },
        'msg-highlight': { '0%': { boxShadow: 'inset 0 0 0 2px var(--accent)' }, '100%': { boxShadow: 'inset 0 0 0 0px transparent' } },
        float: { '0%,100%': { transform: 'translateY(0)' }, '50%': { transform: 'translateY(-6px)' } },
        /* margin-based (NOT transform): a transformed ancestor becomes a
           backdrop root and breaks descendants' backdrop-filter blur.
           Desktop is a fixed 400px sheet, so a px offset clears it. Mobile is
           full-width up to the 767px breakpoint, where -420px would leave the
           sheet half on screen — percentage margins resolve against the
           containing block's width, so -100% always clears it exactly. */
        'nc-slide-in': { from: { marginRight: '-420px' }, to: { marginRight: '0px' } },
        'nc-slide-out': { from: { marginRight: '0px' }, to: { marginRight: '-420px' } },
        'nc-slide-in-full': { from: { marginRight: '-100%' }, to: { marginRight: '0px' } },
        'nc-slide-out-full': { from: { marginRight: '0px' }, to: { marginRight: '-100%' } },
        /* EXIT halves of `fade-in` / `scale-in`, for a Radix-driven overlay.
           Radix `Presence` defers unmount until a CSS ANIMATION ends — it does
           not observe transitions — so a closing dialog needs a real keyframe
           or it vanishes instantly while its entrance is animated. */
        'fade-out': { from: { opacity: '1' }, to: { opacity: '0' } },
        'scale-out': { from: { opacity: '1', transform: 'scale(1)' }, to: { opacity: '0', transform: 'scale(.96)' } },
      },
      animation: {
        'sage-sweep': 'sage-sweep 1.4s ease-in-out infinite',
        rise: 'rise .35s cubic-bezier(.16,1,.3,1) backwards',
        'fade-in': 'fade-in .2s ease',
        'slide-up': 'slide-up .3s cubic-bezier(.16,1,.3,1) backwards',
        'slide-in-right': 'slide-in-right .3s cubic-bezier(.16,1,.3,1) backwards',
        'slide-in-left': 'slide-in-left .25s cubic-bezier(.16,1,.3,1) backwards',
        'scale-in': 'scale-in .2s cubic-bezier(.16,1,.3,1) backwards',
        shimmer: 'shimmer 1.5s ease-in-out infinite',
        blink: 'blink .6s step-end infinite',
        'dot-breathe': 'dot-breathe 2s ease-in-out infinite',
        'brand-glow': 'brand-glow 5s ease-in-out infinite alternate',
        'gradient-shift': 'gradient-shift 20s ease infinite',
        'msg-highlight': 'msg-highlight 2s ease-out forwards',
        float: 'float 3s ease-in-out infinite',
        'nc-slide-in': 'nc-slide-in .32s cubic-bezier(.16,1,.3,1) backwards',
        'nc-slide-out': 'nc-slide-out .24s cubic-bezier(.3,0,.8,.15) forwards',
        'nc-slide-in-full': 'nc-slide-in-full .32s cubic-bezier(.16,1,.3,1) backwards',
        'nc-slide-out-full': 'nc-slide-out-full .24s cubic-bezier(.3,0,.8,.15) forwards',
        'fade-out': 'fade-out .15s ease forwards',
        'scale-out': 'scale-out .15s cubic-bezier(.3,0,.8,.15) forwards',
      },
    },
  },
  plugins: [],
}
