#!/usr/bin/env bash
#
# Generate the public snapshot of this repo ("Sidekick") into a sibling tree.
#
#   usage: scripts/publish_public.sh [TARGET]   (TARGET defaults to ../ai-public)
#
# The private repo's history carries personal information and stays private
# permanently. The public repo is a *snapshot*, republished periodically —
# never a filtered history. This script produces that snapshot:
#
#   1. `git ls-files` is the file set, so .gitignore is respected for free and
#      nothing untracked can wander in.
#   2. The denylist below drops what must not ship.
#   3. public/CLAUDE.md is overlaid as CLAUDE.md — the one file that genuinely
#      must differ (the private one carries machine-specific host notes and
#      personal autonomy rules).
#   4. The target is *mirrored*: files are copied in, and files no longer in
#      the set are deleted, so removals propagate on re-publish.
#   5. scripts/publish_check.sh runs on the OUTPUT tree. A hit fails the run.
#
# It never commits and never pushes — review and commit are the human's.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
target="${1:-$repo_root/../ai-public}"

if [[ ! -d $target ]]; then
  echo "publish_public: no such target: $target" >&2
  echo "  create it first (mkdir + git init) — this script will not." >&2
  exit 2
fi
target="$(cd "$target" && pwd)"

if [[ $target == "$repo_root" ]]; then
  echo "publish_public: target is the source repo — refusing." >&2
  exit 2
fi
# The mirror step deletes files. Only ever do that inside a git repo, so an
# accidental target has its own history to recover from.
if [[ ! -e $target/.git ]]; then
  echo "publish_public: $target is not a git repository — refusing to mirror into it." >&2
  exit 2
fi

# --- denylist ----------------------------------------------------------------
# Python `re` regexes (the manifest filter below runs them through python3, so
# lookaheads like the scratch/ rule work), matched against repo-relative paths.
# PRESERVE further down is bash `=~` — keep its entries lookahead-free.
DENY=(
  # scratch/ is the agent's live write root (core/paths.py): edit_file output,
  # Drive/Gmail downloads, the sandbox workspace, logo explorations, design
  # notes. It ships as structure only. hello.txt is the one exception — the
  # smoke-test fixture that tests/test_conventions.py pins.
  '^scratch/(?!hello\.txt$)'
  '^todo\.md$'               # personal backlog
  '^docs/macos-host\.md$'    # host runbook: real tailnet name, home paths, machine state
  '^\.claude/settings\.json$'      # machine-specific tool permissions
  '^\.claude/settings\.local\.json$'
  '^CLAUDE\.md$'             # replaced by public/CLAUDE.md in the overlay step
  '^public/'                 # the overlay source itself, not content
  # The owner-specific half of the leak patterns. Publishing "the surname is X,
  # the mail provider is Y" would give away the association the check protects.
  '^scripts/publish_check\.local$'
  # Real chat captures (a whole session's dialogue) — test_compaction_eval
  # skips the tests that read them when they are absent.
  '^tests/fixtures/compaction/'
  # The raw session inbox and its archive: unreviewed notes written mid-task,
  # too large and too close to the live chats to vet by pattern alone. The
  # curated files (architecture, decisions, gotchas) still ship.
  '^\.claude/learnings(-archive)?\.md$'
)

# --- preserve ----------------------------------------------------------------
# Paths in the TARGET that the mirror must never delete even though they are
# not published. The target is a working checkout as well as a snapshot: it has
# its own Claude Code configuration, which is deliberately denylisted above and
# would otherwise be swept away on every republish. Learned the hard way.
PRESERVE=(
  '^\.claude/settings\.json$'
  '^\.claude/settings\.local\.json$'
  '^\.claude/party\.json$'
  '^\.claude/party/'
  '^\.env$'
  '^\.env\.local$'
)

# --- 1+2. build the manifest -------------------------------------------------
manifest="$(mktemp)"
trap 'rm -f "$manifest"' EXIT

cd "$repo_root"
git ls-files | python3 -c '
import re, sys
deny = [re.compile(p) for p in sys.argv[1:]]
for line in sys.stdin:
    path = line.rstrip("\n")
    if not any(d.search(path) for d in deny):
        print(path)
' "${DENY[@]}" > "$manifest"

# --- 3. overlay --------------------------------------------------------------
if [[ ! -f $repo_root/public/CLAUDE.md ]]; then
  echo "publish_public: public/CLAUDE.md is missing — it is the public overlay." >&2
  exit 2
fi

kept=$(wc -l < "$manifest")
echo "publish_public: $kept files from git ls-files (after denylist) + public/CLAUDE.md -> CLAUDE.md"

# --- 4. mirror ---------------------------------------------------------------
while IFS= read -r rel; do
  mkdir -p "$target/$(dirname "$rel")"
  cp -p "$repo_root/$rel" "$target/$rel"
done < "$manifest"

cp -p "$repo_root/public/CLAUDE.md" "$target/CLAUDE.md"

# launchd does not expand `~` or `$HOME`, so the private plists and wrapper
# carry absolute home paths. Publish them as templates instead. `>` onto the
# file cp just made keeps its mode (the wrapper must stay executable).
while IFS= read -r rel; do
  sed "s|$HOME|/Users/YOU|g" "$repo_root/$rel" > "$target/$rel"
done < <(grep '^scripts/launchd/' "$manifest")

# Delete anything in the target that is no longer published, so removals
# propagate. `.git` is pruned; CLAUDE.md comes from the overlay rather than the
# manifest, so name it; PRESERVE holds the target's own config.
#
# Empty directories left behind are deliberately not pruned — git does not
# track directories, so they reach nothing, and a recursive delete pass here
# would be a much bigger risk than the tidiness is worth.
is_preserved() {
  local pat
  for pat in "${PRESERVE[@]}"; do
    [[ $1 =~ $pat ]] && return 0
  done
  return 1
}

removed=0
while IFS= read -r rel; do
  [[ $rel == CLAUDE.md ]] && continue
  is_preserved "$rel" && continue
  if ! grep -qxF -- "$rel" "$manifest"; then
    rm -- "$target/$rel"
    echo "  removed: $rel"
    removed=$((removed + 1))
  fi
done < <(cd "$target" && find . -path ./.git -prune -o -type f -print | sed 's|^\./||' | sort)

echo "publish_public: mirrored into $target ($removed stale file(s) removed)"

# --- 5. verify ---------------------------------------------------------------
echo
"$repo_root/scripts/publish_check.sh" "$target"

echo
echo "publish_public: done. Nothing has been committed — review the tree, then"
echo "commit in $target by explicit path (never 'git add -A')."
