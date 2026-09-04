#!/usr/bin/env bash
#
# smc-deploy.sh -- one command to stand a Share My Crew crew up in the owner's
# own AWS account.
#
# This is the Driver track. It is coded against deploy/CONTRACT.md and
# PACKAGING-CONTRACT.md, the seams between the parallel tracks. The crew now rides
# INSIDE the image (PACKAGING-CONTRACT): one artifact whose digest pins both the
# serving code and the crew content. There is no S3 bundle path any more -- two
# sources of truth for "which crew is this" is how the first design served a
# default agent while ten gates reported green. See the PORT NOTES at the bottom.
#
# WHAT IT DOES
#   Resolves every value from `sts` and stack outputs rather than the command line,
#   CURATES the crew into a bundle (delegated to T1's `python -m packaging.build`),
#   deploys the base stack, builds+pushes a CREW IMAGE that bakes the bundle onto a
#   digest-pinned base (delegated to the crew-image track's
#   scripts/build_crew_image.sh), deploys the crew stack, republishes the API
#   Gateway stage, and then VERIFIES with gates that can fail -- including that the
#   running task definition serves the exact image the build reported, built from
#   the bundle that was curated.
#
# USAGE
#   ./smc-deploy.sh --profile smc --region us-west-2 --crew frontdesk
#   ./smc-deploy.sh --crew frontdesk --base <repo>@sha256:<hex>   # pin the base explicitly
#   ./smc-deploy.sh --dry-run --crew frontdesk        # exercise the whole flow, no AWS
#   ./smc-deploy.sh --from 8 --crew frontdesk          # resume at verify only
#
# The bundle is PRODUCED by step 1 (packaging.build), not supplied on the command
# line: there is no --bundle flag, because a bundle handed in alongside a curator
# is a second source of truth. The base image is pinned by --base, or resolved from
# the ECR repo's `smc-base-<ARCH>` tag when --base is absent.
#
# RESUMABLE: --from N skips ahead. Every correctness gate in step 8 runs from
# persisted/live state regardless of the resume point, so `--from 8` re-proves the
# deployment rather than trusting that steps 0-7 once passed.
#
# STEPS
#   0 preflight   1 bundle (packaging.build)   2 base stack   4 crew image
#   5 secrets     6 crew stack   7 republish stage   8 verify
#   (step 3, the old "upload bundle to S3", is deleted: the crew is in the image.)
#
# There is no public endpoint here and none may be added. Every caller
# authenticates with SigV4 against a private control API.
#
# MACHINE-READABLE PROGRESS: --events <path> writes one JSON object per line to
# <path> describing each step as it starts, finishes or fails. It is ADDITIVE --
# without the flag nothing is written and the output below is byte for byte what
# it was. --yes skips the step 0 confirmation for a non-interactive caller; the
# account is printed either way, because that is the last point before AWS
# resources are created. See the "event stream" block below for the schema.
#
# TESTABILITY: this file is sourceable. `set -euo pipefail` is applied inside
# smc_main, never at file scope, and the main flow only runs when the file is
# executed directly. Sourcing it (as deploy/tests/** does) defines the pure judge_*
# functions without running a deploy or flipping the caller's shell options.

# ---------------------------------------------------------------------------
# Presentation helpers (safe to source; no side effects)
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
  _C_RED=$'\033[31m'; _C_GRN=$'\033[32m'; _C_CYN=$'\033[36m'; _C_YEL=$'\033[33m'; _C_RST=$'\033[0m'
else
  _C_RED=''; _C_GRN=''; _C_CYN=''; _C_YEL=''; _C_RST=''
fi
die()  { _ev_step_close fail "$*"; _ev_end fail "$*"; printf '%sFAILED at step %s: %s%s\n' "$_C_RED" "${STEP:-?}" "$*" "$_C_RST" >&2; exit 1; }
step() { _ev_step_open "$1" "$2"; STEP="$1"; printf '\n%s== step %s . %s ==%s\n' "$_C_CYN" "$1" "$2" "$_C_RST"; }
ok()   { _ev_detail ok "$*";   printf '   %s+%s %s\n' "$_C_GRN" "$_C_RST" "$*"; }
note() { printf '   %s\n' "$*"; }
warn() { _ev_detail warn "$*"; printf '   %s! %s%s\n' "$_C_YEL" "$*" "$_C_RST"; }

# ---------------------------------------------------------------------------
# Machine-readable event stream -- opt-in, --events <path>. Safe to source.
#
# WHY A SECOND CHANNEL AT ALL. Everything above is written for a human reading a
# terminal. A wizard cannot drive a progress bar from it: the wording is free to
# change, so a UI that parsed it would turn every copy edit into a breaking
# change. That is the same trap CONTRACT.md already refuses for the image build's
# payload ("do not treat any stdout line as data"), so this channel is ADDITIVE
# and goes somewhere else entirely. With no --events every function here returns
# immediately and writes nothing, which is what keeps the prose byte-identical;
# run_gate_tests.sh proves it by running the same dry run with and without the
# flag and diffing stdout.
#
# WHY A FILE RATHER THAN STDOUT. A real deploy's stdout carries megabytes of
# docker and CloudFormation output, and the gateway-side runner redirects it to a
# log, so a reader tailing it for events would have to parse past all of that. A
# file is one writer, one line per event, and readable while the deploy runs.
#
# THE STEPS ARE 0,1,2,4,5,6,7,8 AND THERE IS NO STEP 3. The numbers emitted are
# the driver's own -- the same ones --from N takes -- so "retry from step 5" in a
# UI and the flag it would pass mean the same thing. Renumbering to close the gap
# would break that identity, and the gap is documented, not accidental.
#
# SCHEMA. One JSON object per line, each with "v":1, "event" and "t" (epoch
# seconds, the same unit backend/progress.py records):
#   run     crew, region, steps[] (the whole ladder, up front), from, dryRun
#   account account -- emitted ONCE, when the account is first known, which is
#           before step 2 creates anything
#   step    step, name, state=start|ok|fail; on fail also reason and resumeFrom
#           (the --from N a retry should pass)
#   detail  step, level=ok|warn, text -- one per assertive line, which for step 8
#           is exactly the 12 gate verdicts
#   end     state=ok|fail|aborted, reason
#
# NOTHING FROM note() IS MIRRORED. Notes are commentary, and one of them -- step
# 5's api-key line -- is the closest this driver comes to a line about a
# credential (it prints a length and a 12-hex digest, never the value). Holding
# the line at "the machine channel carries nothing about a secret at all" is
# stronger than trusting that note to stay digest-only.
# ---------------------------------------------------------------------------

#: Destination path. Empty is the whole of the no-op guarantee above.
SMC_EVENTS=""
#: The ladder, emitted once so a UI can render all eight rows before step 0
#: starts instead of discovering them one at a time. MUST match the step dispatch
#: in smc_main; run_gate_tests.sh asserts a dry run emits exactly these.
SMC_STEP_LADDER='[0,1,2,4,5,6,7,8]'
_EV_STEP=""      # the open step's number; empty when none is open
_EV_NAME=""      # its name, because closing a step re-states it
_EV_ENDED=0      # an end event has been written; the EXIT trap must not double it
_EV_ACCOUNT=0    # the account has been announced

# Escape one string as a JSON string body. Pure bash on purpose: an event is
# emitted for every step and every assertion, and a python3 call per event would
# be a subprocess per line on a deploy's critical path. Control characters are
# DROPPED rather than escaped -- the only ones that can arrive here are a stray
# colour code or a newline, and neither means anything to a reader.
_ev_str() {
  local s="$*"
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  s=${s//$'\n'/ }
  s=${s//$'\t'/ }
  s=${s//[[:cntrl:]]/}
  printf '%s' "$s"
}

# A step number as a JSON number, or null. The call sites all pass literals, so
# this is insurance rather than a real branch: a machine channel that can emit
# one unparseable line is a channel a reader has to defend against.
_ev_num() { case "${1:-}" in ''|*[!0-9]*) printf 'null' ;; *) printf '%s' "$1" ;; esac; }

# Append one event. Best-effort and ALWAYS returns 0: this is called from die(),
# where a non-zero return would change the exit path, and a broken event stream
# must not be able to take a deploy down with it.
_ev() { # EVENT EXTRA_JSON
  [ -n "$SMC_EVENTS" ] || return 0
  printf '{"v":1,"event":"%s","t":%s%s}\n' \
    "$1" "${EPOCHSECONDS:-$(date -u +%s)}" "${2:-}" >> "$SMC_EVENTS" 2>/dev/null || true
  return 0
}

# Close the open step. Called with ok from the next step() and from a clean exit,
# with fail from die(). Closing the previous step when the next one opens is an
# INFERENCE, and a sound one: the driver dies on any step's failure, so reaching
# step N+1 is proof step N finished. The alternative -- a "step done" call at the
# end of all eight steps -- would mean editing every step body.
_ev_step_close() { # STATE [REASON]
  [ -n "$_EV_STEP" ] || return 0
  local extra=",\"step\":$(_ev_num "$_EV_STEP"),\"name\":\"$(_ev_str "$_EV_NAME")\",\"state\":\"$1\""
  if [ "$1" = fail ]; then
    extra="$extra,\"reason\":\"$(_ev_str "${2:-}")\",\"resumeFrom\":$(_ev_num "$_EV_STEP")"
  fi
  _ev step "$extra"
  _EV_STEP=""; _EV_NAME=""
  return 0
}

_ev_step_open() { # NUMBER NAME
  _ev_step_close ok
  _EV_STEP="$1"; _EV_NAME="$2"
  _ev step ",\"step\":$(_ev_num "$1"),\"name\":\"$(_ev_str "$2")\",\"state\":\"start\""
  return 0
}

_ev_detail() { # LEVEL TEXT
  _ev detail ",\"step\":$(_ev_num "$_EV_STEP"),\"level\":\"$1\",\"text\":\"$(_ev_str "$2")\""
  return 0
}

_ev_run() {
  local dry=false
  [ "${DRY_RUN:-0}" -eq 1 ] && dry=true
  _ev run ",\"crew\":\"$(_ev_str "${CREW:-}")\",\"region\":\"$(_ev_str "${REGION:-}")\"\
,\"steps\":$SMC_STEP_LADDER,\"from\":$(_ev_num "${FROM:-0}"),\"dryRun\":$dry"
  return 0
}

# The account, once. Called from step 0 where it is first resolved and again from
# the resume path that reads it back from state, so both entries into the flow
# announce it and neither has to know whether the other ran.
_ev_account() { # ACCOUNT
  if [ "$_EV_ACCOUNT" -eq 1 ]; then return 0; fi
  _EV_ACCOUNT=1
  _ev account ",\"account\":\"$(_ev_str "$1")\""
  return 0
}

_ev_end() { # STATE [REASON]
  if [ "$_EV_ENDED" -eq 1 ]; then return 0; fi
  _EV_ENDED=1
  _ev end ",\"state\":\"$1\",\"reason\":\"$(_ev_str "${2:-}")\""
  return 0
}

# EXIT trap, installed only when --events was given. It is why a UI can never be
# left watching a run that has already stopped: an exit this file does not route
# through die() -- a command failing under set -e, an interrupt, a --why or a
# resume that simply finished -- still closes the stream. die() has already
# written its own fail events by then, and both emitters above are idempotent, so
# this cannot double them.
_ev_exit() { # STATUS
  if [ "${1:-0}" -eq 0 ]; then
    _ev_step_close ok
    _ev_end ok
  else
    _ev_step_close fail "exited with status ${1:-?}"
    _ev_end fail "exited with status ${1:-?}"
  fi
  return 0
}

# ===========================================================================
# PURE GATE JUDGES
#
# Each judge takes already-collected values and decides pass/fail. They never
# touch AWS or the network, they `return` (never `exit`), and they emit a clear
# assertion message. This is what makes every gate individually testable: a test
# feeds a fabricated value and asserts the return code. The impure "collect" side
# is separated out below so it can be stubbed under --dry-run.
#
# Return code convention: 0 == gate passed. Non-zero == gate failed. Distinct
# non-zero codes are used where the caller wants to tell failure modes apart
# (notably the real-turn judge).
# ===========================================================================

# CpuArchitecture spelling maps. CloudFormation uses X86_64/ARM64; docker/uname
# use amd64/arm64. Mapping lives in exactly one place each way.
cfn_of_docker() {
  case "$1" in
    amd64) printf 'X86_64' ;;
    arm64) printf 'ARM64' ;;
    *) return 1 ;;
  esac
}
docker_of_cfn() {
  case "$1" in
    X86_64) printf 'amd64' ;;
    ARM64) printf 'arm64' ;;
    *) return 1 ;;
  esac
}
native_docker_arch() {
  case "$(uname -m)" in
    aarch64|arm64) printf 'arm64' ;;
    x86_64|amd64)  printf 'amd64' ;;
    *) return 1 ;;
  esac
}
# uname machine (what actually ran inside the image) -> CloudFormation spelling.
cfn_of_machine() {
  case "$1" in
    x86_64|amd64)  printf 'X86_64' ;;
    aarch64|arm64) printf 'ARM64' ;;
    *) return 1 ;;
  esac
}

