#!/usr/bin/env bash
# china-check.sh <domain> <ip> [<seconds>]
#
# Whether <domain> works as a REALITY SNI from mainland China right now. Globalping's mainland
# probes (free, no account; 250 probe runs an hour per address) each make an HTTPS request to
# <ip>:443 with <domain> as the host, so as the SNI, without resolving it in China. Prints one
# word on stdout:
#   pass     at least two of the three probes completed the TLS handshake
#   blocked  at least two had the TLS handshake cut off (reset or timed out after the TCP
#            connection was up), as with an SNI the GFW blocks. A failure before that, on the
#            TCP connection, says nothing about the SNI: only the node ever connects to the
#            neighbour, and China may simply not reach it.
#   unknown  anything else: too few probes, mixed answers, a refused or failed request, or no
#            result within WAIT_SECONDS. The caller then goes by its own checks alone.
# The measurement and its result are public on Globalping; they name the neighbour site, never
# the node. The request comes from the node's address, so it carries curl's own User-Agent
# rather than one naming cnsr. <seconds> caps the whole check (each request, and the polling);
# without it the check may take about 150 s. Exits 2 on bad arguments, else 0. Must parse and run under bash 3.2.
# CNSR_CHINA_POLL_SECONDS replaces POLL_SECONDS for tests only.

set -uo pipefail
export LC_ALL=C

API=https://api.globalping.io/v1
PROBES=3
NEEDED=2
POLL_SECONDS=${CNSR_CHINA_POLL_SECONDS:-2}
POLLS=8
TIMEOUT_SECONDS=15
ID_PATTERN='^[A-Za-z0-9]+$'
TLS_CUT='during the TLS handshake|ECONNRESET|before secure TLS connection|socket hang up'

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ] || ! [[ ${3-1} =~ ^[1-9][0-9]*$ ]]; then
    echo "用法：china-check.sh <域名> <IP> [<秒数>]" >&2
    exit 2
fi
domain=$1
ip=$2
END=$(($(date +%s) + ${3:-$((TIMEOUT_SECONDS * (POLLS + 1) + POLL_SECONDS * POLLS))}))

# Seconds the next request may take: TIMEOUT_SECONDS, or what is left of the time when less.
time_left() {
    local left=$((END - $(date +%s)))
    [ "$left" -lt "$TIMEOUT_SECONDS" ] || left=$TIMEOUT_SECONDS
    echo "$left"
}

unknown() {
    echo "国内检验没有结论：$1" >&2
    echo unknown
    exit 0
}

body=$(jq -nc --arg ip "$ip" --arg host "$domain" --argjson probes "$PROBES" '{
    type: "http",
    target: $ip,
    locations: [{country: "CN", limit: $probes}],
    measurementOptions: {protocol: "HTTPS", request: {method: "HEAD", host: $host, path: "/"}}
}') || unknown "无法生成请求"

created=$(curl -sS --max-time "$(time_left)" -H 'Content-Type: application/json' \
    -d "$body" "$API/measurements" </dev/null) || unknown "连不上 Globalping"
id=$(printf '%s' "$created" | jq -r '.id // empty' 2>/dev/null) || id=""
[[ $id =~ $ID_PATTERN ]] || unknown "Globalping 没有接受这次测量：${created:0:200}"

result=""
poll=0
while [ "$poll" -lt "$POLLS" ]; do
    poll=$((poll + 1))
    sleep "$POLL_SECONDS"
    [ "$(time_left)" -gt 0 ] || break
    result=$(curl -sS --max-time "$(time_left)" "$API/measurements/$id" </dev/null) || continue
    [ "$(printf '%s' "$result" | jq -r '.status // empty' 2>/dev/null)" = finished ] && break
    result=""
done
[ -n "$result" ] || unknown "测量没有在限定时间内完成"

# Counted per probe: a finished handshake, or a handshake cut off once TCP was up (what blocking
# looks like). The texts are those of the Globalping probe's HTTP handler.
counts=$(printf '%s' "$result" | jq -r --arg cut "$TLS_CUT" '
    [.results[]?.result] as $all
    | ($all | map(select(.tls.protocol? != null)) | length) as $ok
    | ($all | map(select(.tls.protocol? == null and
        ((.rawOutput // "") | tostring | test($cut; "i")))) | length) as $cut
    | "\($ok) \($cut)"' 2>/dev/null) || unknown "看不懂测量结果"
read -r ok cut <<EOF
$counts
EOF
echo "国内检验 $domain（$ip）：握手成功 $ok 个探针，被重置或超时 $cut 个。" >&2
if [ "${ok:-0}" -ge "$NEEDED" ]; then
    echo pass
elif [ "${cut:-0}" -ge "$NEEDED" ]; then
    echo blocked
else
    echo unknown
fi
