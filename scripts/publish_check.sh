#!/usr/bin/env bash
#
# Leak verifier for the public snapshot (see scripts/publish_public.sh).
#
# A denylist alone silently misses new leaks: it only knows about the files
# that existed when it was written. This scans the *output tree* for the
# shapes a leak actually takes, so a file added six months from now is checked
# by the same rules. Exits non-zero with file:line on any hit.
#
#   usage: scripts/publish_check.sh [TREE]     (TREE defaults to ../ai-public)
#
# Two kinds of check:
#   content  — regexes over every text file in the tree
#   filename — file types that must never ship at all
#
# EXCEPTIONS are deliberate, individually justified, and few. Every entry is
# "pattern-id|path|reason". Adding one is a decision; if this list starts
# growing, the pattern is wrong, not the tree.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tree="${1:-$repo_root/../ai-public}"

if [[ ! -d $tree ]]; then
  echo "publish_check: no such tree: $tree" >&2
  exit 2
fi
tree="$(cd "$tree" && pwd)"

# --- content patterns --------------------------------------------------------
# id <TAB> extended-regex <TAB> what it catches. Matched case-insensitively.
# The separator is a TAB, not '|', because '|' is alternation in half of these.
#
# Deliberately NOT here: `client_secret` and `token.json`. Both appear dozens
# of times across the source as env-var names, function names and setup docs —
# all legitimate. The risk they stand for is a *committed credential file*,
# which the filename checks below catch precisely instead of drowning this
# list in false positives.
# Owner-specific strings are NOT here — see the `publish_check.local` hook at
# the bottom of this section. Naming them would publish the very association
# the check exists to protect: a surname next to a mail provider, in a repo
# published under that person's own account, reconstructs their address.
CONTENT_PATTERNS=(
  $'lan-address\t192\\.168\\.[0-9]\ta home/LAN IPv4 address'
  $'wsl-address\t172\\.(28|30)\\.[0-9]\ta WSL2 host or Docker bridge address'
  $'anthropic-key\tsk-ant-[A-Za-z0-9_-]{8}\ta live Anthropic API key'
  $'google-key\tAIza[A-Za-z0-9_-]{10}\ta live Google API key'
  $'private-key\t-----BEGIN [A-Z ]*PRIVATE KEY-----\ta private key pasted into a text file'
)

# --- filename patterns -------------------------------------------------------
# Shell globs matched against the path. `scratch/` is the agent's live write
# root and accumulates exactly these, so the check must not depend on
# .gitignore having caught them.
FILENAME_GLOBS=(
  "*.db" "*.sqlite" "*.sqlite3" "*.pem" "*.key" "*.p12" "*.pfx"
  "*token*.json" "*client_secret*.json" "credentials.json" ".env"
)

# --- exceptions --------------------------------------------------------------
EXCEPTIONS=(
  # These two files have to spell the patterns out: one IS the checker, the
  # other holds a canary per pattern so a regression turns the suite red. `*`
  # exempts them from every content pattern.
  # Known blind spot, accepted: a real secret pasted into either file would not
  # be caught here. Both are short and both are read on every change to this
  # script — that is the compensating control.
  # Everywhere else is scanned: a postmortem of a leak describes its CLASS,
  # never quotes the leaked string. Owner patterns live in publish_check.local.
  "*|scripts/publish_check.sh|the pattern list itself"
  "*|tests/test_conventions.py|one canary string per pattern, so a neutered pattern fails the suite"

  # The SSRF guard's whole job is refusing RFC1918 destinations, so its tests
  # must name them. These are textbook literals, not anyone's network.
  "lan-address|tests/test_egress_proxy.py|RFC1918 literals are this file's SSRF test vectors"
  "lan-address|tests/test_web_fetch.py|RFC1918 literal is a SSRF-guard test vector"
  "lan-address|tests/test_browser_fetch.py|RFC1918 literal is a SSRF-guard test vector"

  # The snapshot's own Claude Code config. These are PRESERVE'd (the mirror
  # leaves them alone) rather than published: each is denylisted from the
  # manifest AND .gitignore'd in the snapshot, so no `git add` can carry one
  # into the public repo. They accumulate absolute home paths as a side effect
  # of normal use — an approved `Bash(...)` permission embeds one — which would
  # otherwise fail every publish from here on. Exempt only the gitignored ones:
  # .claude/settings.json is NOT ignored in the snapshot, so it really can be
  # committed and must keep being scanned.
  "*|.claude/settings.local.json|PRESERVE'd + gitignored in the snapshot: unpublishable"
  "*|.claude/party.json|PRESERVE'd + gitignored in the snapshot: unpublishable"
  "*|.claude/party/xp.sh|PRESERVE'd + gitignored in the snapshot: unpublishable"
)

# --- local, owner-specific extension -----------------------------------------
# The patterns above are generic: anyone could publish them. The strings that
# identify one particular person — a surname, a mail provider, a house name —
# are that person's configuration, and writing them into a published tool is
# self-defeating. They live in scripts/publish_check.local, which appends to
# CONTENT_PATTERNS and EXCEPTIONS and is denylisted from the snapshot.
#
# No file, no owner patterns: a fork of this repo gets a working verifier and
# is expected to write its own.
_local="$repo_root/scripts/publish_check.local"
if [[ -f $_local ]]; then
  # shellcheck source=/dev/null
  source "$_local"
  echo "publish_check: loaded scripts/publish_check.local"
