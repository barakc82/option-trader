#!/usr/bin/env bash
#
# io_watchdog.sh -- host-level disk I/O watchdog for a GCP VM running a
# Dockerized algorithmic-trading stack (Python bot + IB Gateway).
#
# WHAT IT DOES
#   Samples /proc/<pid>/io for every process on the host every
#   POLL_INTERVAL_SEC seconds, computes each process's write AND read
#   throughput since its last sample, and tracks -- independently for each
#   metric -- how many CONSECUTIVE seconds each PID has spent above
#   WRITE_THRESHOLD_BYTES_PER_SEC / READ_THRESHOLD_BYTES_PER_SEC (read
#   monitoring can be turned off via ENABLE_READ_MONITORING). Once either
#   metric has been sustained-high for SUSTAIN_DURATION_SEC, it is logged
#   and, if the PID belongs to the monitored Docker container, mitigated per
#   MITIGATION_MODE.
#
# WHY /proc INSTEAD OF pidstat/iotop
#   /proc is memory-backed (procfs), so reading it never itself blocks on
#   disk I/O -- important for a watchdog whose entire job is to keep
#   functioning while the disk is under duress. pidstat/iotop also need to
#   be installed (sysstat / iotop packages) and their text output is more
#   fragile to parse across distro versions. /proc/<pid>/io's write_bytes
#   and read_bytes fields are the same "bytes actually issued to the block
#   layer" figures iotop's DISK READ/DISK WRITE columns are built from.
#
# SAFETY DEFAULTS
#   DRY_RUN=true and MITIGATION_MODE=log_only ship as the defaults. This is
#   deliberate: this script can restart a LIVE trading container. Flip these
#   only after you've watched the log for a while and are confident the
#   thresholds are tuned for your workload. See the accompanying README for
#   a recommended rollout order (log_only -> soft/dry-run -> soft/live ->
#   hard/live).
#
# Deploy as a systemd service (see io-watchdog.service in this directory).
set -uo pipefail

# =============================================================================
# CONFIGURATION -- edit these for your environment
# =============================================================================

# --- What to watch -----------------------------------------------------------
TARGET_CONTAINER_NAME="option-trader"     # `docker ps --format '{{.Names}}'`

# --- Thresholds ---------------------------------------------------------------
POLL_INTERVAL_SEC=5                        # sampling cadence
SUSTAIN_DURATION_SEC=60                    # must stay above threshold this long
WRITE_THRESHOLD_BYTES_PER_SEC=$((30 * 1024 * 1024))   # 30 MB/s

# Read monitoring is tracked with its own independent sustain timer (not
# OR'd into the write one) -- a write burst immediately followed by an
# unrelated read burst should not read as one continuous 60s event. Set
# ENABLE_READ_MONITORING=false to watch writes only (the original scope).
ENABLE_READ_MONITORING=true
READ_THRESHOLD_BYTES_PER_SEC=$((30 * 1024 * 1024))    # 30 MB/s

# --- Mitigation ----------------------------------------------------------------
# log_only : log the culprit, take no action (safe default)
# soft     : throttle the container's block-device I/O via cgroups v2 io.max
# hard     : `docker restart` the container
MITIGATION_MODE="log_only"

# When true, every mitigating action is logged as "[DRY_RUN] would ..." and
# NOT actually executed. Detection/logging still runs at full fidelity.
DRY_RUN=true

# Path A (soft) tuning. Both limits are applied together regardless of
# whether the trigger was a write spike, a read spike, or both -- simpler
# and safer than branching the throttle on which metric fired, and a
# generous read limit while write-throttled costs little.
SOFT_THROTTLE_WBPS=$((5 * 1024 * 1024))    # clamp writes to 5 MB/s while throttled
SOFT_THROTTLE_RBPS=$((5 * 1024 * 1024))    # clamp reads to 5 MB/s while throttled
THROTTLE_DURATION_SEC=300                  # auto-revert to "max" after this long

# Block device the container's writes actually land on, as "MAJ:MIN"
# (required by cgroups v2 io.max). Find it with:
#   findmnt -no MAJ:MIN --target /var/lib/docker
#   lsblk -o NAME,MAJ:MIN,MOUNTPOINT
# Leave empty to auto-detect via findmnt at startup (recommended: set it
# explicitly instead -- an auto-detect that guesses wrong silently no-ops).
DEVICE_MAJMIN=""

