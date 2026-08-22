#!/bin/bash
set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

echo "=== 1. Checking Bash Syntax (bash -n) ==="
bash -n templates/runner.template.sh
bash -n templates/reality_rotate.template.sh
bash -n templates/reality_check.template.sh
bash -n templates/async_deploy.template.sh
bash -n templates/tg_templates.sh
bash -n cnsr.sh
bash -n lib/ui.sh
bash -n lib/ssh.sh
echo "✅ All templates, cnsr.sh, and lib modules passed bash -n static syntax validation!"

echo "=== 2. Testing runner.template.sh update & execution ==="
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

cp templates/runner.template.sh "$TMP_DIR/runner.sh"
sed -i '' -e "s|TARGET_IP=\"PLACEHOLDER_IP\"|TARGET_IP=\"1.2.3.4\"|g" "$TMP_DIR/runner.sh" 2>/dev/null || sed -i -e "s|TARGET_IP=\"PLACEHOLDER_IP\"|TARGET_IP=\"1.2.3.4\"|g" "$TMP_DIR/runner.sh"
sed -i '' -e "s|SERVER_HOST=\"PLACEHOLDER_HOST\"|SERVER_HOST=\"node.test.com\"|g" "$TMP_DIR/runner.sh" 2>/dev/null || sed -i -e "s|SERVER_HOST=\"PLACEHOLDER_HOST\"|SERVER_HOST=\"node.test.com\"|g" "$TMP_DIR/runner.sh"
sed -i '' -e "s|SSH_ALIAS=\"PLACEHOLDER_ALIAS\"|SSH_ALIAS=\"sg_test\"|g" "$TMP_DIR/runner.sh" 2>/dev/null || sed -i -e "s|SSH_ALIAS=\"PLACEHOLDER_ALIAS\"|SSH_ALIAS=\"sg_test\"|g" "$TMP_DIR/runner.sh"
sed -i '' -e "s|TARGET_CIDR_V4=\"PLACEHOLDER_CIDR_V4\"|TARGET_CIDR_V4=\"1.2.3.0/24\"|g" "$TMP_DIR/runner.sh" 2>/dev/null || sed -i -e "s|TARGET_CIDR_V4=\"PLACEHOLDER_CIDR_V4\"|TARGET_CIDR_V4=\"1.2.3.0/24\"|g" "$TMP_DIR/runner.sh"
sed -i '' -e "s|TARGET_CIDR_V6=\"PLACEHOLDER_CIDR_V6\"|TARGET_CIDR_V6=\"2001:db8::/32\"|g" "$TMP_DIR/runner.sh" 2>/dev/null || sed -i -e "s|TARGET_CIDR_V6=\"PLACEHOLDER_CIDR_V6\"|TARGET_CIDR_V6=\"2001:db8::/32\"|g" "$TMP_DIR/runner.sh"
chmod +x "$TMP_DIR/runner.sh"

# Mock existing stable reality_rotate.sh
cat << 'EOF' > "$TMP_DIR/reality_rotate.sh"
#!/bin/bash
echo "OLD_V1 $@" > out.log
EOF
chmod +x "$TMP_DIR/reality_rotate.sh"

# Mock curl that simulates gateway returning new template
mkdir -p "$TMP_DIR/bin"
cat << 'EOF' > "$TMP_DIR/bin/curl"
#!/bin/bash
while [[ $# -gt 0 ]]; do
    if [ "$1" = "-o" ]; then
        out_file="$2"
        cat << 'SUBEOF' > "$out_file"
#!/bin/bash
TARGET_IP="PLACEHOLDER_IP"
SERVER_HOST="PLACEHOLDER_HOST"
SSH_ALIAS="PLACEHOLDER_ALIAS"
echo "NEW_V2 ip=$TARGET_IP host=$SERVER_HOST alias=$SSH_ALIAS args=$@" > out.log
SUBEOF
        exit 0
    fi
    shift
done
exit 1
EOF
chmod +x "$TMP_DIR/bin/curl"

PATH="$TMP_DIR/bin:$PATH" GATEWAY_URL="https://mock.gw" GATEWAY_AUTH_KEY="key1" "$TMP_DIR/runner.sh" hello world
RESULT=$(cat "$TMP_DIR/out.log")
if [ "$RESULT" != "NEW_V2 ip=1.2.3.4 host=node.test.com alias=sg_test args=hello world" ]; then
    echo "❌ Runner update test failed! Got: $RESULT"
    exit 1
fi
echo "✅ Runner successfully updated, substituted placeholders, and executed new version!"

echo "=== 3. Testing runner.template.sh syntax rejection & safe fallback ==="
cat << 'EOF' > "$TMP_DIR/bin/curl"
#!/bin/bash
while [[ $# -gt 0 ]]; do
    if [ "$1" = "-o" ]; then
        out_file="$2"
        echo 'if [[ bad syntax' > "$out_file"
        exit 0
    fi
    shift
done
exit 1
EOF

PATH="$TMP_DIR/bin:$PATH" GATEWAY_URL="https://mock.gw" GATEWAY_AUTH_KEY="key1" "$TMP_DIR/runner.sh" fallback_test
RESULT=$(cat "$TMP_DIR/out.log")
if [ "$RESULT" != "NEW_V2 ip=1.2.3.4 host=node.test.com alias=sg_test args=fallback_test" ]; then
    echo "❌ Runner fallback test failed! Got: $RESULT"
    exit 1
fi
if [ -f "$TMP_DIR/reality_rotate.sh.next" ]; then
    echo "❌ Bad .next file was not deleted!"
    exit 1
fi
echo "✅ Runner rejected bad syntax, deleted .next file, and cleanly fell back to current script!"

echo "=== 4. Testing send_tg_message and send_gh_dispatch in reality_rotate ==="
cat << 'EOF' > "$TMP_DIR/test_send.sh"
#!/bin/bash
set -e
DOCKER_DIR="$(pwd)"
TARGET_IP="1.2.3.4"
TARGET_CIDR_V4="1.2.3.0/24"
TARGET_CIDR_V6="2001:db8::/32"

eval "$(sed -n '/function send_tg_message()/,/^}/p' "$REPO_DIR/templates/reality_rotate.template.sh")"
eval "$(sed -n '/function send_gh_dispatch()/,/^}/p' "$REPO_DIR/templates/reality_rotate.template.sh")"

case "$1" in
    tg_gateway)
        send_tg_message "Hello Gateway TG"
        ;;
    tg_direct)
        send_tg_message "Hello Direct TG"
        ;;
    gh_gateway)
        send_gh_dispatch "$TARGET_IP" "$TARGET_CIDR_V4" "$TARGET_CIDR_V6" "20260816_120000"
        ;;
    gh_direct)
        send_gh_dispatch "$TARGET_IP" "$TARGET_CIDR_V4" "$TARGET_CIDR_V6" "20260816_120000"
        ;;
