#!/usr/bin/env bash
#
# Run the in-Nuke test suites against a real Nuke, and aggregate the results.
#
# These are the tests the offline pytest suite cannot express: anything whose
# answer depends on what Nuke actually does. They need a Nuke installation and
# a licence, so they do not run in CI -- see tests/README.md for the split.
#
# Usage:
#   tests/run_in_nuke.sh                 # every *_in_nuke.py suite
#   tests/run_in_nuke.sh acceptance_gsv  # only suites matching a substring
#   NUKE_EXECUTABLE=/path/to/Nuke17.0 tests/run_in_nuke.sh
#
# Environment:
#   NUKE_EXECUTABLE   Nuke binary to use. Auto-detected if unset.
#   foundry_LICENSE   Licence server, e.g. port@host. Read from your
#                     environment; never hardcoded here.
#   NUKE_TEST_HOME    Sandbox HOME for the run. Defaults to a temp dir so the
#                     suites are not affected by plugins in your real ~/.nuke.
#   USE_REAL_HOME=1   Use your actual ~/.nuke instead of the sandbox.

set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILTER="${1:-}"

# -- locate Nuke ------------------------------------------------------------
if [[ -z "${NUKE_EXECUTABLE:-}" ]]; then
    # Highest version wins. macOS app bundle first, then Linux layout.
    NUKE_EXECUTABLE="$(ls -d /Applications/Nuke*/Nuke*.app/Contents/MacOS/Nuke* 2>/dev/null \
        | grep -v -- '-c$' | sort -V | tail -1)"
    if [[ -z "$NUKE_EXECUTABLE" ]]; then
        NUKE_EXECUTABLE="$(ls -d /usr/local/Nuke*/Nuke* 2>/dev/null | sort -V | tail -1)"
    fi
fi

if [[ -z "$NUKE_EXECUTABLE" || ! -x "$NUKE_EXECUTABLE" ]]; then
    echo "error: no Nuke executable found. Set NUKE_EXECUTABLE." >&2
    exit 127
fi

# -- sandbox HOME -----------------------------------------------------------
# A crashing plugin in ~/.nuke aborts startup before the test script runs, so
# an isolated HOME is the default. Opt back in with USE_REAL_HOME=1.
CLEANUP_HOME=""
if [[ "${USE_REAL_HOME:-0}" == "1" ]]; then
    RUN_HOME="$HOME"
else
    RUN_HOME="${NUKE_TEST_HOME:-}"
    if [[ -z "$RUN_HOME" ]]; then
        RUN_HOME="$(mktemp -d)"
        CLEANUP_HOME="$RUN_HOME"
    fi
    mkdir -p "$RUN_HOME/.nuke"
fi
trap '[[ -n "$CLEANUP_HOME" ]] && rm -rf "$CLEANUP_HOME"' EXIT

echo "nuke      : $NUKE_EXECUTABLE"
echo "home      : $RUN_HOME$([[ -n "$CLEANUP_HOME" ]] && echo '  (sandbox)')"
echo "licence   : ${foundry_LICENSE:-<from Foundry client files>}"
echo

# -- run --------------------------------------------------------------------
# -ti, not -t: -t requests a render-only licence, which some sites do not carry
# for every Nuke version. -i asks for an interactive one instead.
declare -a PASSED=() FAILED=()

for suite in "$TESTS_DIR"/*_in_nuke.py; do
    [[ -e "$suite" ]] || continue
    name="$(basename "$suite" .py)"
    if [[ -n "$FILTER" && "$name" != *"$FILTER"* ]]; then
        continue
    fi

    echo "═══ $name ═══"
    HOME="$RUN_HOME" "$NUKE_EXECUTABLE" -ti "$suite"
    status=$?
    if [[ $status -eq 0 ]]; then
        PASSED+=("$name")
    else
        FAILED+=("$name (exit $status)")
    fi
    echo
done

# -- summary ----------------------------------------------------------------
total=$(( ${#PASSED[@]} + ${#FAILED[@]} ))
if [[ $total -eq 0 ]]; then
    echo "no suites matched${FILTER:+ '$FILTER'}" >&2
    exit 1
fi

echo "──────────────────────────────────────────────"
for name in "${PASSED[@]}"; do echo "  ok    $name"; done
for name in "${FAILED[@]}"; do echo "  FAIL  $name"; done
echo "──────────────────────────────────────────────"
echo "${#PASSED[@]}/$total suites passed"

[[ ${#FAILED[@]} -eq 0 ]]
