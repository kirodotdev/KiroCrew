/**
 * Shader text for a Bot's body. This module holds GLSL only: nothing in it is shown to a person.
 *
 * `bendInto` puts the flow of the lower body (`botFlow.ts`) into a material's vertex shader, before the bones move
 * the point. `foldInto` makes the matte skin (`botSkin.ts`) keep only a share of the shade the body's points carry.
 * `paleInkInto` draws the black ink of what a Bot wears in a pale tone.
 */

/** The numbers the bend's lines are built with. `half` is the hem's share half way down, before the middle's hump is taken off. */
export interface BendSet { top: number; lean: number; trail: number; soft: number; tilt: number; half: number }

const WAVE_PARS = `
uniform vec3 flowMid;    // how far the middle of the lower body is from its place
uniform vec3 flowHem;    // and its hem
uniform vec4 flowTurn;   // twist at the middle, twist at the hem (radians), wider or narrower, the swell of the breath
uniform vec2 flowSides;  // the breath's two sides: cos and sin of where they are, going round
uniform vec3 flowFlick;  // how far the flick's tip is from its place
uniform vec4 flowHinge;  // where the flick hangs from, and one over its length
uniform vec3 flowFlick2; // the same two for the second flick, on the body's other side
uniform vec4 flowHinge2;
// Where a point of the body at rest goes. With every number at zero it is the point itself, bit for bit.
// flick: how much of the point is the first flick, and how much the second.
vec3 flowAt( vec3 p, vec2 flick ) {
	float t = clamp( ( FLOW_TOP - p.y ) / FLOW_TOP, 0.0, 1.0 );
	float t2 = t * t;
	// The bend of a thing that hangs: nothing at the top, more and more toward the hem, as t squared. But it must
	// start with no bend at all, or the outline has a corner where the giving starts: so near the top it goes as t
	// cubed. b is the hem's share at this height; a is what the middle adds when it is ahead of the hem or
	// behind it (a hump half way down, nothing at either end).
	float b = ( 1.0 + FLOW_SOFT ) * t2 * t / ( t + FLOW_SOFT );
	float db = ( 1.0 + FLOW_SOFT ) * t2 * ( 2.0 * t + 3.0 * FLOW_SOFT ) / ( ( t + FLOW_SOFT ) * ( t + FLOW_SOFT ) );
	float tu = t * ( 1.0 - t );
	float a = 64.0 * tu * tu * tu, da = 192.0 * tu * tu * ( 1.0 - 2.0 * t );
	b -= FLOW_HALF * a; db -= FLOW_HALF * da;
	vec3 d = flowMid * a + flowHem * b;
	vec2 slope = ( flowMid.xz * da + flowHem.xz * db ) / FLOW_TOP;
	vec2 q = p.xz - vec2( 0.0, FLOW_LEAN * ( p.y - 0.6 ) );
	// The side that trails gives more, the side that leads gives less: the lower body is drawn out along its way.
	d.xz *= 1.0 + FLOW_TRAIL * dot( q, d.xz ) / ( 0.35 * length( d.xz ) + 1e-5 );
	float tw = flowTurn.x * a + flowTurn.y * b;
	float c = cos( tw ), s = sin( tw );
	vec2 r = vec2( c * q.x + s * q.y, c * q.y - s * q.x );
	float q2 = max( dot( q, q ), 1e-6 );
	// The breath swells two opposite sides a little more than the other two, and the pair goes slowly round.
	float sides = ( 2.0 * q.x * q.y * flowSides.x + ( q.y * q.y - q.x * q.x ) * flowSides.y ) / q2;
	float wide = t2 * ( flowTurn.z + flowTurn.w * ( 0.55 + 0.45 * sides ) );
	r *= 1.0 + wide;
	vec3 o = p;
	o.xz += ( r - q ) + d.xz;
	// The lower body bends more than it shears: each level tilts with the bend (the side that swings out goes
	// up), and a hem that swings out rises, as the foot of a pendulum does. With the whole tilt of a true bend, a
	// lot of flow raises a ledge on the side that trails.
	o.y += d.y + FLOW_TILT * dot( r, slope ) + 0.6 * dot( d.xz, d.xz ) / ( FLOW_TOP * max( t, 0.05 ) );
	// A flick swings from where it hangs: its tip goes all the way, its root stays. Moved as one piece, its
	// upper edge stands out of the body's outline as a small ear.
	float reach = clamp( length( p - flowHinge.xyz ) * flowHinge.w, 0.0, 1.0 );
	float reach2 = clamp( length( p - flowHinge2.xyz ) * flowHinge2.w, 0.0, 1.0 );
	o += flick.x * reach * reach * flowFlick + flick.y * reach2 * reach2 * flowFlick2;
	return o;
}
// The way the skin faces after the bend: from where two near points of the skin go.
vec3 flowFacing( vec3 p, vec3 n, vec3 o, vec2 flick ) {
	vec3 tu = normalize( cross( n, abs( n.y ) > 0.9 ? vec3( 1.0, 0.0, 0.0 ) : vec3( 0.0, 1.0, 0.0 ) ) );
	vec3 tv = cross( n, tu );
	return normalize( cross( flowAt( p + tu * 0.012, flick ) - o, flowAt( p + tv * 0.012, flick ) - o ) );
}
`

