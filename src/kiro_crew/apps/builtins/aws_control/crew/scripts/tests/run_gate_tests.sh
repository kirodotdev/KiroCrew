#!/usr/bin/env bash
#
# run_gate_tests.sh -- tests for deploy/smc-deploy.sh (Driver track).
#
# Zero dependency: pure bash, no bats/pytest. (The `bats` on this box is the
# Build Artifact Transform Service, not the Bash test framework, so the driver
# ships its own runner.)
#
#   ./deploy/tests/run_gate_tests.sh          # from the repo root, or anywhere
#
# Two layers:
#   1. Pure judge_* functions, sourced and called with fabricated inputs. This is
#      where "every gate can fail" is proven: each judge is shown passing on the
#      good value and FAILING on the bad one.
#   2. Dry-run integration: the whole script run with --dry-run, green once and
#      with flipped security fixtures once, asserting the failing run exits
#      non-zero and never prints the success banner.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(cd "$HERE/.." && pwd)"
SCRIPT="$DEPLOY_DIR/smc-deploy.sh"
FIXTURES="$HERE/fixtures"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PASS=0 FAIL=0
_grn=$'\033[32m'; _red=$'\033[31m'; _rst=$'\033[0m'
[ -t 1 ] || { _grn=''; _red=''; _rst=''; }

# ok NAME -- record a pass. fail NAME MSG -- record a failure.
pass() { PASS=$((PASS+1)); printf '  %sok%s   %s\n' "$_grn" "$_rst" "$1"; }
fail() { FAIL=$((FAIL+1)); printf '  %sFAIL%s %s\n     %s\n' "$_red" "$_rst" "$1" "${2:-}"; }

# expect_rc NAME EXPECTED_RC  -- run the pending function via $rc set by caller
# Helper assertions on a captured return code / output.
assert_rc() { # NAME EXPECTED ACTUAL
  if [ "$2" -eq "$3" ]; then pass "$1"; else fail "$1" "expected rc=$2, got rc=$3"; fi
}
assert_ok() {   # NAME ACTUAL_RC
  if [ "$2" -eq 0 ]; then pass "$1"; else fail "$1" "expected pass (rc=0), got rc=$2"; fi
}
assert_fail() { # NAME ACTUAL_RC
  if [ "$2" -ne 0 ]; then pass "$1"; else fail "$1" "expected failure (rc!=0), got rc=0"; fi
}
assert_eq() {   # NAME EXPECTED ACTUAL
  if [ "$2" = "$3" ]; then pass "$1"; else fail "$1" "expected '$2', got '$3'"; fi
}
assert_contains() { # NAME HAYSTACK NEEDLE
  case "$2" in *"$3"*) pass "$1" ;; *) fail "$1" "output did not contain: $3" ;; esac
}
assert_not_contains() { # NAME HAYSTACK NEEDLE
  case "$2" in *"$3"*) fail "$1" "output unexpectedly contained: $3" ;; *) pass "$1" ;; esac
}

# Source the driver for the pure-function tests. Sourcing does NOT run smc_main
# (guarded on BASH_SOURCE==$0) and does NOT flip this shell's options (the driver
# sets `set -euo pipefail` only inside smc_main).
# shellcheck source=/dev/null
source "$SCRIPT"

echo "== pure gate judges =="

# route prefix ---------------------------------------------------------------
judge_route_prefix "/c/frontdesk" >/dev/null 2>&1; assert_ok   "route prefix: leading slash passes" $?
judge_route_prefix "c/frontdesk"  >/dev/null 2>&1; assert_fail "route prefix: bare word refused"    $?

# arch spelling maps ---------------------------------------------------------
assert_eq "arch map: amd64->X86_64" "X86_64" "$(cfn_of_docker amd64)"
assert_eq "arch map: arm64->ARM64"  "ARM64"  "$(cfn_of_docker arm64)"
assert_eq "arch map: X86_64->amd64" "amd64"  "$(docker_of_cfn X86_64)"
assert_eq "arch map: ARM64->arm64"  "arm64"  "$(docker_of_cfn ARM64)"
assert_eq "machine map: x86_64->X86_64" "X86_64" "$(cfn_of_machine x86_64)"
assert_eq "machine map: aarch64->ARM64" "ARM64"  "$(cfn_of_machine aarch64)"
cfn_of_machine risc-v >/dev/null 2>&1; assert_fail "machine map: unknown rejected" $?
cfn_of_docker sparc >/dev/null 2>&1; assert_fail "arch map: unknown rejected, not defaulted" $?

# stack output presence ------------------------------------------------------
judge_output_present BucketName "smc-1-us-west-2" >/dev/null 2>&1; assert_ok   "output present: real value" $?
judge_output_present BucketName "" >/dev/null 2>&1;               assert_fail "output present: empty fails" $?
judge_output_present BucketName "None" >/dev/null 2>&1;           assert_fail "output present: 'None' fails" $?

# TRAP 1: architecture agreement (intended / taskdef / image_arch / platform_machine)
judge_arch X86_64 X86_64 X86_64 x86_64  >/dev/null 2>&1; assert_rc "arch gate: all agree + ran-as x86_64 -> fully proven" 0 $?
judge_arch ARM64 ARM64 ARM64 aarch64    >/dev/null 2>&1; assert_rc "arch gate: all agree + ran-as aarch64 -> fully proven" 0 $?
judge_arch X86_64 ARM64 X86_64 x86_64   >/dev/null 2>&1; assert_rc "arch gate: taskdef!=intended FAILS (template ignored param)" 1 $?
judge_arch X86_64 X86_64 ARM64 x86_64   >/dev/null 2>&1; assert_rc "arch gate: image!=intended FAILS" 1 $?
judge_arch X86_64 X86_64 X86_64 ""      >/dev/null 2>&1; assert_rc "arch gate: empty platform_machine -> code 2 (exec unproven, NOT a pass)" 2 $?
judge_arch X86_64 X86_64 "" x86_64      >/dev/null 2>&1; assert_rc "arch gate: image arch missing -> code 2 (declared leg unproven)" 2 $?
judge_arch X86_64 X86_64 X86_64 aarch64 >/dev/null 2>&1; assert_rc "arch gate: image RAN as aarch64 but task X86_64 -> FAILS" 1 $?

# NEW GATE: the running task definition serves the EXACT image build_crew_image.sh
# reported, built from the bundle packaging.build produced. Each guard is shown
# passing on the matching pair and FAILING on the mutation named in the label --
# a guard never seen fail is indistinguishable from one that cannot.
img="repo.example/smc@sha256:aaaa"
judge_image_digest "$img" "$img" >/dev/null 2>&1; assert_ok   "image digest: deployed==reported passes" $?
judge_image_digest "repo.example/smc@sha256:bbbb" "$img" >/dev/null 2>&1; assert_fail "image digest: deployed!=reported FAILS (served != packaged) [MUTATION: flip deployed digest]" $?
judge_image_digest "repo.example/smc:latest" "$img" >/dev/null 2>&1; assert_fail "image digest: deployed is a tag, not a digest, FAILS" $?
judge_image_digest "$img" "repo.example/smc:latest" >/dev/null 2>&1; assert_fail "image digest: reported not digest-pinned (nothing pushed) FAILS" $?

