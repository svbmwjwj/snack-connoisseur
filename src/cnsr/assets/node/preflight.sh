#!/usr/bin/env bash
# preflight.sh <ssh_port>
#
# Looks at a server before cnsr changes anything and prints key=value facts on stdout.
# Strictly read-only: it runs no command that writes or starts anything (docker is asked only
# when its service already runs, so a socket-activated dockerd is not woken), and an optional
# tool that is missing (docker, ufw, nft, iptables-save, ...) means "nothing there", never an
# error. The one tool it cannot do without is ss (see read_listeners).
# src/cnsr/server/preflight.py reads the facts and decides; this script only reports.
#
# The script arrives on stdin of `bash -s`, so no command may read stdin: main runs with
# stdin on /dev/null. It exits 0 after printing the facts, or 1 with a message on stderr when
# ss is unusable; it never exits 255, which the caller would take for an SSH failure.
# The ssh port argument is accepted for symmetry with the other scripts and is not used here.
# It must also parse under bash 3.2 (macOS).
#
# Facts, in this order:
#   os_id, os_version, arch, user
#   listen   (repeated) local address of every listening TCP socket, as ss prints it
#   udp_listen (repeated) local address of every UDP socket bound for listening
#   containers           running docker containers
#   fw       (repeated) ufw rules when ufw is active, else one summary line each for nft and
#            iptables when they hold more than an empty default ruleset
#   admin_exists         0 or 1
#   port_line (repeated) uncommented Port lines of sshd_config, then of sshd_config.d/*.conf
#            (cnsr's own 10-cnsr.conf excluded)
#   ipv6                 first stable global IPv6 address, empty when none

export LC_ALL=C
# No pathname expansion: unquoted `words=($line)` below must only split on whitespace.
set -f
PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"
# The environment variables are test seams; nothing on a server sets them.
OS_RELEASE=${PREFLIGHT_OS_RELEASE:-/etc/os-release}
SSHD_CONFIG=${PREFLIGHT_SSHD_CONFIG:-/etc/ssh/sshd_config}
SSHD_CONFIG_D=${PREFLIGHT_SSHD_CONFIG_D:-/etc/ssh/sshd_config.d}
CNSR_DROP_IN=10-cnsr.conf
PROBE_SECONDS=20

# emit <key> <value>: one fact per line, so the value must stay on one line.
emit() {
    local value=$2
    value=${value//$'\r'/}
    value=${value//$'\n'/ }
    printf '%s=%s\n' "$1" "$value"
}

# probe <command...>: run a read-only probe, with a time limit where timeout(1) exists.
probe() {
    if command -v timeout >/dev/null 2>&1; then
        timeout "$PROBE_SECONDS" "$@"
    else
        "$@"
    fi
}

# The file is read as text, never sourced.
os_release() {
    local id="" version="" line value
    if [ -r "$OS_RELEASE" ]; then
        while IFS= read -r line || [ -n "$line" ]; do
            line=${line%$'\r'}
            value=${line#*=}
            value=${value#[\"\']}
            value=${value%[\"\']}
            case "$line" in
                ID=*) id=$value ;;
                VERSION_ID=*) version=$value ;;
            esac
        done <"$OS_RELEASE"
    fi
    emit os_id "$id"
    emit os_version "$version"
}

# The 443 and other-service checks are void without the listener lists, so a server where ss
# is missing, fails or times out stops the preflight here: a message on stderr, exit 1.
read_listeners() {
    if ! LISTENING=$(probe ss -tln 2>/dev/null) || ! LISTENING_UDP=$(probe ss -uln 2>/dev/null); then
        echo "无法读取服务器的监听端口（ss 不存在、执行失败或超时），无法确认 443 等端口是否空闲，已停止体检。" >&2
        exit 1
    fi
}

# listeners <key> <state> <ss output>: one fact per socket in <state>.
listeners() {
    local state address
    printf '%s\n' "$3" | while read -r state _ _ address _; do
        if [ "$state" = "$2" ]; then
            emit "$1" "$address"
        fi
    done
}

# docker ps would start a socket-activated dockerd. With systemd, only a docker service that
# already runs is asked; a stopped one has no containers running.
containers() {
    local count=0
    if ! command -v systemctl >/dev/null 2>&1 || probe systemctl is-active --quiet docker 2>/dev/null; then
        count=$(probe docker ps -q 2>/dev/null | grep -c .)
    fi
    emit containers "${count:-0}"
}

# Prints the rules and succeeds when ufw is active; fails (printing nothing) otherwise.
ufw_rules() {
    local line words active=0 in_rules=0
    while IFS= read -r line; do
        case "$line" in
            "Status: active"*) active=1 ;;
            --*) in_rules=1 ;;
            *)
                words=($line)
                if [ "$in_rules" = 1 ] && [ "${#words[@]}" -gt 0 ]; then
                    emit fw "${words[*]}"
                fi
                ;;
        esac
    done < <(probe ufw status 2>/dev/null)
    [ "$active" = 1 ]
}

# Counts the rules of an `nft list ruleset`: every line inside a chain except its
# declaration, plus one for a base chain whose policy is not accept. Sets are skipped.
nft_summary() {
    local line words in_chain=0 count=0
    while IFS= read -r line; do
        words=($line)
        line="${words[*]}"
        case "$line" in
            "chain "*) in_chain=1 ;;
            "}") in_chain=0 ;;
            "type "*"policy drop"*) [ "$in_chain" = 1 ] && count=$((count + 1)) ;;
            "type "* | "") ;;
            *) [ "$in_chain" = 1 ] && count=$((count + 1)) ;;
        esac
    done < <(probe nft list ruleset 2>/dev/null)
    if [ "$count" -gt 0 ]; then
        emit fw "nft: $count rules"
    fi
}