# RoutePrefix must start with '/'. A bare word is refused, not repaired
# (CONTRACT: SMC_ROUTE_PREFIX).
judge_route_prefix() {
  case "$1" in
    /*) return 0 ;;
    *) printf 'route prefix %q must start with "/" -- a bare word is refused, not repaired\n' "$1" >&2; return 1 ;;
  esac
}

# A stack output must be present and not the literal "None" the CLI prints for a
# missing key.
judge_output_present() { # KEY VALUE
  if [ -z "$2" ] || [ "$2" = "None" ]; then
    printf 'required stack output %s is missing\n' "$1" >&2; return 1
  fi
  return 0
}

# TRAP 1 (arch pinned in two places). Proven against DEPLOYED reality, not the
# template source (which the Driver track does not own). Three facts must agree:
# the intended arch, the registered task definition's arch, and the built image's
# arch (all CloudFormation spelling now, since the build script reports it that
# way). PLATFORM_MACHINE is the uname of what ACTUALLY RAN inside the image (the
# strongest proof it can exec) and CONTRACT says it may be empty for a cross-arch
# build -- an empty value is UNPROVEN, never agreement.
#   rc 0 = fully proven: intended==taskdef==image, and platform_machine confirms it ran.
#   rc 2 = declared arch agrees, but the exec proof is unproven (platform_machine
#          empty, i.e. a cross-arch build that could not run inside CI).
#   rc 1 = a real contradiction.
judge_arch() { # INTENDED_CFN TASKDEF_CFN IMAGE_CFN PLATFORM_MACHINE(uname, may be empty)
  local intended="$1" taskdef="$2" image="$3" machine="$4"
  if [ "$taskdef" != "$intended" ]; then
    printf 'the deployed task definition runs %s but the deploy intended %s.\n' "$taskdef" "$intended" >&2
    printf 'the template ignored CpuArchitecture -- this is the "exec format error" that kills the task without saying why.\n' >&2
    return 1
  fi
  if [ -n "$image" ] && [ "$image" != "$intended" ]; then
    printf 'the built image is %s but the task runs %s -- Fargate would fail to start it (exec format error).\n' "$image" "$taskdef" >&2
    return 1
  fi
  if [ -z "$image" ]; then
    return 2   # the build record carried no architecture -> declared leg unproven
  fi
  if [ -z "$machine" ]; then
    return 2   # cross-arch build could not exec inside CI -> exec proof unproven, NOT passing
  fi
  local m; m="$(cfn_of_machine "$machine")" || { printf 'unrecognised platform_machine %q\n' "$machine" >&2; return 1; }
  if [ "$m" != "$intended" ]; then
    printf 'the image actually ran as %s (%s) but the task runs %s -- it cannot exec on the task host.\n' "$m" "$machine" "$taskdef" >&2
    return 1
  fi
  return 0
}

# The crew rides INSIDE the image now, so the strongest deploy-time proof that the
# crew being SERVED is the crew that was PACKAGED is a digest identity, split into
# two judges because they fail for different reasons and a reader needs to know
# which link broke.
#
# (The kiro-cli-chat-binary check that used to live here is GONE. It read
# kiro_cli_present/kiro_cli_chat_present from the build record, and the crew-image
# build record smc-crew-image/v1 does not carry them: the chat binary is a property
# of the BASE image, verified by the maintainer's base builder, and its runtime
# symptom -- a turn that 502s -- is already caught by the real-turn gate. Reading a
# field the record does not contain would fail every real deploy. See the report.)

# THE gate whose absence let the earlier version report success while serving a
# default agent. The running task definition's image digest must EQUAL the image
# build_crew_image.sh reported: with the crew baked into the image, "served ==
# reported" is "served crew == packaged crew". Both sides must be digest-pinned; a
# tag can be repointed and proves nothing about content.
judge_image_digest() { # DEPLOYED_IMAGE REPORTED_IMAGE
  local deployed="$1" reported="$2"
  case "$reported" in
    *@sha256:*) : ;;
    *) printf 'build_crew_image.sh reported no digest-pinned image (%q) -- nothing was pushed, so there is no packaged image the task can be proven to run.\n' "$reported" >&2; return 1 ;;
  esac
  case "$deployed" in
    *@sha256:*) : ;;
    *) printf 'the running task definition references %q, not a digest -- a tag can be repointed, so it cannot prove which crew is served.\n' "$deployed" >&2; return 1 ;;
  esac
  if [ "${deployed##*@}" != "${reported##*@}" ]; then
    printf 'the running task definition serves %s but build_crew_image.sh reported %s -- the crew being SERVED is NOT the crew that was packaged.\n' "$deployed" "$reported" >&2
    return 1
  fi
  return 0
}

# The companion: the reported image was built from OUR curated bundle. The image
# gate proves "served == reported"; this proves "reported was built from the bundle
# packaging.build produced", by comparing build_crew_image.sh's bundle_digest
# (which it reads from the bundle manifest) to the digest packaging.build printed.
# Without this link, "served == reported" only pins an image, not the crew content.
# A property with no gate is a claim. This reads the SUMMARY line restore emits
# The judge takes the MODE, because the two modes claim different things and a
# gate that only knew one would refuse every deployment of the other. chatbot
# claims nothing is persisted; persistent claims nothing was restored that this
# task did not serve. Both are read from the same SUMMARY line.
#
#   line ABSENT     restore did not run at all. Not the same as reporting zero,
#                   and it must not read as a pass: a restore that never ran also
#                   never restored a transcript.
#   restored > 0    a bulk restore happened, so the task holds conversations it
#                   did not serve. Fatal in either mode.
#
# chatbot also requires state=disabled and available=0: with no bucket there is
# nothing to restore FROM, so anything else means the crew is persisting after
# all. persistent instead requires state=ok and available>0, because zero out of
# zero proves nothing there: a bucket with no transcript passes trivially, and the
# gate would go green on the one case it cannot speak about.
judge_no_transcripts_restored() { # SUMMARY_LINE MODE
  local line="$1" mode="${2:-persistent}" restored avail state
  if [ -z "$line" ]; then
    printf 'the container never logged "restore: SUMMARY". Restore did not run, so this\n' >&2
    printf 'deployment cannot say whether it restored a transcript. That is a different\n' >&2
    printf 'failure from restoring zero, and it is not a pass.\n' >&2
    return 1
  fi
  # Each field is read by COUNTING its occurrences and taking the only one, not
  # by a greedy match. `sed -n 's/.*field=\(...\).*/\1/p'` is greedy, so it
  # silently returns the LAST occurrence: a line reading
  # `state=partial state=ok transcripts_restored=7 transcripts_restored=0`
  # parsed as a clean pass and the gate reported the deployment isolated. A field
  # that appears twice means the line is not the line this gate was written
  # against, and guessing which copy to believe is exactly the wrong response.
  _field() { # LINE NAME -> the single value, or "" if absent, repeated or non-canonical
    local n hits
    hits="$(printf '%s\n' "$1" | grep -o "[[:space:]]$2=[^[:space:]]*" | sed "s/^[[:space:]]*$2=//")"
    n="$(printf '%s' "$hits" | grep -c . || true)"
    [ "$n" = "1" ] || return 0
    printf '%s' "$hits"
  }
  restored="$(_field "$line" transcripts_restored)"
  avail="$(_field "$line" transcripts_available)"
  state="$(_field "$line" state)"
  # Numbers are compared as strings against a canonical form first. `[ 058 -eq 0 ]`
  # asks bash for arithmetic on a value it may read as octal, and 00 is not the
  # same token as 0 even though it compares equal, so a line carrying either is a
  # line whose producer is not the one this gate reads.
  case "$restored" in ''|*[!0-9]*) restored="" ;; 0) : ;; 0*) restored="" ;; esac
  case "$avail" in ''|*[!0-9]*) avail="" ;; 0) : ;; 0*) avail="" ;; esac
  if [ -z "$restored" ] || [ -z "$avail" ] || [ -z "$state" ]; then
    printf 'the SUMMARY line does not carry each field this gate reads exactly once in\n' >&2
    printf 'canonical form: %s\n' "$line" >&2
    printf 'state, transcripts_restored and transcripts_available are an interface. A\n' >&2
    printf 'repeated or padded field means the line came from something else.\n' >&2
    return 1
  fi
  # restored>0 is fatal in BOTH modes and is checked first, because it is the one
  # reading that means the same thing everywhere: this task holds a conversation
  # it did not serve.
  if [ "$restored" -ne 0 ]; then
    printf 'restore put %s transcript(s) on the task disk, so the task holds conversations\n' "$restored" >&2
    printf 'it never served, which neither mode permits.\n' >&2
    return 1
  fi

  if [ "$mode" = chatbot ]; then
    # A chatbot crew has no bucket, so restore must report that it had nothing to
    # restore FROM. state=ok here would mean the crew IS persisting, which is the
    # mode silently not being in effect: the template's Memory parameter and the
    # container's environment would have disagreed.
    if [ "$state" != disabled ]; then
      printf 'this crew is deployed as a chatbot, but restore reported state=%s instead of\n' "$state" >&2
      printf 'disabled, so a bucket IS configured and conversations are being persisted.\n' >&2
      printf 'The Memory parameter and SMC_BACKUP_BUCKET disagree.\n' >&2
      return 1
    fi
    if [ "$avail" -ne 0 ]; then
      printf 'restore saw %s transcript(s) available with backup disabled, which cannot\n' "$avail" >&2
      printf 'happen: listing them requires the bucket this mode does not grant.\n' >&2
      return 1
    fi
    return 0
  fi

  # persistent. state is checked BEFORE availability, because a degraded boot makes
  # the counters unreadable rather than false. state=partial means an authority
  # file was missing, so resume or the conversation list is broken, and it would
  # sail through a counter-only gate with a perfect transcripts_restored=0.
  if [ "$state" != "ok" ]; then
    printf 'restore finished in state=%s, so this deployment is not serving normally and\n' "$state" >&2
    case "$state" in
      partial) printf 'the zero cannot be trusted: an authority file was missing, which breaks resume\n' >&2
               printf 'or the conversation list. Read missing= on the SUMMARY line.\n' >&2 ;;
      disabled) printf 'nothing was restored because backup is off. If that is intended, deploy with\n' >&2
                printf 'Memory=chatbot so the gate asserts the property this crew actually claims.\n' >&2 ;;
      empty) printf 'the bucket held nothing at all, so there was no conversation to leave behind.\n' >&2 ;;
      *) printf 'a persistent crew must report state=ok.\n' >&2 ;;
    esac
    return 1
  fi
  if [ "$avail" -eq 0 ]; then
    printf 'restored 0 transcripts out of 0 available, which proves nothing: this bucket\n' >&2
    printf 'holds no conversation that could have been left behind. Send a turn, let it\n' >&2
    printf 'back up, then re-run this gate.\n' >&2
    return 1
  fi
  return 0
}

judge_bundle_digest_match() { # PACKAGED_DIGEST IMAGE_REPORTED_BUNDLE_DIGEST
  local packaged="$1" reported="$2"
  case "$packaged" in sha256:*) : ;; *) printf 'packaging.build reported a non-sha256 bundle digest %q\n' "$packaged" >&2; return 1 ;; esac
  case "$reported" in sha256:*) : ;; *) printf 'the crew image reported a non-sha256 bundle digest %q (build_crew_image.sh pushed nothing?)\n' "$reported" >&2; return 1 ;; esac
  if [ "$packaged" != "$reported" ]; then
    printf 'the image baked in bundle %s but packaging.build produced %s -- the image does not contain the crew that was curated.\n' "$reported" "$packaged" >&2
    return 1
  fi
  return 0
}

# TRAP 6/8 + point 1/4. /health through the CONTROL api must be REFUSED (403).
#   200 here is a SECURITY REGRESSION: a customer path accepted a request carrying
#        the gateway-attached control secret, i.e. the customer/control split is
#        not enforced in the container.
#   403 is the pass: the container refuses the control secret on a customer path.
# The load balancer's own health check hits the bare /health directly (no gateway,
# no prefix, no secret) and is already proven by the service reaching steady state;
# it is a different caller and not interchangeable with this one.
#
# SUPERSEDED, and kept only as this note. The premise is false: the gateway attaches
# no control secret (see judge_forged_secret_rejected), so a 200 on /health never
# indicated what this judge claimed. The deployed stack answered 200 and the message
# named a header the probe had not sent. Replaced by judge_health_through_control
# plus judge_control_refused_without_secret, which split the one assertion into the
# two properties that are really enforced.

# point 1/4, TRAP 6/8. What the control API's boundary ACTUALLY is.
#
# The first version of this gate required /health to answer 403 through the control
# API, on the theory that the API carries no customer routes. Nothing enforces that:
# the integration is {proxy+} onto the container's single port, so the API structurally
# carries every route the container serves, and the deployed stack answered 200. The
# assertion described an intention, not a mechanism, and the message it printed on
# failure claimed the request had carried the control secret when the probe sends none.
#
# The boundary that IS enforced, and worth gating, has two parts:
#   - IAM: nothing reaches this API without SigV4 from a principal in the account.
#   - The container: a control route is refused unless it carries the control secret.
# So /health answering 200 to the owner is expected. What must never happen is a
# control route answering without the secret.
judge_health_through_control() { # HTTP_CODE
  case "$1" in
    200) return 0 ;;
    403) printf '/health returned 403. The owner is SigV4-authorised, so the container is refusing a path it should serve: check SMC_ROUTE_PREFIX against the deployed RoutePrefix.\n' >&2; return 1 ;;
    *)   printf '/health returned %s through the control api, expected 200.\n' "$1" >&2; return 2 ;;
  esac
}

# The control-route refusal, which is the enforced half of the boundary. A control
# path with NO secret must be refused, and refused by the CONTAINER rather than by a
# missing route: 403 control_forbidden, not 404.
judge_control_refused_without_secret() { # HTTP_CODE
  [ "$1" = "403" ] && return 0
  printf 'a control route without the control secret returned %s, expected 403. The container is the only thing enforcing this; if it answered, the control surface is open to any account principal.\n' "$1" >&2
  return 1
}

# TRAP 8 + point 4. Injection probe polarity, per casing. The caller is ALREADY
# the owner (SigV4 authorised before any header was read), so:
#   200 = the gateway's mapped header value won  -> SAFE, required.
#   403 = the client's forged header reached the container and it rejected it -> FAIL.
#
# BOTH of those readings assumed API Gateway injects the real control secret and so
# overwrites whatever the client sent. The deployment deliberately does not do that:
# a static header value in the integration is readable with apigateway:GET (R3, trap
# #10), so the secret reaches the container through the secrets block instead and the
# gateway carries no copy of it. There is nothing to overwrite a forged header WITH.
#
# So the property to gate is the one the container implements: a forged control secret
# is REJECTED, in every casing, because header lookup is case-insensitive and a
# capitalisation that missed the comparison would be a real bypass. 403 is the pass.
#
# Kept as its own judge rather than folded into the refusal judge above so that a
# future gateway-injecting design changes one function, and so the log still names
# which casing failed.
judge_forged_secret_rejected() { # HTTP_CODE
  case "$1" in
    403) return 0 ;;
    200) return 1 ;;
    *)   return 2 ;;
  esac
}

# Name WHO produced an unexpected response, which the status code alone cannot say.
# A 404 has three sources in this path and they need different fixes:
#   {"error":"no such crew"}  the ALB's default action -- its listener rule did not
#                             match, so the path reaching it is not RoutePrefix/*
#   {"message":"Not Found"}   API Gateway -- no resource or no such stage
#   anything else             the container answered, so routing works and the
#                             front process classified the path as unknown
#
# The body was already written to $WORK/resp by the call that failed and was being
# thrown away. Printing it is the difference between naming the layer and guessing
# between two plausible ones.
whose_response() {
  local body; body="$(head -c 200 "$WORK/resp" 2>/dev/null | tr -d '\n')"
  [ -n "$body" ] || { printf 'empty body'; return 0; }
  case "$body" in
    *'no such crew'*) printf 'the ALB default action (listener rule did not match): %s' "$body" ;;
    *'Missing Authentication Token'*) printf 'API Gateway (no such resource): %s' "$body" ;;
    *'Not Found'*|*'Forbidden'*) printf 'API Gateway or the container: %s' "$body" ;;
    *) printf 'the container: %s' "$body" ;;
  esac
}

# point 3 + CHORUS 9 + Appendix A ("a present credential is a working one"). The
# ONE real turn, judged on the RESPONSE rather than on a field claiming it happened.
#
# A 200 is not sufficient on its own. The front process answers 200 for shapes that
# are not completions, and a backend with no working credential returns 502/503 while
# the container stays healthy, so the gate reads the completion itself: a non-empty
# assistant message with a finish_reason. That is the difference between "the service
# replied" and "the crew answered".
judge_real_turn_response() { # HTTP_CODE
  case "$1" in
    200) : ;;
    502|503)
      printf 'the turn reached the container and the model call FAILED (%s). The credential is present but not working, or the backend is not ready. The deployment is fine; check the key.\n' "$1" >&2
      return 2 ;;
    403)
      printf 'the customer path returned 403. The front process classified it as a control route, so the path or its prefix is not what the container expects.\n' >&2
      return 3 ;;
    000)
      printf 'no response within the timeout. A first turn can be slow while the warm pool boots; re-run --from 8, and raise SMC_TURN_TIMEOUT if it recurs.\n' >&2
      return 4 ;;
    *)
      printf 'the customer path returned %s.\n' "$1" >&2
      return 5 ;;
  esac
  python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
ch = (d.get("choices") or [{}])[0]
content = (ch.get("message") or {}).get("content") or ""
assert content.strip(), "200 with an EMPTY assistant message: the turn was accepted and produced nothing"
assert ch.get("finish_reason"), "200 with no finish_reason: the completion did not finish"
' "$WORK/resp" 2>&1 >/dev/null | sed 's/^/     /' >&2
  # Report the python exit status, not sed's.
  python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
ch = (d.get("choices") or [{}])[0]
content = (ch.get("message") or {}).get("content") or ""
raise SystemExit(0 if content.strip() and ch.get("finish_reason") else 6)
' "$WORK/resp" 2>/dev/null
}

# The LAST thing any other gate cannot establish: WHOSE prompt answered.
#
# Every gate above can pass while the wrong crew answers. The image digest proves the
# right artifact is deployed; the container's checks prove the right bundle is
# installed; the crew address proves the request named this crew. None of them reaches
# into the answer. The first live deployment failed exactly there -- a stock agent
# answered "reply with the single word: ok" indistinguishably from a tuned crew,
# because that question has the same answer either way.
#
# So this gate asks something only the packaged prompt can answer: a value derived
# from the bundle's own content, present nowhere but inside the prompt that shipped.
#
# It is the one gate that depends on the MODEL cooperating, so a failure has two
# causes and the message names both, likeliest first.
judge_prompt_fingerprint() { # HTTP_CODE EXPECTED_FINGERPRINT
  local code="$1" want="$2"
  if [ -z "$want" ]; then
    printf 'no fingerprint was recorded for this bundle, so the served prompt cannot be identified. packaging.build must emit one.\n' >&2
    return 4
  fi
  if [ "$code" != "200" ]; then
    printf 'the fingerprint challenge returned %s, so nothing was proven about which prompt is serving.\n' "$code" >&2
    return 3
  fi
  local body; body="$(cat "$WORK/resp" 2>/dev/null)"
  case "$body" in
    *"$want"*) return 0 ;;
  esac
  printf 'the deployed crew did NOT return its prompt fingerprint (%s).\n' "$want" >&2
  printf '  Likeliest cause: the prompt serving this deployment is not the one that was\n' >&2
  printf '  packaged -- a different agent answered. That is the failure this gate exists\n' >&2
  printf '  for, and every other gate passes while it happens.\n' >&2
  printf '  Other cause: the model declined to follow the verification instruction. Read\n' >&2
  printf '  the reply below and judge which it was.\n' >&2
  printf '  reply: %s\n' "$(printf '%s' "$body" | head -c 300)" >&2
  return 1
}

# point 5 + CHORUS 9. IAM decision judge. A positive test alone is worthless
# (a bucket-wide grant passes it), so the caller pairs each with an EXPECTATION
# and four of six are denials.
judge_iam() { # EXPECT(allowed|denied) DECISION(from simulate-principal-policy)
  case "$1:$2" in
    allowed:allowed) return 0 ;;
    denied:implicitDeny|denied:explicitDeny) return 0 ;;
    *) printf 'IAM decision %q, expected %q\n' "$2" "$1" >&2; return 1 ;;
  esac
}

# The identity policy can be perfect and a bucket policy can widen it. A Deny-only
# bucket policy cannot grant, so it leaves the denials intact; an Allow statement
# can override them. SIDS is the comma list of Allow-statement Sids, "none" for a
# deny-only policy, "" for no policy, "unparsable" when parsing failed.
judge_bucket_policy() { # SIDS
  case "$1" in
    ""|none) return 0 ;;
    unparsable) printf 'could not parse the bucket policy; treat the IAM denials as identity-only (unproven against a bucket grant)\n' >&2; return 1 ;;
    *) printf 'bucket policy GRANTS via: %s -- the IAM denials above are identity-only and may be overridden\n' "$1" >&2; return 1 ;;
  esac
}

# TRAP (KIROCREW_BIND / SMC_CONFIG_DIR). Proven from the registered task
# definition's environment, independent of the resume point. These two traps
# "pass every test" if wrong, so they are checked against deployed reality.
judge_env_equals() { # NAME ACTUAL EXPECTED
  if [ "$2" != "$3" ]; then
    printf 'container env %s=%q, expected %q\n' "$1" "$2" "$3" >&2; return 1
  fi
  return 0
}

# ===========================================================================
# Everything below runs only when the script is executed (not sourced).
# ===========================================================================

