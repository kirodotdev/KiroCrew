"use client";

/*
 * Liquid Glass surface. This is a MATERIAL primitive, not an icon: the SVG
 * strings below are a displacement map and a mask handed to the CSS filter and
 * mask parsers (`feImage`, `mask-image: url(data:…)`), and the path data is the
 * panel's own rounded outline. Product icons stay on lucide.
 *
 * Copied in from liquid-glass-cli (MIT) so the repo owns it, then cut down to
 * what the composer and the mobile Settings capsule use. Corners are circular
 * (the chrome that clips and borders both panes is a circular border-radius,
 * and a squircle rim would sit ~1.4px outside that arc at every corner), the
 * bend, its depth and spread, the light direction and the tint (the theme's
 * `--glass-tint`, fixed per polarity) are constants both panes share, and
 * upstream's tint prop, tint opacity, chromatic dispersion, drop shadow, rim
 * stroke are gone — the host carries the caller's shadow class, and the edge
 * is two lit bands (each backed by a
 * half-pixel dark hairline, `--glass-hairline`) plus a 1px side line
 * (`--glass-edge`) drawn here, never a ring. The three values the two panes do
 * set differently (radius, frost, light) are the only optical props, and they
 * are required: a default nobody renders is a second design nobody sees.
 * The specular ring and bevel differ from upstream on purpose; see the notes
 * at each.
 *
 * The HOST IS THE CONTROL. A follow-up chip is a `<button>`, the split chip's
 * wrapper is a `<span>`, and the tests, the entrance animation and the flex row
 * all address that one element — so the pane does not wrap the control in a
 * box of its own; the control is rendered as the pane's host (`as`), takes the
 * caller's className / style / attributes, and the effect layers sit INSIDE it
 * at `z-index: -1` in the host's own stacking context (`isolation: isolate`),
 * under the children, which render directly. The layers are `<span>`s so a
 * `<button>` host stays valid phrasing content, and every layer (and the
 * filter's zero-size `<svg>`) carries `data-liquid-glass-layer`: that
 * attribute, not `aria-hidden`, is what the solidifying rules in index.css
 * hide, because the children render directly and a decorative icon among them
 * (`<Lightbulb aria-hidden>`) is a direct child too. Sizing reads the host's
 * padding box (`clientWidth`), which is the box the layers span, not the
 * content box a padded control would report.
 *
 * The host carries the stable `liquid-glass` class so index.css can solidify it
 * under prefers-reduced-transparency / prefers-contrast / no backdrop-filter;
 * `glass-accent` / `glass-warn` / `glass-hover` (index.css) swap `--glass-tint`
 * on the host for a hue-mixed step, so a picked chip or a pending approval
 * stays the same material.
 */

import {
  forwardRef,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type HTMLAttributes,
  type JSX,
  type ReactElement,
  type ReactNode,
  type Ref,
} from "react";

/** The elements a pane can be: a box, an inline wrapper, or the control itself. */
export type GlassHostTag = "div" | "span" | "button";

export interface LiquidGlassOwnProps {
  /** Which element the pane IS. Default `div`. */
  as?: GlassHostTag;
  children?: ReactNode;
  /** Corner radius in pixels. */
  cornerRadius: number;
  /** Backdrop blur in pixels. */
  frost: number;
  /** Strength of the bevel shading and of the top / bottom light bands (25 =
   *  the theme's full `--glass-band`). 0–100. */
  lightIntensity: number;
}

type HostAttributes<T extends GlassHostTag> = Omit<
  JSX.IntrinsicElements[T],
  keyof LiquidGlassOwnProps | "ref"
>;

export type LiquidGlassProps<T extends GlassHostTag = "div"> = LiquidGlassOwnProps & { as?: T } & HostAttributes<T>;

/** Fill layered on top of the refracted backdrop: the polarity-fixed theme token. */
const TINT = "var(--glass-tint)";
/** Largest displacement map we rasterise; bigger elements sample a scaled copy. */
const MAP_MAX = 512;
/**
 * How long the host's size must hold still before the map is re-rasterised.
 * A resize drag or a wrapping line delivers a stream of sizes; the map already
 * on screen stretches to each of them and is rebuilt once, at rest.
 */