judge_bundle_digest_match "sha256:dead" "sha256:dead" >/dev/null 2>&1; assert_ok   "bundle continuity: packaged==image-baked passes" $?
judge_bundle_digest_match "sha256:dead" "sha256:beef" >/dev/null 2>&1; assert_fail "bundle continuity: image baked a DIFFERENT bundle FAILS [MUTATION: flip the image's bundle_digest]" $?
judge_bundle_digest_match "sha256:dead" "" >/dev/null 2>&1; assert_fail "bundle continuity: image reported no bundle digest FAILS" $?

# the ephemeral property: a task holds only the conversations it served --------
# Four refusals, because each says something different and a gate that collapsed
# them would report the wrong cause. The available>0 case is the subtle one: a
# bucket holding no transcript passes trivially, so without it the gate goes
# green on the one deployment it cannot actually speak about.
sum_ok="restore: SUMMARY state=ok transcripts_restored=0 transcripts_available=58 config_restored=2 restored_bytes=36 skipped=59 missing=none"
judge_no_transcripts_restored "$sum_ok" persistent >/dev/null 2>&1; assert_ok   "ephemeral: 0 restored out of 3 available passes" $?
judge_no_transcripts_restored "" persistent >/dev/null 2>&1; assert_fail "ephemeral: no SUMMARY line at all FAILS -- restore never ran, which is not the same as zero [MUTATION: treat an unreadable log as a pass]" $?
judge_no_transcripts_restored "${sum_ok/transcripts_restored=0/transcripts_restored=7}" persistent >/dev/null 2>&1; assert_fail "ephemeral: 7 transcripts restored FAILS -- the task holds conversations it never served [MUTATION: reintroduce the bulk restore]" $?
judge_no_transcripts_restored "${sum_ok/transcripts_available=58/transcripts_available=0}" persistent >/dev/null 2>&1; assert_fail "ephemeral: 0 out of 0 available FAILS -- nothing could have been left behind, so the zero proves nothing" $?
judge_no_transcripts_restored "restore: SUMMARY state=ok config_restored=2" persistent >/dev/null 2>&1; assert_fail "ephemeral: a SUMMARY line without the counters FAILS -- the field names are an interface [MUTATION: rename transcripts_restored]" $?
judge_no_transcripts_restored "${sum_ok/state=ok/state=partial}" persistent >/dev/null 2>&1; assert_fail "ephemeral: state=partial FAILS even with a perfect zero -- an authority file was missing, so resume is broken [MUTATION: read only the counters]" $?
judge_no_transcripts_restored "${sum_ok/state=ok/state=disabled}" persistent >/dev/null 2>&1; assert_fail "ephemeral: state=disabled FAILS -- nothing was restored because backup is off, which says nothing about a bulk restore" $?
judge_no_transcripts_restored "${sum_ok/state=ok/state=empty}" persistent >/dev/null 2>&1; assert_fail "ephemeral: state=empty FAILS -- the bucket held nothing to leave behind" $?
# A field read greedily returns its LAST occurrence, so a contradictory line
# parsed as a clean pass. Found by an adversarial cross-model review.
judge_no_transcripts_restored "restore: SUMMARY state=ok transcripts_restored=7 transcripts_restored=0 transcripts_available=58" persistent >/dev/null 2>&1; assert_fail "ephemeral: a repeated transcripts_restored FAILS -- greedy parsing took the last copy and called 7 restored a pass [MUTATION: sed -n s/.*field=\\(..\\).*/]" $?
judge_no_transcripts_restored "restore: SUMMARY state=partial state=ok transcripts_restored=0 transcripts_available=58" persistent >/dev/null 2>&1; assert_fail "ephemeral: a repeated state FAILS -- partial and ok on one line cannot both be believed" $?
judge_no_transcripts_restored "restore: SUMMARY state=ok transcripts_restored=00 transcripts_available=058" persistent >/dev/null 2>&1; assert_fail "ephemeral: padded numbers FAIL -- 058 asks bash for octal arithmetic and 00 is not the token 0" $?

# chatbot mode claims the OPPOSITE thing, so the same line must be judged
# differently. A gate that only knew persistent would refuse every chatbot deploy.
sum_bot="restore: SUMMARY state=disabled transcripts_restored=0 transcripts_available=0 config_restored=0 restored_bytes=0 skipped=0 missing=none"
judge_no_transcripts_restored "$sum_bot" chatbot >/dev/null 2>&1; assert_ok   "chatbot: state=disabled with nothing available passes" $?
judge_no_transcripts_restored "$sum_bot" persistent >/dev/null 2>&1; assert_fail "chatbot line judged as persistent FAILS -- disabled is not a proven restore" $?
judge_no_transcripts_restored "$sum_ok" chatbot >/dev/null 2>&1; assert_fail "chatbot: state=ok FAILS -- a bucket is configured, so the crew IS persisting [MUTATION: drop the mode argument]" $?
judge_no_transcripts_restored "${sum_bot/transcripts_available=0/transcripts_available=58}" chatbot >/dev/null 2>&1; assert_fail "chatbot: transcripts seen with backup disabled FAILS -- listing needs the grant this mode withholds" $?
judge_no_transcripts_restored "${sum_bot/transcripts_restored=0/transcripts_restored=3}" chatbot >/dev/null 2>&1; assert_fail "chatbot: any transcript restored FAILS, which is fatal in both modes" $?

# point 1/4 + TRAP 6/8: what the control API boundary really is ---------------
# The superseded judge required /health to answer 403 on the theory that API Gateway
# attaches the control secret and the container must refuse it on a customer path.
# The gateway attaches no secret (R3/trap #10), so a 200 never meant what the old
# assertion claimed, and the deployed stack returned 200. The one assertion is now
# two, each covering a property something actually enforces.
judge_health_through_control 200 >/dev/null 2>&1; assert_ok "health gate: 200 is the pass -- the owner is SigV4-authorised" $?
out="$(judge_health_through_control 403 2>&1)"; rc=$?; assert_fail "health gate: 403 fails -- the container is refusing a path it should serve" $rc
assert_contains "health gate: 403 points at the route prefix" "$out" "SMC_ROUTE_PREFIX"
judge_health_through_control 404 >/dev/null 2>&1; assert_fail "health gate: 404 fails" $?

judge_control_refused_without_secret 403 >/dev/null 2>&1; assert_ok "control refusal: 403 is the pass" $?
out="$(judge_control_refused_without_secret 200 2>&1)"; rc=$?; assert_fail "control refusal: 200 fails -- control surface open to any account principal" $rc
assert_contains "control refusal: names the container as the enforcer" "$out" "container is the only thing enforcing"
judge_control_refused_without_secret 404 >/dev/null 2>&1; assert_fail "control refusal: 404 fails -- absent is not refused" $?

