import unittest
import subprocess
import os
import tempfile
import shutil

class TestTemplates(unittest.TestCase):
    def setUp(self):
        self.repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        self.templates_dir = os.path.join(self.repo_root, "templates")

    def test_bash_syntax(self):
        """All shell templates must pass bash -n static syntax check."""
        for filename in os.listdir(self.templates_dir):
            if filename.endswith(".sh"):
                file_path = os.path.join(self.templates_dir, filename)
                res = subprocess.run(["bash", "-n", file_path], capture_output=True, text=True)
                self.assertEqual(res.returncode, 0, f"Syntax error in {filename}:\n{res.stderr}")

    def test_runner_successful_update(self):
        """Test runner downloads new script, substitutes placeholders, validates, and atomically replaces."""
        temp_dir = tempfile.mkdtemp(prefix="snack_test_runner_")
        try:
            runner_src = os.path.join(self.templates_dir, "runner.template.sh")
            runner_dest = os.path.join(temp_dir, "runner.sh")
            with open(runner_src, "r") as f:
                content = f.read()
            content = content.replace('TARGET_IP="PLACEHOLDER_IP"', 'TARGET_IP="1.2.3.4"')
            content = content.replace('SERVER_HOST="PLACEHOLDER_HOST"', 'SERVER_HOST="node1.example.com"')
            content = content.replace('SSH_ALIAS="PLACEHOLDER_ALIAS"', 'SSH_ALIAS="sg_test"')
            content = content.replace('TARGET_CIDR_V4="PLACEHOLDER_CIDR_V4"', 'TARGET_CIDR_V4="1.2.3.0/24"')
            content = content.replace('TARGET_CIDR_V6="PLACEHOLDER_CIDR_V6"', 'TARGET_CIDR_V6="2001:db8::/32"')
            with open(runner_dest, "w") as f:
                f.write(content)
            os.chmod(runner_dest, 0o755)

            # Initial old script
            old_script = os.path.join(temp_dir, "reality_rotate.sh")
            with open(old_script, "w") as f:
                f.write('#!/bin/bash\necho "OLD_SCRIPT_V1 $@" > run.log\n')
            os.chmod(old_script, 0o755)

            # Create a mock curl that writes a valid next template
            bin_dir = os.path.join(temp_dir, "bin")
            os.makedirs(bin_dir)
            mock_curl = os.path.join(bin_dir, "curl")
            with open(mock_curl, "w") as f:
                f.write(r'''#!/bin/bash
# Mock curl: check args and output a mock template to reality_rotate.sh.next
while [[ $# -gt 0 ]]; do
    if [ "$1" = "-o" ]; then
        out_file="$2"
        cat << 'EOF' > "$out_file"
#!/bin/bash
# reality_rotate.sh mock new template
TARGET_IP="PLACEHOLDER_IP"
SERVER_HOST="PLACEHOLDER_HOST"
SSH_ALIAS="PLACEHOLDER_ALIAS"
echo "NEW_SCRIPT_V2 ip=$TARGET_IP host=$SERVER_HOST alias=$SSH_ALIAS args=$@" > run.log
EOF
        exit 0
    fi
    shift
done
exit 1
''')
            os.chmod(mock_curl, 0o755)

            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}:{env['PATH']}"
            env["GATEWAY_URL"] = "https://mock-gateway.example.com"
            env["GATEWAY_AUTH_KEY"] = "secret123"

            proc = subprocess.run([runner_dest, "arg1", "arg2"], cwd=temp_dir, env=env, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, f"Runner failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}")

            # Check that reality_rotate.sh was updated and executed
            run_log = os.path.join(temp_dir, "run.log")
            self.assertTrue(os.path.exists(run_log))
            with open(run_log, "r") as f:
                log_content = f.read().strip()
            self.assertEqual(log_content, "NEW_SCRIPT_V2 ip=1.2.3.4 host=node1.example.com alias=sg_test args=arg1 arg2")

            # Check that reality_rotate.sh.next is cleaned up
            self.assertFalse(os.path.exists(os.path.join(temp_dir, "reality_rotate.sh.next")))

        finally:
            shutil.rmtree(temp_dir)

    def test_runner_syntax_error_fallback(self):
        """Test that runner rejects corrupt/invalid script and falls back to existing stable script."""
        temp_dir = tempfile.mkdtemp(prefix="snack_test_runner_bad_")
        try:
            runner_src = os.path.join(self.templates_dir, "runner.template.sh")
            runner_dest = os.path.join(temp_dir, "runner.sh")
            with open(runner_src, "r") as f:
                content = f.read()
            with open(runner_dest, "w") as f:
                f.write(content)
            os.chmod(runner_dest, 0o755)

            # Initial stable script
            old_script = os.path.join(temp_dir, "reality_rotate.sh")
            with open(old_script, "w") as f:
                f.write('#!/bin/bash\necho "STABLE_SCRIPT" > run.log\n')
            os.chmod(old_script, 0o755)

            # Mock curl outputs a script with broken bash syntax
            bin_dir = os.path.join(temp_dir, "bin")
            os.makedirs(bin_dir)
            mock_curl = os.path.join(bin_dir, "curl")
            with open(mock_curl, "w") as f:
                f.write(r'''#!/bin/bash
while [[ $# -gt 0 ]]; do
    if [ "$1" = "-o" ]; then
        out_file="$2"
        echo 'if [[ broken syntax then' > "$out_file"
        exit 0
    fi
    shift
done
exit 1
''')
            os.chmod(mock_curl, 0o755)

            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}:{env['PATH']}"
            env["GATEWAY_URL"] = "https://mock-gateway.example.com"
            env["GATEWAY_AUTH_KEY"] = "secret123"

            proc = subprocess.run([runner_dest], cwd=temp_dir, env=env, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, f"Runner execution failed on fallback:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}")

            # Check that stable script was executed and bad .next was removed
            run_log = os.path.join(temp_dir, "run.log")
            self.assertTrue(os.path.exists(run_log))
            with open(run_log, "r") as f:
                log_content = f.read().strip()
            self.assertEqual(log_content, "STABLE_SCRIPT")
            self.assertFalse(os.path.exists(os.path.join(temp_dir, "reality_rotate.sh.next")))

        finally:
            shutil.rmtree(temp_dir)

    def test_rotate_and_check_send_functions(self):
        """Test send_tg_message and send_gh_dispatch routing via Gateway vs Direct."""
        temp_dir = tempfile.mkdtemp(prefix="snack_test_send_")
        try:
            test_script = os.path.join(temp_dir, "test_send.sh")
            with open(test_script, "w") as f:
                f.write(r'''#!/bin/bash
set -e
TARGET_IP="1.2.3.4"
TARGET_CIDR_V4="1.2.3.0/24"
TARGET_CIDR_V6="2001:db8::/32"

# Extract function definitions from reality_rotate.template.sh
eval "$(sed -n '/function send_tg_message()/,/^}/p' "'''+os.path.join(self.templates_dir, "reality_rotate.template.sh")+r'''")"
eval "$(sed -n '/function send_gh_dispatch()/,/^}/p' "'''+os.path.join(self.templates_dir, "reality_rotate.template.sh")+r'''")"

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
''')
            os.chmod(test_script, 0o755)

            bin_dir = os.path.join(temp_dir, "bin")
            os.makedirs(bin_dir)
            mock_curl = os.path.join(bin_dir, "curl")
            with open(mock_curl, "w") as f:
                f.write(r'''#!/bin/bash
echo "CURL_CALL: $@" >> "$LOG_FILE"
for i in "$@"; do
    if [[ "$i" =~ ^\{ ]]; then
        echo "PAYLOAD: $i" >> "$LOG_FILE"
    fi
done
exit 0
''')
            os.chmod(mock_curl, 0o755)

            log_file = os.path.join(temp_dir, "curl.log")

            # 1. Test Gateway TG
            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}:{env['PATH']}"
            env["LOG_FILE"] = log_file
            env["GATEWAY_URL"] = "https://gw.example.com"
            env["GATEWAY_AUTH_KEY"] = "gw_key"
            if os.path.exists(log_file): os.remove(log_file)
            subprocess.run([test_script, "tg_gateway"], env=env, check=True)
            with open(log_file, "r") as f:
                log = f.read()
            self.assertIn("https://gw.example.com/api/tg", log)
            self.assertIn("Authorization: Bearer gw_key", log)
            self.assertIn('"text": "Hello Gateway TG"', log)

            # 2. Test Direct TG
            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}:{env['PATH']}"
            env["LOG_FILE"] = log_file
            env["TG_BOT_TOKEN"] = "mock_bot_token"
            env["TG_CHAT_ID"] = "mock_chat_id"
            if os.path.exists(log_file): os.remove(log_file)
            subprocess.run([test_script, "tg_direct"], env=env, check=True)
            with open(log_file, "r") as f:
                log = f.read()
            self.assertIn("https://api.telegram.org/botmock_bot_token/sendMessage", log)
            self.assertIn("chat_id=mock_chat_id", log)
            self.assertIn("text=Hello Direct TG", log)

            # 3. Test Gateway GH Dispatch
            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}:{env['PATH']}"
            env["LOG_FILE"] = log_file
            env["GATEWAY_URL"] = "https://gw.example.com"
            env["GATEWAY_AUTH_KEY"] = "gw_key"
            if os.path.exists(log_file): os.remove(log_file)
            subprocess.run([test_script, "gh_gateway"], env=env, check=True)
            with open(log_file, "r") as f:
                log = f.read()
            self.assertIn("https://gw.example.com/api/gh-dispatch", log)
            self.assertIn("Authorization: Bearer gw_key", log)
            self.assertIn('"target_ip": "1.2.3.4"', log)
            self.assertIn('"target_cidr_v4": "1.2.3.0/24"', log)

            # 4. Test Direct GH Dispatch
            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}:{env['PATH']}"
            env["LOG_FILE"] = log_file
            env["GH_TOKEN"] = "mock_gh_token"
            if os.path.exists(log_file): os.remove(log_file)
            subprocess.run([test_script, "gh_direct"], env=env, check=True)
            with open(log_file, "r") as f:
                log = f.read()
            self.assertIn("https://api.github.com/repos/svbmwjwj/snack-connoisseur/dispatches", log)
            self.assertIn("Authorization: Bearer mock_gh_token", log)
            self.assertIn('"scan_trigger"', log)
            self.assertIn('"target_ip": "1.2.3.4"', log)

        finally:
            shutil.rmtree(temp_dir)

    def test_tg_templates_render(self):
        """Test tg_templates.sh functions output expected formats and support test mode prefix."""
        tg_template_path = os.path.join(self.templates_dir, "tg_templates.sh")
        test_script = f"""#!/bin/bash
set -e
source "{tg_template_path}"

case "$1" in
    node_config_init)
        tpl_node_config "true" "sg_aws" "sg.node.com" "gateway.icloud.com" "uuid-123" "pub-456" "sid-789" "qx_line" "vless_line"
        ;;
    node_config_rotate)
        tpl_node_config "false" "sg_aws" "sg.node.com" "apple-relay.apple.com" "uuid-123" "pub-456" "sid-789" "qx_line" "vless_line"
        ;;
    sni_blocked)
        tpl_sni_blocked "sg_aws" "gateway.icloud.com" "1.2.3.0/24"
        ;;
    alert_failure)
        tpl_alert_failure "sg_aws" "Cloud scan timed out and all fallback SNIs failed" "Node is completely unreachable" "Run ./cnsr.sh check sg_aws"
        ;;
    health_full)
        tpl_health_full "sg_aws" "🟢 Normal" "1.2.3.4" "none" "sg.node.com" "🟢 OK" "Ping OK" "gateway.icloud.com" "200" "apple.com" "25ms" "Valid" "Cloudflare" "A" "10 days" "🟢" "🟢" "🟢" "Up" "10d" "vision" "uuid-***" "pub-***" "sid-789"
        ;;
esac
"""
        # Test Normal Mode
        res_init = subprocess.run(["bash", "-c", test_script, "bash", "node_config_init"], capture_output=True, text=True, check=True)
        self.assertIn("X-ray 节点部署完成", res_init.stdout)
        self.assertIn("gateway.icloud.com", res_init.stdout)
        self.assertNotIn("TEST MODE", res_init.stdout)

        res_rot = subprocess.run(["bash", "-c", test_script, "bash", "node_config_rotate"], capture_output=True, text=True, check=True)
        self.assertIn("X-ray 节点配置换新", res_rot.stdout)
        self.assertIn("apple-relay.apple.com", res_rot.stdout)

        res_blk = subprocess.run(["bash", "-c", test_script, "bash", "sni_blocked"], capture_output=True, text=True, check=True)
        self.assertIn("SNI 域名阻断告警", res_blk.stdout)
        self.assertIn("gateway.icloud.com", res_blk.stdout)

        res_fail = subprocess.run(["bash", "-c", test_script, "bash", "alert_failure"], capture_output=True, text=True, check=True)
        self.assertIn("致命故障告警", res_fail.stdout)
        self.assertIn("Node is completely unreachable", res_fail.stdout)

        # Test TEST MODE prefix (zh)
        env_test = os.environ.copy()
        env_test["IS_TEST_MODE"] = "1"
        res_test = subprocess.run(["bash", "-c", test_script, "bash", "node_config_init"], env=env_test, capture_output=True, text=True, check=True)
        self.assertIn("⚠️ *[TEST MODE / 模拟演练]* ⚠️", res_test.stdout)

        # Test English Mode (CNSR_LANG=en)
        env_en = os.environ.copy()
        env_en["CNSR_LANG"] = "en"
        res_init_en = subprocess.run(["bash", "-c", test_script, "bash", "node_config_init"], env=env_en, capture_output=True, text=True, check=True)
        self.assertIn("X-ray Node Deployed", res_init_en.stdout)
        self.assertIn("Node Alias", res_init_en.stdout)

        res_rot_en = subprocess.run(["bash", "-c", test_script, "bash", "node_config_rotate"], env=env_en, capture_output=True, text=True, check=True)
        self.assertIn("X-ray Node Rotated", res_rot_en.stdout)

        res_blk_en = subprocess.run(["bash", "-c", test_script, "bash", "sni_blocked"], env=env_en, capture_output=True, text=True, check=True)
        self.assertIn("SNI Blocked Alert", res_blk_en.stdout)

        res_fail_en = subprocess.run(["bash", "-c", test_script, "bash", "alert_failure"], env=env_en, capture_output=True, text=True, check=True)
        self.assertIn("Critical Alert", res_fail_en.stdout)

if __name__ == "__main__":
    unittest.main()