const SETTLE_MS = 120;
/** How strongly the backdrop bends at the edges, 0–1. Half strength: visible on
 *  large shapes, imperceptible over 13px type. */
const REFRACTION = 0.5;
/** Thickness of the refracting edge band as a share of the maximum. */
const DEPTH = 0.45;
/** The top / bottom light bands take their color from the theme's `--glass-band`
 *  (index.css; read from data-mode, so the band is near-white on a light page
 *  and a faint white on smoked glass). lightIntensity scales it: at 25 the band
 *  is the full token, the chip variant's 18 is ~70% of it. */
const BAND_FULL_LIGHT = 25;
/** How far the bend spreads inward from the rim: flattens the profile's shoulder. */
const SHOULDER = 1 - 0.62 * 0.18;
/** Refractive index of the bevel. Roughly crown glass. */
const IOR = 1.46;
/** Deviation at a grazing rim, used to normalise the profile to 0–1. */
const MAX_DEVIATION = Math.tan(Math.PI / 2 - Math.asin(1 / IOR));

const clamp01 = (v: number) => (v < 0 ? 0 : v > 1 ? 1 : v);

/** Signed distance to a rounded rectangle centred on the origin. Negative inside. */
function sdRoundRect(px: number, py: number, hw: number, hh: number, r: number): number {
  const qx = Math.abs(px) - hw + r;
  const qy = Math.abs(py) - hh + r;
  return Math.hypot(Math.max(qx, 0), Math.max(qy, 0)) + Math.min(Math.max(qx, qy), 0) - r;
}

/** The same shape as an SVG path, so the rim band and the map agree with the box. */
function roundRectPath(w: number, h: number, radius: number): string {
  const r = Math.max(0, Math.min(radius, Math.min(w, h) / 2));
  const p = (v: number) => v.toFixed(2);
  if (r < 0.5) return `M0 0H${p(w)}V${p(h)}H0Z`;
  const arc = (x: number, y: number) => `A${p(r)} ${p(r)} 0 0 1 ${p(x)} ${p(y)}`;
  return (
    `M${p(r)} 0H${p(w - r)}${arc(w, r)}V${p(h - r)}${arc(w - r, h)}` +
    `H${p(r)}${arc(0, h - r)}V${p(r)}${arc(r, 0)}Z`
  );
}

/**
 * Displacement map. Every pixel inside the edge band stores the shape's outward
 * normal scaled by how hard a ray bends at that point on the bevel, so the
 * backdrop is pulled around the corners along the true surface direction rather
 * than along the x and y axes.
 *
 * The bevel is modelled as a quarter-round of thickness `band`: at depth u into
 * it (0 at the rim, 1 at the inner edge) the surface tilts by asin(1 - u), and
 * Snell's law turns that tilt into a deviation that climbs steeply over the
 * last few pixels. That concentration is what reads as liquid.
 *
 * Rebuilt once the host's integer size has settled (see SETTLE_MS). The loop
 * is capped at MAP_MAX on the long side and only the band pixels do the trig,
 * so a rebuild at the composer's sizes is a few milliseconds — well inside one
 * frame even when it does land mid-interaction.
 *
 * The map is a pure function of (size, radius, band), and a LIST of panes — the
 * bell popover's rows, a row of follow-up chips — mounts many at the same size
 * in one commit, so the rasterised data URL is memoised across instances in a
 * small bounded cache (`MAP_CACHE_MAX`, oldest key evicted): N equal rows cost
 * one canvas encode, not N.
 */
const MAP_CACHE_MAX = 64;
const mapCache = new Map<string, string>();

function displacementMapFor(width: number, height: number, radius: number, band: number): string {
  const key = `${width}x${height}r${radius}b${band}`;
  const hit = mapCache.get(key);
  if (hit !== undefined) return hit;
  const map = buildDisplacementMap(width, height, radius, band);
  // No 2D context yields "" (the pane renders without its bend); that is a
  // property of the host, not of the size, so it is never remembered.
  if (map === "") return map;
  if (mapCache.size >= MAP_CACHE_MAX) {
    const oldest = mapCache.keys().next().value;
    if (oldest !== undefined) mapCache.delete(oldest);
  }
  mapCache.set(key, map);
  return map;
}