# Don't fire another mitigation for this container within this many seconds
# of the last one -- prevents restart-looping a live trading process.
COOLDOWN_AFTER_ACTION_SEC=300

# --- Docker interaction safety --------------------------------------------
# All docker CLI calls are wrapped in `timeout` so a wedged dockerd (which
# is a realistic side-effect of the very I/O storm we're watching for)
# cannot hang the watchdog itself.
DOCKER_CMD_TIMEOUT=5
DOCKER_RESTART_TIMEOUT=30

# How often to re-resolve the container's id via `docker inspect` once it's
# been cached at least once (catches the container being recreated with the
# same name but a new id). The FIRST resolution is retried every poll until
# it succeeds -- container-membership detection is unavailable until then.
CONTAINER_ID_REFRESH_SEC=600

# If a PID's container membership can't be determined (container id not yet
# resolved, or its /proc/<pid>/cgroup is unreadable) -- classified "unknown",
# never silently treated as "no" -- should mitigation still be attempted?
# false errs toward not touching a process we can't confirm is ours; true
# errs toward protecting the trading container even without certainty (a
# real risk: an unrelated host-wide I/O storm hitting some other process at
# the same moment id-resolution happens to be down would then also throttle/
# restart the container). "unknown" should be rare in steady state -- see
# module docstring -- so the conservative default costs little.
MITIGATE_ON_UNKNOWN_MEMBERSHIP=false

# --- Logging -----------------------------------------------------------------
LOG_FILE="/var/log/io_watchdog.log"

# Log a "still alive" status line at least this often even when nothing
# crosses the alert threshold, so the log can distinguish "quietly healthy"
# from "silently stuck" without spamming a line every poll. Includes the
# aggregate host write/read throughput from the most recent poll (summed
# across every process already sampled that round -- no extra /proc reads
# needed). Set to 0 to disable.
HEARTBEAT_INTERVAL_SEC=600

# --- Testability ---------------------------------------------------------
# Root of the procfs tree to scan. Always "/proc" in production; overridable
# via the IO_WATCHDOG_PROC_ROOT environment variable so the sampling logic
# can be exercised against a synthetic directory tree in tests without a
# real Linux /proc.
PROC_ROOT="${IO_WATCHDOG_PROC_ROOT:-/proc}"

# --- Exclusions --------------------------------------------------------------
# Space-separated regexes (matched against the full cmdline) to never act on
# even if they look like a container process -- e.g. your own backup jobs.
EXCLUDE_CMD_PATTERNS=()

# =============================================================================
# END CONFIGURATION
# =============================================================================

SCRIPT_NAME="io_watchdog"
LOCK_FILE="/var/run/${SCRIPT_NAME}.lock"

# ---- single-instance lock (systemd already enforces this, but be defensive
#      against a manual double-launch during testing) -----------------------
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
    echo "Another instance is already running (lock: $LOCK_FILE). Exiting." >&2
    exit 1
fi

# ---- state (associative arrays keyed by PID / container name) -------------
declare -A PREV_WRITE_BYTES
declare -A PREV_READ_BYTES
declare -A PREV_TS
declare -A CONSEC_HIGH_WRITE_SEC
declare -A CONSEC_HIGH_READ_SEC
declare -A LAST_ACTION_TS
declare -A THROTTLE_EXPIRY_TS
declare -A THROTTLE_CGROUP_PATH

CACHED_CONTAINER_ID=""
LAST_CONTAINER_ID_REFRESH_TS=0
LAST_HEARTBEAT_TS=0
RUNNING=true

log() {
    # timestamped, single-line log entries to LOG_FILE (and stdout, which
    # systemd/journald captures too).
    local line
    line="$(date -u '+%Y-%m-%dT%H:%M:%SZ') [$$] $*"
    echo "$line" | tee -a "$LOG_FILE"
}

on_term() {
    log "Received stop signal, shutting down."
    RUNNING=false
}
trap on_term SIGTERM SIGINT

# ---- helpers ----------------------------------------------------------------

detect_device_majmin() {
    local majmin
    majmin=$(timeout 5 findmnt -no MAJ:MIN --target /var/lib/docker 2>/dev/null)
    if [[ -z "$majmin" ]]; then
        majmin=$(timeout 5 findmnt -no MAJ:MIN --target / 2>/dev/null)
    fi
    printf '%s' "$majmin"
}