smc_main() {
  set -euo pipefail

  # --- defaults -----------------------------------------------------------
  PROFILE="${AWS_PROFILE:-smc}"
  REGION="${AWS_REGION:-us-west-2}"
  CREW=""
  # chatbot by default: it is the mode whose claim a gate can prove, and the one
  # that grants the task role no S3 action. persistent has never run on a real
  # account, so it is opt-in rather than the thing you get by forgetting a flag.
  MEMORY="chatbot"
  BASE_OPT=""            # maintainer base image, digest-pinned; resolved from ECR smc-base-<ARCH> tag if empty
  # The Kiro Crew source checkout the base builder (scripts/build_image.sh) builds
  # the wheel from. Only consulted when a base image must be BUILT -- that is, no
  # --base and no 'smc-base-<ARCH>' tag in the account yet. build_image.sh requires it
  # (--kirocrew-src or $KIROCREW_SRC) and has no defensible fixed default, so the
  # driver passes it through rather than inventing one. Empty by default; preflight
  # refuses early if a build turns out to be needed and this is unset/unresolvable.
  KIROCREW_SRC="${KIROCREW_SRC:-}"
  ALLOW_ARGS=()          # passthrough --allow <path> flags for packaging.build (deny-by-default override)
  FROM=0
  RETENTION=30            # RULED value for this deployment (CHORUS R6 / NOTES). No product default.
  YES=0
  DRY_RUN=0
  ARCH_OPT=""
  STAGE_OPT=""
  TASK_CPU="${SMC_TASK_CPU:-2048}"
  TASK_MEMORY="${SMC_TASK_MEMORY:-4096}"
  REDEPLOY_BASE=0   # retired; step 2 always deploys. Kept so nothing reads it unset.
  WHY_ONLY=0
  # Set when step 5 stores a DIFFERENT value than was there. A task reads its secrets
  # only at start, so this is what tells step 6 the running task must be replaced even
  # when the template is unchanged.
  SECRET_ROTATED=0
  # Fargate does not permit an unprivileged user namespace, so 'false' means the
  # container correctly refuses to start there. Sent on every deploy and printed at
  # step 6: a posture that has to be re-typed every run stops being read, while one
  # that is recorded and shown stays true.
  #
  # 'true' is the DEFAULT, and a reviewer is right to stop on that: it means the
  # model subprocess runs with the task role's credentials, files and network
  # unconfined. It is not a value chosen for convenience -- on this runtime the
  # alternative is not "confined", it is "does not run at all", so defaulting to
  # 'false' would ship a crew that cannot start rather than a safer one. An owner
  # who wants that refusal passes --require-sandbox, and the container then says so
  # instead of implying confinement it does not have. Closing this properly needs a
  # runtime that permits the namespace, which is an architecture decision recorded
  # in the pull request rather than something this flag can fix.
  ALLOW_UNSANDBOXED=true
  TRUST_DOMAIN=""
  HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  # One level up from this script: templates/, packaging/ and the contracts. Under
  # an installed copy there is no repository above it, so nothing here may reach
  # for one.
  CREW_ROOT="$(cd "$HERE/.." && pwd)"

  while [ $# -gt 0 ]; do
    case "$1" in
      --profile) PROFILE="$2"; shift 2 ;;
      --region)  REGION="$2";  shift 2 ;;
      --crew)    CREW="$2"
                 # An allowlist, not a check for "..". The name reaches three
                 # consumers -- a CloudFormation stack name, a URL route prefix,
                 # and a local scratch directory -- and this is the intersection
                 # of what all three accept. Refusing traversal specifically would
                 # still let through an absolute path, a space, a shell
                 # metacharacter, or a name CloudFormation rejects five steps in.
                 #
                 # The scratch path is the one that bites first and hardest:
                 # WORK is built from this name and produce_bundle runs
                 # `rm -rf "$out"` on a directory derived from it, so
                 # `--crew x/../../../some/project` deleted a directory outside
                 # the scratch root before any AWS call was made.
                 case "$CREW" in
                   [a-z0-9]*) : ;;
                   *) die "--crew must start with a lowercase letter or digit, got '${CREW}'." ;;
                 esac
                 # The SAME rule the template enforces, checked here so it fails at
                 # parse time instead of five steps in. crew.yaml's CrewName carries
                 # AllowedPattern '^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$', and this check
                 # was looser in three ways at once: it accepted uppercase, put no
                 # bound on length, and allowed a trailing hyphen. `--crew FrontDesk`
                 # therefore passed here, created a scratch tree, built a bundle and
                 # pushed an image, and only then did CloudFormation refuse the
                 # parameter -- leaving the side effects of a deployment that could
                 # never finish.
                 #
                 # Two spellings of one rule drift; this is the fifth instance of that
                 # on this branch, so the pattern is quoted from the template rather
                 # than paraphrased, and a gate test asserts the two agree.
                 if ! printf '%s' "$CREW" | grep -Eq '^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$'; then
                   die "--crew must match the template's CrewName pattern
     ^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]\$ -- lowercase letters, digits and hyphens,
     2 to 32 characters, not starting or ending with a hyphen. Got '${CREW}'.
     Checked here so CloudFormation does not refuse it after the bundle is built
     and the image is pushed."
                 fi
                 case "$CREW" in
                   *[!a-zA-Z0-9-]*)
                     die "--crew takes letters, digits and hyphens only, got '${CREW}'.
     That is what a CloudFormation stack name and a URL path both accept, and the
     name is also used as a scratch directory, so anything else is refused here
     rather than after five steps of work." ;;
                 esac
                 shift 2 ;;
      # --memory-mode selects WHICH memory model the crew runs, not how much RAM
      # the task gets (that is --memory, below, in MiB). The two were both spelled
      # --memory once; bash case takes the first branch, so the sizing flag became
      # dead code and --memory 8192 died with "takes chatbot or persistent". The
      # name says what it selects and reads next to --memory/--cpu without colliding.
      # chatbot (default) keeps a conversation only while its task lives and
      # grants the task role no S3 action at all. persistent syncs to S3.
      --trust-domain) TRUST_DOMAIN="$2"; shift 2
                 case "$TRUST_DOMAIN" in single-principal) : ;;
                   *) die "--trust-domain takes single-principal, got '${TRUST_DOMAIN}'." ;;
                 esac ;;
      --memory-mode)  MEMORY="$2";  shift 2
                 case "$MEMORY" in chatbot|persistent) : ;;
                   *) die "--memory-mode takes chatbot or persistent, not ${MEMORY}. The template
     rejects anything else too, but it would say so only after five steps of work." ;;
                 esac ;;
      --base)    BASE_OPT="$2"; shift 2 ;;
      # Source checkout the base image is built from, passed straight to
      # scripts/build_image.sh when a base must be built. Only used on a fresh
      # account with no base image and no --base; ignored otherwise.
      --kirocrew-src) KIROCREW_SRC="$2"; shift 2 ;;
      --allow)   ALLOW_ARGS+=(--allow "$2"); shift 2 ;;
      --from)    FROM="$2";    shift 2 ;;
      --retention) RETENTION="$2"; shift 2 ;;
      --arch)    ARCH_OPT="$2"; shift 2 ;;
      --stage)   STAGE_OPT="$2"; shift 2 ;;
      --cpu)     TASK_CPU="$2"; shift 2 ;;
      # Task memory SIZE in MiB (the ECS TaskMemory), NOT the memory model; that is
      # --memory-mode above. Keep this the plain --memory, next to --cpu, because
      # that is what --memory means to anyone reading a deploy driver.
      --memory)  TASK_MEMORY="$2"; shift 2 ;;
      --why) WHY_ONLY=1; shift ;;
      --require-sandbox) ALLOW_UNSANDBOXED=false; shift ;;
      # Retained so an existing invocation does not fail on an unknown option, but
      # it no longer does anything: step 2 always deploys the base stack, because
      # skipping it let a template change silently never land.
      --redeploy-base) shift ;;
      --dry-run) DRY_RUN=1; shift ;;
      --yes)     YES=1; shift ;;
      # Opt-in machine-readable progress. Additive: absent, nothing is written and
      # the human output is unchanged. See the event stream block at the top.
      --events)  SMC_EVENTS="$2"; shift 2 ;;
      -h|--help) sed -n '2,58p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
      *) die "unknown argument: $1" ;;
    esac
  done

  [ -n "$CREW" ] || CREW="frontdesk"

  # Persistent memory needs the owner to have stated the trust domain. crew.yaml
  # asserts this too (a Rule, so a console or pipeline deploy cannot skip it), but
  # refusing here means the owner learns it before step 0 rather than watching six
  # steps of work reach CreateChangeSet and fail.
  #
  # The property being declared is not one this template can enforce: the task role
  # is scoped to the crew's own S3 prefix, which separates crews from each other and
  # NOT customers from each other inside a crew. Every conversation the crew ever
  # served is under that one prefix, the backend auto-approves every tool because an
  # unattended crew cannot stall on an approval nobody sees, and the conversation id
  # arrives in the request rather than being derived from the caller.
  if [ "$MEMORY" = persistent ] && [ "$TRUST_DOMAIN" != single-principal ]; then
    die "--memory-mode persistent requires --trust-domain single-principal.
     Conversations for every customer this crew serves share one S3 prefix that the
     task role can read, and the conversation id comes from the request rather than
     the caller, so one customer's turn can read another's history. Pass
     --trust-domain single-principal to state that every caller you authorise is one
     tenant, or that your own authentication scopes ids in front of the crew.
     Use --memory-mode chatbot if you cannot: it grants no S3 action at all."
  fi

  # CloudFormation stack names -- CONTRACT.md, exact.
  BASE_STACK="smc-base"
  CREW_STACK="smc-crew-$CREW"
  ROUTE_PREFIX="/c/$CREW"          # CONTRACT: SMC_ROUTE_PREFIX / crew param RoutePrefix

  # Architecture: default to this host's, because a native build is minutes and an
  # emulated cross-build is tens of them. (NOTES: the old hardcoded ARM64 was an
  # AgentCore constraint that survived the move to ECS unexamined.)
  local native; native="$(native_docker_arch)" || die "unsupported host architecture: $(uname -m)"
  DOCKER_ARCH="${ARCH_OPT:-$native}"
  case "$DOCKER_ARCH" in arm64|amd64) : ;; *) die "--arch must be arm64 or amd64, got '$DOCKER_ARCH'" ;; esac
  CFN_ARCH="$(cfn_of_docker "$DOCKER_ARCH")" || die "cannot map docker arch $DOCKER_ARCH"

  # State dir, namespaced by profile/region/crew so two crews do not share state
  # (the source script used a single /tmp/smc-deploy for all crews).
  WORK="${SMC_WORK:-${KIROCREW_SCRATCH:-/tmp}/smc-deploy/$PROFILE-$REGION-$CREW}"
  mkdir -p "$WORK"
  # 700, because _curl_auth_file writes a credential in here. Best-effort: a
  # pre-existing directory the owner made differently is not worth aborting over,
  # and the file itself is created under `umask 077` regardless.
  chmod 700 "$WORK" 2>/dev/null || true
  # Defense in depth on the default path only. --crew is already validated, but
  # PROFILE and REGION reach this line too, and the point of this assertion is
  # that produce_bundle runs `rm -rf` on a directory under WORK: a path that
  # escaped would delete somebody's work. SMC_WORK is exempt because a caller who
  # sets it explicitly (the gate tests do) has chosen the directory.
  if [ -z "${SMC_WORK:-}" ]; then
    local _scratch_root="${KIROCREW_SCRATCH:-/tmp}/smc-deploy"
    local _resolved; _resolved="$(cd "$WORK" && pwd -P)" || die "cannot resolve work dir: $WORK"
    local _root_resolved; _root_resolved="$(cd "$_scratch_root" && pwd -P)" \
      || die "cannot resolve scratch root: $_scratch_root"
    case "$_resolved/" in
      "$_root_resolved"/*) : ;;
      *) die "work dir '$_resolved' resolved outside the scratch root '$_root_resolved'.
     Refusing, because a later step runs 'rm -rf' beneath it." ;;
    esac
  fi

  # Open the event stream here: every scalar the run event reports is resolved by
  # now, and nothing has failed inside a step yet. A caller that asked for events
  # is a UI, so an unwritable path is REFUSED rather than ignored -- a deploy
  # running invisibly behind a progress bar that never moves is worse than one
  # that did not start.
  if [ -n "$SMC_EVENTS" ]; then
    mkdir -p "$(dirname "$SMC_EVENTS")" 2>/dev/null || true
    : > "$SMC_EVENTS" || die "cannot write the event stream to $SMC_EVENTS"
    trap '_ev_exit $?' EXIT
    _ev_run
  fi

  if [ "$DRY_RUN" -eq 1 ]; then
    warn "DRY RUN -- no AWS call, no docker build, no push. Exercising the step sequence, state, and resume logic only."
  fi

  # --- AWS + build wrappers ----------------------------------------------
  # Every AWS call goes through aws_(). Under --dry-run it is routed to a fixture
  # dispatcher and never reaches the network. No credential is ever read from a
  # file by this script.
  aws_() {
    if [ "$DRY_RUN" -eq 1 ]; then dry_aws "$@"; return $?; fi
    aws --profile "$PROFILE" --region "$REGION" "$@"
  }
  # The running task's restore SUMMARY line, or "" when it cannot be read.
  #
  # Returns the LAST match: a service that replaced its task leaves several boot
  # logs in the group, and the gate must judge the task that is serving now, not
  # the first one ECS ever started.
  #
  # An unreadable log yields "" and the judge treats that as a refusal rather than
  # a pass, so a missing log stream cannot quietly satisfy this gate.
  restore_summary_line() {
    local cluster sid
    cluster="$(out "$BASE_STACK" ClusterArn)"
    sid="$(aws_ ecs list-tasks --cluster "$cluster" --service-name "$(crew_service_name)" \
            --desired-status RUNNING --query 'taskArns[:1]' --output text 2>/dev/null)"
    sid="${sid##*/}"
    [ -n "$sid" ] && [ "$sid" != "None" ] || return 0
    # `|| true` is load-bearing: grep exits 1 when the line is absent, and under
    # `set -e` that return propagates out of the command substitution at the call
    # site and kills the driver. The absent line is exactly the case the judge is
    # written to refuse, so it has to arrive there as an empty string.
    aws_ logs get-log-events --log-group-name /smc \
      --log-stream-name "$CREW/crew/$sid" --limit 200 \
      --query 'events[].message' --output text 2>/dev/null \
      | tr '\t' '\n' | { grep 'restore: SUMMARY' || true; } | tail -1
  }

  out() { # STACK KEY -> output value ("" if absent)
    aws_ cloudformation describe-stacks --stack-name "$1" \
      --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text 2>/dev/null
  }
  stack_status() {
    aws_ cloudformation describe-stacks --stack-name "$1" \
      --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo NONE
  }

  # Fixture dispatcher for --dry-run. Returns plausible, contract-shaped values so
  # the whole script runs. Individual values are overridable by SMC_FIX_* env vars
  # so a dry run can be pointed at a failing case too.
  dry_aws() {
    local svc="$1" op="${2:-}"
    case "$svc $op" in
      "sts get-caller-identity")
        case " $* " in *" Account "*) echo "${SMC_FIX_ACCOUNT:-111122223333}" ;; *) echo "arn:aws:iam::${SMC_FIX_ACCOUNT:-111122223333}:user/dry-run" ;; esac ;;
      "cloudformation describe-stacks")
        # Feed output queries; unknown -> empty. base + crew outputs.
        local key; key="$(printf '%s\n' "$@" | sed -n "s/.*OutputKey=='\\([^']*\\)'.*/\\1/p")"
        # SMC_FIX_MISSING_OUTPUT=<Name> makes that one output absent, which is how a
        # base stack older than this driver behaves. It exists so the guard against a
        # missing output is proven to FAIL, not merely to pass when nothing is wrong.
        if [ -n "$key" ] && [ "$key" = "${SMC_FIX_MISSING_OUTPUT:-}" ]; then echo ""; return 0; fi
        case "$key" in
          VpcId) echo "vpc-0dryrun" ;;
          PrivateSubnetIds) echo "subnet-a,subnet-b" ;;
          ClusterArn) echo "arn:aws:ecs:$REGION:${SMC_FIX_ACCOUNT:-111122223333}:cluster/smc" ;;
          AlbListenerArn) echo "arn:aws:elasticloadbalancing:$REGION:x:listener/app/smc/1/2" ;;
          AlbArn) echo "arn:aws:elasticloadbalancing:$REGION:x:loadbalancer/app/smc/1" ;;
          AlbDnsName) echo "internal-smc-1.$REGION.elb.amazonaws.com" ;;
          AlbSecurityGroupId) echo "sg-0dryrun" ;;
          LogGroupName) echo "/ecs/smc" ;;
          RestApiId) echo "abc123rest" ;;
          RestApiRootResourceId) echo "rootres01" ;;
          CrewsResourceId) echo "crewsres1" ;;
          VpcLinkId) echo "vpcl-0dryrun" ;;
          BucketName) echo "smc-${SMC_FIX_ACCOUNT:-111122223333}-$REGION" ;;
          RepositoryUri) echo "${SMC_FIX_ACCOUNT:-111122223333}.dkr.ecr.$REGION.amazonaws.com/smc" ;;
          ExecutionRoleArn) echo "arn:aws:iam::${SMC_FIX_ACCOUNT:-111122223333}:role/smc-exec" ;;
          ServiceArn) echo "arn:aws:ecs:$REGION:x:service/smc/smc-$CREW" ;;
          TaskDefinitionArn) echo "arn:aws:ecs:$REGION:x:task-definition/smc-$CREW:1" ;;
          TargetGroupArn) echo "arn:aws:elasticloadbalancing:$REGION:x:targetgroup/smc-$CREW/1" ;;
          ControlBaseUrl) echo "https://abc123rest.execute-api.$REGION.amazonaws.com/${SMC_FIX_STAGE:-prod}/c/$CREW" ;;
          "") echo "${SMC_FIX_STACK_STATUS:-CREATE_COMPLETE}" ;;   # stack_status query
          *) echo "" ;;
        esac ;;
      "cloudformation deploy") : ;;
      "cloudformation delete-stack") : ;;
      "cloudformation wait") : ;;
      "ecr describe-images")
        # Resolve the base image digest from the 'smc-base-<ARCH>' tag. SMC_FIX_BASE_DIGEST
        # overrides; SMC_FIX_NO_BASE=1 makes the tag absent (proves the "no base"
        # die-path can fire).
        if [ "${SMC_FIX_NO_BASE:-0}" = "1" ]; then echo "None"
        else echo "${SMC_FIX_BASE_DIGEST:-sha256:$(printf %064d 1)}"; fi ;;
      "secretsmanager create-secret") echo "arn:aws:secretsmanager:$REGION:${SMC_FIX_ACCOUNT:-111122223333}:secret:smc-$CREW-AbCdEf" ;;
      "secretsmanager put-secret-value") : ;;
      "secretsmanager describe-secret") echo "arn:aws:secretsmanager:$REGION:${SMC_FIX_ACCOUNT:-111122223333}:secret:smc-$CREW-AbCdEf" ;;
      "apigateway create-deployment") : ;;
      "ecs wait") : ;;
      "ecs describe-task-definition")
        # branch on the --query: arch / role / env-value
        case "$*" in
          *cpuArchitecture*) echo "${SMC_FIX_TASKDEF_ARCH:-$CFN_ARCH}" ;;
          *taskRoleArn*)     echo "arn:aws:iam::${SMC_FIX_ACCOUNT:-111122223333}:role/smc-task-$CREW" ;;
          *KIROCREW_BIND*)   echo "${SMC_FIX_ENV_BIND:-127.0.0.1}" ;;
          *SMC_CONFIG_DIR*)  echo "${SMC_FIX_ENV_CONFIG:-/var/lib/kirocrew}" ;;
          *SMC_DATA_HOME*)   echo "${SMC_FIX_ENV_HOME:-/var/lib/kirocrew}" ;;
          *containerDefinitions*image*)
            # The image the deployed task runs. Defaults to the image the build
            # recorded (so the digest gate passes), overridable to force a mismatch.
            if [ -n "${SMC_FIX_TASKDEF_IMAGE:-}" ]; then echo "$SMC_FIX_TASKDEF_IMAGE"
            else cat "$WORK/image-uri" 2>/dev/null || echo ""; fi ;;
          *) echo "" ;;
        esac ;;
      "iam simulate-principal-policy")
        # persistent: own crews/<crew>/ prefix allowed, every other prefix denied.
        # chatbot: the role has no S3 statement, so EVERYTHING is implicitDeny,
        # including the bucket-level ListBucket the persistent branch never probes.
        #
        # The fixture is ACTION-aware, not only prefix-aware, because the template
        # grants GetObject and PutObject and deliberately not DeleteObject. A
        # prefix-only fixture answered "allowed" for delete in the crew's own prefix
        # and the isolation gate's delete assertions would have been vacuous -- the
        # dry run must model what crew.yaml actually grants, or it proves nothing
        # about it.
        if [ "$MEMORY" = chatbot ]; then
          echo "${SMC_FIX_IAM_OWN:-implicitDeny}"
        else
          case "$*" in
            *DeleteObject*) echo "${SMC_FIX_IAM_DELETE:-implicitDeny}" ;;
            *"crews/$CREW/"*) echo "${SMC_FIX_IAM_OWN:-allowed}" ;;
            *) echo "${SMC_FIX_IAM_OTHER:-implicitDeny}" ;;
          esac
        fi ;;
      "s3api get-bucket-policy") echo "${SMC_FIX_BUCKET_POLICY:-An error occurred (NoSuchBucketPolicy)}" ;;
      "ecs list-tasks") echo "arn:aws:ecs:$REGION:x:task/smc/dryruntask01" ;;
      # The container's boot log. SMC_FIX_RESTORE_SUMMARY replaces the whole line so
      # the ephemeral gate can be pointed at a failing case: set it to a line with a
      # non-zero transcripts_restored, or to the empty string to simulate a log that
      # cannot be read. Without a knob the gate could only ever be observed passing.
      "logs get-log-events")
        if [ "${SMC_FIX_RESTORE_SUMMARY+set}" = set ]; then
          printf '%s\n' "$SMC_FIX_RESTORE_SUMMARY"
        elif [ "$MEMORY" = chatbot ]; then
          # What a chatbot crew really logs: no bucket, so nothing to restore FROM.
          printf 'restore: SUMMARY state=disabled transcripts_restored=0 transcripts_available=0 config_restored=0 restored_bytes=0 skipped=0 missing=none\n'
        else
          printf 'restore: SUMMARY state=ok transcripts_restored=0 transcripts_available=58 config_restored=2 restored_bytes=36 skipped=59 missing=none\n'
        fi ;;
      *) : ;;
    esac
  }

  # --- diagnose only ------------------------------------------------------
  # --why answers "the tasks are crashing, why" without deploying anything. The
  # evidence expires (ECS keeps a stopped task about an hour), so it must be
  # reachable without a redeploy that would first delete the rolled-back stack.
  if [ "$WHY_ONLY" -eq 1 ]; then
    step 0 "diagnose only -- nothing is created or changed"
    ACCOUNT="$(aws_ sts get-caller-identity --query Account --output text)" \
      || die "cannot reach AWS with profile '$PROFILE'. Run: aws sso login --profile $PROFILE"
    ok "account $ACCOUNT"
    ClusterArn="$(out "$BASE_STACK" ClusterArn)"
    [ -n "$ClusterArn" ] && [ "$ClusterArn" != "None" ] \
      || die "base stack $BASE_STACK has no ClusterArn output; there is nothing to diagnose yet"
    why_failed "$CREW_STACK"
    why_task_died "$ClusterArn" "smc-$CREW"
    return 0
  fi

  # --- 0 . preflight ------------------------------------------------------
  if [ "$FROM" -le 0 ]; then
  step 0 "preflight -- confirm the account before anything is created"
  judge_route_prefix "$ROUTE_PREFIX" || die "route prefix is malformed"
  ACCOUNT="$(aws_ sts get-caller-identity --query Account --output text)" \
    || die "cannot reach AWS with profile '$PROFILE'. Run: aws sso login --profile $PROFILE"
  ARN="$(aws_ sts get-caller-identity --query Arn --output text)"
  ok "account $ACCOUNT"
  _ev_account "$ACCOUNT"
  note "identity  $ARN"
  note "region    $REGION"
  note "crew      $CREW   route $ROUTE_PREFIX"
  note "base      ${BASE_OPT:-<resolve from the ECR smc-base-<ARCH> tag at step 4>}"
  note "arch      $CFN_ARCH ($DOCKER_ARCH)"
  note "task size cpu=$TASK_CPU mem=$TASK_MEMORY   retention=$RETENTION days"
  [ ${#ALLOW_ARGS[@]} -gt 0 ] && note "allow     ${ALLOW_ARGS[*]}"

  # If this account has no base image and none was pinned with --base, step 4 will
  # BUILD one, and build_image.sh needs a Kiro Crew source checkout to build the
  # wheel from. Resolve that now -- at preflight, before step 2 creates the first
  # resource -- so a first-time deploy that cannot build refuses HERE with a message
  # naming what to set, instead of aborting five steps in with stacks already
  # created. Only when a build is actually needed: --base or an existing 'smc-base-<ARCH>'
  # tag both mean no build, so neither path is asked for a source it will not use.
  if [ -z "${BASE_OPT:-}" ]; then
    local _pf_dg
    _pf_dg="$(base_tag_digest)"
    if [ -z "$_pf_dg" ] || [ "$_pf_dg" = "None" ]; then
      local _pf_src
      _pf_src="$(resolve_kirocrew_src)" || die \
        "this account has no base image yet, so the deploy must build one, and
   scripts/build_image.sh needs a Kiro Crew source checkout to build the wheel from.
   Set --kirocrew-src <path> (or export KIROCREW_SRC) to a Kiro Crew git checkout,
   or pass --base <repo>@sha256:<hex> to reuse an existing base image.
   Refusing now, before anything is created."
      note "base      none in this account yet; step 4 will build one from $_pf_src"
    fi
  fi

  # The bundle is not supplied here: step 1 produces it with packaging.build, and
  # its four-entry layout is checked there (bundle_layout_check) before an image is
  # built. There is nothing to shape-check at preflight because nothing has been
  # curated yet.

  if [ "$YES" -ne 1 ] && [ "$DRY_RUN" -ne 1 ]; then
    printf '\n   This creates AWS resources in account %s. Continue? [y/N] ' "$ACCOUNT"
    read -r reply
    case "$reply" in y|Y) ;; *) _ev_end aborted "the operator declined at the confirmation"; echo "   aborted, nothing created"; exit 0 ;; esac
  fi
  echo "$ACCOUNT" > "$WORK/account"
  fi
  ACCOUNT="$(cat "$WORK/account" 2>/dev/null)" || die "run without --from first to record the account"
  _ev_account "$ACCOUNT"

  # --- 1 . bundle ---------------------------------------------------------
  if [ "$FROM" -le 1 ]; then
  step 1 "bundle -- curate the crew into the four-entry layout (T1: python -m packaging.build)"
  produce_bundle    # writes $WORK/bundle-dir and $WORK/bundle-digest from SMC_BUNDLE_JSON
  fi
  BUNDLE_DIR="$(cat "$WORK/bundle-dir" 2>/dev/null)" || die "no bundle recorded; run step 1"
  BUNDLE_DIGEST="$(cat "$WORK/bundle-digest" 2>/dev/null)" || die "no bundle digest recorded; run step 1"

  # --- 2 . base stack -----------------------------------------------------
  if [ "$FROM" -le 2 ]; then
  step 2 "base stack ($BASE_STACK) -- vpc, cluster, internal ALB, VPC link, control API, bucket"
  clear_if_rollback "$BASE_STACK"
  local bstate; bstate="$(stack_status "$BASE_STACK")"
  # The base stack is ALWAYS deployed, never skipped for existing. `cloudformation
  # deploy` with --no-fail-on-empty-changeset is already idempotent: an unchanged
  # template is a no-op that costs one changeset. Skipping it saved that and cost
  # correctness -- a change to base.yaml silently never landed, so the run
  # succeeded against infrastructure that did not match the templates it deployed
  # from, and the mismatch surfaced later as a missing stack output.
  if printf '%s' "$bstate" | grep -q '_COMPLETE$' && [ "$bstate" != "ROLLBACK_COMPLETE" ]; then
    note "base stack present ($bstate); deploying anyway -- an unchanged template is a no-op"
  else
    note "first run takes several minutes; an ALB and a VPC link are not fast"
  fi
  if ! aws_ cloudformation deploy \
      --stack-name "$BASE_STACK" \
      --template-file "$CREW_ROOT/templates/base.yaml" \
      --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM --no-fail-on-empty-changeset \
      --parameter-overrides "LogRetentionDays=$RETENTION"; then
    # base.yaml declares LogRetentionDays (container/CloudWatch log retention),
    # not TranscriptRetentionDays. Passing the undeclared name aborted the base
    # deploy at CreateChangeSet. LogRetentionDays is the only retention knob the
    # template actually enforces (the LogGroup's RetentionInDays); transcript
    # retention has no enforcement here -- see base.yaml's note by ArtifactsBucket.
    # $RETENTION is a single ruled days value (CHORUS R6, default 30) and now
    # drives that enforceable retention.
    why_failed "$BASE_STACK"
    die "base stack failed -- see the reasons above"
  fi
  ok "base stack deployed"
  fi

  # Resolve every base output -- ALWAYS, on every run, because verify at --from 8
  # needs them. Names are exact per CONTRACT.
  resolve_base_outputs

  # --- 3 . (removed) ------------------------------------------------------
  # The old step 3 uploaded the bundle to S3 and passed its URI to the container.
  # Nothing in the container ever read it, so the deployment served a default agent
  # while every gate reported green. The bundle now rides INSIDE the crew image
  # (step 4): one artifact whose digest pins both serving code and crew content, and
  # a layer of the running image cannot be absent. So there is nothing to upload.
  #
  # The conversation-BACKUP path (crews/<crew>/*) is a DIFFERENT path and is
  # untouched: the backup sidecar writes it at runtime and the task role still
  # grants it. Only the bundle's S3 round-trip is gone.

  # --- 4 . crew image (build + push, delegated to the crew-image track) ---
  if [ "$FROM" -le 4 ]; then
  step 4 "crew image -- thin layer on the base, bundle baked in, digest-pinned (T2: build_crew_image.sh)"
  build_crew_image_step    # writes $WORK/image-uri, $WORK/image-arch, $WORK/image-machine, $WORK/image-bundle-digest
  fi
  IMAGE_URI="$(cat "$WORK/image-uri" 2>/dev/null)" || die "no image uri recorded; run step 4"
  IMAGE_ARCH="$(cat "$WORK/image-arch" 2>/dev/null || echo "")"                 # CloudFormation spelling
  IMAGE_MACHINE="$(cat "$WORK/image-machine" 2>/dev/null || echo "")"           # uname of what ran inside; a thin crew layer execs nothing, so empty by design
  IMAGE_BUNDLE_DIGEST="$(cat "$WORK/image-bundle-digest" 2>/dev/null || echo "")"
  echo "$CFN_ARCH" > "$WORK/cfn-arch"
  ok "image $IMAGE_URI"

  # --- 5 . secrets --------------------------------------------------------
  if [ "$FROM" -le 5 ]; then
  step 5 "secrets -- stored by reference; the crew stack receives ARNs, never plaintext"
  local api_name="smc/$CREW/kiro-api-key"
  local ctl_name="smc/$CREW/control-secret"

  # The plaintext is needed only to CREATE or ROTATE. When the secret already
  # exists and no value is exported, reuse its ARN: the crew stack takes the ARN,
  # so demanding the value on every resume would make the operator fetch a live
  # credential into a shell for nothing.
  #
  # The dry run deliberately does NOT force the write branch, so whether
  # KIRO_API_KEY is exported selects which branch the suite exercises.
  if [ -n "${KIRO_API_KEY:-}" ]; then
    judge_api_key_shape "$KIRO_API_KEY" \
      || die "refusing to store that as $api_name -- see above. Export the real key and re-run."
    # Compare BEFORE writing: a put that stores the same bytes is not a rotation, and
    # forcing a redeploy on every run with the key exported would restart the service
    # for nothing.
    local before after
    before="$(secret_digest "$api_name")"
    API_KEY_ARN="$(put_secret "$api_name" "$(printf '%s' "$KIRO_API_KEY" | tr -d '[:space:]')")"
    after="$(printf '%s' "$KIRO_API_KEY" | tr -d '[:space:]' | sha256sum | cut -c1-12)"
    # Length and a short digest, never the value: enough to tell "the key I meant"
    # from "a different key" across two deploys, which is the question that arises
    # when one crew answers and another reports an expired session.
    note "api key written from the environment (${#KIRO_API_KEY} chars, sha256 $after)"
    if [ -n "$before" ] && [ "$before" != "$after" ]; then
      SECRET_ROTATED=1
      note "            the stored value CHANGED (was sha256 $before)"
    fi
  else
    API_KEY_ARN="$(secret_arn "$api_name")"
    [ -n "$API_KEY_ARN" ] && [ "$API_KEY_ARN" != "None" ] || die \
      "$api_name does not exist yet and KIRO_API_KEY is not exported. The value is
   needed once to create it. Export your real key in your shell, then re-run:
     $0 --from 5 --crew $CREW
   Deliberately not written here as a copyable assignment: a pasted example
   becomes the stored key, and that surfaces three steps later as \"your session
   has expired\", which points at sign-in rather than at the value.
   To rotate it later, export the new value and re-run this step. To keep the
   stored value, leave KIRO_API_KEY unset -- this step reuses it."
    note "api key reused from the existing secret (KIRO_API_KEY not exported, nothing rotated)"
  fi

  # Same reasoning, and one more: regenerating this on every resume would rotate a
  # credential the owner may already hold, for no reason.
  CONTROL_SECRET_ARN="$(secret_arn "$ctl_name")"
  if [ -z "$CONTROL_SECRET_ARN" ] || [ "$CONTROL_SECRET_ARN" = "None" ]; then
    CONTROL_SECRET="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
    CONTROL_SECRET_ARN="$(put_secret "$ctl_name" "$CONTROL_SECRET")"
    printf '%s' "$CONTROL_SECRET" > "$WORK/control-secret"; chmod 600 "$WORK/control-secret"
    note "control secret generated"
  else
    note "control secret reused from the existing secret (not rotated)"
  fi

  printf '%s' "$API_KEY_ARN" > "$WORK/api-key-arn"
  printf '%s' "$CONTROL_SECRET_ARN" > "$WORK/control-secret-arn"
  ok "both stored; ARNs carry no version pin, so rotation is update-then-force-new-deployment"
  fi
  API_KEY_ARN="$(cat "$WORK/api-key-arn")"
  CONTROL_SECRET_ARN="$(cat "$WORK/control-secret-arn")"

  # --- 6 . crew stack -----------------------------------------------------
  if [ "$FROM" -le 6 ]; then
  step 6 "crew stack ($CREW_STACK) -- task definition, service, listener rule, control routes"
  note "DesiredCount is 1 by contract (one gateway per data home); the template only accepts 0 or 1"
  allocate_rule_priority
  if [ "$ALLOW_UNSANDBOXED" = "true" ]; then
    note "sandbox: the model subprocess runs UNSANDBOXED (Fargate permits no user namespace)."
    note "         it compounds with auto-approved tools; --require-sandbox refuses instead."
  else
    note "sandbox: required (--require-sandbox). On Fargate the container will refuse to start."
  fi
  # Early arch fail before the create -- cheaper than a failed service. Only a real
  # contradiction (rc 1) blocks here; an unproven exec leg (rc 2, cross-build) is
  # fine pre-deploy. The authoritative arch gate still runs at verify regardless.
  local arc_rc=0
  judge_arch "$CFN_ARCH" "$CFN_ARCH" "$IMAGE_ARCH" "$IMAGE_MACHINE" || arc_rc=$?
  [ "$arc_rc" -eq 1 ] && die "built image arch contradicts the intended task arch (see above)"
  clear_if_rollback "$CREW_STACK"
  if ! aws_ cloudformation deploy \
      --stack-name "$CREW_STACK" \
      --template-file "$CREW_ROOT/templates/crew.yaml" \
      --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM --no-fail-on-empty-changeset \
      --parameter-overrides \
        "CrewName=$CREW" \
        "ImageUri=$IMAGE_URI" \
        "CpuArchitecture=$CFN_ARCH" \
        "RulePriority=$RULE_PRIORITY" \
        "TaskCpu=$TASK_CPU" \
        "TaskMemory=$TASK_MEMORY" \
        "RoutePrefix=$ROUTE_PREFIX" \
        "ApiKeySecretArn=$API_KEY_ARN" \
        "ControlSecretArn=$CONTROL_SECRET_ARN" \
        "VpcId=$VpcId" \
        "PrivateSubnetIds=$PrivateSubnetIds" \
        "ClusterArn=$ClusterArn" \
        "AlbListenerArn=$AlbListenerArn" \
        "AlbArn=$AlbArn" \
        "AlbDnsName=$AlbDnsName" \
        "AlbSecurityGroupId=$AlbSecurityGroupId" \
        "LogGroupName=$LogGroupName" \
        "RestApiId=$RestApiId" \
        "VpcLinkId=$VpcLinkId" \
        "CrewsResourceId=$CrewsResourceId" \
        "BucketName=$BUCKET_NAME" \
        "Memory=$MEMORY" \
        "TrustDomain=$TRUST_DOMAIN" \
        "ExecutionRoleArn=$ExecutionRoleArn" \
        "AllowUnsandboxedExec=$ALLOW_UNSANDBOXED"; then
    why_failed "$CREW_STACK"
    why_task_died "$ClusterArn" "smc-$CREW"
    die "crew stack failed -- see the reasons above"
  fi
  ok "crew stack deployed"
  # A "No changes to deploy" changeset does NOT mean nothing needs to happen: a
  # rewritten secret is invisible to CloudFormation and the running task still holds
  # the old value.
  force_new_deployment_if_secret_changed
  fi

  # Resolve crew outputs -- ALWAYS (verify needs them at --from 8).
  resolve_crew_outputs

  # --- 7 . republish the stage -------------------------------------------
  if [ "$FROM" -le 7 ]; then
  step 7 "republish the API stage (CloudFormation cannot express this)"
  note "without it the new crew's route answers 403 as though it did not exist"
  local stage; stage="$(stage_name)"
  aws_ apigateway create-deployment --rest-api-id "$RestApiId" --stage-name "$stage" >/dev/null \
    || die "stage republish failed"
  ok "stage $stage republished on $RestApiId"
  fi

  # --- 8 . verify ---------------------------------------------------------
  smc_verify
}

