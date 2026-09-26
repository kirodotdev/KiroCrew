/**
 * The standing brief the owner's Perpetual mode switch arms a crewmate's loop
 * with, VERBATIM from the backend (`PERPETUAL_INSTRUCTION` / `PERPETUAL_BANNER`
 * in the members handler). The message is the prompt the crewmate receives on
 * every wake; the banner is the loop's short stand-in for it.
 *
 * A `*.prompt.ts` module by the i18n gate's named boundary: this is model-facing
 * text, English by design, and nothing here is rendered -- the two hosts of the
 * Perpetual mode block use these only as EQUALITY sentinels (`perpetualBriefText`
 * in useCrewPerpetual.ts) to hide an instruction row that would restate the
 * block's own title. Translating either would break that match, not localize UI.
 *
 * Kept in lockstep with the backend by the equality itself: if the backend's
 * text changes, the row simply shows again until this is updated.
 */
export const PERPETUAL_DEFAULT_BANNER = 'Keeps working on its own until the owner turns Perpetual mode off'

/** The bare feature name as a brief: a banner that only names the feature is
 *  as circular as the default and is hidden with it. Compared, never shown. */
export const PERPETUAL_FEATURE_NAME = 'Perpetual mode'

export const PERPETUAL_DEFAULT_INSTRUCTION =
  'Perpetual mode wake. Review your standing goals, your inbox and the work ' +
  'you own; act on whatever is due; leave routine progress in your ledger ' +
  'and message the user only for a decision they alone can make. If the ' +
  'cadence is wrong, change the interval with monitor_update. End your turn ' +
  'when nothing is due. Stop this loop yourself only in the rare case the ' +
  'standing duty is truly over, and say why in the stop reason; otherwise ' +
  'the user turns Perpetual mode off on your detail page.'
