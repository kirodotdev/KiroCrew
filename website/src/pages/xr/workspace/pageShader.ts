/**
 * Shader text for a page's material. This module holds GLSL only: nothing in it is shown to a person.
 */
import { PAGES } from './layout'

/**
 * The page's material reads its small copies a little toward the larger one: on a mesh the page is filtered a
 * second time by the eye's own picture, and the plain choice of copy is too soft for small text.
 */
export const sharper = (shader: { fragmentShader: string }) => {
  shader.fragmentShader = shader.fragmentShader.replace('#include <map_fragment>', `#ifdef USE_MAP\n\tdiffuseColor *= texture2D( map, vMapUv, ${PAGES.mipBias.toFixed(2)} );\n#endif`)
}
/** One compiled program for every page: the change above is the same for all of them. */
export const sharperKey = () => 'xr-page'