is_excluded_cmd() {
    local cmdline="$1"
    local pattern
    for pattern in "${EXCLUDE_CMD_PATTERNS[@]:-}"; do
        [[ -z "$pattern" ]] && continue
        [[ "$cmdline" =~ $pattern ]] && return 0
    done
    return 1
}

get_cmdline() {
    local pid="$1" comm cmdline
    comm=$(cat "$PROC_ROOT/$pid/comm" 2>/dev/null) || comm="?"
    cmdline=$(tr '\0' ' ' < "$PROC_ROOT/$pid/cmdline" 2>/dev/null)
    if [[ -z "$cmdline" ]]; then
        printf '[%s]' "$comm"
    else
        printf '%s' "$cmdline"
    fi
}

# ---- container-membership detection --------------------------------------
#
# Deliberately does NOT shell out to `docker top`/`docker ps` on the hot
# per-alert path. The condition this watchdog exists to catch -- dockerd
# blocked on the same disk I/O storm -- is exactly the condition under which
# a live docker CLI call is slow or hangs. If classification depended on
# such a call succeeding in real time, it would silently fail open (every
# offending PID reads as "not part of the container", nothing gets
# mitigated) at precisely the moment mitigation matters most.
#
# Instead, the container's ID is resolved via `docker inspect` and CACHED
# (once at startup, refreshed on a slow cadence thereafter). Classifying a
# PID then only needs to read /proc/<pid>/cgroup and check whether the
# cached ID appears in it -- procfs is memory-backed, so this never blocks
# on disk I/O regardless of how stuck dockerd or the disk itself is.

resolve_container_id() {
    local id
    id=$(timeout "$DOCKER_CMD_TIMEOUT" docker inspect --format '{{.Id}}' "$TARGET_CONTAINER_NAME" 2>/dev/null)
    if [[ -n "$id" ]]; then
        if [[ "$id" != "$CACHED_CONTAINER_ID" ]]; then
            if [[ -n "$CACHED_CONTAINER_ID" ]]; then
                log "Container '$TARGET_CONTAINER_NAME' id changed (recreated?): ${CACHED_CONTAINER_ID:0:12} -> ${id:0:12}"
            else
                log "Resolved container '$TARGET_CONTAINER_NAME' id=${id:0:12}"
            fi
            CACHED_CONTAINER_ID="$id"
        fi
        return 0
    fi
    if [[ -z "$CACHED_CONTAINER_ID" ]]; then
        log "WARNING: could not resolve container id for '$TARGET_CONTAINER_NAME' (docker inspect failed/timed out, or container not running). Container-membership detection is unavailable until this succeeds -- retrying every poll."
    else
        log "WARNING: docker inspect failed/timed out refreshing '$TARGET_CONTAINER_NAME'; continuing with cached id=${CACHED_CONTAINER_ID:0:12}. Membership detection still works off this cached id via /proc, independent of whether docker itself is currently responsive."
    fi
    return 1
}

# Classifies a PID as "yes" (matches the cached container id), "no"
# (readable cgroup that doesn't match), or "unknown" (no cached id yet, or
# /proc/<pid>/cgroup unreadable -- e.g. the PID just exited). Callers must
# handle "unknown" explicitly rather than treating it as "no": that
# collapse is exactly the bug this design replaces.
classify_pid() {
    local pid="$1"
    if [[ -z "$CACHED_CONTAINER_ID" ]]; then
        printf 'unknown'
        return
    fi
    local line
    line=$(cat "$PROC_ROOT/$pid/cgroup" 2>/dev/null)
    if [[ -z "$line" ]]; then
        printf 'unknown'
        return
    fi
    if [[ "$line" == *"$CACHED_CONTAINER_ID"* ]]; then
        printf 'yes'
    else
        printf 'no'
    fi
}

# Resolves a PID's own cgroup v2 unified path directly from /proc/<pid>/cgroup
# -- no docker CLI call needed. Used to locate io.max for soft throttling.
cgroup_path_for_pid() {
    local pid="$1" line relpath
    line=$(cat "$PROC_ROOT/$pid/cgroup" 2>/dev/null) || return 1
    [[ "$line" == 0::* ]] || return 1   # not cgroups v2 unified, or unreadable
    relpath="${line#0::}"
    printf '/sys/fs/cgroup%s' "$relpath"
}