# ---------------------------------------------------------------------------
# Impure helpers used by smc_main. Defined at file scope so smc_main's inner
# functions can call them; they read the caller's PROFILE/REGION/WORK/etc.
# ---------------------------------------------------------------------------

# Bundle layout check -- the four-entry layout the container enforces at boot
# (PACKAGING-CONTRACT "Layout, exact"), pulled out so it is callable from tests.
# This is a FAIL-FAST before an expensive image build + deploy; the AUTHORITATIVE
# enforcement -- recomputing the content digest and refusing to boot on a mismatch
# -- is the container's (T3). This does NOT recompute the digest, because that
# needs bundle.py's exact algorithm, owned by the curation track. Returns non-zero
# on any failure. Optional second arg asserts manifest crew_name == that value.
bundle_layout_check() { # BUNDLE_DIR [EXPECTED_CREW]
  python3 - "$1" "${2:-}" <<'PY'
import json, sys, os
b = sys.argv[1]
expect = sys.argv[2] or None
for entry in ("manifest.json", "agent.json", "mcp.json", "skills"):
    if not os.path.exists(os.path.join(b, entry)):
        print(f"   bundle missing required entry: {entry}", file=sys.stderr); sys.exit(1)
if not os.path.isdir(os.path.join(b, "skills")):
    print("   'skills' must be a directory (may be empty, but MUST exist)", file=sys.stderr); sys.exit(1)
try:
    man  = json.load(open(f"{b}/manifest.json"))
    spec = json.load(open(f"{b}/agent.json"))
    json.load(open(f"{b}/mcp.json"))   # must parse; may be {}
except Exception as e:
    print(f"   bundle json will not parse: {e}", file=sys.stderr); sys.exit(1)
if man.get("bundle_version") != 1:
    print(f"   bundle_version must be 1, got {man.get('bundle_version')}", file=sys.stderr); sys.exit(1)
cn = man.get("crew_name")
if not cn:
    print("   manifest.json has no crew_name", file=sys.stderr); sys.exit(1)
if expect is not None and cn != expect:
    print(f"   manifest crew_name={cn!r} != requested crew {expect!r}", file=sys.stderr); sys.exit(1)
if spec.get("name") != cn:
    print(f"   agent.json name={spec.get('name')!r} != manifest crew_name {cn!r}", file=sys.stderr); sys.exit(1)
print(f"   crew={cn} bundle_version={man.get('bundle_version')} (four-entry layout present)")
PY
}

