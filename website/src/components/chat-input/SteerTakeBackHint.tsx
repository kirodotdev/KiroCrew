import { i18nT } from '../../i18n/t'

/** The status row above the composer box while Escape takes back a steer the
 *  running turn has not read yet (`steerTakeBackHintShown`). It is a row of its
 *  own, like the optimize notice, because in the button cluster the translated
 *  text would squeeze the text column at phone widths. */
export function SteerTakeBackHint() {
  return <div role="status" data-testid="escape-steer-take-back-hint" className="px-4 mb-1 text-muted text-[13px]">{i18nT('components.chatInput.esc_to_take_back_message')}</div>
}
