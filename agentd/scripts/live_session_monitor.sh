#!/bin/bash
# Live-exercise monitor for docs/ops/live-sign-offs.md windows A / B / C.
#
# Read-only. Never writes the DB, never touches GitHub. Safe mid-session.
#
#   scripts/live_session_monitor.sh <issue-number> [outfile]
#
# Window A's acceptance is a *flat* zombie count sampled WHILE A TURN IS RUNNING.
# A sample taken between turns reads 0 on a container accumulating hundreds --
# that reading is how #210 was closed once already. So this samples only while
# the turns table shows an open turn, and it verifies the probe against its own
# case before trusting any 0.
set -u
N="${1:?usage: live_session_monitor.sh <issue-number> [outfile]}"
OUT="${2:-$HOME/.agentd/logs/live-exercise-$N.log}"
# Overridable so the failure paths below can actually be exercised. A monitor
# whose inputs cannot be varied cannot be tested, and this one's output is
# evidence for four sign-offs.
DB="${AGENTD_MONITOR_DB:-$HOME/.agentd/state.db}"
LOGS="${AGENTD_MONITOR_LOG:-$HOME/.agentd/logs/agentd.log}"
CONTAINER="${AGENTD_MONITOR_CONTAINER:-agentd-huozhe-code-workflow}"
POLL_S="${AGENTD_MONITOR_POLL_S:-10}"
SK="huozhe/code-workflow#$N"

say() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*" | tee -a "$OUT"; }

probe_zombies() {
  docker exec "$CONTAINER" sh -c \
    'grep -l "^State:.*Z" /proc/*/status 2>/dev/null | wc -l' 2>/dev/null | tr -d ' \r'
}

# --- probe self-check: prove it can see a zombie before believing a 0 --------
# The runbook's caveat is pinned to session-runner:1.4.0; we run 1.5.0, so the
# probe is unverified on this image until this passes.
verify_probe() {
  local before after settled i
  before=$(probe_zombies); [ -n "$before" ] || { say "PROBE: container not up yet"; return 1; }
  # Must be a parent that never wait()s while we measure. `sh -c "sleep 0 & ..."`
  # does NOT work: sh reaps its own background child, so no zombie is created and
  # the probe correctly reports 0 -- which reads as a broken probe.
  #
  # It must also REAP before window A starts sampling. The first version slept 20 s
  # without waiting, so every WIN-A sample in that window carried +1 from the guard
  # itself -- a null-result guard biasing the series it guards. Measured: 1, 1, 1
  # at +2 s, +5 s, +9 s. Raised by the Architect on #249.
  docker exec -d "$CONTAINER" python3 -c '
import os, time
if os.fork() == 0:
    os._exit(0)
time.sleep(3)
os.wait()
' 2>/dev/null
  sleep 1
  after=$(probe_zombies)
  # Do not return until the guard'"'"'s own zombie is gone, so sampling starts clean.
  settled=""
  for i in 1 2 3 4 5 6 7 8; do
    sleep 1
    settled=$(probe_zombies)
    [ "${settled:-1}" = "$before" ] && break
  done
  if [ "${after:-0}" -gt "${before:-0}" ]; then
    if [ "${settled:-1}" != "$before" ]; then
      say "PROBE OK but NOT SETTLED: still $settled vs baseline $before — later counts carry the guard"
    else
      say "PROBE OK: sees a zombie ($before -> $after), reaped back to $settled"
    fi
    return 0
  fi
  say "PROBE UNVERIFIED: could not make it report one ($before -> $after). A 0 below is a NULL RESULT, not evidence."
  return 1
}

turn_open() {
  sqlite3 "$DB" "SELECT COUNT(*) FROM turns WHERE session_key='$SK' AND started_at IS NOT NULL AND ended_at IS NULL;" 2>/dev/null
}
sess_row() {
  sqlite3 "$DB" "SELECT state||' turns='||turn_count||' silent='||silent_turns||' design_pr='||COALESCE(design_pr,'-')||' feature_pr='||COALESCE(feature_pr,'-') FROM sessions WHERE issue_num=$N;" 2>/dev/null
}

# Match the strings the runbook itself greps for, not a description of the windows.
# Three of #214's and #212's load-bearing lines carry NO session key, so scoping
# them to $SK matched nothing: `merge_auth superseded` (session_loop.py:706) has
# delivery id and pr only; `route defer id=` (:870) likewise, and it is #214's
# "no id past ~20/hour"; and `reconcile pass ... synthesized=0 attached=1
# probe_skipped=0` IS #212's evidence, which a `synthesized=[1-9]` filter drops.
# Raised by the Architect on #249 after piping the real lines at the old regex.
TAIL_RE="fsm $SK|turn progress|author-sent PR event|merge_auth superseded|route defer id=|unauthorized Feature PR merge|reconcile pass|$SK.*(escalat|paused)"