in_cooldown() {
    local now last
    now=$(date +%s)
    last=${LAST_ACTION_TS[$TARGET_CONTAINER_NAME]:-0}
    (( now - last < COOLDOWN_AFTER_ACTION_SEC ))
}

mark_action_taken() {
    LAST_ACTION_TS[$TARGET_CONTAINER_NAME]=$(date +%s)
}

# ---- mitigation paths --------------------------------------------------------

apply_soft_throttle() {
    local pid="$1"
    if [[ -z "$DEVICE_MAJMIN" ]]; then
        log "ERROR: DEVICE_MAJMIN is not set and auto-detect failed; cannot apply soft throttle."
        return 1
    fi
    local cgroup_path io_max_file
    cgroup_path=$(cgroup_path_for_pid "$pid") || {
        log "ERROR: could not resolve cgroup path for PID $pid (its /proc/$pid/cgroup is unreadable -- process may have already exited, or this host isn't on cgroups v2 unified)."
        return 1
    }
    io_max_file="$cgroup_path/io.max"

    local limits="$DEVICE_MAJMIN rbps=$SOFT_THROTTLE_RBPS wbps=$SOFT_THROTTLE_WBPS"

    if $DRY_RUN; then
        log "[DRY_RUN] would write '$limits' to $io_max_file"
        return 0
    fi
    if [[ ! -w "$io_max_file" ]]; then
        log "ERROR: $io_max_file is not writable -- is the io controller delegated to this cgroup? (check /sys/fs/cgroup/cgroup.subtree_control up the tree, and that this script runs as root)"
        return 1
    fi

    if echo "$limits" > "$io_max_file" 2>>"$LOG_FILE"; then
        log "Applied soft throttle: $limits -> $io_max_file (auto-revert in ${THROTTLE_DURATION_SEC}s)"
        THROTTLE_EXPIRY_TS[$TARGET_CONTAINER_NAME]=$(( $(date +%s) + THROTTLE_DURATION_SEC ))
        # Cached at apply time -- revert doesn't need to re-derive it later
        # (the offending PID may itself be gone by then), and needs no
        # docker CLI call at all.
        THROTTLE_CGROUP_PATH[$TARGET_CONTAINER_NAME]="$cgroup_path"
    else
        log "ERROR: failed writing io.max to $io_max_file"
        return 1
    fi
}

revert_expired_throttles() {
    local now name cgroup_path io_max_file
    now=$(date +%s)
    for name in "${!THROTTLE_EXPIRY_TS[@]}"; do
        if (( now >= THROTTLE_EXPIRY_TS[$name] )); then
            cgroup_path="${THROTTLE_CGROUP_PATH[$name]:-}"
            if [[ -z "$cgroup_path" ]]; then
                log "WARNING: throttle for '$name' expired but no cached cgroup path was recorded to revert it (should not happen -- it's set when the throttle is applied)."
                unset 'THROTTLE_EXPIRY_TS[$name]' 'THROTTLE_CGROUP_PATH[$name]'
                continue
            fi
            io_max_file="$cgroup_path/io.max"
            if $DRY_RUN; then
                log "[DRY_RUN] would revert throttle on $io_max_file (write '$DEVICE_MAJMIN rbps=max wbps=max')"
            elif echo "$DEVICE_MAJMIN rbps=max wbps=max" > "$io_max_file" 2>>"$LOG_FILE"; then
                log "Reverted throttle for '$name' ($io_max_file back to rbps=max wbps=max)"
            else
                log "ERROR: failed to revert throttle on $io_max_file"
            fi
            unset 'THROTTLE_EXPIRY_TS[$name]' 'THROTTLE_CGROUP_PATH[$name]'
        fi
    done
}

apply_hard_restart() {
    if $DRY_RUN; then
        log "[DRY_RUN] would run: docker restart $TARGET_CONTAINER_NAME"
        return 0
    fi
    log "Executing hard mitigation: docker restart $TARGET_CONTAINER_NAME"
    # `docker restart` sends SIGTERM (honoring the app's own shutdown
    # handling), waits up to its --time grace period (default 10s), then
    # SIGKILLs and starts a fresh container -- not an instant kill -9.
    if timeout "$DOCKER_RESTART_TIMEOUT" docker restart "$TARGET_CONTAINER_NAME" >>"$LOG_FILE" 2>&1; then
        log "docker restart succeeded for $TARGET_CONTAINER_NAME"
    else
        log "ERROR: docker restart FAILED or timed out for $TARGET_CONTAINER_NAME"
        return 1
    fi
}