# Counts -A rules and DROP policies over iptables and ip6tables.
iptables_summary() {
    local line tool count=0
    for tool in iptables-save ip6tables-save; do
        while IFS= read -r line; do
            case "$line" in
                "-A "* | ":"*" DROP "*) count=$((count + 1)) ;;
            esac
        done < <(probe "$tool" 2>/dev/null)
    done
    if [ "$count" -gt 0 ]; then
        emit fw "iptables: $count rules"
    fi
}

# ufw works through nft/iptables, so when it is active its own rules are the whole story.
firewall() {
    if ! ufw_rules; then
        nft_summary
        iptables_summary
    fi
}

admin() {
    if id admin >/dev/null 2>&1; then
        emit admin_exists 1
    else
        emit admin_exists 0
    fi
}

# Port lines of the main file, then of the drop-ins it includes. A file that is missing or
# unreadable (also the unexpanded pattern when there is no drop-in) just gives no lines. The
# drop-in cnsr writes when it changes the port is skipped, so a rerun does not see its own work.
port_lines() {
    local line words file files
    set +f
    files=("$SSHD_CONFIG_D"/*.conf)
    set -f
    for file in "$SSHD_CONFIG" "${files[@]}"; do
        [ "${file##*/}" != "$CNSR_DROP_IN" ] || continue
        while IFS= read -r line; do
            words=($line)
            emit port_line "${words[*]}"
        done < <(grep -iE '^[[:space:]]*port([[:space:]]|=)' "$file" 2>/dev/null)
    done
}

# The first global address that is not temporary, deprecated or unique-local (fc00::/7).
first_ipv6() {
    local found="" line words address i
    while IFS= read -r line; do
        [ -z "$found" ] || continue
        case " $line " in
            *" temporary "* | *" deprecated "* | *" tentative "* | *" dadfailed "*) continue ;;
        esac
        words=($line)
        for ((i = 0; i + 1 < ${#words[@]}; i++)); do
            if [ "${words[i]}" = inet6 ]; then
                address=${words[i + 1]%%/*}
                case "$address" in
                    fc* | fd*) ;;
                    *) found=$address ;;
                esac
                break
            fi
        done
    done < <(probe ip -6 -o addr show scope global 2>/dev/null)
    emit ipv6 "$found"
}

main() {
    read_listeners
    os_release
    emit arch "$(uname -m 2>/dev/null)"
    emit user "$(id -un 2>/dev/null)"
    listeners listen LISTEN "$LISTENING"
    listeners udp_listen UNCONN "$LISTENING_UDP"
    containers
    firewall
    admin
    port_lines
    first_ipv6
}

main </dev/null
exit 0