# point 4: forged control secret, per casing ----------------------------------
# Polarity is the INVERSE of the superseded judge, because the mechanism it assumed
# (the gateway overwriting a client header) was deliberately not built.
judge_forged_secret_rejected 403 >/dev/null 2>&1; assert_rc "forged secret: 403 = rejected = SAFE" 0 $?
judge_forged_secret_rejected 200 >/dev/null 2>&1; assert_rc "forged secret: 200 = ACCEPTED = real bypass" 1 $?
judge_forged_secret_rejected 404 >/dev/null 2>&1; assert_rc "forged secret: 404 = inconclusive, not a bypass" 2 $?
judge_forged_secret_rejected 502 >/dev/null 2>&1; assert_rc "forged secret: 502 = inconclusive, not a bypass" 2 $?

# point 3: the one real turn --------------------------------------------------
# Judged on the RESPONSE now, not on a credential_valid field read from a control
# route that the container never implemented. Needs $WORK/resp, which the judge reads.
WORK="$(mktemp -d)"; export WORK
_body() { printf '%s' "$1" > "$WORK/resp"; }

_body '{"choices":[{"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}'
judge_real_turn_response 200 >/dev/null 2>&1; assert_rc "real turn: 200 with a real completion passes" 0 $?

judge_real_turn_response 502 >/dev/null 2>&1; assert_rc "real turn: 502 -> credential present but not working (code 2)" 2 $?
judge_real_turn_response 403 >/dev/null 2>&1; assert_rc "real turn: 403 -> classified as a control route (code 3)" 3 $?
judge_real_turn_response 000 >/dev/null 2>&1; assert_rc "real turn: no response -> timeout (code 4)" 4 $?
judge_real_turn_response 418 >/dev/null 2>&1; assert_rc "real turn: any other code (code 5)" 5 $?

# The assertions that make this gate stronger than "the service replied".
_body '{"choices":[{"message":{"role":"assistant","content":""},"finish_reason":"stop"}]}'
judge_real_turn_response 200 >/dev/null 2>&1; assert_rc "real turn: 200 with an EMPTY message does NOT pass" 6 $?

_body '{"choices":[{"message":{"role":"assistant","content":"ok"}}]}'
judge_real_turn_response 200 >/dev/null 2>&1; assert_rc "real turn: 200 with no finish_reason does NOT pass" 6 $?

_body '{"choices":[]}'
judge_real_turn_response 200 >/dev/null 2>&1; assert_rc "real turn: 200 with no choices does NOT pass" 6 $?

rm -rf "$WORK"; unset WORK

# point 5: IAM decisions -----------------------------------------------------
judge_iam allowed allowed      >/dev/null 2>&1; assert_ok   "iam gate: expected allow, allowed" $?
judge_iam denied  implicitDeny >/dev/null 2>&1; assert_ok   "iam gate: expected deny, implicitDeny" $?
judge_iam denied  explicitDeny >/dev/null 2>&1; assert_ok   "iam gate: expected deny, explicitDeny" $?
judge_iam denied  allowed      >/dev/null 2>&1; assert_fail "iam gate: denial came back allowed = cross-crew leak" $?
judge_iam allowed implicitDeny >/dev/null 2>&1; assert_fail "iam gate: allow came back denied" $?

# bucket policy widening -----------------------------------------------------
judge_bucket_policy ""            >/dev/null 2>&1; assert_ok   "bucket policy: no policy passes" $?
judge_bucket_policy none          >/dev/null 2>&1; assert_ok   "bucket policy: deny-only passes" $?
judge_bucket_policy "AllowEveryone" >/dev/null 2>&1; assert_fail "bucket policy: Allow statement fails" $?
judge_bucket_policy unparsable    >/dev/null 2>&1; assert_fail "bucket policy: unparsable fails (not ignored)" $?

# container env traps --------------------------------------------------------
judge_env_equals KIROCREW_BIND 127.0.0.1 127.0.0.1 >/dev/null 2>&1; assert_ok   "env gate: equal passes" $?
judge_env_equals KIROCREW_BIND 0.0.0.0   127.0.0.1 >/dev/null 2>&1; assert_fail "env gate: differ fails" $?

# stage name derivation ------------------------------------------------------
STAGE_OPT="" ControlBaseUrl="https://a.execute-api.us-west-2.amazonaws.com/prod/c/frontdesk"
assert_eq "stage name: derived from url" "prod" "$(stage_name)"
STAGE_OPT="staging"
assert_eq "stage name: --stage overrides" "staging" "$(stage_name)"
STAGE_OPT="" ControlBaseUrl="not-a-url"
assert_eq "stage name: falls back to prod" "prod" "$(stage_name)"
unset STAGE_OPT ControlBaseUrl

# bundle layout check (four-entry layout) ------------------------------------
bundle_layout_check "$FIXTURES/bundle-ok" frontdesk >/dev/null 2>&1; assert_ok "bundle layout: valid four-entry bundle passes" $?
bundle_layout_check "$FIXTURES/bundle-ok" other-crew >/dev/null 2>&1; assert_fail "bundle layout: manifest crew_name != requested crew fails" $?
out="$(bundle_layout_check "$FIXTURES/bundle-missing-mcp" frontdesk 2>&1)"; rc=$?; assert_fail "bundle layout: missing mcp.json fails" $rc
assert_contains "bundle layout: names the missing entry" "$out" "mcp.json"
bundle_layout_check "$FIXTURES/bundle-bad-version" frontdesk >/dev/null 2>&1; assert_fail "bundle layout: bundle_version!=1 fails" $?
out="$(bundle_layout_check "$FIXTURES/bundle-name-mismatch" frontdesk 2>&1)"; rc=$?; assert_fail "bundle layout: agent.name != crew_name fails" $rc
assert_contains "bundle layout: names the name mismatch" "$out" "!="

echo "== dry-run integration (whole script, no AWS) =="

# happy path -----------------------------------------------------------------
out="$(SMC_WORK="$TMP/ok" "$SCRIPT" --dry-run --crew frontdesk 2>&1)"; rc=$?
assert_ok "dry-run: happy path exits 0" $rc
assert_contains "dry-run: prints the banner"           "$out" "deployed and verified"
assert_contains "dry-run: proved a real turn"          "$out" "ONE REAL TURN answered on the customer path"
assert_contains "dry-run: proved served image == reported image" "$out" "serves the image build_crew_image.sh reported"
assert_contains "dry-run: proved the image baked in the curated bundle" "$out" "baked in the bundle packaging.build produced"
assert_contains "dry-run: arch exec proof is deferred (thin crew layer)" "$out" "exec proof deferred"

# flipped security fixtures --------------------------------------------------
# SMC_FIX_HEALTH_CODE=403 is now the FAILING direction (the pass is 200), and the
# forged-secret polarity is inverted with it: 200 means a casing was ACCEPTED.
out="$(SMC_WORK="$TMP/bad" SMC_FIX_HEALTH_CODE=403 SMC_FIX_INJECT_CODES="200 403 403" \
       SMC_FIX_TURN_CODE=502 SMC_FIX_IAM_OTHER=allowed \
       "$SCRIPT" --dry-run --crew frontdesk 2>&1)"; rc=$?