# Explain why any stack failed (source: matched every *_FAILED status).
why_failed() {
  printf '\n%s   the failing resources and why:%s\n' "$_C_RED" "$_C_RST"
  aws_ cloudformation describe-stack-events --stack-name "$1" \
    --query "StackEvents[?ends_with(ResourceStatus, '_FAILED')].[LogicalResourceId,ResourceType,ResourceStatusReason]" \
    --output text 2>/dev/null | sed 's/^/     /' | head -20
}

# When a service fails to stabilize, the stack events say only that the circuit
# breaker fired. The reason is on the stopped TASK, and if the container started
# and then exited it is only in its log stream.
#
# This used to print the three commands for the operator to run. The driver holds
# the profile and the cluster arn, so printing them was handing over work it could
# do -- and the evidence expires: ECS keeps a stopped task for about an hour.
why_task_died() {
  local cluster="$1" family="$2" arns arn
  printf '\n%s   why the task died:%s\n' "$_C_RED" "$_C_RST"

  arns="$(aws_ ecs list-tasks --cluster "$cluster" --family "$family" \
          --desired-status STOPPED --query 'taskArns[:3]' --output text 2>/dev/null)"
  if [ -z "$arns" ] || [ "$arns" = "None" ]; then
    # NOT dying is a different failure, and the more confusing one: a task that
    # answers 200 with an empty completion is healthy by every infrastructure
    # measure. Its LIVE log is the only place that says why, and this function only
    # ever looked at STOPPED tasks -- so the case where every gate is green except
    # the answer had no diagnosis at all.
    local live
    live="$(aws_ ecs list-tasks --cluster "$cluster" --family "$family" \
            --desired-status RUNNING --query 'taskArns[:1]' --output text 2>/dev/null)"
    if [ -n "$live" ] && [ "$live" != "None" ]; then
      local sid="${live##*/}"
      printf '     nothing died: a task is RUNNING (%s). If turns come back empty the\n' "$sid"
      printf '     reason is in its log, not in an exit code. Last 60 lines:\n'
      aws_ logs get-log-events --log-group-name /smc \
        --log-stream-name "$CREW/crew/$sid" --limit 200 \
        --query 'events[].message' --output text 2>/dev/null \
        | tr '\t' '\n' | tail -60 | sed 's/^/       /' \
        || printf '       no log stream at %s/crew/%s\n' "$CREW" "$sid"
      return 0
    fi
    printf '     no stopped task recorded for family %s.\n' "$family"
    printf '     A task that never started leaves no record. The service events:\n'
    local ev
    ev="$(aws_ ecs describe-services --cluster "$cluster" --services "$(crew_service_name)" \
           --query 'services[0].events[:5].message' --output text 2>/dev/null)"
    if [ -z "$ev" ] || [ "$ev" = "None" ]; then
      # A bare "None" reads as an empty event list on an existing service. It usually
      # means the service was never created, which points at a resource that failed
      # BEFORE it -- read the stack events above, not here.
      printf '       the service does not exist, so nothing ran. The failure is in the\n'
      printf '       stack events above, at a resource that comes before the service.\n'
    else
      printf '%s' "$ev" | tr '\t' '\n' | sed 's/^/       /'
    fi
    return 0
  fi

  for arn in $arns; do
    printf '     task %s\n' "${arn##*/}"
    aws_ ecs describe-tasks --cluster "$cluster" --tasks "$arn" \
      --query 'tasks[0].[stopCode,stoppedReason,containers[0].name,containers[0].exitCode,containers[0].reason]' \
      --output text 2>/dev/null | tr '\t' '\n' | sed 's/^/       /'

    # An exit code means the container RAN. Then the reason is in its log stream,
    # whose name ECS builds as <prefix>/<container>/<task id>.
    local sid="${arn##*/}"
    printf '     its last log lines (%s/%s/%s):\n' "$CREW" crew "$sid"
    aws_ logs get-log-events --log-group-name /smc \
      --log-stream-name "$CREW/crew/$sid" --limit 40 \
      --query 'events[].message' --output text 2>/dev/null \
      | tr '\t' '\n' | sed 's/^/       /' | tail -40 \
      || printf '       no log stream: the container never started (image pull or secret fetch)\n'
    printf '\n'
  done
}

# A first-create rollback leaves ROLLBACK_COMPLETE, which cannot be updated;
# delete it before retry. Nothing was successfully created in that state.
clear_if_rollback() {
  local s; s="$(stack_status "$1")"
  if [ "$s" = "ROLLBACK_COMPLETE" ]; then
    note "previous attempt left $1 in ROLLBACK_COMPLETE; deleting before retry (destroys no working resource)"
    aws_ cloudformation delete-stack --stack-name "$1"
    aws_ cloudformation wait stack-delete-complete --stack-name "$1" || true
    ok "cleared"
  fi
}

# Create or update a secret; print its ARN. Idempotent.
#
# The VALUE goes through a mode-600 file, never on the command line. `aws` does not
# rewrite its own argv the way curl does, so `--secret-string "$v"` leaves the API
# key readable in /proc for the whole call. `file://` is the CLI's own documented way
# to read an argument from disk.
#
# The file is created empty under `umask 077`, written, used, and removed on every
# exit path via a trap -- so a failed call does not leave the secret behind.
put_secret() { # NAME VALUE -> arn
  local n="$1" v="$2" arn sf
  sf="$WORK/.secret-in"
  ( umask 077; : > "$sf" )
  printf '%s' "$v" > "$sf"
  # shellcheck disable=SC2064  # expand $sf now: the trap must name this exact file
  trap "rm -f '$sf'" RETURN
  if arn="$(aws_ secretsmanager create-secret --name "$n" --secret-string "file://$sf" \
            --query ARN --output text 2>/dev/null)"; then
    printf '%s' "$arn"; return 0
  fi
  aws_ secretsmanager put-secret-value --secret-id "$n" --secret-string "file://$sf" >/dev/null
  aws_ secretsmanager describe-secret --secret-id "$n" --query ARN --output text
}

# Print an existing secret's ARN, or nothing when it does not exist. The crew
# stack only ever receives the ARN, so a re-run does not need the plaintext at
# all: requiring the value every time turns every `--from N` resume into a
# credential-handling step for no benefit, which is a reason to fetch a live
# credential into a shell rather than a reason to have it there.
secret_arn() { # NAME -> arn or empty
  aws_ secretsmanager describe-secret --secret-id "$1" --query ARN --output text 2>/dev/null || true
}

# Produce the crew bundle. Delegated to T1's `python -m packaging.build` per
# PACKAGING-CONTRACT track ownership. Reads the machine contract from the
# SMC_BUNDLE_JSON=<path> marker (last stdout line), never from prose. Under
# --dry-run a fixture stands in and writes a REAL four-entry bundle + a real
# SMC_BUNDLE_JSON, so the rest of the flow runs against reality rather than intent.
produce_bundle() {
  local out="$WORK/bundle" jpath
  if [ "$DRY_RUN" -eq 1 ]; then
    dry_packaging_build "$out"        # writes $out/* and $WORK/bundle.json
    jpath="$WORK/bundle.json"
    note "dry-run: skipped packaging.build; wrote a fixture four-entry bundle + SMC_BUNDLE_JSON"
  else
    local py="${VIRTUAL_ENV:+$VIRTUAL_ENV/bin/python}"; [ -x "$py" ] || py="$(command -v python3)"
    mkdir -p "$out"
    # No `rm -rf "$out"` here, and removing it is a fix rather than a tidy-up.
    # packaging.build stages the bundle and swaps it in atomically, so it already
    # leaves nothing half-written -- and it now also carries a signed
    # curation-plan.json through that swap and REFUSES a directory holding files it
    # does not own. Wiping the directory first defeated both: an owner who passed
    # --allow "$out/curation-plan.json" had the signed plan deleted right here,
    # before packaging.build ever started, so the refusal it would have raised never
    # ran and the plan was already gone when the failure appeared.
    # CONTRACT (PACKAGING-CONTRACT T1): `python -m packaging.build --crew <name>
    # --out <dir> [--allow <path>]...` writes the four-entry layout into <dir> and
    # prints, as the LAST line, SMC_BUNDLE_JSON=<path>. Deny-by-default lives inside
    # packaging.build; --allow is the owner's explicit override, forwarded here.
    local raw marker
    raw="$(cd "$CREW_ROOT" && "$py" -m packaging.build --crew "$CREW" --out "$out" "${ALLOW_ARGS[@]}")" \
      || die "packaging.build failed (T1, owned by the curation track)"
    marker="$(printf '%s\n' "$raw" | grep '^SMC_BUNDLE_JSON=' | tail -n 1)"
    [ -n "$marker" ] || die "packaging.build emitted no SMC_BUNDLE_JSON= marker; cannot locate the bundle record"
    jpath="${marker#SMC_BUNDLE_JSON=}"
    [ -f "$jpath" ] || die "SMC_BUNDLE_JSON points at a missing file: $jpath"
    cp "$jpath" "$WORK/bundle.json"
    jpath="$WORK/bundle.json"
  fi

  local bdir digest skills mcp denied fingerprint
  bdir="$(bundle_json_field bundle_dir)"
  digest="$(bundle_json_field digest)"
  skills="$(bundle_json_field skill_count)"
  mcp="$(bundle_json_field mcp_servers)"
  denied="$(bundle_json_field denied)"
  fingerprint="$(bundle_json_field fingerprint)"
  [ -n "$bdir" ] || die "SMC_BUNDLE_JSON has no bundle_dir"
  case "$digest" in sha256:*) : ;; *) die "SMC_BUNDLE_JSON digest is not sha256-form: '$digest'" ;; esac
  # Without this, the one gate that can tell whose prompt is answering has nothing to
  # compare against, and it would report "unproven" on every deploy.
  [ -n "$fingerprint" ] || die "SMC_BUNDLE_JSON has no fingerprint; the served prompt could not be identified"
  # Fail fast on a malformed layout before an image is built (the container is the
  # authoritative enforcer at boot).
  bundle_layout_check "$bdir" "$CREW" || die "packaging.build output failed the four-entry layout check"
  echo "$bdir"        > "$WORK/bundle-dir"
  echo "$digest"      > "$WORK/bundle-digest"
  echo "$fingerprint" > "$WORK/bundle-fingerprint"
  ok "crew '$CREW' curated: skills=$skills mcp_servers=$mcp"
  note "bundle_dir  $bdir"
  note "digest      $digest"
  note "fingerprint $fingerprint  (the prompt gate's challenge answer)"
  # The full list ran to 117 entries on one line and buried the four values above it,
  # which are the ones a reader needs. What matters in the log is HOW MANY were held
  # back and that the list is somewhere readable; the deny decision itself is already
  # enforced, not advisory.
  printf '%s' "$denied" > "$WORK/denied.json"
  local n_denied
  n_denied="$(printf '%s' "$denied" | python3 -c 'import json,sys
try: print(len(json.load(sys.stdin)))
except Exception: print("?")' 2>/dev/null || echo "?")"
  note "denied      $n_denied candidate(s) held back by deny-by-default -> $WORK/denied.json"
  if [ "$skills" = "0" ] && [ "$mcp" = "[]" ]; then
    note "            nothing was selected, so this bundle is the crew's PERSONA ONLY."
    note "            To ship a skill or an MCP server, sign a curation plan and pass --allow."
  fi
}

# Read one field from the SMC_BUNDLE_JSON record. JSON null -> ""; lists/dicts ->
# compact JSON (for the note lines).
bundle_json_field() { # FIELD
  python3 -c 'import json,sys
v=json.load(open(sys.argv[1])).get(sys.argv[2])
if v is None: print("")
elif isinstance(v,(list,dict)): print(json.dumps(v,separators=(",",":")))
else: print(v)' "$WORK/bundle.json" "$1" 2>/dev/null
}

# Fixture producer for --dry-run step 1. Writes a REAL four-entry bundle to <out>
# and a real SMC_BUNDLE_JSON to $WORK/bundle.json -- what `python -m packaging.build`
# actually returns, NOT what the design intends (a fixture that encoded intent is
# how a gate for a nonexistent route reached a real account). The digest is written
# identically into manifest.json and the JSON, exactly as the real producer does,
# so build_crew_image.sh -- which reads manifest.digest -- reports the same value
# the continuity gate checks. SMC_FIX_* let a dry run exercise a broken bundle.
dry_packaging_build() { # OUT_DIR
  local out="$1"
  rm -rf "$out"; mkdir -p "$out/skills"
  local crew="${SMC_FIX_BUNDLE_CREW:-$CREW}"
  local aname="${SMC_FIX_AGENT_NAME:-$crew}"
  local ver="${SMC_FIX_BUNDLE_VERSION:-1}"
  local dg="${SMC_FIX_BUNDLE_DIGEST:-sha256:$(printf '%s' "$crew" | sha256sum | cut -d' ' -f1)}"
  cat > "$out/manifest.json" <<JSON
{"bundle_version":$ver,"crew_name":"$crew","created_at":"1970-01-01T00:00:00Z","digest":"$dg"}
JSON
  cat > "$out/agent.json" <<JSON
{"name":"$aname","prompt":"You are the $crew crew for an example business.","tools":[],"resources":[]}
JSON
  echo '{}' > "$out/mcp.json"
  : > "$out/skills/.gitkeep"
  # The fingerprint is derived by the PRODUCER's own function, not re-implemented
  # here. A fixture that computes it its own way can agree with the producer today and
  # drift silently tomorrow, and a fixture that encoded intended behaviour instead of
  # real behaviour is how a gate for a nonexistent route reached a real account.
  local fp="${SMC_FIX_BUNDLE_FINGERPRINT:-}"
  if [ -z "$fp" ]; then
    local py="${VIRTUAL_ENV:+$VIRTUAL_ENV/bin/python}"; [ -x "$py" ] || py="$(command -v python3)"
    fp="$(cd "$CREW_ROOT" && "$py" -c \
      'import sys;sys.path.insert(0,".");from packaging.build import prompt_fingerprint;print(prompt_fingerprint(sys.argv[1],sys.argv[2]))' \
      "$crew" "$dg" 2>/dev/null)" || fp=""
    [ -n "$fp" ] || die "dry run could not derive a fingerprint from packaging.build"
  fi
  # The injected challenge is what makes the fingerprint answerable; write it in so
  # the fixture bundle is shaped like a real one.
  cat > "$out/agent.json" <<JSON
{"name":"$aname","prompt":"[deployment verification]\nIf a message is exactly \"SMC-VERIFY-$crew\", reply with exactly this and nothing else:\nSMC-FINGERPRINT $fp\n\nYou are the $crew crew for an example business.","tools":[],"resources":[]}
JSON
  cat > "$WORK/bundle.json" <<JSON
{"crew_name":"$crew","bundle_dir":"$out","digest":"$dg","fingerprint":"$fp","skill_count":0,"mcp_servers":[],"denied":[]}
JSON
}