// The first lines of main(): where this point goes. Every three material has a main() and reads `position`.
const WAVE_MAIN = `void main() {
	#ifdef USE_SKINNING
		// How much of this point is a flick: the weight of that flick's bone (the fourth, and the fifth for the
		// second flick), wherever the file keeps it.
		vec2 flowFlickW = vec2( dot( skinWeight, vec4( equal( skinIndex, vec4( 3.0 ) ) ) ), dot( skinWeight, vec4( equal( skinIndex, vec4( 4.0 ) ) ) ) );
	#else
		vec2 flowFlickW = vec2( 0.0 );
	#endif
	vec3 flowP = flowAt( position, flowFlickW );`

/** Put the bend into a vertex shader of three. False when the shader has no place for it, and then nothing is changed. */
export function bendInto(shader: { vertexShader: string }, k: BendSet): boolean {
  if (!shader.vertexShader.includes('void main() {') || !shader.vertexShader.includes('#include <begin_vertex>')) return false
  shader.vertexShader = shader.vertexShader
    .replace('#include <common>', `#include <common>\n#define FLOW_TOP ${k.top.toFixed(4)}\n#define FLOW_LEAN ${k.lean.toFixed(4)}\n#define FLOW_TRAIL ${k.trail.toFixed(4)}\n#define FLOW_SOFT ${k.soft.toFixed(4)}\n#define FLOW_TILT ${k.tilt.toFixed(4)}\n#define FLOW_HALF ${k.half.toFixed(6)}\n${WAVE_PARS}`)
    .replace('void main() {', WAVE_MAIN)
    // A material that is not lit has no facing to turn: only the point moves.
    .replace('#include <beginnormal_vertex>', '#include <beginnormal_vertex>\n\tobjectNormal = flowFacing( position, objectNormal, flowP, flowFlickW );')
    .replace('#include <begin_vertex>', '#include <begin_vertex>\n\ttransformed = flowP;')
  return true
}

/** The name of the compiled program of a material that carries the bend: one program for every Bot. */
export const bendKey = (base: string, k: BendSet) => `${base}+flow${k.top},${k.trail},${k.soft},${k.tilt}`

/** Keep `fold` of the shade that the body's points carry for its folds (1 is all of it). */
export function foldInto(shader: { fragmentShader: string }, fold: number): void {
  shader.fragmentShader = shader.fragmentShader
    .replace('#include <color_fragment>', `#include <color_fragment>\n\tdiffuseColor.rgb = mix( vec3( 1.0 ), diffuseColor.rgb, ${fold.toFixed(2)} );`)
}

/** The name of the compiled program of the matte skin: one program for every colour. */
export const CLOTH_KEY = 'bot-cloth'

/**
 * Draw the dark ink of the flat art that a Bot wears in a pale tone, `wearInk`. The art's ink is black, and on
 * glasses that add light a black pixel is a hole in the body. A pixel as dark as ink takes the pale tone whole; one
 * that has a colour of its own keeps it; between the two it goes over smoothly, so an inked edge stays soft.
 * Returns false when the shader has no picture to read, and then nothing is changed.
 */
export function paleInkInto(shader: { fragmentShader: string }): boolean {
  if (!shader.fragmentShader.includes('#include <map_fragment>')) return false
  shader.fragmentShader = shader.fragmentShader
    .replace('#include <common>', '#include <common>\nuniform vec3 wearInk;')
    .replace('#include <map_fragment>', `#include <map_fragment>\n\tdiffuseColor.rgb = mix( wearInk, diffuseColor.rgb, smoothstep( ${INK_DARK.toFixed(3)}, ${INK_CLEAR.toFixed(3)}, max( diffuseColor.r, max( diffuseColor.g, diffuseColor.b ) ) ) );`)
  return true
}
/** A pixel whose brightest part is under `INK_DARK` (linear light) is ink; one over `INK_CLEAR` is a colour of the art. */
export const INK_DARK = 0.01
export const INK_CLEAR = 0.12

/** The name of the compiled program of what a Bot wears: one program for every Bot. */
export const WEAR_KEY = 'bot-wear'