assert_fail "dry-run: flipped security gates fail the deploy" $rc
assert_contains     "dry-run: says NOT proven"        "$out" "NOT proven"
assert_not_contains "dry-run: prints NO success banner" "$out" "deployed and verified"

# the two memory flags are DIFFERENT knobs, in one invocation --------------------
# Regression guard for the collision that made this a review finding: --memory was
# declared twice, so the sizing branch was dead code and `--memory 8192` died with
# "takes chatbot or persistent". The mode flag is now --memory-mode; --memory is the
# task-memory SIZE. This asserts that in the SAME command they do different things --
# the assertion whose absence let the collision through, since the only values ever
# tested (chatbot/persistent) were exactly the ones that could not detect it.
DRYKEY_MEM='aws-kiro-abcdefghijklmnopqrstuvwxyz0123456789'
# --memory 8192 (SIZE) no longer dies, and sets the task memory rather than being
# parsed as a mode.
out="$(KIRO_API_KEY="$DRYKEY_MEM" SMC_WORK="$TMP/memsize" "$SCRIPT" --dry-run --crew frontdesk --memory 8192 2>&1)"; rc=$?
assert_ok       "dry-run: --memory 8192 is a valid SIZE, not a dead mode branch" $rc
assert_contains "dry-run: --memory 8192 sets the task memory size"               "$out" "mem=8192"
assert_not_contains "dry-run: --memory 8192 is NOT judged as a mode"             "$out" "takes chatbot or persistent"
# --memory-mode persistent (MODE) selects persistence: the per-crew S3 isolation
# gate is the one that only a persistent deploy reaches.
out="$(KIRO_API_KEY="$DRYKEY_MEM" SMC_WORK="$TMP/memmode" "$SCRIPT" --dry-run --crew frontdesk --memory-mode persistent --trust-domain single-principal 2>&1)"; rc=$?
assert_ok       "dry-run: --memory-mode persistent is accepted" $rc
assert_contains "dry-run: --memory-mode persistent selects persistence (per-crew S3 isolation gate)" "$out" "per-crew isolation holds"
# BOTH at once: mode=persistent AND size=8192, from the same invocation. If the two
# flags ever collapse back onto one name, one of these two assertions fails.
out="$(KIRO_API_KEY="$DRYKEY_MEM" SMC_WORK="$TMP/memboth" "$SCRIPT" --dry-run --crew frontdesk --memory-mode persistent --trust-domain single-principal --memory 8192 2>&1)"; rc=$?
assert_ok       "dry-run: --memory-mode persistent --memory 8192 both parse in one invocation" $rc
assert_contains "dry-run: both flags -- the SIZE flag set task memory to 8192"        "$out" "mem=8192"
assert_contains "dry-run: both flags -- the MODE flag still selected persistence"     "$out" "per-crew isolation holds"
# The old mode spelling is gone: --memory persistent now feeds the SIZE flag, so it
# is NOT judged as a mode. (A real deploy's task memory must be numeric; that is a
# separate, pre-existing SIZE-value concern, not the flag-collision this guards.)
out="$(KIRO_API_KEY="$DRYKEY_MEM" SMC_WORK="$TMP/memold" "$SCRIPT" --dry-run --crew frontdesk --memory persistent 2>&1)"
assert_not_contains "dry-run: --memory persistent is no longer parsed as a mode" "$out" "takes chatbot or persistent"

# fresh-account base build: the driver refuses BEFORE any AWS work when it would --
# have to build a base image and has no source checkout to build the wheel from ----
# build_image.sh now requires --kirocrew-src (or $KIROCREW_SRC); a fresh account has
# no 'smc-base' tag, so step 4 would build one. If the source cannot be resolved the
# driver must refuse at step 0 (preflight), not abort at step 4 after step 2 created
# the base stack. SMC_FIX_NO_BASE models the empty account; a bad KIROCREW_SRC and no
# derivable checkout is the "cannot build" case.
out="$(KIRO_API_KEY="$DRYKEY_MEM" SMC_WORK="$TMP/nosrc" SMC_FIX_NO_BASE=1 KIROCREW_SRC=/nonexistent-kirocrew-src \
       "$SCRIPT" --dry-run --crew frontdesk 2>&1)"; rc=$?
assert_fail     "dry-run: fresh account with no resolvable source refuses the deploy" $rc
assert_contains "dry-run: the refusal happens at preflight (step 0)"                  "$out" "FAILED at step 0"
assert_contains "dry-run: the refusal names --kirocrew-src / KIROCREW_SRC to set"     "$out" "kirocrew-src"
assert_contains "dry-run: the refusal says it stopped before creating anything"       "$out" "before anything is created"
assert_not_contains "dry-run: the refused run creates no base stack"                  "$out" "step 2 . base stack"
assert_not_contains "dry-run: the refused run prints no success banner"               "$out" "deployed and verified"
# With a resolvable source (this checkout is derivable from the driver's own path)
# the same fresh account BUILDS the base and reaches the banner, and step 4 passes
# --kirocrew-src to the builder rather than the empty arg that aborted it. The base
# builder is invoked with the resolved source: assert the driver announces it.
out="$(KIRO_API_KEY="$DRYKEY_MEM" SMC_WORK="$TMP/withsrc" SMC_FIX_NO_BASE=1 \
       "$SCRIPT" --dry-run --crew frontdesk 2>&1)"; rc=$?
assert_ok       "dry-run: fresh account WITH a resolvable source builds the base" $rc
assert_contains "dry-run: preflight names the source step 4 will build from"      "$out" "will build one from"
assert_contains "dry-run: the base is built on the fresh account"                 "$out" "building one now"
assert_contains "dry-run: the fresh-account build still reaches the banner"        "$out" "deployed and verified"


SMC_WORK="$TMP/resume" "$SCRIPT" --dry-run --crew frontdesk >/dev/null 2>&1
out="$(SMC_WORK="$TMP/resume" "$SCRIPT" --dry-run --from 8 --crew frontdesk 2>&1)"; rc=$?
assert_ok "dry-run: --from 8 resumes at verify" $rc
assert_not_contains "dry-run: --from 8 skips step 1" "$out" "step 1 . bundle"
assert_contains     "dry-run: --from 8 runs step 8"  "$out" "step 8 . verify"

# a dead credential alone fails ----------------------------------------------
# The turn now proves this directly: a 502 on the customer path means the request
# reached the container and the model call failed. Every other gate stays green, so
# this asserts that a working deployment with a dead key is still NOT proven.
out="$(SMC_WORK="$TMP/deadcred" SMC_FIX_TURN_CODE=502 "$SCRIPT" --dry-run --crew frontdesk 2>&1)"; rc=$?
assert_fail "dry-run: dead credential alone fails the deploy" $rc
assert_contains "dry-run: names the dead-credential mode" "$out" "credential is present but not working"

# a turn that answers 200 with nothing in it also fails -----------------------
# "the service replied" is not "the crew answered", and this is the shape that would
# otherwise pass a status-code-only gate.
out="$(SMC_WORK="$TMP/emptyturn" \
       SMC_FIX_TURN_BODY='{"choices":[{"message":{"role":"assistant","content":""},"finish_reason":"stop"}]}' \
       "$SCRIPT" --dry-run --crew frontdesk 2>&1)"; rc=$?