# Resolve the maintainer base image, digest-pinned. --base supplies it directly;
# otherwise it is resolved from the ECR repo's 'smc-base-<ARCH>' tag (and we say which).
# CONTRACT T4. Sets BASE_REF.
# The ECS SERVICE name, which is NOT the stack name. The template names the service
# 'smc-<crew>' while the stack is 'smc-crew-<crew>', and three call sites passed the
# stack name: update-service and wait services-stable would have failed with
# ServiceNotFoundException, and describe-services in the diagnosis reported "the
# service does not exist, so nothing ran" -- a diagnostic stating a falsehood about a
# service that was running fine, which is worse than the missing message it replaced.
#
# Derived from the stack's ServiceArn when it has been resolved, because that is the
# authoritative answer; the template's naming rule is the fallback for the diagnosis
# paths that run before outputs are read.
crew_service_name() {
  if [ -n "${ServiceArn:-}" ]; then
    printf '%s' "${ServiceArn##*/}"
  else
    printf 'smc-%s' "$CREW"
  fi
}

# A short digest of a stored secret, never its value. Empty when it does not exist.
secret_digest() { # NAME -> 12 hex chars or empty
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '%s' "${SMC_FIX_STORED_KEY_DIGEST:-}"
    return 0
  fi
  # The read is its own statement with `|| true`, NOT a `|| true` tacked onto the
  # hashing pipeline. On a FIRST deploy the secret does not exist yet, so
  # get-secret-value exits non-zero; under `set -euo pipefail` that made the whole
  # pipeline non-zero and the caller's `before="$(secret_digest ...)"` aborted the
  # deploy before the secret was ever created. The secret could therefore never be
  # created on a fresh account -- the one path nobody had run.
  #
  # Empty is the honest answer for any failure here, not just absence: the caller
  # uses this value only to decide whether the key CHANGED, and "could not read it"
  # has to count as changed or a rotation would be missed. `secret_arn` above already
  # takes exactly this shape, and the two now agree.
  local stored=""
  stored="$(aws_ secretsmanager get-secret-value --secret-id "$1" \
    --query SecretString --output text 2>/dev/null || true)"
  [ -n "$stored" ] || return 0
  printf '%s' "$stored" | tr -d '\n' | sha256sum | cut -c1-12
}

# A task reads a secret ONCE, at start. The task definition references the secret by
# an ARN with no version stage, which is deliberate -- pinning a version would make
# every rotation a template change -- but it means updating the secret changes nothing
# about a running task. So a deploy that rewrites a key and then finds the crew stack
# unchanged ("No changes to deploy") leaves the OLD value in service and reports
# success. Fixing a bad credential and having nothing happen is the failure mode this
# exists to prevent.
force_new_deployment_if_secret_changed() {
  [ "$SECRET_ROTATED" = "1" ] || return 0
  if [ "$DRY_RUN" -eq 1 ]; then
    ok "dry-run: would force a new deployment so the task re-reads the rotated secret"
    return 0
  fi
  note "a secret was rewritten, and a task reads its secrets only at start"
  aws_ ecs update-service --cluster "$ClusterArn" --service "$(crew_service_name)" \
    --force-new-deployment >/dev/null 2>&1 \
    || die "could not force a new deployment; the task is still running with the OLD secret value"
  ok "forced a new deployment; waiting for it to stabilize"
  aws_ ecs wait services-stable --cluster "$ClusterArn" --services "$(crew_service_name)" \
    || die "the forced deployment did not stabilize -- run --why"
  ok "the running task has re-read the secrets"
}

# Refuse a value that cannot be an API key, before it is stored as one.
#
# This does NOT validate the key against the service -- nothing here can. It rejects
# the values that are obviously not keys, because the cost of storing one is paid
# three steps later and points the wrong way: the container reports "Your session has
# expired. Run `kiro-cli login` in your terminal", which sends the operator to fix a
# sign-in that was never the mechanism.
#
# The value that caused this was the literal "..." from a pasted example command. So
# the placeholder shapes are named explicitly rather than only bounded by length: an
# operator who pastes an example deserves to be told so, not to debug a login.
judge_api_key_shape() { # VALUE
  local v="$1" n=${#1}
  case "$v" in
    ...|…|'<key>'|'<KIRO_API_KEY>'|'your-key-here'|'REPLACE_ME'|'xxx'|'TODO')
      printf 'KIRO_API_KEY is the literal placeholder %q -- an example command was pasted verbatim.\n' "$v" >&2
      return 1 ;;
  esac
  if [ "$n" -lt 20 ]; then
    printf 'KIRO_API_KEY is %d characters, too short to be a key. Storing it would fail three steps later as "your session has expired", which points at sign-in rather than at this value.\n' "$n" >&2
    return 2
  fi
  # All-punctuation or all-identical characters cannot be a key and are what a
  # truncated paste or a shell-expansion accident produces.
  case "$v" in
    *[!A-Za-z0-9_.\-+/=]*)
      printf 'KIRO_API_KEY contains characters no key uses (whitespace or shell metacharacters). Check the export quoting.\n' >&2
      return 3 ;;
  esac
  return 0
}

# Authenticate docker to the ECR registry. The driver is the only side that holds the
# profile -- neither build script takes --profile by contract -- so both delegated
# builds call this first. Without it a push fails with a bare "denied", which reads
# like a repository permission problem rather than a missing login; that cost an hour
# once already.
ecr_login() {
  local registry="${REPO_URI%%/*}"
  if [ "$DRY_RUN" -eq 1 ]; then
    note "dry-run: would authenticate docker to $registry"
    return 0
  fi
  aws ecr get-login-password --region "$REGION" ${PROFILE:+--profile "$PROFILE"} \
    | docker login --username AWS --password-stdin "$registry" >/dev/null \
    || die "could not authenticate docker to $registry; the push would fail with 'denied'"
  ok "docker authenticated to $registry"
}

# The digest behind the moving per-architecture 'smc-base-<ARCH>' tag, or empty when it does not exist.
base_tag_digest() {
  if [ "$DRY_RUN" -eq 1 ]; then
    # SMC_FIX_NO_BASE means the account has no base YET. Once the fixture build has
    # run, the tag exists -- a fixture that kept answering "absent" after a
    # successful build would model an impossible account and make the auto-build
    # path look broken when it works.
    if [ -n "${SMC_FIX_NO_BASE:-}" ] && [ ! -f "$WORK/base-built" ]; then
      echo ""; return 0
    fi
    echo "sha256:$(printf 'dryrun-base' | sha256sum | cut -d' ' -f1)"
    return 0
  fi
  aws_ ecr describe-images --repository-name smc \
    --image-ids imageTag="smc-base-$CFN_ARCH" \
    --query 'imageDetails[0].imageDigest' --output text 2>/dev/null || true
}

# Resolve the Kiro Crew source checkout the base builder needs, or fail with a
# message naming exactly what to set. scripts/build_image.sh builds the wheel from
# this checkout and REQUIRES it (--kirocrew-src or $KIROCREW_SRC), validating that
# it is a git repo. There is no defensible fixed default -- the old one was a single
# developer's home directory -- so the driver either passes through what the owner
# set or derives it from its own on-disk location, and refuses when it can do
# neither. Echoes the resolved path on success; returns non-zero (no output) on
# failure so the caller controls WHEN to die (preflight, not five steps in).
resolve_kirocrew_src() {
  # 1. Explicit wins: --kirocrew-src / $KIROCREW_SRC. Validate it here so a wrong
  #    path is caught by the driver at preflight, not by build_image.sh at step 4.
  #    -e not -d: a git worktree checkout has a .git FILE (a gitdir pointer), not a
  #    directory, and git operates on it exactly the same.
  if [ -n "${KIROCREW_SRC:-}" ]; then
    [ -e "$KIROCREW_SRC/.git" ] || return 1
    echo "$KIROCREW_SRC"; return 0
  fi
  # 2. Derive from the driver's own location: if this script is running from inside
  #    a source checkout (a developer or maintainer running it in-tree), the repo
  #    root is an ancestor of CREW_ROOT. An INSTALLED copy has no repo above it, by
  #    design -- so this yields nothing there and we fall through to the refusal.
  local d="$CREW_ROOT"
  while [ "$d" != "/" ] && [ -n "$d" ]; do
    if [ -e "$d/.git" ]; then echo "$d"; return 0; fi
    d="$(dirname "$d")"
  done
  return 1
}

# Build and push the BASE image by delegating to the maintainer builder. Called only
# when no base exists, so the owner never runs a second tool to satisfy a resolver.
build_base_image() {
  if [ "$DRY_RUN" -eq 1 ]; then
    : > "$WORK/base-built"
    ok "dry-run: would build the base image via scripts/build_image.sh"
    return 0
  fi
  local builder="$CREW_ROOT/scripts/build_image.sh"
  [ -x "$builder" ] || die "base builder missing or not executable: $builder"
  # build_image.sh requires the Kiro Crew source checkout. Resolve it now; a failure
  # here should not normally be reachable because preflight already refused a deploy
  # that would need a build without a resolvable source, but this is the last guard
  # before the builder runs and it names what to set rather than letting the builder
  # abort with its own message five steps into a deploy that has created stacks.
  local src
  src="$(resolve_kirocrew_src)" || die \
    "cannot build the base image: no Kiro Crew source checkout to build the wheel from.
   Set --kirocrew-src <path> (or export KIROCREW_SRC) to a Kiro Crew git checkout,
   or pass --base <repo>@sha256:<hex> to reuse an existing base image."
  # The driver is the only side holding the profile, so it authenticates before
  # delegating. build_image.sh takes no --profile by contract.
  ecr_login
  "$builder" --arch "$CFN_ARCH" --repo "$REPO_URI" --kirocrew-src "$src" \
    || die "base image build failed (scripts/build_image.sh). Its output is above."
  ok "base image built and the 'smc-base-$CFN_ARCH' tag published"
}

# Allocate this crew's ALB listener-rule priority.
#
# The design is many crews on ONE listener, each owning RoutePrefix/*, so the priority
# must be unique per crew. The template declares the parameter with Default: 100 and a
# description saying the driver must supply a unique value -- and the driver never
# supplied one, so the FIRST crew took 100 and every crew after it failed with
# "Priority '100' is currently in use". The parameter having a default is also why the
# bidirectional seam guard did not catch it: a defaulted parameter is not a REQUIRED
# one, so "all required parameters are passed" was true and useless here.
#
# Allocation is by inspection rather than by hashing the crew name: a hash collision
# between two crews would present as this same error with no way to resolve it, and
# the driver is already talking to this listener.
#
# Re-deploying the SAME crew must reuse its own priority. Picking a fresh one each
# time would leave the old rule in place, and two rules matching the same path pattern
# means the lower-numbered one silently keeps winning after a route change.
allocate_rule_priority() {
  if [ "$DRY_RUN" -eq 1 ]; then
    RULE_PRIORITY="${SMC_FIX_RULE_PRIORITY:-100}"
    note "listener rule priority: $RULE_PRIORITY (dry-run fixture)"
    return 0
  fi

  local rules mine
  rules="$(aws_ elbv2 describe-rules --listener-arn "$AlbListenerArn" \
             --query 'Rules[].[Priority,Conditions[0].Values[0]]' --output text 2>/dev/null)" || rules=""

  # Does a rule for THIS crew's path pattern already exist? Then it owns that priority.
  mine="$(printf '%s\n' "$rules" | awk -v pat="$ROUTE_PREFIX/*" '$2 == pat {print $1; exit}')"
  if [ -n "$mine" ] && [ "$mine" != "default" ]; then
    RULE_PRIORITY="$mine"
    note "listener rule priority: $RULE_PRIORITY (reusing this crew's existing rule)"
    return 0
  fi

  # Otherwise take the lowest free slot from 100 up. 'default' is the listener's
  # default action, which has no numeric priority and must not be parsed as one.
  local taken p
  taken="$(printf '%s\n' "$rules" | awk '$1 ~ /^[0-9]+$/ {print $1}' | sort -n | tr '\n' ' ')"
  p=100
  while printf '%s' " $taken " | grep -q " $p "; do
    p=$((p + 1))
    [ "$p" -le 50000 ] || die "no free ALB listener-rule priority below 50000 on $AlbListenerArn"
  done
  RULE_PRIORITY="$p"
  note "listener rule priority: $RULE_PRIORITY (allocated; taken: ${taken:-none})"
}

resolve_base_ref() {
  if [ -n "${BASE_OPT:-}" ]; then
    BASE_REF="$BASE_OPT"
    note "base image: $BASE_REF (from --base)"
  else
    local dg
    dg="$(base_tag_digest)"
    if [ -z "$dg" ] || [ "$dg" = "None" ]; then
      # No base in the registry. Build one rather than stopping to tell the owner to
      # run a maintainer script: the product is ONE command from the owner's machine,
      # and a first-time owner has no base image by definition. Sending them to a
      # second tool to satisfy a resolver is the deploy failing to do its job.
      note "no base image in this account yet; building one now (one-time, several minutes)"
      build_base_image
      dg="$(base_tag_digest)"
      [ -n "$dg" ] && [ "$dg" != "None" ] || die \
        "built the base image but the 'smc-base-$CFN_ARCH' tag still does not resolve.
   Pass --base <repo>@sha256:<hex> using the digest scripts/build_image.sh printed."
    fi
    BASE_REF="${REPO_URI}@${dg}"
    note "base image: $BASE_REF (resolved from the ECR 'smc-base-$CFN_ARCH' tag)"
  fi
  case "$BASE_REF" in *@sha256:*) : ;; *) die "resolved base is not digest-pinned: '$BASE_REF'" ;; esac
}

# Build + push the CREW IMAGE. Delegated to the crew-image track's
# scripts/build_crew_image.sh per PACKAGING-CONTRACT track ownership. See PORT
# NOTES for the interface the Driver requires of that script.
build_crew_image_step() {
  resolve_base_ref     # sets BASE_REF (digest-pinned)
  if [ "$DRY_RUN" -eq 1 ]; then
    # Fixture in the crew-image track's smc-crew-image/v1 shape, and specifically
    # the shape build_crew_image.sh returns on a SUCCESSFUL push: image_uri
    # digest-pinned, pushed=true, dry_run=false. The driver never passes --dry-run
    # to the real script (a real deploy pushes), so the fixture must return a
    # pushed record, NOT the null-image record the script emits under its own
    # --dry-run. bundle_digest is READ FROM THE BUNDLE MANIFEST, exactly as the
    # real script does, so the continuity gate sees the real relationship rather
    # than a value invented to make it pass.
    local mdg; mdg="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["digest"])' "$BUNDLE_DIR/manifest.json" 2>/dev/null || echo "")"
    local a="${SMC_FIX_IMAGE_ARCH:-$CFN_ARCH}"
    local img="${SMC_FIX_IMAGE_URI:-${REPO_URI}@sha256:$(printf %064d 0)}"
    cat > "$WORK/crew-image.json" <<JSON
{"schema":"smc-crew-image/v1",
 "built_at":"1970-01-01T00:00:00Z",
 "image_uri":"$img",
 "repo_digest":"${img##*@}",
 "base_digest":"${BASE_REF##*@}",
 "base_ref":"$BASE_REF",
 "architecture":"$a",
 "docker_platform":"linux/$(docker_of_cfn "$a" 2>/dev/null || echo unknown)",
 "crew_name":"${SMC_FIX_IMAGE_CREW:-$CREW}",
 "bundle_digest":"${SMC_FIX_IMAGE_BUNDLE_DIGEST:-$mdg}",
 "bundle_version":"1",
 "push_tag":"${REPO_URI}:crew-$CREW-dryrun",
 "pushed":true,"dry_run":false}
JSON
    note "dry-run: skipped build/push; wrote a fixture smc-crew-image/v1 record (pushed=true)"
  else
    local script="$CREW_ROOT/scripts/build_crew_image.sh"
    [ -x "$script" ] || die "crew image build script not found or not executable: $script (owned by the crew-image track)"
    # Authenticate to ECR HERE, not in the build script: build_crew_image.sh takes
    # no --profile/--region and cannot log in; the driver is the only side that
    # knows the credentials.
    ecr_login
    # CONTRACT (PACKAGING-CONTRACT T2 / scripts/README.md): --base (digest-pinned),
    # --bundle (T1's output dir), --repo (RepositoryUri), --crew, --arch in
    # CloudFormation spelling (so ONE string travels flag -> CpuArchitecture param
    # -> task def). The machine contract is a JSON file named on a stdout line as
    # SMC_CREW_IMAGE_JSON=<path>; no stdout line is treated as data.
    local raw marker jpath
    raw="$("$script" --base "$BASE_REF" --bundle "$BUNDLE_DIR" --repo "$REPO_URI" \
            --crew "$CREW" --arch "$CFN_ARCH")" || die "crew image build/push failed"
    marker="$(printf '%s\n' "$raw" | grep '^SMC_CREW_IMAGE_JSON=' | tail -n 1)"
    [ -n "$marker" ] || die "build_crew_image.sh emitted no SMC_CREW_IMAGE_JSON= marker; cannot locate the build record"
    jpath="${marker#SMC_CREW_IMAGE_JSON=}"
    [ -f "$jpath" ] || die "SMC_CREW_IMAGE_JSON points at a missing file: $jpath"
    cp "$jpath" "$WORK/crew-image.json"    # persist so verify can re-prove on --from 8
  fi

  # Unified parse + validate (both dry and real land here). Everything the driver
  # needs is a JSON field; no stdout line is ever treated as data.
  local img_uri arch bundle_digest crew_name
  img_uri="$(crew_image_json_field image_uri)"
  arch="$(crew_image_json_field architecture)"
  bundle_digest="$(crew_image_json_field bundle_digest)"
  crew_name="$(crew_image_json_field crew_name)"
  case "$img_uri" in
    *@sha256:*) : ;;
    *) die "the crew-image build record carries no digest-pinned image_uri (got: '$img_uri'). Nothing was pushed? --repo '$REPO_URI', --base '$BASE_REF'. ImageUri must never be a tag." ;;
  esac
  # The image must NAME the crew we asked for and must have BAKED IN the bundle this
  # run produced. Fail here as well (cheaper than a failed service), but the
  # authoritative proof runs at verify from persisted state regardless of resume.
  [ "$crew_name" = "$CREW" ] || die "crew image reports crew_name='$crew_name' but this deploy is for '$CREW'"
  judge_bundle_digest_match "$BUNDLE_DIGEST" "$bundle_digest" \
    || die "the crew image was not built from the bundle this run produced (see above)"
  echo "$img_uri"       > "$WORK/image-uri"
  echo "$arch"          > "$WORK/image-arch"            # CloudFormation spelling (X86_64/ARM64)
  echo ""               > "$WORK/image-machine"         # thin layer execs nothing; exec-ability is a base-image + runtime property
  echo "$bundle_digest" > "$WORK/image-bundle-digest"
  note "image_uri $img_uri"
  note "arch $arch  base $BASE_REF"
  note "bundle baked in: $bundle_digest (matches packaging.build)"
}

# Read one field from the persisted crew-image build record, normalising JSON
# null/bool to ""/"true"/"false" for shell string comparison.
crew_image_json_field() { # FIELD
  python3 -c 'import json,sys
v=json.load(open(sys.argv[1])).get(sys.argv[2])
print("" if v is None else ("true" if v is True else ("false" if v is False else v)))' \
    "$WORK/crew-image.json" "$1" 2>/dev/null
}

# Every base output the driver reads. ONE list: it drives both the resolution and
# the assertion, because two hand-maintained lists drift. They already did -- three
# outputs were added to the resolution and not to the assertion, so a base stack
# predating them resolved AlbDnsName to "" and the empty value reached API Gateway
# as the Uri http:///c/<crew>/{proxy}, which fails five minutes later as "Invalid
# HTTP endpoint specified for URI" rather than here as a missing output.
BASE_OUTPUTS=(
  VpcId PrivateSubnetIds ClusterArn
  AlbListenerArn AlbArn AlbDnsName AlbSecurityGroupId
  LogGroupName RestApiId RestApiRootResourceId VpcLinkId
  CrewsResourceId BucketName ExecutionRoleArn RepositoryUri
)

# Resolve + assert every base output. Sets globals named exactly as the outputs.
resolve_base_outputs() {
  local k v
  for k in "${BASE_OUTPUTS[@]}"; do
    v="$(out "$BASE_STACK" "$k")"
    case "$v" in ""|None)
      die "base stack $BASE_STACK has no output $k.
   The base stack predates this driver. Re-run including step 2 so it is updated:
     $0 --profile $PROFILE --region $REGION --crew $CREW --from 2 --bundle <dir>
   Step 2 is a changeset on the existing stack; it does not rebuild the VPC." ;;
    esac
    declare -g "$k=$v"
  done
  # Two call-site names predate this loop and are kept as aliases rather than
  # renamed across the script.
  BUCKET_NAME="$BucketName"
  REPO_URI="$RepositoryUri"
}

# Resolve + assert crew outputs.
resolve_crew_outputs() {
  ServiceArn="$(out "$CREW_STACK" ServiceArn)"
  TaskDefinitionArn="$(out "$CREW_STACK" TaskDefinitionArn)"
  TargetGroupArn="$(out "$CREW_STACK" TargetGroupArn)"
  ControlBaseUrl="$(out "$CREW_STACK" ControlBaseUrl)"
  local k
  for k in ServiceArn TaskDefinitionArn ControlBaseUrl; do
    judge_output_present "$k" "${!k}" || die "crew stack is missing output $k"
  done
}

# Stage name. CONTRACT exposes no StageName base output, so derive it from the
# ControlBaseUrl invoke path (https://{id}.execute-api.{region}.amazonaws.com/{stage}/...).
# --stage overrides; falls back to prod. (Reported as a gap; see PORT NOTES.)
stage_name() {
  if [ -n "${STAGE_OPT:-}" ]; then printf '%s' "$STAGE_OPT"; return; fi
  local s
  s="$(printf '%s' "${ControlBaseUrl:-}" | sed -n 's#https\{0,1\}://[^/]*/\([^/]*\)/.*#\1#p')"
  [ -n "$s" ] && printf '%s' "$s" || printf 'prod'
}