esac
EOF
chmod +x "$TMP_DIR/test_send.sh"

cat << 'EOF' > "$TMP_DIR/bin/curl"
#!/bin/bash
echo "CURL_CALL: $@" >> "$(pwd)/curl.log"
exit 0
EOF
chmod +x "$TMP_DIR/bin/curl"

# 4a. TG via Gateway
rm -f "$TMP_DIR/curl.log"
(cd "$TMP_DIR" && REPO_DIR="$REPO_DIR" PATH="$TMP_DIR/bin:$PATH" GATEWAY_URL="https://gw.mock" GATEWAY_AUTH_KEY="gwkey" ./test_send.sh tg_gateway)
grep -q "https://gw.mock/api/tg" "$TMP_DIR/curl.log" || { echo "❌ TG Gateway URL not matched"; exit 1; }
grep -q "Authorization: Bearer gwkey" "$TMP_DIR/curl.log" || { echo "❌ TG Gateway Auth header not matched"; exit 1; }
grep -q 'Hello Gateway TG' "$TMP_DIR/curl.log" || { echo "❌ TG Gateway Payload text not matched"; exit 1; }

# 4b. TG Direct
rm -f "$TMP_DIR/curl.log"
(cd "$TMP_DIR" && REPO_DIR="$REPO_DIR" PATH="$TMP_DIR/bin:$PATH" TG_BOT_TOKEN="mytoken" TG_CHAT_ID="mychat" ./test_send.sh tg_direct)
grep -q "https://api.telegram.org/botmytoken/sendMessage" "$TMP_DIR/curl.log" || { echo "❌ TG Direct URL not matched"; exit 1; }
grep -q "chat_id=mychat" "$TMP_DIR/curl.log" || { echo "❌ TG Direct chat_id not matched"; exit 1; }
grep -q "text=Hello Direct TG" "$TMP_DIR/curl.log" || { echo "❌ TG Direct text not matched"; exit 1; }

# 4c. GH Dispatch via Gateway
rm -f "$TMP_DIR/curl.log"
(cd "$TMP_DIR" && REPO_DIR="$REPO_DIR" PATH="$TMP_DIR/bin:$PATH" GATEWAY_URL="https://gw.mock" GATEWAY_AUTH_KEY="gwkey" ./test_send.sh gh_gateway)
grep -q "https://gw.mock/api/gh-dispatch" "$TMP_DIR/curl.log" || { echo "❌ GH Gateway URL not matched"; exit 1; }
grep -q "Authorization: Bearer gwkey" "$TMP_DIR/curl.log" || { echo "❌ GH Gateway Auth header not matched"; exit 1; }
grep -q '"target_ip":"1.2.3.4"' "$TMP_DIR/curl.log" || grep -q '"target_ip": "1.2.3.4"' "$TMP_DIR/curl.log" || { echo "❌ GH Gateway target_ip not matched"; exit 1; }

# 4d. GH Dispatch Direct
rm -f "$TMP_DIR/curl.log"
(cd "$TMP_DIR" && REPO_DIR="$REPO_DIR" PATH="$TMP_DIR/bin:$PATH" GH_TOKEN="ghp_mock123" ./test_send.sh gh_direct)
grep -q "https://api.github.com/repos/svbmwjwj/snack-connoisseur/dispatches" "$TMP_DIR/curl.log" || { echo "❌ GH Direct URL not matched"; exit 1; }
grep -q "Authorization: Bearer ghp_mock123" "$TMP_DIR/curl.log" || { echo "❌ GH Direct token not matched"; exit 1; }
grep -q '"scan_trigger"' "$TMP_DIR/curl.log" || { echo "❌ GH Direct event_type not matched"; exit 1; }

echo "✅ All Gateway / Direct routing tests passed!"

echo "=========================================="
echo "🎉 ALL TESTS PASSED SUCCESSFULLY! 🎉"
echo "=========================================="