/** Test seam: the cache outlives a render, so a test counting rasterisations
 *  starts from an empty one. */
export function resetDisplacementMapCache(): void {
  mapCache.clear();
}

function buildDisplacementMap(width: number, height: number, radius: number, band: number): string {
  const fit = Math.min(1, MAP_MAX / Math.max(width, height));
  const w = Math.max(2, Math.round(width * fit));
  const h = Math.max(2, Math.round(height * fit));
  const r = Math.max(0, Math.min(radius, Math.min(width, height) / 2)) * fit;
  const t = Math.max(1, band * fit);

  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d");
  if (!ctx) return "";

  const image = ctx.createImageData(w, h);
  const data = image.data;
  const hw = w / 2;
  const hh = h / 2;
  const eps = 0.75;

  for (let y = 0; y < h; y++) {
    const py = y + 0.5 - hh;
    for (let x = 0; x < w; x++) {
      const i = (y * w + x) * 4;
      const px = x + 0.5 - hw;
      let red = 128;
      let blue = 128;
      const dist = sdRoundRect(px, py, hw, hh, r);

      if (dist > -t * 1.4 && dist < 0.5) {
        // The outward normal, by central difference.
        const gx = sdRoundRect(px + eps, py, hw, hh, r) - sdRoundRect(px - eps, py, hw, hh, r);
        const gy = sdRoundRect(px, py + eps, hw, hh, r) - sdRoundRect(px, py - eps, hw, hh, r);
        const glen = Math.hypot(gx, gy) || 1;
        const u = clamp01(-dist / t);
        const tilt = Math.asin(clamp01(1 - u));
        const deviation = Math.tan(tilt - Math.asin(Math.sin(tilt) / IOR)) / MAX_DEVIATION;
        // Snell's curve reaches zero with slope to spare, which would leave a
        // visible crease where the band meets the flat centre. Smoothstep the
        // inner third so both the offset and its gradient land at zero.
        const k = clamp01((1 - u) / 0.35);
        let m = Math.pow(clamp01(deviation), SHOULDER) * k * k * (3 - 2 * k);
        // Feather the outermost pixel so the rim does not alias.
        if (dist > -0.5) m *= clamp01(0.5 - dist);
        red = Math.round(128 + 127 * (gx / glen) * m);
        blue = Math.round(128 + 127 * (gy / glen) * m);
      }

      data[i] = red;
      data[i + 1] = 128;
      data[i + 2] = blue;
      data[i + 3] = 255;
    }
  }

  ctx.putImageData(image, 0, 0);
  return canvas.toDataURL();
}

/** A CSS mask that keeps only a soft band hugging the shape's outline. */
function outlineMask(w: number, h: number, path: string, width: number, blur: number): string {
  const stroke = (sw: number, opacity: number) =>
    `<path d="${path}" fill="none" stroke="#fff" stroke-opacity="${opacity}" stroke-width="${sw.toFixed(
      2
    )}"/>`;
  const body = stroke(width, 0.28) + stroke(width * 0.55, 0.55) + stroke(width * 0.22, 1);
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}">` +
    `<defs><filter id="s" x="-50%" y="-50%" width="200%" height="200%">` +
    `<feGaussianBlur stdDeviation="${blur.toFixed(2)}"/></filter></defs>` +
    `<g filter="url(#s)">${body}</g>` +
    `</svg>`;
  return `url("data:image/svg+xml;utf8,${encodeURIComponent(svg)}")`;
}

/**
 * Specular ring. A lit bevel flares where its normal faces the light, then
 * again — more faintly — on the far arc where the light leaves the glass. That
 * twin highlight is what makes Apple's edges read as a solid rim of material
 * rather than a drawn border.
 */