mitigate() {
    local pid="$1" cmdline="$2" wbps_human="$3"

    if in_cooldown; then
        log "Sustained high write I/O from PID $pid ($cmdline) at ${wbps_human} -- action suppressed, '$TARGET_CONTAINER_NAME' is within its ${COOLDOWN_AFTER_ACTION_SEC}s post-action cooldown."
        return
    fi

    case "$MITIGATION_MODE" in
        log_only)
            log "MITIGATION_MODE=log_only -- no action taken against '$TARGET_CONTAINER_NAME'."
            ;;
        soft)
            apply_soft_throttle "$pid" && mark_action_taken
            ;;
        hard)
            apply_hard_restart && mark_action_taken
            ;;
        *)
            log "ERROR: unknown MITIGATION_MODE '$MITIGATION_MODE' -- taking no action."
            ;;
    esac
}

# ---- one sampling pass -------------------------------------------------------

human_bps() {
    # Only ever called for ALERT/HEARTBEAT/startup lines -- never in the
    # per-PID hot polling loop -- so a single awk fork per call is fine.
    # Sub-1MB/s rates are common for a bot that mostly talks over network
    # sockets (not disk I/O) and only writes to disk occasionally; plain
    # integer MB/s would print "0 MB/s" for e.g. 200 KB/s and make a real,
    # nonzero rate look like silence, so this shows KB/s below 1 MB/s and
    # two decimal places of MB/s above it.
    local bytes="$1"
    awk -v b="$bytes" 'BEGIN {
        if (b < 1024*1024) printf "%.0f KB/s", b/1024
        else printf "%.2f MB/s", b/1024/1024
    }'
}

