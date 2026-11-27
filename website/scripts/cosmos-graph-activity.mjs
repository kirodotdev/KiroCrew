/**
 * Point-only activation: arrival and packed hold are uploaded once per recall.
 * The clock uniforms drive the strike, brightness flickers and shared afterglow.
 * shortcut: the dev pre-bundle omits this transform; verify with a built preview
 * until Vite's pre-bundle supports the same hook.
 */
const MARKER = '// kiro-crew: graph activity'
const UNIFORM_TYPES = '          kcClock: "vec4<f32>",\n          kcCentre: "vec4<f32>",\n          kcLight: "vec4<f32>"'
const UNIFORM_DEFAULTS = '          kcClock: [0, 0, 0, 0],\n          kcCentre: [0, 0, 0, 0],\n          kcLight: [0, 0, 0, 0]'
const UNIFORM_READS = '      kcClock: t.kcActivity ? t.kcActivity.clock : [0, 0, 0, 0],\n      kcCentre: t.kcActivity ? t.kcActivity.centre : [0, 0, 0, 0],\n      kcLight: t.kcActivity ? t.kcActivity.light : [0, 0, 0, 0]'

export const ACTIVITY_GLSL = `
  ${MARKER}
  if (imageIndex >= 0.0 && kcLight.z > 0.0) {
    float age = kcClock.x - imageIndex;
    // Negative packed hold identifies the compact seed, fully lit on first draw.
    float hold = floor(abs(imageSize));
    float riseMs = imageSize < 0.0 ? 0.0 : kcLight.x;
    float rise = imageSize < 0.0 ? step(0.0, age) : smoothstep(0.0, riseMs, age);
    float fade = clamp((age - riseMs - hold) / kcLight.z, 0.0, 1.0);
    float tail = (exp(-2.5 * fade) - exp(-2.5)) / (1.0 - exp(-2.5));
    float kcA = rise * tail * fract(abs(imageSize));
    float low = min(pointColor.r, min(pointColor.g, pointColor.b));
    float high = max(pointColor.r, max(pointColor.g, pointColor.b));
    vec3 hue = (pointColor.rgb - low) / max(0.001, high - low);
    // HSL: retain hue and lightness, raise saturation to 98% on light themes.
    float lightness = (high + low) * 0.5;
    float chroma = (1.0 - abs(2.0 * lightness - 1.0)) * 0.98;
    vec3 lightLit = vec3(lightness) + chroma * (hue - 0.5);
    vec3 darkLit = mix(vec3(0.03), vec3(1.0), hue);
    vec3 lit = mix(lightLit, darkLit, kcLight.w);
    if (high - low < 0.01) lit = pointColor.rgb;
    float flashAge = age - riseMs - hold + 320.0;
    float flash;
    if (flashAge < 0.0) flash = 0.0;
    else if (flashAge < 60.0) {
      float p = flashAge / 60.0 - 1.0;
      flash = 1.0 + 2.70158 * p * p * p + 1.70158 * p * p;
    }
    else if (flashAge < 100.0) flash = 1.0 - 0.78 * (flashAge - 60.0) / 40.0;
    else if (flashAge < 120.0) flash = 0.22;
    else if (flashAge < 140.0) flash = 0.22 + 0.5 * (flashAge - 120.0) / 20.0;
    else if (flashAge < 180.0) flash = 0.72 - 0.54 * (flashAge - 140.0) / 40.0;
    else if (flashAge < 220.0) flash = 0.18;
    else if (flashAge < 240.0) flash = 0.18 + 0.32 * (flashAge - 220.0) / 20.0;
    else if (flashAge < 320.0) flash = 0.5 - 0.25 * (flashAge - 240.0) / 80.0;
    else flash = 0.25 * tail;
    pointColor.rgb = mix(pointColor.rgb, lit, kcA);
    float flashL = 0.73;
    vec3 flashLit = vec3(flashL) + (1.0 - abs(2.0 * flashL - 1.0)) * 0.98 * (hue - 0.5);
    pointColor.rgb = mix(pointColor.rgb, flashLit, min(1.0, flash) * kcLight.w);
    pointColor.a = mix(pointColor.a, 1.0, min(1.0, flash * 0.45));
    float sizeFlash = flashAge >= 60.0 ? tail : flash;
    pointSize *= 1.0 + kcCentre.z * kcA + 0.36 * sizeFlash;
  }
  pointColor.rgb *= 1.0 + kcClock.z * sin(kcClock.y - length(point - kcCentre.xy) * kcClock.w);
`

const REWRITES = [
  ['  setPointImageSizes(e) {\n', `  setPointActivity(at, strength) {
    if (this._isDestroyed || !this.points || this.graph.pointsNumber !== at.length) return;
    this.graph.inputPointImageIndices = this.graph.pointImageIndices = at;
    this.graph.inputPointImageSizes = this.graph.pointImageSizes = strength;
    this.points.updateImageIndices();
    this.points.updateImageSizes();
    this.requestRender();
  }
  setPointImageSizes(e) {
`],
  ['  float animateStrokeWidths;\n} drawVertex;', '  float animateStrokeWidths;\n  vec4 kcClock;\n  vec4 kcCentre;\n  vec4 kcLight;\n} drawVertex;'],
  ['#define animateStrokeWidths drawVertex.animateStrokeWidths\n', '#define animateStrokeWidths drawVertex.animateStrokeWidths\n#define kcClock drawVertex.kcClock\n#define kcCentre drawVertex.kcCentre\n#define kcLight drawVertex.kcLight\n'],
  ['uniform float animateStrokeWidths;\n', 'uniform float animateStrokeWidths;\nuniform vec4 kcClock;\nuniform vec4 kcCentre;\nuniform vec4 kcLight;\n'],
  ['    : resolveColor(targetColor, exit);\n', '    : resolveColor(targetColor, exit);\n' + ACTIVITY_GLSL],
  ['          animateStrokeWidths: "f32"\n', '          animateStrokeWidths: "f32",\n' + UNIFORM_TYPES + '\n'],
  ['          animateStrokeWidths: 0\n', '          animateStrokeWidths: 0,\n' + UNIFORM_DEFAULTS + '\n'],
  ['      animateStrokeWidths: this.shouldAnimateStrokeWidths ? 1 : 0\n', '      animateStrokeWidths: this.shouldAnimateStrokeWidths ? 1 : 0,\n' + UNIFORM_READS + '\n'],
]

/** Refuse an upstream shader change instead of silently dropping animation. */
export function addGraphActivity(code) {
  if (code.includes(MARKER)) return code
  let out = code
  for (const [anchor, replacement] of REWRITES) {
    if (out.split(anchor).length !== 2) {
      throw new Error(`cosmos-graph-activity: the point shader or its uniforms in @cosmos.gl/graph changed (${JSON.stringify(anchor.trim().slice(0, 40))}); update scripts/cosmos-graph-activity.mjs for the new version.`)
    }
    out = out.replace(anchor, replacement)
  }
  return out
}
export function cosmosGraphActivityPlugin() {
  return {
    name: 'cosmos-graph-activity',
    enforce: 'pre',
    transform(code, id) {
      if (!/[\\/]@cosmos\.gl[\\/]graph[\\/]dist[\\/]index\.js$/.test(id.split('?')[0])) return null
      return { code: addGraphActivity(code), map: null }
    },
  }
}