function specularRing(lightIntensity: number, radius: number): string {
  // Even band along the top and bottom edges. On the sides it fades to CLEAR
  // just past the corner arc — measured in px from the corner radius, not as a
  // share of the height — so a tall panel and a thin capsule wear the same rim.
  // The flanks stay unlit: their boundary is the crisp `--glass-edge` side line
  // the bevel layer draws (a line, not a glow — the reference material sets its
  // two lit edges against thin dark sides, and nothing runs around the corners).
  const share = Math.min(100, Math.round((lightIntensity / BAND_FULL_LIGHT) * 100));
  const a = (k: number) =>
    `color-mix(in srgb, var(--glass-band) ${Math.round(share * k)}%, transparent)`;
  const r1 = (radius * 0.7).toFixed(1);
  const r2 = (radius * 1.5).toFixed(1);
  // A 2px plateau at full strength before the fall-off: on a light page the
  // band is white on near-white, and a single-pixel peak read as no band at
  // all; two pixels of #ffffff inside the hairline is what the maintainer
  // measured the reference at.
  return `linear-gradient(to bottom, ${a(1)} 0px, ${a(1)} 2px, ${a(0.35)} ${r1}px, transparent ${r2}px, transparent calc(100% - ${r2}px), ${a(0.35)} calc(100% - ${r1}px), ${a(1)} calc(100% - 2px), ${a(1)} 100%)`;
}

type Size = { width: number; height: number };

/** The bevel is a fixed slice of the smaller side, capped so large panels keep
 *  a rim instead of turning into one big lens. */
const bandFor = (s: Size) => Math.max(2, DEPTH * Math.min(Math.min(s.width, s.height) * 0.18, 26));

/** The implementation's view: the optics plus whatever attributes the host takes.
 *  The public signature below narrows the attributes to the element `as` names. */
type AnyHostProps = LiquidGlassOwnProps & Omit<HTMLAttributes<HTMLElement>, keyof LiquidGlassOwnProps>;

