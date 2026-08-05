/**
 * Token retry logic for 403 auto-recovery after gateway restart.
 * Extracted for testability.
 */
function createTokenRetryHandler(refreshFn, maxRetries = 2) {
  let retries = 0;
  return async function onNavigate(httpCode) {
    if (httpCode === 403 && retries < maxRetries) {
      retries++;
      await refreshFn();
    } else if (httpCode === 200) {
      retries = 0;
    }
  };
}

module.exports = { createTokenRetryHandler };