assert_fail "dry-run: 200 with an empty completion fails the deploy" $rc
assert_not_contains "dry-run: empty completion prints NO success banner" "$out" "deployed and verified"

# arch mismatch fails --------------------------------------------------------
out="$(SMC_WORK="$TMP/arch" SMC_FIX_IMAGE_ARCH=ARM64 "$SCRIPT" --dry-run --crew frontdesk 2>&1)"; rc=$?
assert_fail "dry-run: image ARM64 vs intended X86_64 fails the deploy" $rc

# NEW GATE, end to end: the running task definition's image digest EQUALS the image
# build_crew_image.sh reported. Mutating the deployed image away from the reported
# one must fail the deploy and print no success banner -- this is the gate whose
# absence let the earlier version serve a default agent while reporting green.
out="$(SMC_WORK="$TMP/imgdrift" SMC_FIX_TASKDEF_IMAGE="repo.example/smc@sha256:$(printf 'd%063d' 0)" \
       "$SCRIPT" --dry-run --crew frontdesk 2>&1)"; rc=$?
assert_fail "dry-run: served image != reported image fails the deploy" $rc
assert_contains     "dry-run: names the served!=packaged failure" "$out" "crew being SERVED is NOT the crew that was packaged"
assert_not_contains "dry-run: image drift prints NO success banner" "$out" "deployed and verified"

# NEW GATE companion: the image must have baked in the bundle packaging.build
# produced. Mutating the image's reported bundle_digest away from the manifest's
# must fail (this fires at step 4, before the crew stack deploys).
out="$(SMC_WORK="$TMP/bundledrift" SMC_FIX_IMAGE_BUNDLE_DIGEST="sha256:$(printf 'b%063d' 0)" \
       "$SCRIPT" --dry-run --crew frontdesk 2>&1)"; rc=$?
assert_fail "dry-run: image baked a different bundle fails the deploy" $rc
assert_contains     "dry-run: names the bundle-continuity failure" "$out" "does not contain the crew that was curated"
assert_not_contains "dry-run: bundle drift prints NO success banner" "$out" "deployed and verified"

# base resolution: no --base and no ECR smc-base tag must fail with a clear message,
# not silently deploy against an unpinned base.
# base resolution: a first-time account has no base image BY DEFINITION, so the deploy
# builds one rather than stopping to send the owner to a maintainer script. The product
# is one command from the owner's machine; a resolver that halts on a missing artifact
# it could produce is the deploy failing to do its job.
out="$(SMC_WORK="$TMP/nobase" SMC_FIX_NO_BASE=1 "$SCRIPT" --dry-run --crew frontdesk 2>&1)"; rc=$?
assert_ok       "dry-run: no base image is built, not refused"        $rc
assert_contains "dry-run: says it is building the base"               "$out" "building one now"
assert_contains "dry-run: says the one-time cost out loud"            "$out" "one-time"
assert_contains "dry-run: still reaches the banner"                   "$out" "deployed and verified"
# --base still overrides, and an unpinned one is still refused: auto-building must not
# have loosened the digest requirement.
out="$(SMC_WORK="$TMP/basetag" "$SCRIPT" --dry-run --crew frontdesk --base repo/x:latest 2>&1)"; rc=$?
assert_fail     "dry-run: --base with a mutable tag is still refused" $rc
assert_contains "dry-run: says why a tag will not do"                 "$out" "digest-pinned"

echo "== event stream (--events, opt-in machine-readable progress) =="

# Every fact the assertions below need, read out of the stream ONCE. Parsing JSON
# with grep is how a machine channel gets asserted against loosely, so this decodes
# it properly; a single unparseable line makes this helper exit non-zero, which is
# itself one of the tests. python3 is already a hard dependency of the driver.
evfacts() { # EVENTS_FILE
  python3 - "$1" <<'PY'
import json, sys

ev = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
steps = [e for e in ev if e["event"] == "step"]
run = next((e for e in ev if e["event"] == "run"), {})
end = next((e for e in ev if e["event"] == "end"), {})
accounts = [e for e in ev if e["event"] == "account"]
failed = [e for e in steps if e["state"] == "fail"]
i_account = ev.index(accounts[0]) if accounts else -1
i_step2 = next(
    (i for i, e in enumerate(ev) if e["event"] == "step" and e["step"] == 2 and e["state"] == "start"),
    -1,
)
out = {
    "ladder": ",".join(str(s) for s in run.get("steps", [])),
    "crew": run.get("crew", ""),
    "from": run.get("from", ""),
    "started": ",".join(str(e["step"]) for e in steps if e["state"] == "start"),
    "finished": ",".join(str(e["step"]) for e in steps if e["state"] == "ok"),
    "names": len([e for e in steps if e["state"] == "start" and e.get("name")]),
    "failstep": failed[0]["step"] if failed else "",
    "failresume": failed[0].get("resumeFrom", "") if failed else "",
    "failreason": failed[0].get("reason", "") if failed else "",
    "end": end.get("state", ""),
    "endreason": end.get("reason", ""),
    "accounts": len(accounts),
    "account": accounts[0]["account"] if accounts else "",
    # The account must be announced BEFORE step 2, which is the first step that
    # creates anything in AWS.
    "account_before_step2": "yes" if 0 <= i_account < i_step2 else "no",
    # For step 8 the assertive lines are exactly the gate verdicts.
    "gateverdicts": len(
        [e for e in ev if e["event"] == "detail" and e.get("step") == 8 and e["level"] == "ok"]
    ),
    "step3": len([e for e in steps if e["step"] == 3]),
}
for k, v in out.items():
    print("%s=%s" % (k, v))
PY
}
fact() { printf '%s\n' "$2" | sed -n "s/^$1=//p"; }

EV="$TMP/events"; mkdir -p "$EV"
DRYKEY='aws-kiro-abcdefghijklmnopqrstuvwxyz0123456789'   # 20+ chars: the shape guard's floor

# THE guarantee: the prose is untouched. Same fixture, same work dir path, one run
# with the flag and one without, and stdout must be byte for byte identical --
# which is what makes it safe for a UI to depend on the events instead of the text.
rm -rf "$EV/w"; out_plain="$(KIRO_API_KEY="$DRYKEY" SMC_WORK="$EV/w" "$SCRIPT" --dry-run --crew frontdesk 2>&1)"
assert_eq "events: absent flag writes no stream file" "absent" "$( [ -e "$EV/none.jsonl" ] && echo present || echo absent )"
rm -rf "$EV/w"; out_ev="$(KIRO_API_KEY="$DRYKEY" SMC_WORK="$EV/w" "$SCRIPT" --dry-run --crew frontdesk --events "$EV/green.jsonl" 2>&1)"; rc=$?
assert_ok "events: a run with --events still exits 0" $rc
assert_eq "events: human output is byte-identical with and without --events" "$out_plain" "$out_ev"