function LiquidGlassImpl(
  {
    as: Host = "div",
    children,
    cornerRadius,
    frost,
    lightIntensity,
    className,
    style,
    ...rest
  }: AnyHostProps,
  forwarded: Ref<HTMLElement>
) {
  const hostRef = useRef<HTMLElement | null>(null);
  /** One node, two readers: the measurer here and whatever ref the caller passed. */
  const setHost = useCallback((node: HTMLElement | null) => {
    hostRef.current = node;
    if (typeof forwarded === "function") forwarded(node);
    else if (forwarded) (forwarded as { current: HTMLElement | null }).current = node;
  }, [forwarded]);
  /** Live size: what the layers, the rim mask and the filter's viewport follow at once. */
  const [size, setSize] = useState<Size>({ width: 0, height: 0 });
  /** Settled size: what the displacement map was rasterised for. */
  const [mapSize, setMapSize] = useState<Size>({ width: 0, height: 0 });
  const rawId = useId();
  const filterId = `lg-${rawId.replace(/[^a-zA-Z0-9]/g, "")}`;

  useEffect(() => {
    const el = hostRef.current;
    if (!el) return;
    let settle: ReturnType<typeof setTimeout> | undefined;
    let rasterised = false;
    const same = (a: Size, b: Size) => a.width === b.width && a.height === b.height;
    const observer = new ResizeObserver(([entry]) => {
      // The layers span the host's PADDING box (`inset: 0`), so that is the box
      // to measure; `contentRect` is the content box, which a padded control
      // (a chip's own `px-3 py-1.5`) reports smaller than what the layers cover.
      const width = el.clientWidth || entry.contentRect.width;
      const height = el.clientHeight || entry.contentRect.height;
      const next = { width: Math.round(width), height: Math.round(height) };
      setSize((prev) => (same(prev, next) ? prev : next));
      // The first measurement rasterises at once — a pane with no map has no
      // bend. Every later one waits for the size to hold still, so a drag or a
      // wrapping line costs one rebuild rather than one per step; meanwhile the
      // map on screen is stretched to the live size by the filter's feImage.
      if (!rasterised) {
        rasterised = true;
        setMapSize(next);
        return;
      }
      clearTimeout(settle);
      settle = setTimeout(() => setMapSize((prev) => (same(prev, next) ? prev : next)), SETTLE_MS);
    });
    observer.observe(el);
    return () => {
      observer.disconnect();
      clearTimeout(settle);
    };
  }, []);

  const { width, height } = size;
  const measured = width > 1 && height > 1;
  const band = bandFor(size);

  const map = useMemo(
    () =>
      mapSize.width > 1 && mapSize.height > 1
        ? displacementMapFor(mapSize.width, mapSize.height, cornerRadius, bandFor(mapSize))
        : "",
    [mapSize, cornerRadius]
  );

  const ready = measured && map !== "";
  // feDisplacementMap moves a pixel by scale * (channel - 0.5) and the map only
  // reaches 127/255 at the rim, so double the offset we actually want there.
  const displacement = REFRACTION * band * 2;

  const light = clamp01(lightIntensity / 100);
  const bevel = Math.max(1, band * 0.42);

  /**
   * Every layer is clipped by its own border-radius — which is also what clips a
   * backdrop-filter in every engine — so the frost, the bend, the bevel and the
   * rim all land on the one circular outline the chrome around them uses. They
   * paint at `z-index: -1` inside the host's isolated stacking context: over the
   * host's own (transparent) background, under every child, with no wrapper
   * around the children.
   */
  const layer: CSSProperties = {
    position: "absolute",
    inset: 0,
    zIndex: -1,
    pointerEvents: "none",
    borderRadius: cornerRadius,
    display: "block",
  };

  // The specular band's mask, built once per size for both mask properties.
  const rim =
    ready && band > 2
      ? outlineMask(width, height, roundRectPath(width, height, cornerRadius), band * 1.2, band * 0.3)
      : "";

  return (
    <Host
      {...rest}
      ref={setHost}
      className={className ? `liquid-glass ${className}` : "liquid-glass"}
      // The caller's style rides along (an animation delay, a layout var); the
      // pane's own position, isolation and radius are the material's and win.
      style={{ ...style, position: "relative", isolation: "isolate", borderRadius: cornerRadius }}
    >
      {/* Not an icon: a zero-size host for the backdrop-filter definition. */}
      {ready && (
        <svg width="0" height="0" style={{ position: "absolute" }} aria-hidden="true" data-liquid-glass-layer="">
          <defs>
            <filter
              id={filterId}
              x="0%"
              y="0%"
              width="100%"
              height="100%"
              colorInterpolationFilters="sRGB"
            >
              <feImage
                href={map}
                x="0"
                y="0"
                width={width}
                height={height}
                preserveAspectRatio="none"
                result="map"
              />
              <feDisplacementMap
                in="SourceGraphic"
                in2="map"
                scale={displacement}
                xChannelSelector="R"
                yChannelSelector="B"
                result="bent"
              />
              {/* Smooths the 8-bit steps in the map without softening the panel. */}
              <feGaussianBlur in="bent" stdDeviation="0.35" />
            </filter>
          </defs>
        </svg>
      )}

      {/* refraction */}
      <span
        aria-hidden="true"
        data-liquid-glass-layer=""
        style={{
          ...layer,
          backdropFilter: ready ? `url(#${filterId})` : undefined,
          WebkitBackdropFilter: ready ? `url(#${filterId})` : undefined,
        }}
      />

      {/* frost + tint. Chromium's backdrop blur under-blurs the last ~blur px
          along the far (right and bottom) edges of the element that carries it:
          over 8px stripes the bottom 17px of a 24px blur showed the stripes at a
          third of their contrast while the top edge was flat, and a bare div did
          the same, so it is the engine, not this composition. The blurring box
          is therefore two blur radii larger than the pane on every side and the
          pane clips it, so the under-blurred band lies outside what is shown.
          saturate(1.55): a blurred backdrop reads foggy because blur averages
          hues toward grey; the lift gives what shows through its colour back
          (a white page is unchanged, an image or a colour block under the pane
          keeps its life). */}
      <span aria-hidden="true" data-liquid-glass-layer="" style={{ ...layer, overflow: "hidden" }}>
        <span
          style={{
            position: "absolute",
            inset: -frost * 2,
            display: "block",
            background: TINT,
            // `--glass-tint` steps on hover / focus / accent (index.css); ease it.
            transition: "background-color 0.15s ease",
            backdropFilter: `blur(${frost}px) saturate(1.55)`,
            WebkitBackdropFilter: `blur(${frost}px) saturate(1.55)`,
          }}
        />
      </span>

      {/* bevel shading: light from straight above, so the top edge catches it and
          the bottom edge answers with the fainter far-side flare. The two 1px
          horizontal insets are the SIDE LINES: `--glass-edge` (dark on a light
          page, light on smoked glass) along the left and right edges only — a
          horizontal offset paints nothing on the top and bottom edges and thins
          out through the corner arcs, so the line never closes into a ring. The
          two OUTER half-pixel shadows are the HAIRLINES: a `--glass-hairline`
          sliver just outside the top and bottom edges (a shape shifted 0.5px
          shows only along the edge it moved away from and tapers through the
          arcs), so each lit band sits against a very thin dark line, as the
          reference material's does. Outside the box, so the rim layer above
          cannot cover it. */}
      <span
        aria-hidden="true"
        data-liquid-glass-layer=""
        style={{
          ...layer,
          boxShadow: [
            `inset 1px 0 0 var(--glass-edge)`,
            `inset -1px 0 0 var(--glass-edge)`,
            // The lit edges' crisp core: one full-strength pixel of the band
            // colour just inside each hairline, top and bottom. The soft band
            // below is masked to hug the outline and never reaches full
            // strength at the very edge (a light page measured 252, not 255);
            // this pixel does, so the edge inside the hairline IS the band
            // colour -- #ffffff on a light page. Thins through the arcs like the
            // side lines, so it never closes into a ring.
            `inset 0 1px 0 var(--glass-band)`,
            `inset 0 -1px 0 var(--glass-band)`,
            `0 -0.5px 0 0 var(--glass-hairline)`,
            `0 0.5px 0 0 var(--glass-hairline)`,
            `inset 0px ${bevel.toFixed(2)}px ${(bevel * 1.15).toFixed(2)}px ${(-bevel * 0.5).toFixed(
              2
            )}px rgba(255,255,255,${(0.2 * light).toFixed(3)})`,
            `inset 0px ${(-bevel).toFixed(2)}px ${(bevel * 1.15).toFixed(2)}px ${(-bevel * 0.5).toFixed(
              2
            )}px rgba(255,255,255,${(0.2 * light).toFixed(3)})`,
            `inset 0 0 ${(bevel * 0.9).toFixed(2)}px rgba(0,0,0,${(0.1 * light).toFixed(3)})`,
          ].join(", "),
        }}
      />

      {/* the lit bevel — a soft specular band following the outline. The
          gradient rides a custom property: its stops are `color-mix()` of the
          theme's `--glass-band`, which jsdom's CSSStyleDeclaration drops from
          `background` but stores verbatim on a `--*` property, so the tests can
          read the recipe the browser paints. */}
      {rim && (
        <span
          aria-hidden="true"
          data-liquid-glass-layer=""
          style={{
            ...layer,
            ["--liquid-glass-bands" as string]: specularRing(lightIntensity, cornerRadius),
            background: "var(--liquid-glass-bands)",
            maskImage: rim,
            WebkitMaskImage: rim,
            maskSize: "100% 100%",
            WebkitMaskSize: "100% 100%",
            maskRepeat: "no-repeat",
            WebkitMaskRepeat: "no-repeat",
          }}
        />
      )}

      {children}
    </Host>
  );
}

/** Polymorphic on `as`: the host's own attributes (a button's `type`, `onClick`,
 *  `aria-*`) type-check against the element the pane is rendered as. */
export const LiquidGlass = forwardRef(LiquidGlassImpl) as <T extends GlassHostTag = "div">(
  props: LiquidGlassProps<T> & { ref?: Ref<HTMLElement> }
) => ReactElement;

export default LiquidGlass;