# SigV4 GET through the control API; prints the http code and writes the body to
# $WORK/resp. Under --dry-run returns a fixture code + body.
sig() { # PATH -> http code
  if [ "$DRY_RUN" -eq 1 ]; then
    # These are the codes the DEPLOYED stack returns, checked against it rather than
    # against the expectations the gates used to hold. A fixture that encodes the
    # intended behaviour instead of the real one makes the suite pass while the
    # deployment fails, which is how the /preflight gate survived to reach a real
    # account: the fixture answered 200 for a route that does not exist.
    case "$1" in
      /health)   printf '{"status":"ok"}' > "$WORK/resp"
                 echo "${SMC_FIX_HEALTH_CODE:-200}" ;;
      /sessions) printf '{"detail":"control access denied","code":"control_forbidden"}' > "$WORK/resp"
                 echo "${SMC_FIX_CONTROL_CODE:-403}" ;;
      *) printf '{}' > "$WORK/resp"; echo 200 ;;
    esac
    return 0
  fi
  eval "$(aws --profile "$PROFILE" --region "$REGION" configure export-credentials --format env)"
  curl -sS --aws-sigv4 "aws:amz:$REGION:execute-api" \
    -K "$(_curl_auth_file)" \
    ${AWS_SESSION_TOKEN:+-H "x-amz-security-token: $AWS_SESSION_TOKEN"} \
    -o "$WORK/resp" -w '%{http_code}' "${ControlBaseUrl}$1"
}

# curl's credentials go in a MODE 600 FILE, never on the command line.
#
# Passing them with `--user` puts the owner's secret and session token in argv.
# Modern curl overwrites that argv with asterisks -- verified locally on 8.17, where
# `/proc/<pid>/cmdline` showed the value replaced -- but the overwrite happens AFTER
# exec, so there is a window in which any user on the box can read the real value,
# and the mitigation is a curl-version detail rather than something this script
# controls. A config file has no such window.
#
# Truncated on every write via a subshell umask, so a rotated credential does not
# linger and the file is never briefly world-readable.
_curl_auth_file() {
  local f="$WORK/.curlrc-auth"
  ( umask 077; : > "$f" )
  printf 'user = "%s:%s"\n' "${AWS_ACCESS_KEY_ID}" "${AWS_SECRET_ACCESS_KEY}" >> "$f"
  printf '%s' "$f"
}

# SigV4 GET with a forged header (injection probe). Prints the http code and, like
# sig(), writes the body to $WORK/resp. The body used to go to /dev/null, which left
# whose_response() reading the PREVIOUS call's body and naming the wrong layer -- a
# diagnostic that lies is worse than one that says nothing.
#
# It also took a PATH argument and then ignored it, hardcoding /preflight -- a route
# the container does not implement. The gate passed anyway, because the front process
# checks control authorization BEFORE resolving the route, so a forged secret is
# refused with 403 whether or not the path exists. Passing for a reason the probe did
# not intend is not passing: reverse those two steps in the container and the probe
# would collect 404s and prove nothing while still reporting green.
sig_forged() { # HEADER PATH -> http code
  if [ "$DRY_RUN" -eq 1 ]; then
    # Default 403: under the design that was actually built, a REJECTED forged secret
    # is the pass. This default was 200, which was the pass under the gateway-injecting
    # design that does not exist.
    local codes; read -r -a codes <<<"${SMC_FIX_INJECT_CODES:-403 403 403}"
    local c="${codes[${SMC_INJECT_IDX:-0}]:-403}"
    # Body consistent with the code, so the fixture exercises whose_response too.
    case "$c" in
      404) printf '{"error":"no such crew"}' > "$WORK/resp" ;;
      403) printf '{"detail":"control access denied","code":"control_forbidden"}' > "$WORK/resp" ;;
      *)   printf '{}' > "$WORK/resp" ;;
    esac
    echo "$c"; return 0
  fi
  eval "$(aws --profile "$PROFILE" --region "$REGION" configure export-credentials --format env)"
  curl -sS --aws-sigv4 "aws:amz:$REGION:execute-api" \
    -K "$(_curl_auth_file)" \
    ${AWS_SESSION_TOKEN:+-H "x-amz-security-token: $AWS_SESSION_TOKEN"} \
    -H "$1: forged-by-client" \
    -o "$WORK/resp" -w '%{http_code}' "${ControlBaseUrl}$2"
}

# ONE REAL TURN on the customer path. Prints the http code, body in $WORK/resp.
# This is the strongest gate in the suite: it is the request a customer makes, and
# nothing short of the whole deployment working can answer it.
turn_probe() { # [MESSAGE] [SLOT_ID] -> http code
  local msg="${1:-reply with the single word: ok}"
  local slot="${2:-smc-verify}"
  if [ "$DRY_RUN" -eq 1 ]; then
    if [ -n "${SMC_FIX_TURN_BODY:-}" ]; then
      printf '%s' "$SMC_FIX_TURN_BODY" > "$WORK/resp"
    else
      # Body consistent with the code. A fixture whose body contradicts its status is
      # how a diagnostic learns to name the wrong thing. The fingerprint challenge
      # gets the fingerprint back, because that is what a CORRECT deployment does --
      # a fixture that answered "ok" here would make the gate pass for the wrong
      # reason and prove nothing, which is the mistake that reached a real account.
      case "${SMC_FIX_TURN_CODE:-200}" in
        200)
          local content="ok"
          case "$msg" in
            SMC-VERIFY-*) content="SMC-FINGERPRINT ${SMC_FIX_FINGERPRINT_REPLY:-$(cat "$WORK/bundle-fingerprint" 2>/dev/null)}" ;;
          esac
          cat > "$WORK/resp" <<JSON
{"id":"chatcmpl-dryrun","object":"chat.completion","model":"$CREW",
 "choices":[{"index":0,"message":{"role":"assistant","content":"$content"},"finish_reason":"stop"}]}
JSON
          ;;
        403) printf '{"detail":"control access denied","code":"control_forbidden"}' > "$WORK/resp" ;;
        000) : > "$WORK/resp" ;;
        *)   printf '{"detail":"backend refused the turn","code":"kiro_prerequisite_required"}' > "$WORK/resp" ;;
      esac
    fi
    echo "${SMC_FIX_TURN_CODE:-200}"; return 0
  fi
  eval "$(aws --profile "$PROFILE" --region "$REGION" configure export-credentials --format env)"
  curl -sS --aws-sigv4 "aws:amz:$REGION:execute-api" \
    -K "$(_curl_auth_file)" \
    ${AWS_SESSION_TOKEN:+-H "x-amz-security-token: $AWS_SESSION_TOKEN"} \
    -H 'content-type: application/json' \
    --max-time "${SMC_TURN_TIMEOUT:-180}" \
    -d "$(printf '{"model":"%s","id":"%s","stream":false,"messages":[{"role":"user","content":"%s"}]}' \
          "$CREW" "$slot" "$msg")" \
    -o "$WORK/resp" -w '%{http_code}' \
    "${ControlBaseUrl}/v1/chat/completions"
}

# One line describing what came back, so the pass is legible as evidence.
turn_summary() {
  python3 -c '
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print("unreadable body"); raise SystemExit(0)
ch = (d.get("choices") or [{}])[0]
msg = (ch.get("message") or {}).get("content")
print("content=%r finish_reason=%s model=%s" % (msg, ch.get("finish_reason"), d.get("model")))
' "$WORK/resp" 2>/dev/null || echo "could not summarise the body"
}

# read a value from a JSON body fetched into $WORK/resp
resp_json_field() { # FIELD
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2]))' "$WORK/resp" "$1" 2>/dev/null || echo parse-error
}

# describe-task-definition helpers (need --include TAGS is not required here)
task_role_arn() {
  aws_ ecs describe-task-definition --task-definition "$TaskDefinitionArn" \
    --query 'taskDefinition.taskRoleArn' --output text 2>/dev/null
}
task_def_arch() {
  aws_ ecs describe-task-definition --task-definition "$TaskDefinitionArn" \
    --query 'taskDefinition.runtimePlatform.cpuArchitecture' --output text 2>/dev/null
}
# The image the deployed task definition actually references. This is what the
# image-digest gate compares against the image build_crew_image.sh reported.
task_def_image() {
  aws_ ecs describe-task-definition --task-definition "$TaskDefinitionArn" \
    --query 'taskDefinition.containerDefinitions[0].image' --output text 2>/dev/null
}
task_env_value() { # NAME
  aws_ ecs describe-task-definition --task-definition "$TaskDefinitionArn" \
    --query "taskDefinition.containerDefinitions[0].environment[?name=='$1'].value | [0]" \
    --output text 2>/dev/null
}
iam_decision() { # ACTION RESOURCE_ARN
  aws_ iam simulate-principal-policy --policy-source-arn "$TASK_ROLE" \
    --action-names "$1" --resource-arns "$2" \
    --query 'EvaluationResults[0].EvalDecision' --output text 2>/dev/null || echo error
}