sample_once() {
    local now
    now=$(date +%s)

    # Keep the cached container id fresh, without ever blocking the poll on
    # it: retry every poll until the first successful resolution, then only
    # on the slow CONTAINER_ID_REFRESH_SEC cadence. classify_pid() below
    # works entirely off whatever is currently cached, so a failure here
    # never stalls detection -- see the container-membership section above.
    if [[ -z "$CACHED_CONTAINER_ID" ]] || (( now - LAST_CONTAINER_ID_REFRESH_TS >= CONTAINER_ID_REFRESH_SEC )); then
        resolve_container_id
        LAST_CONTAINER_ID_REFRESH_TS=$now
    fi

    # Build the set of currently-live PIDs so we can prune stale state for
    # PIDs that have exited (and avoid misreading a reused PID's counters
    # as a continuation of a dead process's).
    declare -A LIVE_PID=()

    # Aggregate host throughput this poll, for the heartbeat line -- summed
    # across every PID with a real (non-baseline) delta this round.
    local total_write_bps=0 total_read_bps=0 tracked_count=0

    local pid_path pid write_bytes read_bytes io_line
    for pid_path in "$PROC_ROOT"/[0-9]*; do
        pid="${pid_path##*/}"
        # <proc>/<pid>/io can vanish mid-read (process exited) or be
        # unreadable (permissions on a handful of kernel/root-owned
        # threads even as root, rare); skip gracefully either way. Checked
        # up front so the read loop below never has to fork a `cat` --
        # `read` is a bash builtin, so parsing this file costs zero
        # subprocess forks, which matters when this runs every few seconds
        # against every PID on the host.
        [[ -r "$PROC_ROOT/$pid/io" ]] || continue
        LIVE_PID[$pid]=1

        write_bytes=""
        read_bytes=""
        while IFS= read -r io_line; do
            case "$io_line" in
                write_bytes:*) write_bytes="${io_line#write_bytes: }" ;;
                read_bytes:*)  read_bytes="${io_line#read_bytes: }" ;;
            esac
        done < "$PROC_ROOT/$pid/io" 2>/dev/null
        [[ -n "$write_bytes" && -n "$read_bytes" ]] || continue

        if [[ -z "${PREV_TS[$pid]:-}" ]]; then
            # First time we've seen this PID: record a baseline only, don't
            # evaluate a delta against nothing (that would look like an
            # instant multi-GB/s spike for every long-lived process on
            # watchdog startup).
            PREV_WRITE_BYTES[$pid]=$write_bytes
            PREV_READ_BYTES[$pid]=$read_bytes
            PREV_TS[$pid]=$now
            CONSEC_HIGH_WRITE_SEC[$pid]=0
            CONSEC_HIGH_READ_SEC[$pid]=0
            continue
        fi

        local elapsed
        elapsed=$(( now - PREV_TS[$pid] ))
        if (( elapsed <= 0 )); then
            continue  # clock hasn't advanced (or went backwards); skip this pid this round
        fi

        local delta_write delta_read write_bps read_bps
        delta_write=$(( write_bytes - PREV_WRITE_BYTES[$pid] ))
        delta_read=$(( read_bytes - PREV_READ_BYTES[$pid] ))
        # Counters are monotonically non-decreasing during a process's
        # life; a negative delta means the PID was reused. Treat as a
        # fresh baseline rather than a (nonsensical) negative rate.
        if (( delta_write < 0 || delta_read < 0 )); then
            PREV_WRITE_BYTES[$pid]=$write_bytes
            PREV_READ_BYTES[$pid]=$read_bytes
            PREV_TS[$pid]=$now
            CONSEC_HIGH_WRITE_SEC[$pid]=0
            CONSEC_HIGH_READ_SEC[$pid]=0
            continue
        fi

        write_bps=$(( delta_write / elapsed ))
        read_bps=$(( delta_read / elapsed ))
        total_write_bps=$(( total_write_bps + write_bps ))
        total_read_bps=$(( total_read_bps + read_bps ))
        tracked_count=$(( tracked_count + 1 ))

        PREV_WRITE_BYTES[$pid]=$write_bytes
        PREV_READ_BYTES[$pid]=$read_bytes
        PREV_TS[$pid]=$now

        if (( write_bps >= WRITE_THRESHOLD_BYTES_PER_SEC )); then
            CONSEC_HIGH_WRITE_SEC[$pid]=$(( ${CONSEC_HIGH_WRITE_SEC[$pid]:-0} + elapsed ))
        else
            CONSEC_HIGH_WRITE_SEC[$pid]=0
        fi

        if $ENABLE_READ_MONITORING && (( read_bps >= READ_THRESHOLD_BYTES_PER_SEC )); then
            CONSEC_HIGH_READ_SEC[$pid]=$(( ${CONSEC_HIGH_READ_SEC[$pid]:-0} + elapsed ))
        else
            CONSEC_HIGH_READ_SEC[$pid]=0
        fi

        # Tracked independently (not OR'd into one counter): a write burst
        # immediately followed by an unrelated read burst should not read as
        # one continuous sustained-high event.
        local write_sustained=0 read_sustained=0
        (( CONSEC_HIGH_WRITE_SEC[$pid] >= SUSTAIN_DURATION_SEC )) && write_sustained=1
        if $ENABLE_READ_MONITORING && (( CONSEC_HIGH_READ_SEC[$pid] >= SUSTAIN_DURATION_SEC )); then
            read_sustained=1
        fi

        if (( write_sustained == 0 && read_sustained == 0 )); then
            continue
        fi

        local cmdline
        cmdline=$(get_cmdline "$pid")
        if is_excluded_cmd "$cmdline"; then
            continue
        fi

        local wbps_h rbps_h reason_str
        wbps_h=$(human_bps "$write_bps")
        rbps_h=$(human_bps "$read_bps")
        reason_str=""
        if (( write_sustained )); then
            reason_str="write >= $(human_bps "$WRITE_THRESHOLD_BYTES_PER_SEC") for ${CONSEC_HIGH_WRITE_SEC[$pid]}s"
        fi
        if (( read_sustained )); then
            [[ -n "$reason_str" ]] && reason_str+=", "
            reason_str+="read >= $(human_bps "$READ_THRESHOLD_BYTES_PER_SEC") for ${CONSEC_HIGH_READ_SEC[$pid]}s"
        fi

        log "ALERT: PID $pid ($cmdline) sustained I/O -- ${reason_str}. current write=${wbps_h} read=${rbps_h}"

        local membership
        membership=$(classify_pid "$pid")
        case "$membership" in
            yes)
                log "PID $pid belongs to monitored container '$TARGET_CONTAINER_NAME'."
                mitigate "$pid" "$cmdline" "write=${wbps_h} read=${rbps_h}"
                ;;
            no)
                log "PID $pid is a host process outside '$TARGET_CONTAINER_NAME' -- logging only, no container action taken."
                ;;
            unknown)
                if $MITIGATE_ON_UNKNOWN_MEMBERSHIP; then
                    log "WARNING: could not determine whether PID $pid belongs to '$TARGET_CONTAINER_NAME' (container id not yet resolved, or its cgroup info is unreadable). MITIGATE_ON_UNKNOWN_MEMBERSHIP=true -- mitigating defensively."
                    mitigate "$pid" "$cmdline" "write=${wbps_h} read=${rbps_h}"
                else
                    log "WARNING: could not determine whether PID $pid belongs to '$TARGET_CONTAINER_NAME' (container id not yet resolved, or its cgroup info is unreadable). MITIGATE_ON_UNKNOWN_MEMBERSHIP=false -- no action taken."
                fi
                ;;
        esac

        # Don't re-alert every single second this PID stays hot; give it
        # room to either resolve or hit the mitigation's own cooldown.
        CONSEC_HIGH_WRITE_SEC[$pid]=0
        CONSEC_HIGH_READ_SEC[$pid]=0
    done

    # Prune state for PIDs that are no longer alive.
    local tracked
    for tracked in "${!PREV_TS[@]}"; do
        if [[ -z "${LIVE_PID[$tracked]:-}" ]]; then
            unset 'PREV_TS[$tracked]' 'PREV_WRITE_BYTES[$tracked]' 'PREV_READ_BYTES[$tracked]' \
                  'CONSEC_HIGH_WRITE_SEC[$tracked]' 'CONSEC_HIGH_READ_SEC[$tracked]'
        fi
    done

    revert_expired_throttles

    if (( HEARTBEAT_INTERVAL_SEC > 0 && now - LAST_HEARTBEAT_TS >= HEARTBEAT_INTERVAL_SEC )); then
        log "HEARTBEAT: watchdog alive, tracking $tracked_count process(es) with I/O this poll." \
            "aggregate host throughput: write=$(human_bps "$total_write_bps") read=$(human_bps "$total_read_bps")"
        LAST_HEARTBEAT_TS=$now
    fi
}

