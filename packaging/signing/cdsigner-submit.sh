#!/usr/bin/env bash
# Sign-task submission for the signing service. Sourced by sign.sh and sign-dmg.sh.
#
# The service answers a burst of submissions with HTTP 429 and the body
# {"message":"Too Many Requests"}, and awscurl still exits 0, so the response
# simply has no signTaskId. The macOS legs of one release (universal, arm64,
# x64) all submit within seconds of each other, which is exactly such a burst:
# a nightly lost its arm64 leg to it while the other two signed. A throttled
# request created no task, so submitting the same manifest again is safe.
#
# Only a throttled answer is retried. Any other failure -- a rejected manifest,
# an auth error, an unreachable endpoint -- is reported at once, because
# waiting would not change it and would only delay the red job.
#
# Tuning knobs (defaults are production values; drills shorten them):
#   SIGN_SUBMIT_ATTEMPTS          submissions before giving up (5)
#   SIGN_SUBMIT_RETRY_BASE_SECS   first backoff; doubles per retry, plus up to as
#                                 much again of jitter so concurrent legs spread
#                                 out (10). Five attempts wait at most 300s. The
#                                 sign job's 60-minute timeout must also hold the
#                                 45-minute poll and the steps around it; those
#                                 steps take a few minutes (a whole successful job
#                                 runs 7-15 minutes), so the worst case still
#                                 lands inside the timeout.

# True when an awscurl response is the service throttling the caller.
sign_submit_throttled() {
  case "$1" in
    *"Too Many Requests"* | *TooManyRequests* | *ThrottlingException* | *"Rate exceeded"*)
      return 0 ;;
  esac
  return 1
}

# submit_sign_task <request-json>
# Prints the signTaskId on stdout. On failure prints the reason and the last
# response on stderr and returns 1.
submit_sign_task() {
  local request="$1"
  local attempts="${SIGN_SUBMIT_ATTEMPTS:-5}"
  local delay="${SIGN_SUBMIT_RETRY_BASE_SECS:-10}"
  local attempt=1 response task_id wait_secs

  while :; do
    if ! response=$(awscurl --service signer-builder-tools --region us-west-2 \
        -X POST -H "Content-Type: application/json" -d "$request" \
        "${CDSIGNER_API_ENDPOINT}/v2/sign-tasks" 2>&1); then
      if ! sign_submit_throttled "$response"; then
        echo "ERROR: sign-task submission failed" >&2
        echo "$response" >&2
        return 1
      fi
    elif task_id=$(printf '%s' "$response" \
        | python3 -c "import json,sys; print(json.load(sys.stdin)['signTaskId'])" 2>/dev/null); then
      printf '%s\n' "$task_id"
      return 0
    elif ! sign_submit_throttled "$response"; then
      echo "ERROR: submission returned no signTaskId:" >&2
      echo "$response" >&2
      return 1
    fi

    if [ "$attempt" -ge "$attempts" ]; then
      echo "ERROR: sign-task submission still throttled after ${attempts} attempts:" >&2
      echo "$response" >&2
      return 1
    fi
    wait_secs=$(( delay + RANDOM % (delay + 1) ))
    echo "  sign-task submission throttled (attempt ${attempt}/${attempts}); retrying in ${wait_secs}s" >&2
    sleep "$wait_secs"
    attempt=$(( attempt + 1 ))
    delay=$(( delay * 2 ))
  done
}