# ---------------------------------------------------------------------------
# VERIFY -- gates, not reports. Every gate can fail; the banner comes AFTER the
# gates. Every correctness gate runs from persisted/live state regardless of the
# resume point (a --from 8 run re-proves the deployment).
# ---------------------------------------------------------------------------
smc_verify() {
  step 8 "verify -- gates that can fail, run regardless of resume point"
  local FAILS=0
  bump() { FAILS=$((FAILS + 1)); }

  # Reload persisted scalars (verify may be the only step running).
  CFN_ARCH="$(cat "$WORK/cfn-arch" 2>/dev/null || echo "$CFN_ARCH")"
  IMAGE_ARCH="$(cat "$WORK/image-arch" 2>/dev/null || echo "")"
  IMAGE_MACHINE="$(cat "$WORK/image-machine" 2>/dev/null || echo "")"
  IMAGE_URI="$(cat "$WORK/image-uri" 2>/dev/null || echo "")"
  IMAGE_BUNDLE_DIGEST="$(cat "$WORK/image-bundle-digest" 2>/dev/null || echo "")"
  BUNDLE_DIGEST="$(cat "$WORK/bundle-digest" 2>/dev/null || echo "${BUNDLE_DIGEST:-}")"
  [ -n "${BUCKET_NAME:-}" ] || resolve_base_outputs
  [ -n "${ControlBaseUrl:-}" ] || resolve_crew_outputs

  # Gate: service reached steady state (also the ALB /health direct check).
  local svc; svc="${ServiceArn##*/}"
  note "waiting for the service to reach steady state (the warm pool must boot first)"
  if aws_ ecs wait services-stable --cluster "$ClusterArn" --services "$svc"; then
    ok "gate PASS  service stable -- the ALB's direct /health check on :8080 is green"
  else
    warn "gate FAIL  service never stabilised. Container logs:"
    note "  aws --profile $PROFILE --region $REGION logs tail $LogGroupName --since 10m --filter-pattern preflight"
    bump
  fi

  # Gate: architecture agrees (TRAP 1). Runs HERE regardless of resume, against
  # the DEPLOYED task definition and the build record -- catches a template that
  # ignored the parameter AND an image/task mismatch.
  local tda; tda="$(task_def_arch)"
  case "$(judge_arch "$CFN_ARCH" "$tda" "$IMAGE_ARCH" "$IMAGE_MACHINE"; echo $?)" in
    0) ok "gate PASS  arch fully proven -- intended $CFN_ARCH, task def $tda, image $IMAGE_ARCH, ran-as $IMAGE_MACHINE" ;;
    2) ok "gate PASS  arch: intended/task/image all agree ($CFN_ARCH); exec proof deferred (crew image is a thin layer, platform_machine empty)"
       note "         a crew image execs nothing at build time, so it reports no platform_machine; the base image's exec-ability is a base property, and the task host plus the real turn are where it is proven on this arch" ;;
    *) warn "gate FAIL  architecture mismatch (see above)"; bump ;;
  esac

  # Gate: the running task definition serves the EXACT image build_crew_image.sh
  # reported, built from the bundle packaging.build produced. THIS is the gate
  # whose absence let the earlier version report success while serving a default
  # agent: with the crew baked into the image, this proves the crew being SERVED is
  # the crew that was PACKAGED. Read from persisted/live state, so it runs
  # regardless of the resume point.
  local deployed_image; deployed_image="$(task_def_image)"
  if judge_image_digest "$deployed_image" "$IMAGE_URI"; then
    ok "gate PASS  the running task definition serves the image build_crew_image.sh reported ($IMAGE_URI)"
  else
    warn "gate FAIL  the served image is not the reported image -- the crew served may not be the crew packaged (see above)"; bump
  fi
  if judge_bundle_digest_match "$BUNDLE_DIGEST" "$IMAGE_BUNDLE_DIGEST"; then
    ok "gate PASS  that image baked in the bundle packaging.build produced ($IMAGE_BUNDLE_DIGEST)"
  else
    warn "gate FAIL  the served image does not contain the bundle this run curated (see above)"; bump
  fi

  # Gate: container env traps (KIROCREW_BIND, SMC_CONFIG_DIR==SMC_DATA_HOME).
  local bind cfg home
  bind="$(task_env_value KIROCREW_BIND)"; cfg="$(task_env_value SMC_CONFIG_DIR)"; home="$(task_env_value SMC_DATA_HOME)"
  if judge_env_equals KIROCREW_BIND "$bind" "127.0.0.1"; then
    ok "gate PASS  KIROCREW_BIND=127.0.0.1 -- backend is not on the network (the trust boundary holds)"
  else
    warn "gate FAIL  KIROCREW_BIND is not 127.0.0.1 -- the backend is exposed on the network"; bump
  fi
  if judge_env_equals SMC_CONFIG_DIR "$cfg" "$home"; then
    ok "gate PASS  SMC_CONFIG_DIR == SMC_DATA_HOME ($cfg) -- session_map.json / open_slots.json get backed up"
  else
    warn "gate FAIL  SMC_CONFIG_DIR ($cfg) != SMC_DATA_HOME ($home) -- backup would silently miss the resume files"; bump
  fi

  # Gate: the task did not restore a transcript it was never asked to serve.
  # Read from the container's own boot log, because this is a fact about what the
  # task PUT ON DISK and nothing outside the task can observe that. The line is an
  # interface: container/backup/restore.py SUMMARY_TOKEN.
  local restore_summary
  restore_summary="$(restore_summary_line)"
  if judge_no_transcripts_restored "$restore_summary" "$MEMORY"; then
    if [ "$MEMORY" = chatbot ]; then
      ok "gate PASS  nothing is persisted: restore reported state=disabled, so no conversation outlives this task"
    else
      ok "gate PASS  the task restored NO transcript ($(printf '%s' "$restore_summary" | sed -n 's/.*\(transcripts_restored=[0-9]* transcripts_available=[0-9]*\).*/\1/p')) -- it holds only what it serves"
    fi
  else
    if [ "$MEMORY" = chatbot ]; then
      warn "gate FAIL  this crew persists conversations despite being deployed as a chatbot (see above)"; bump
    else
      warn "gate FAIL  the task's disk is not ephemeral: it holds conversations it never served (see above)"; bump
    fi
  fi

  # Gate: /health through the CONTROL api. The owner is SigV4-authorised, so this
  # must be SERVED (200). See judge_health_through_control for why the earlier
  # "must be 403" assertion described an intention no mechanism enforced.
  local code; code="$(sig /health)"
  if judge_health_through_control "$code"; then
    ok "gate PASS  the owner reaches the crew through the control api (/health 200, IAM is the outer boundary)"
  else
    warn "gate FAIL  /health through the control api (code $code) -- answered by $(whose_response)"; bump
  fi

  # Gate: the enforced half of the boundary. A CONTROL route carrying no control
  # secret must be refused by the container, 403, not served and not merely absent.
  code="$(sig /sessions)"
  if judge_control_refused_without_secret "$code"; then
    ok "gate PASS  control route refused without the control secret (403) -- the container enforces it, not the api"
  else
    warn "gate FAIL  control route without the secret (code $code) -- answered by $(whose_response)"; bump
  fi

  # Gate: injection probe across 3 casings. 200 = gateway value won (SAFE);
  # anything else = client's forged header reached the container (FAIL). Assert,
  # do not print three numbers for a human to compare (point 4).
  local accepted=0 odd=0 idx=0 h c rc
  for h in X-Control-Secret x-control-secret X-CONTROL-SECRET; do
    SMC_INJECT_IDX="$idx" c="$(sig_forged "$h" /sessions)"
    # `judge ...; rc=$?` would EXIT here under set -e on any non-zero return, which
    # is every case this gate exists to report. The if/else form is the one that
    # survives its own findings.
    if judge_forged_secret_rejected "$c"; then rc=0; else rc=$?; fi
    case "$rc" in
      0) note "     $h -> $c  forged secret rejected (safe)" ;;
      1) warn "     $h -> $c  forged secret ACCEPTED -- this casing bypasses the comparison"
         accepted=$((accepted + 1)) ;;
      *) warn "     $h -> $c  unexpected code; proves nothing about the comparison"
         odd=$((odd + 1)) ;;
    esac
    idx=$((idx + 1))
  done
  if [ "$accepted" -gt 0 ]; then
    warn "gate FAIL  $accepted of 3 casings ACCEPTED a forged X-Control-Secret -- the control surface is open to any account principal"
    bump
  elif [ "$odd" -gt 0 ]; then
    warn "gate FAIL  forged-secret rejection UNPROVEN: $odd of 3 casings answered unexpectedly ($(whose_response))"
    bump
  else
    ok "gate PASS  a forged X-Control-Secret is rejected in all 3 casings (header lookup is case-insensitive)"
  fi

  # Gate: ONE REAL TURN (point 3). A present credential is not a working one.
  #
  # This used to read credential_valid from a /preflight control route. That route
  # does not exist and was never going to: the front process implements NO control
  # operations by design ("the owner's control plane is off-box"), so a control path
  # returns 404 even when the secret is correct. The gate tested an architecture the
  # container track had decided not to build, and both sides had written down their
  # reasoning without either noticing the other.
  #
  # The customer path proves more anyway. It is the exact request a customer makes,
  # it needs no control secret, and passing it exercises the whole chain: SigV4, the
  # VPC link, the listener rule, the prefix strip, the loopback backend, and a
  # kiro-cli worker actually spawning and answering.
  code="$(turn_probe)"
  if judge_real_turn_response "$code"; then
    ok "gate PASS  ONE REAL TURN answered on the customer path -- $(turn_summary)"
  else
    warn "gate FAIL  real turn on the customer path (code $code) -- answered by $(whose_response)"; bump
  fi

  # Gate: WHOSE prompt answered. The one thing no other gate reaches.
  local want_fp; want_fp="$(cat "$WORK/bundle-fingerprint" 2>/dev/null || echo "")"
  code="$(turn_probe "SMC-VERIFY-$CREW" "smc-verify-fingerprint")"
  if judge_prompt_fingerprint "$code" "$want_fp"; then
    ok "gate PASS  the crew SERVING this deployment is the crew that was PACKAGED (prompt fingerprint ${want_fp} returned)"
  else
    warn "gate FAIL  prompt fingerprint (code $code) -- see above"; bump
  fi

  # Gate: IAM isolation -- the NEGATIVE case (point 5). Four of six are denials,
  # including a lookalike crew name where a slash is the only separator. Uses
  # simulate-principal-policy (the task role's trust names ecs-tasks only, so it
  # cannot be assumed).
  TASK_ROLE="$(task_role_arn)"
  if [ -z "$TASK_ROLE" ] || [ "$TASK_ROLE" = "None" ]; then
    warn "gate FAIL  could not resolve the task role from $TaskDefinitionArn"; bump
  else
    note "task role $TASK_ROLE"
    local bkt="arn:aws:s3:::$BUCKET_NAME" iam_bad=0
    check_iam() { # LABEL EXPECT ACTION RESOURCE
      local d; d="$(iam_decision "$3" "$4")"
      if judge_iam "$2" "$d"; then
        note "     $(printf '%-40s' "$1") $d"
      else
        warn "     $(printf '%-40s' "$1") $d (expected $2)"; iam_bad=$((iam_bad + 1))
      fi
    }
    # A chatbot crew's role carries NO S3 statement, so its OWN prefix must be
    # denied too. That is a stronger claim than per-crew isolation and it is the one
    # worth asserting: a permission that does not exist cannot be reached by a
    # future code path, and the mode becomes auditable from the role rather than
    # from the container's environment. If these flip to allowed, the template
    # granted S3 to a crew that is not supposed to persist anything.
    if [ "$MEMORY" = chatbot ]; then
      check_iam "read own prefix (must be denied)"  denied  s3:GetObject "$bkt/crews/$CREW/transcripts/c1.json"
      check_iam "write own prefix (must be denied)" denied  s3:PutObject "$bkt/crews/$CREW/transcripts/c1.json"
      check_iam "list the bucket (must be denied)"  denied  s3:ListBucket "$bkt"
    else
      # own prefix (crews/<crew>/*) allowed; every other prefix denied. The bundle no
      # longer lives in S3 (it rides in the image), so these exercise the ONLY thing
      # this role now touches in the bucket: the crew's conversation-backup prefix.
      check_iam "read own backup"                  allowed s3:GetObject "$bkt/crews/$CREW/transcripts/c1.json"
      check_iam "write own transcript"             allowed s3:PutObject "$bkt/crews/$CREW/transcripts/c1.json"
      # Delete is granted NOWHERE, including in the crew's own prefix. Nothing in
      # the container can call it (the object store is put/get/list), and this
      # backend auto-approves every tool, so an ungranted destructive action is the
      # difference between a customer-driven turn reading a transcript and erasing
      # one. Asserted rather than commented, because the template is what a future
      # edit changes.
      check_iam "delete own transcript (must be denied)" denied s3:DeleteObject "$bkt/crews/$CREW/transcripts/c1.json"
    fi
    check_iam "delete another crew's transcript" denied  s3:DeleteObject "$bkt/crews/some-other-crew/transcripts/c1.json"
    check_iam "read another crew's transcript"   denied  s3:GetObject "$bkt/crews/some-other-crew/transcripts/c1.json"
    check_iam "write another crew's transcript"  denied  s3:PutObject "$bkt/crews/some-other-crew/transcripts/c1.json"
    # the sneaky one: a crew whose name STARTS WITH this crew's name. The grant
    # ends in /*, so the slash is what separates crews/<crew>/ from crews/<crew>2/.
    check_iam "read a lookalike crew name"       denied  s3:GetObject "$bkt/crews/${CREW}2/transcripts/c1.json"
    check_iam "read a lookalike as bare suffix"  denied  s3:GetObject "$bkt/crews/${CREW}-evil/transcripts/c1.json"

    # A bucket policy can widen an identity denial; a deny-only one cannot.
    local bp allows
    bp="$(aws_ s3api get-bucket-policy --bucket "$BUCKET_NAME" --query Policy --output text 2>&1 || true)"
    case "$bp" in
      *NoSuchBucketPolicy*|"")
        note "     bucket has no policy, so the identity policy is the whole story" ;;
      *)
        allows="$(printf '%s' "$bp" | python3 -c '
import json, sys
try:
    doc = json.load(sys.stdin)
except Exception:
    print("unparsable"); raise SystemExit
st = doc.get("Statement", [])
st = st if isinstance(st, list) else [st]
sids = [s.get("Sid", "(no sid)") for s in st if s.get("Effect") == "Allow"]
print(",".join(sids) if sids else "none")' 2>/dev/null || echo unparsable)"
        if judge_bucket_policy "$allows"; then
          note "     bucket policy cannot widen the denials above"
        else
          iam_bad=$((iam_bad + 1))
        fi ;;
    esac

    if [ "$iam_bad" -eq 0 ]; then
      if [ "$MEMORY" = chatbot ]; then
        ok "gate PASS  the task role holds NO S3 permission at all, its own prefix included"
      else
        ok "gate PASS  per-crew isolation holds: own prefix allowed, every other prefix (incl. lookalikes) denied"
      fi
    else
      warn "gate FAIL  $iam_bad IAM expectations failed -- one crew can reach another crew's bundle or conversations"
      bump
    fi
  fi

  # --- banner comes AFTER the gates, and only if they all passed -----------
  if [ "$FAILS" -ne 0 ]; then
    die "$FAILS verify gate(s) failed. The deployment is NOT proven. Nothing above this line is a success banner."
  fi
  printf '\n%s== deployed and verified ==%s\n' "$_C_GRN" "$_C_RST"
  note "control base   ${ControlBaseUrl}"
  note "image          ${IMAGE_URI}"
  note "bundle         $(cat "$WORK/bundle-digest" 2>/dev/null)"
  note "control secret $WORK/control-secret (0600)"
  note ""
  # This block is the line a reader quotes as evidence, so it must not outlive the
  # gates. It once claimed "/health 403", "/preflight 200" and "header injection
  # replaced in 3 casings" -- describing the architecture the gates had been rewritten
  # to stop asserting, directly above output showing otherwise. A summary that drifts
  # from what was checked is worse than no summary, because it is the part that
  # travels. It also once claimed "arch agrees end to end" and "nothing exports a real
  # crew into a bundle" -- both stale now. The headline it MUST carry today is the
  # image-digest proof (served image == reported image == built from the curated
  # bundle); if that gate changes, this line changes with it.
  note "proved: service stable; the running task definition serves the exact image"
  note "        build_crew_image.sh reported, and that image baked in the bundle"
  note "        packaging.build produced -- so the crew SERVED is the crew PACKAGED;"
  note "        arch agrees (intended==task==image); backend loopback-only; config==home;"
  note "        the owner reaches the crew through the control api; a control route"
  note "        without the secret is refused by the CONTAINER; a forged control secret"
  note "        is rejected in all 3 casings; ONE REAL TURN answered on the customer"
  note "        path with a real completion; the crew SERVING it returned the prompt"
  note "        fingerprint of the bundle that was PACKAGED; per-crew S3 isolation"
  note "        incl. lookalike-name"
  note "        denials."
  note ""
  note "not proved here: the BASE image's contents (the kiro-cli chat binary lives in"
  note "        the base, a maintainer artifact pinned by digest) are not re-verified by"
  note "        this driver -- the base builder verifies them and the real turn is their"
  note "        runtime symptom; and the bundle's content digest is recomputed and"
  note "        enforced by the container at boot (T3), not by this driver."
  if [ "$DRY_RUN" -eq 1 ]; then
    warn "this was a DRY RUN: gate outcomes are fixtures, not evidence from a real account."
  fi
}

# ===========================================================================
# PORT NOTES -- reported gaps where CONTRACT.md is silent (do not fix here; these
# belong to other tracks). The driver codes to the contract and states plainly
# what it had to assume.
#
#  1. Crew-image build interface. PACKAGING-CONTRACT (T2) + scripts/README.md. The
#     driver calls `build_crew_image.sh --base <repo>@sha256:<hex> --bundle <T1 out
#     dir> --repo <RepositoryUri base output> --crew <name> --arch <X86_64|ARM64>`,
#     reads the SMC_CREW_IMAGE_JSON=<path> marker line, and parses the
#     smc-crew-image/v1 JSON: image_uri -> ImageUri (must be @sha256, else nothing
#     pushed), architecture -> arch cross-check, bundle_digest -> continuity against
#     packaging.build's digest, crew_name -> must equal --crew. No stdout line is
#     treated as data. RepositoryUri is a base output the driver consumes but does
#     NOT pass to the crew stack. The bundle producer interface is T1's
#     `python -m packaging.build --crew <name> --out <dir> [--allow <path>]...`,
#     whose last line is SMC_BUNDLE_JSON=<path> naming {crew_name, bundle_dir,
#     digest, skill_count, mcp_servers, denied}.
#
#  2. ControlSecretValue. The source template took a plaintext ControlSecretValue
#     param (R3: a header mapping cannot dereference a secret). CONTRACT crew
#     params list ONLY ControlSecretArn. The driver passes only the ARN and assumes
#     crew.yaml resolves the header value with a CloudFormation dynamic reference
#     ({{resolve:secretsmanager:<arn>:SecretString}}). REPORT to Templates track:
#     confirm the integration gets its static header value that way, or add
#     ControlSecretValue back to the contract.
#
#  3. RulePriority. The source passed a RulePriority crew param for the ALB listener
#     rule. It is NOT in the CONTRACT crew params, so the driver does not pass it and
#     assumes the template derives a unique priority per crew. REPORT to Templates
#     track: confirm priority uniqueness across crews without a driver-supplied value.
#
#  4. StageName. CONTRACT has no StageName base output. The driver derives the stage
#     from the ControlBaseUrl invoke path and falls back to "prod" (--stage overrides).
#     REPORT to Templates track: consider adding a StageName base output.
#
#  5. Base parameters. CONTRACT lists base OUTPUTS but not base PARAMETERS. The driver
#     passes LogRetentionDays (the only retention parameter base.yaml declares; ruled
#     value $RETENTION, CHORUS R6). REPORT to Templates track: confirm that is the only
#     base parameter the driver must supply.
#
#  6. TaskCpu/TaskMemory have no CONTRACT defaults; the driver defaults 2048/4096 and
#     exposes --cpu/--memory. These are deployment sizing choices, not contract values.
#
#  7. kiro-cli presence is no longer gated by the driver. It was, reading
#     kiro_cli_present/kiro_cli_chat_present from the OLD smc-image-build/v1 record.
#     The crew rides in the image now and the driver reads smc-crew-image/v1, which
#     does NOT carry those fields -- the chat binary is a BASE-image property. So the
#     static gate is removed; the base builder (scripts/build_image.sh) verifies it,
#     and the real-turn gate catches its runtime symptom (a launcher-only image 502s
#     the first turn). REPORT to the crew-image / base tracks: if a static deploy-time
#     kiro-cli gate is still wanted, surface kiro_cli_present/kiro_cli_chat_present in
#     smc-crew-image/v1 (carried forward from the base build record) and this driver
#     will gate on it again.
#
#  8. deploy/CONTRACT.md is STALE and is held centrally (not owned by this track), so
#     it is reported, not fixed. Its "Crew parameters" list still names BundleObjectKey,
#     which PACKAGING-CONTRACT deletes and both crew.yaml and this driver have now
#     removed. check_param_seam.sh parses crew.yaml (not CONTRACT.md), so this does not
#     break the gate, but CONTRACT.md should be updated to match PACKAGING-CONTRACT.
# ===========================================================================

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  smc_main "$@"
fi