else
  # Say so out loud. A missing file is normal in a fork and fatal in the source
  # repo, and the difference is invisible from the exit code — "clean" would
  # otherwise mean "clean apart from the checks that didn't run".
  echo "publish_check: NOTE — no scripts/publish_check.local; running with" \
       "generic patterns only, and no owner-identifying ones." >&2
fi

# Host labels that may legitimately precede `.ts.net` — placeholders in docs
# and tests. Anything else is a real tailnet name and fails.
TAILNET_PLACEHOLDERS=(
  "<host>" "<machine>" "<windows-host>" "<tailnet>" "your-host" "your-tailnet"
  "host" "my-box" "tailnet" "example"
)

is_excepted() {  # $1=pattern-id  $2=repo-relative path
  local entry id rest
  for entry in "${EXCEPTIONS[@]}"; do
    id=${entry%%|*}
    [[ $id == "$1" || $id == '*' ]] || continue
    rest=${entry#*|}
    [[ ${rest%%|*} == "$2" ]] && return 0
  done
  return 1
}

in_list() {  # $1=needle, rest=haystack
  local needle=$1; shift
  local item
  for item in "$@"; do [[ $item == "$needle" ]] && return 0; done
  return 1
}

hits=0
report() {  # $1=location  $2=reason
  printf '  %s\n      %s\n' "$1" "$2"
  hits=$((hits + 1))
}

# Run one regex over the tree, echoing `path:line:text` matches.
#
# Returns 2 on a grep *error* (bad regex, unreadable tree) so the caller can
# abort. Scanning on with a pattern that never matches because it does not
# compile is the one failure mode this whole script exists to prevent — it
# would report "clean" and mean nothing. Callers MUST use `|| exit 2`: this
# runs in a command-substitution subshell, so exiting from here would only
# kill the subshell.
scan() {  # $1=extended regex
  local out status=0
  out=$(cd "$tree" && grep -rInEi --binary-files=without-match --exclude-dir=.git -- "$1" .) || status=$?
  if (( status > 1 )); then
    echo "publish_check: ABORTED — grep exited $status on pattern: $1" >&2
    return 2
  fi
  [[ -n $out ]] && sed 's|^\./||' <<<"$out"
  return 0
}

echo "publish_check: scanning $tree"

# --- 1. content --------------------------------------------------------------
for spec in "${CONTENT_PATTERNS[@]}"; do
  IFS=$'\t' read -r id regex desc <<<"$spec"
  matches=$(scan "$regex") || exit 2
  while IFS= read -r line; do
    [[ -n $line ]] || continue
    rel=${line%%:*}
    is_excepted "$id" "$rel" && continue
    report "$line" "$id: $desc"
  done <<<"$matches"
done

# --- 2. tailnet hostnames ----------------------------------------------------
# `.ts.net` appears legitimately as a documented placeholder, so match the host
# label instead of the domain and allow only the known placeholder forms.
matches=$(scan '\.ts\.net') || exit 2
while IFS= read -r line; do
  [[ -n $line ]] || continue
  rel=${line%%:*}
  is_excepted "tailnet-host" "$rel" && continue
  # Pull the full dotted host chain before `.ts.net` and check EVERY label —
  # a Tailscale FQDN is `<machine>.<tailnet>.ts.net`, and the machine label is
  # the identifying one (it defaults to the hostname), so checking only the
  # label adjacent to `.ts.net` would wave through a real machine name in
  # front of a placeholder tailnet. A bare `.ts.net` with no label — prose, or
  # an elided `https://….ts.net` — names no node and is not a leak, so no
  # match here means nothing to report.
  while IFS= read -r hit; do
    [[ -n $hit ]] || continue
    labels_ok=1
    IFS=. read -ra labels <<<"${hit%.ts.net}"
    for label in "${labels[@]}"; do
      in_list "$label" "${TAILNET_PLACEHOLDERS[@]}" || { labels_ok=0; break; }
    done
    (( labels_ok )) && continue
    report "$line" "tailnet-host: '$hit' looks like a real tailnet node, not a placeholder"
  done < <(grep -oE '([A-Za-z0-9_<>-]+\.)+ts\.net' <<<"$line" || true)
done <<<"$matches"

# --- 3. filenames ------------------------------------------------------------
for glob in "${FILENAME_GLOBS[@]}"; do
  while IFS= read -r rel; do
    [[ -n $rel ]] || continue
    report "$rel" "filename: matches '$glob' — credential/database files never ship"
  done < <(
    cd "$tree" &&
    find . -path ./.git -prune -o -type f -name "$glob" -print 2>/dev/null |
      sed 's|^\./||' || true
  )
done

# --- verdict -----------------------------------------------------------------
if (( hits )); then
  echo
  echo "publish_check: FAILED — $hits potential leak(s) above. Fix at source in the"
  echo "private repo, re-run scripts/publish_public.sh, and check again."
  exit 1
fi

echo "publish_check: clean (${#CONTENT_PATTERNS[@]} content patterns, tailnet hosts, ${#FILENAME_GLOBS[@]} filename globs, ${#EXCEPTIONS[@]} exceptions)"
