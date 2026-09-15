/**
 * Aperture wire values for the session-pulse survey (SessionPulseSurveyCard.tsx).
 *
 * PROTOCOL VALUES ONLY, same category as `apps/issue-radar/lib/wireValues.ts`:
 * `ratingOptions` are the `responseValue`s Aperture's registered form template
 * (category=KiroCrew, name=SessionFeedback, version=1.0.1) expects verbatim — // brand-ok: registered category id
 * `_customer_responses` in `feedback.py` sends the selected value straight
 * through as `responseValue`, so translating one would submit a value
 * Aperture's template does not recognize, 400ing ingestion rather than
 * localizing the submission. The user-visible label shown for each option is
 * a separate, fully translated string from the catalog (see
 * `RATING_LABEL_KEYS` in SessionPulseSurveyCard.tsx).
 *
 * The question text itself is NOT a wire value: `feedback.py`'s
 * `_customer_responses` builds `question` from its own server-side
 * `_RATING_QUESTION` constant, built independently of anything the frontend
 * sends — the client never transmits question text at all. The on-screen
 * question is therefore ordinary UI copy and lives in the i18n catalog
 * (`components.sessionPulseSurveyCard.rating_question`), not here.
 */
export const ratingOptions = ['Very Poor', 'Poor', 'Fair', 'Good', 'Excellent']