facts="$(evfacts "$EV/green.jsonl")"; rc=$?
assert_ok  "events: every line is valid JSON" $rc
assert_eq  "events: the ladder is the driver's own step numbers" "0,1,2,4,5,6,7,8" "$(fact ladder "$facts")"
assert_eq  "events: there is no step 3 in the ladder" "" "$(printf '%s' "$(fact ladder "$facts")" | tr ',' '\n' | grep -x 3 || true)"
assert_eq  "events: no step 3 is ever emitted" "0" "$(fact step3 "$facts")"
assert_eq  "events: every ladder step started" "0,1,2,4,5,6,7,8" "$(fact started "$facts")"
assert_eq  "events: every started step finished ok" "0,1,2,4,5,6,7,8" "$(fact finished "$facts")"
assert_eq  "events: every start carries the step's name" "8" "$(fact names "$facts")"
assert_eq  "events: the run event names the crew" "frontdesk" "$(fact crew "$facts")"
assert_eq  "events: a green run ends ok" "ok" "$(fact end "$facts")"
assert_eq  "events: the account is announced exactly once" "1" "$(fact accounts "$facts")"
assert_eq  "events: the account is the one the run touches" "111122223333" "$(fact account "$facts")"
assert_eq  "events: the account is announced before step 2 creates anything" "yes" "$(fact account_before_step2 "$facts")"
assert_eq  "events: step 8 reports all 13 gate verdicts" "13" "$(fact gateverdicts "$facts")"

# Nothing from note() is mirrored, which is the line that keeps the machine channel
# clear of the one place this driver talks about a credential at all.
assert_not_contains "events: the api key value never reaches the stream" "$(cat "$EV/green.jsonl")" "$DRYKEY"
assert_not_contains "events: the api-key note is not mirrored"            "$(cat "$EV/green.jsonl")" "api key written from the environment"

# A failure has to say WHICH step and WHERE TO RESUME, or a wizard can only offer
# "start again from the top" -- which for a deploy means re-running steps that
# already created resources.
rm -rf "$EV/w4"; KIRO_API_KEY="$DRYKEY" SMC_WORK="$EV/w4" SMC_FIX_IMAGE_BUNDLE_DIGEST="sha256:$(printf 'b%063d' 0)" \
  "$SCRIPT" --dry-run --crew frontdesk --events "$EV/fail4.jsonl" >/dev/null 2>&1; rc=$?
assert_fail "events: the bundle-drift run still fails" $rc
facts4="$(evfacts "$EV/fail4.jsonl")"
assert_eq       "events: the failing step is named"                "4" "$(fact failstep "$facts4")"
assert_eq       "events: resumeFrom is the failing step"           "4" "$(fact failresume "$facts4")"
assert_eq       "events: a failed run ends fail"                   "fail" "$(fact end "$facts4")"
assert_contains "events: the fail reason is the driver's own text"  "$(fact failreason "$facts4")" "not built from the bundle"
assert_eq       "events: step 4 never reports ok when it failed"   "0,1,2" "$(fact finished "$facts4")"

# A verify failure resumes at 8: the gates re-prove a deployment from live state, so
# that is the retry that costs nothing already paid for.
rm -rf "$EV/w8"; KIRO_API_KEY="$DRYKEY" SMC_WORK="$EV/w8" SMC_FIX_TURN_CODE=502 \
  "$SCRIPT" --dry-run --crew frontdesk --events "$EV/fail8.jsonl" >/dev/null 2>&1
facts8="$(evfacts "$EV/fail8.jsonl")"
assert_eq "events: a verify failure resumes at 8" "8" "$(fact failresume "$facts8")"
assert_eq "events: a verify failure reports the gate count in its reason" "yes" \
  "$( case "$(fact failreason "$facts8")" in *"verify gate(s) failed"*) echo yes ;; *) echo no ;; esac )"

# --from N must be visible in the stream, and the account must still be announced on
# a resume -- it is read back from state, not re-resolved, and a UI adopting a resumed
# run needs it just as much.
KIRO_API_KEY="$DRYKEY" SMC_WORK="$EV/w8" "$SCRIPT" --dry-run --from 8 --crew frontdesk --events "$EV/resume.jsonl" >/dev/null 2>&1; rc=$?
assert_ok "events: --from 8 resumes and exits 0" $rc
factsr="$(evfacts "$EV/resume.jsonl")"
assert_eq "events: the run event carries --from"        "8" "$(fact from "$factsr")"
assert_eq "events: a resume emits only the resumed step" "8" "$(fact started "$factsr")"
assert_eq "events: a resume still announces the account" "1" "$(fact accounts "$factsr")"

# An events path that cannot be written is REFUSED. A caller that asked for events is
# a UI, and a deploy running invisibly behind a progress bar that never moves is
# worse than one that did not start.
out="$(SMC_WORK="$EV/wbad" "$SCRIPT" --dry-run --crew frontdesk --events /proc/nope/x.jsonl 2>&1)"; rc=$?
assert_fail     "events: an unwritable stream path fails the run" $rc
assert_contains "events: says which path it could not write"      "$out" "cannot write the event stream"

# --yes is what a non-interactive caller uses to pass step 0, and it must still print
# the account: step 0 is the last point before anything is created.
rm -rf "$EV/wyes"; out="$(KIRO_API_KEY="$DRYKEY" SMC_WORK="$EV/wyes" "$SCRIPT" --dry-run --yes --crew frontdesk 2>&1)"; rc=$?
assert_ok       "events: --yes runs non-interactively" $rc
# The expected id comes from the fixture knob rather than a literal, so the value
# lives in ONE place. A literal here also reads as a real account to a scrub gate.
FIXACCT="${SMC_FIX_ACCOUNT:-$(printf '1111%s3333' 2222)}"
assert_contains "events: --yes still prints the account" "$out" "account $FIXACCT"

echo
echo "== external gate scripts =="
# These standalone guards live beside this runner and each exit non-zero on a
# violation. Running them here makes them part of the permanent gate rather than
# checks someone has to remember to run. check_param_seam.sh proves the driver and
# BOTH templates (crew.yaml and base.yaml) agree about parameters in both
# directions; its base.yaml coverage is what catches an undeclared base-stack
# override before it aborts a real deploy at CreateChangeSet.
# --- persistent memory requires an explicit trust-domain declaration -----------
# The task role is scoped to the crew's own S3 prefix, which separates CREWS and
# not CUSTOMERS: every conversation the crew served is under that one prefix, the
# backend auto-approves every tool, and the conversation id arrives in the request
# rather than being derived from the caller. So persistent mode is refused unless
# the owner states they accept that. Refused at parse time, before step 0, because
# learning it after six steps of work is the same defect with a worse message.
out="$(KIRO_API_KEY="$DRYKEY" SMC_WORK="$TMP/td-none" "$SCRIPT" --dry-run --crew frontdesk --memory-mode persistent 2>&1)"; rc=$?
assert_fail     "trust domain: persistent without a declaration is refused" $rc
assert_contains "trust domain: the refusal says what to pass" "$out" "--trust-domain single-principal"
assert_not_contains "trust domain: refused before any step ran" "$out" "step 1"