say "=== live exercise #$N — monitor start (read-only) ==="
say "windows: A=#210 (zombies, MID-TURN, flat not small) B=#214 (rework round) C=#209/#173 (synthesised review)"
PROBE_OK=1; LAST_STATE=""; LAST_LOG_LINE=0; MAXZ=0; SAMPLES=0
[ -f "$LOGS" ] && LAST_LOG_LINE=$(wc -l < "$LOGS" | tr -d ' ')

while :; do
  # session state transitions
  row=$(sess_row)
  if [ -n "$row" ] && [ "$row" != "$LAST_STATE" ]; then
    say "STATE  $row"
    LAST_STATE="$row"
  fi

  # window A — only while a turn is genuinely open.
  # A failed sqlite read returns empty, and `[ "" -gt 0 ]` is a shell error, not
  # a false. Read it explicitly: an unreadable DB must not look like a quiet
  # session, which is the shape that made an earlier poll of mine treat a TLS
  # timeout as a hit.
  open_turns=$(turn_open)
  case "$open_turns" in ''|*[!0-9]*) say "WARN   turns query unreadable — not sampling"; open_turns=0;; esac
  if [ "$open_turns" -gt 0 ]; then
    if [ "$PROBE_OK" -ne 0 ]; then verify_probe && PROBE_OK=0; fi
    z=$(probe_zombies)
    case "$z" in ''|*[!0-9]*) z="";; esac
    if [ -n "$z" ]; then
      SAMPLES=$((SAMPLES+1))
      [ "$z" -gt "$MAXZ" ] && MAXZ=$z
      say "WIN-A  mid-turn zombies=$z (samples=$SAMPLES max=$MAXZ)"
    fi
  fi

  # new gateway log lines worth seeing
  if [ -f "$LOGS" ]; then
    now=$(wc -l < "$LOGS" 2>/dev/null | tr -d ' ')
    case "$now" in ''|*[!0-9]*) now="$LAST_LOG_LINE";; esac
    # The log rotates at ~1 MB and a live session produces plenty. A line-count
    # cursor silently stops matching after a rotation -- the same failure as
    # grepping agentd.log instead of agentd.log* (#239), and it fails quiet: no
    # lines, which reads as a calm session. Shrink means rotated; restart at 0.
    if [ "$now" -lt "$LAST_LOG_LINE" ]; then
      # Resetting to 0 reads the NEW file and silently drops everything between
      # the cursor and EOF of the file that just rotated out. That is the #239
      # failure again -- agentd.log when the evidence is in agentd.log.1 -- and it
      # is quiet. Drain the remainder first. Raised by the Architect on #249.
      if [ -f "$LOGS.1" ]; then
        old_end=$(wc -l < "$LOGS.1" 2>/dev/null | tr -d ' ')
        case "$old_end" in ''|*[!0-9]*) old_end=0;; esac
        if [ "$old_end" -gt "$LAST_LOG_LINE" ]; then
          say "NOTE   draining $((old_end-LAST_LOG_LINE)) unread line(s) from $LOGS.1"
          sed -n "$((LAST_LOG_LINE+1)),${old_end}p" "$LOGS.1" | grep -E "$TAIL_RE" \
            | while IFS= read -r l; do printf '%s LOG(r) %s\n' "$(date -u +%H:%M:%S)" "$l" | tee -a "$OUT" >/dev/null; done
        fi
      else
        say "WARN   log rotated but $LOGS.1 not found — unread tail is lost"
      fi
      say "NOTE   gateway log rotated (had $LAST_LOG_LINE lines, now $now) — cursor reset"
      LAST_LOG_LINE=0
    fi
    if [ "$now" -gt "$LAST_LOG_LINE" ]; then
      sed -n "$((LAST_LOG_LINE+1)),${now}p" "$LOGS" \
        | grep -E "$TAIL_RE" \
        | while IFS= read -r l; do printf '%s LOG    %s\n' "$(date -u +%H:%M:%S)" "$l" | tee -a "$OUT" >/dev/null; done
      LAST_LOG_LINE=$now
    fi
  fi
  sleep "$POLL_S"
done