# ---- entry point --------------------------------------------------------------

main() {
    mkdir -p "$(dirname "$LOG_FILE")"
    touch "$LOG_FILE" 2>/dev/null || { echo "Cannot write to $LOG_FILE" >&2; exit 1; }

    if [[ -z "$DEVICE_MAJMIN" ]]; then
        DEVICE_MAJMIN=$(detect_device_majmin)
        if [[ -n "$DEVICE_MAJMIN" ]]; then
            log "DEVICE_MAJMIN not set; auto-detected $DEVICE_MAJMIN. Set it explicitly in the config if this is wrong."
        else
            log "WARNING: DEVICE_MAJMIN not set and auto-detect failed. Soft throttling (Path A) will be unavailable until it's configured."
        fi
    fi

    # Best-effort at startup -- failure here is not fatal, sample_once()
    # keeps retrying every poll until it succeeds (see resolve_container_id).
    resolve_container_id || true
    LAST_CONTAINER_ID_REFRESH_TS=$(date +%s)

    local read_threshold_desc="disabled"
    $ENABLE_READ_MONITORING && read_threshold_desc="$(human_bps "$READ_THRESHOLD_BYTES_PER_SEC")"
    log "io_watchdog starting. container=$TARGET_CONTAINER_NAME mode=$MITIGATION_MODE dry_run=$DRY_RUN " \
        "write_threshold=$(human_bps "$WRITE_THRESHOLD_BYTES_PER_SEC") read_threshold=$read_threshold_desc " \
        "sustain=${SUSTAIN_DURATION_SEC}s poll=${POLL_INTERVAL_SEC}s device=$DEVICE_MAJMIN " \
        "container_id=${CACHED_CONTAINER_ID:-<unresolved>}"
    # Anchor the heartbeat clock to startup so the first heartbeat fires
    # HEARTBEAT_INTERVAL_SEC from now, not immediately after this line.
    LAST_HEARTBEAT_TS=$(date +%s)

    if [[ "${1:-}" == "--once" ]]; then
        sample_once
        exit 0
    fi

    while $RUNNING; do
        sample_once
        # Run sleep as a backgrounded job and `wait` on it so a delivered
        # SIGTERM/SIGINT interrupts the wait immediately (systemd stop
        # shouldn't have to wait out a full poll interval).
        sleep "$POLL_INTERVAL_SEC" &
        wait $! 2>/dev/null || true
    done

    log "io_watchdog stopped."
}

main "$@"