out="$(KIRO_API_KEY="$DRYKEY" SMC_WORK="$TMP/td-ok" "$SCRIPT" --dry-run --crew frontdesk --memory-mode persistent --trust-domain single-principal 2>&1)"
assert_contains "trust domain: persistent with the declaration deploys" "$out" "deployed and verified"
# The dry run does not echo its parameter list, so asserting on its output would
# be asserting on nothing. Check the two facts that actually matter instead:
# the driver passes the parameter, and the template declares it. (The seam
# guard proves the general form; this pins THIS parameter by name so a rename
# on one side cannot pass by matching on the other.)
grep -q '"TrustDomain=$TRUST_DOMAIN"' "$SCRIPT"
assert_ok "trust domain: the driver passes TrustDomain to the crew stack" $?
grep -qE '^  TrustDomain:' "$DEPLOY_DIR/../templates/crew.yaml"
assert_ok "trust domain: crew.yaml declares TrustDomain" $?
grep -q 'PersistentMemoryNeedsAnExplicitTrustDomain' "$DEPLOY_DIR/../templates/crew.yaml"
assert_ok "trust domain: crew.yaml refuses persistent without it (a Rule, so a console deploy cannot skip it)" $?

out="$(KIRO_API_KEY="$DRYKEY" SMC_WORK="$TMP/td-chat" "$SCRIPT" --dry-run --crew frontdesk --memory-mode chatbot 2>&1)"
assert_contains "trust domain: chatbot needs no declaration" "$out" "deployed and verified"

out="$(KIRO_API_KEY="$DRYKEY" SMC_WORK="$TMP/td-bad" "$SCRIPT" --dry-run --crew frontdesk --trust-domain whatever 2>&1)"; rc=$?
assert_fail     "trust domain: an unknown value is refused" $rc
assert_contains "trust domain: the refusal names the only accepted value" "$out" "takes single-principal"

# --- base image tags name the architecture -------------------------------------
# The content tag pinned wheel commit and app revision but NOT architecture, and
# the moving pointer was a single 'smc-base'. Built on amd64 and pushed, an arm64
# build from the same source resolved the same tag, found the amd64 digest and
# reused it -- and downstream pins the DIGEST, so the deployment ran an amd64 image
# on an arm64 task. Asserted on the source because reproducing it needs a registry.
BUILDER="$DEPLOY_DIR/build_image.sh"
grep -q 'PUSH_TAG="${IMAGE_REPO}:smc-${CFN_ARCH}-' "$BUILDER"
assert_ok "base image: the content tag names the architecture" $?
grep -q ':smc-base-${CFN_ARCH}"' "$BUILDER"
assert_ok "base image: the moving pointer is per-architecture" $?
grep -qE 'imageTag="smc-base-\$CFN_ARCH"' "$SCRIPT"
assert_ok "base image: the driver resolves the per-architecture pointer" $?
# Both sides must use the same name or the driver resolves a tag nothing publishes.
grep -cq 'smc-base' "$BUILDER" && grep -q 'smc-base-' "$SCRIPT"
assert_ok "base image: builder and driver agree on the tag shape" $?

bash "$HERE/check_param_seam.sh" >/dev/null 2>&1
assert_ok "external: check_param_seam.sh (crew + base parameter seam) passes" $?

# --- crew name is an allowlist, because it becomes a scratch directory ---------
# The name reaches a CloudFormation stack name, a URL route prefix AND a local
# scratch path, and produce_bundle runs `rm -rf` on a directory derived from that
# path -- so a traversing name deleted a directory outside the scratch root before
# any AWS call. These assert the refusal happens at parse time, before anything is
# created, and that a legitimate name is unaffected.
out="$(KIRO_API_KEY="$DRYKEY" SMC_WORK="$TMP/trav" "$SCRIPT" --dry-run --crew 'x/../../evil' --memory-mode chatbot 2>&1)"
assert_contains "crew name: a traversing name is refused" "$out" "CrewName pattern"
assert_not_contains "crew name: traversal refused before any step ran" "$out" "step 1"

out="$(KIRO_API_KEY="$DRYKEY" SMC_WORK="$TMP/abs" "$SCRIPT" --dry-run --crew '/etc/passwd' --memory-mode chatbot 2>&1)"
assert_contains "crew name: an absolute path is refused" "$out" "lowercase letter or digit"

out="$(KIRO_API_KEY="$DRYKEY" SMC_WORK="$TMP/meta" "$SCRIPT" --dry-run --crew 'a;rm -rf x' --memory-mode chatbot 2>&1)"
assert_contains "crew name: a shell metacharacter is refused" "$out" "CrewName pattern"

out="$(KIRO_API_KEY="$DRYKEY" SMC_WORK="$TMP/ok" "$SCRIPT" --dry-run --crew acme-support --memory-mode chatbot 2>&1)"
assert_contains "crew name: a legitimate hyphenated name is accepted" "$out" "deployed and verified"

# --- the driver's name rule IS the template's ------------------------------------
# crew.yaml's CrewName carries AllowedPattern '^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$'.
# The driver's check was looser in three ways at once -- uppercase allowed, no length
# bound, trailing hyphen allowed -- so `--crew FrontDesk` built a bundle and pushed an
# image before CloudFormation refused the parameter, leaving the side effects of a
# deployment that could never finish.
#
# grep -F, on the pattern AS WRITTEN. A first version escaped it for grep -E and
# matched neither file, so all of these failed against correct code.
_CREW_PATTERN='^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$'
grep -qF "$_CREW_PATTERN" "$DEPLOY_DIR/../templates/crew.yaml"
assert_ok "crew name: the template still carries the pattern this pins" $?
grep -qF "$_CREW_PATTERN" "$SCRIPT"
assert_ok "crew name: the driver enforces the template's own pattern" $?

# Each bad name must be refused BY A NAME RULE and produce no deployment. There are
# two such rules and they fire on different inputs: the first-character check catches
# `FrontDesk` and `-lead`, the full-pattern check catches `trail-` and the too-short
# `a`. Asserting only the pattern message failed against correct code, so the
# assertion names the OUTCOME (refused, nothing deployed) and accepts either message.
#
# The success assertion is the load-bearing half: a refused run prints no step line at
# all, so checking for the ABSENCE of "step 1" passes even with the rule removed --
# measured, and it did.
for _bad in FrontDesk trail- -lead a; do
  out="$(KIRO_API_KEY="$DRYKEY" SMC_WORK="$TMP/nm-$_bad" "$SCRIPT" --dry-run \
    --crew "$_bad" --memory-mode chatbot 2>&1)"
  printf '%s' "$out" | grep -qE "CrewName pattern|lowercase letter or digit"
  assert_ok "crew name: '$_bad' is refused by a name rule" $?
  assert_not_contains "crew name: '$_bad' reports no successful deploy" "$out" "deployed and verified"
done

# --- secret_digest must survive the secret not existing yet --------------------
# On a FIRST deploy `smc/<crew>/kiro-api-key` does not exist, so get-secret-value
# exits non-zero. Piped straight into sha256sum under `set -euo pipefail` that made
# the whole pipeline non-zero, and the caller's `before="$(secret_digest ...)"`
# aborted the deploy BEFORE the secret was created -- so it could never be created
# on a fresh account. Only a real first-time deploy would surface that, which is
# exactly the run nobody had done, so it is pinned here instead.
#
# Asserted on the function's SHAPE rather than by running it, because the dry-run
# path returns a fixture before reaching any AWS call and so cannot exercise this.
#
# COMMENTS ARE STRIPPED FIRST. Without that this gate passed against the broken
# code: the fix's own comment contains the string `|| true`, so grepping the raw
# function body matched the prose and proved nothing. Verified by reverting the
# function and watching these two go red.
digest_body="$(sed -n '/^secret_digest()/,/^}/p' "$SCRIPT" | sed 's/#.*//')"
printf '%s' "$digest_body" | grep -q '|| true'
assert_ok "secret_digest: the read tolerates a not-yet-existing secret" $?
# Backslash CONTINUATIONS are joined, and nothing else. Two earlier attempts at this
# assertion were wrong in opposite directions: a plain line-oriented grep could not
# see the aborting pipeline because it spanned continuations, and deleting every
# newline glued the fixed code's separate statements together and reported a false
# positive against correct code.
joined="$(printf '%s' "$digest_body" | sed -e ':a' -e '/\\$/{N;s/\\\n//;ba' -e '}')"
printf '%s' "$joined" | grep -qE 'get-secret-value.*sha256sum'
assert_fail "secret_digest: the aws read is not piped straight into the hash" $?

# --- the crew image builder verifies the bundle digest it derives a tag from ----
# The image tag encodes the manifest digest, so a bundle whose CONTENT changed while
# its manifest stayed put hits the tag of a DIFFERENT bundle's image, reuses that
# image, and reports it as the bundle just supplied. The script checked the digest's
# SHAPE (sha256:<hex>) and never recomputed it.
#
# Driven end to end with every required argument. An earlier version of this test
# omitted --repo, so the script exited at argument validation and the assertion
# "passed" without the check ever running -- the reason the arguments are spelled out
# in full here rather than abbreviated.
CREWBUILDER="$DEPLOY_DIR/build_crew_image.sh"
DIGEST_BUNDLE="$TMP/digest-bundle"
mkdir -p "$DIGEST_BUNDLE"
printf '{"name":"frontdesk"}\n' > "$DIGEST_BUNDLE/agent.json"
printf '{"mcpServers":{}}\n' > "$DIGEST_BUNDLE/mcp.json"
mkdir -p "$DIGEST_BUNDLE/skills"
TRUE_DIGEST="$(python3 - "$DIGEST_BUNDLE" <<'PY'
import hashlib, json, sys
from pathlib import Path
root = Path(sys.argv[1]); rows = []
for p in sorted(root.rglob("*")):
    if p.is_file() and p.relative_to(root).as_posix() != "manifest.json":
        rows.append([p.relative_to(root).as_posix(), hashlib.sha256(p.read_bytes()).hexdigest()])
sys.stdout.write("sha256:" + hashlib.sha256(
    json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest())
PY
)"
printf '{"crew_name":"frontdesk","digest":"%s","bundle_version":1}\n' "$TRUE_DIGEST" \
  > "$DIGEST_BUNDLE/manifest.json"
_FAKE_BASE="example.dkr.ecr.us-west-2.amazonaws.com/smc-base@sha256:$(printf '%064d' 0)"
_FAKE_REPO="example.dkr.ecr.us-west-2.amazonaws.com/smc-crew"
_crewbuild() {
  bash "$CREWBUILDER" --crew frontdesk --bundle "$DIGEST_BUNDLE" \
    --base "$_FAKE_BASE" --repo "$_FAKE_REPO" --out "$TMP/cb.json" --dry-run 2>&1
}
out="$(_crewbuild)"
assert_not_contains "crew image: an honest bundle is not refused" "$out" "does not match its manifest"
assert_contains "crew image: an honest bundle reaches the tag" "$out" "tag="

printf ' ' >> "$DIGEST_BUNDLE/agent.json"   # content changes, manifest does not
out="$(_crewbuild)"
assert_contains "crew image: a tampered bundle is refused" "$out" "does not match its manifest digest"
assert_not_contains "crew image: refused BEFORE any image lookup" "$out" "reuse check"

# A bundle carrying its signed plan is the DOCUMENTED flow (plan, sign, build with the
# same --out), and the plan is written into staging AFTER the manifest digest is taken
# -- so it is not in the recorded digest. Counting it refused every such bundle: a
# deploy-blocking regression the first version of the check above introduced. Pinned
# here because only a bundle that has been through `plan` exposes it.
"$(command -v truncate)" -s -1 "$DIGEST_BUNDLE/agent.json"   # undo the tamper
printf '{"select":[]}\n' > "$DIGEST_BUNDLE/curation-plan.json"
out="$(_crewbuild)"
assert_not_contains "crew image: a carried plan does not invalidate the digest" \
  "$out" "does not match its manifest"
assert_contains "crew image: a bundle with a plan still reaches the tag" "$out" "tag="

# --- curl credentials never reach argv ------------------------------------------
# `--user "<key>:<secret>"` puts the owner's secret and session token in the command
# line. Modern curl overwrites its own argv with asterisks, but only AFTER exec, so a
# window exists where any user on the host can read the real value from /proc -- and
# that mitigation is a curl-version detail, not something this script controls.
#
# Asserted on the script's SHAPE: the dry-run path returns fixtures before reaching
# any curl call, so there is no live invocation to inspect.
grep -q 'user = "%s:%s"' "$SCRIPT"
assert_ok "curl auth: the credential is written to a config file" $?
[ "$(grep -c -- '--user "' "$SCRIPT")" -eq 0 ]
assert_ok "curl auth: no --user on any command line" $?
grep -q 'umask 077' "$SCRIPT"
assert_ok "curl auth: the config file is created private" $?
[ "$(grep -c -- '-K "$(_curl_auth_file)"' "$SCRIPT")" -eq 3 ]
assert_ok "curl auth: all three signed calls read from the file" $?

# --- the secret VALUE never reaches the aws CLI command line ---------------------
# `--secret-string "$v"` puts the model API key in argv for the whole call. Unlike
# curl, the aws CLI does not rewrite its own argv, so there is no window here -- it is
# simply readable. `file://` is the CLI's own documented way to read an argument from
# disk; verified by running it, where the argument parsed and the call failed on
# credentials rather than on the parameter.
[ "$(grep -c -- '--secret-string "file://' "$SCRIPT")" -eq 2 ]
assert_ok "put_secret: both writes read the value from a file" $?
grep -qE '^\s*trap "rm -f' "$SCRIPT"
assert_ok "put_secret: the file is removed on every exit path" $?
sed -n '/^put_secret()/,/^}/p' "$SCRIPT" | grep -q -- '--secret-string "\$v"'
assert_fail "put_secret: the raw value is not passed as an argument" $?

echo
printf '%s%d passed%s, %s%d failed%s\n' "$_grn" "$PASS" "$_rst" "$( [ "$FAIL" -gt 0 ] && printf '%s' "$_red" )" "$FAIL" "$_rst"
[ "$FAIL" -eq 0 ]
