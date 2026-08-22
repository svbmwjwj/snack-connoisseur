import unittest
import subprocess
import os
import tempfile
import shutil

class TestCnsrSync(unittest.TestCase):
    def setUp(self):
        self.repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        self.cnsr_path = os.path.join(self.repo_root, "cnsr.sh")

    def test_cnsr_bash_syntax(self):
        """cnsr.sh must pass bash -n static syntax check."""
        res = subprocess.run(["bash", "-n", self.cnsr_path], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Syntax error in cnsr.sh:\n{res.stderr}")

    def test_cnsr_usage_includes_update(self):
        """Running cnsr.sh with no args should print usage containing the update command."""
        res = subprocess.run(["bash", self.cnsr_path], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("update <别名", res.stdout)

    def test_sanitize_env_for_node_removes_cloud_credentials(self):
        """sanitize_env_for_node must strictly strip AWS and Cloudflare secrets."""
        temp_dir = tempfile.mkdtemp(prefix="snack_test_sanitize_")
        try:
            mock_env_path = os.path.join(temp_dir, ".env")
            with open(mock_env_path, "w") as f:
                f.write("""
TG_BOT_TOKEN="0000000000:AAFakeMockToken"
TG_CHAT_ID="9999999999"
CF_API_TOKEN="secret_cf_token_12345"
CF_ZONE_ID="secret_zone_67890"
AWS_ACCESS_KEY_ID="AKIA_SENSITIVE_AWS_KEY"
AWS_SECRET_ACCESS_KEY="sensitive_aws_secret_key"
DEFAULT_CLOUD_USER="admin"
GH_TOKEN="ghp_sensitive_token"
GATEWAY_URL="https://snack-gateway.example.workers.dev"
GATEWAY_AUTH_KEY="gw_auth_token_999"
""")
            out_env_path = os.path.join(temp_dir, "node_sanitized.env")

            # Extract sanitize_env_for_node function from lib/security.sh and run in bash
            bash_script = f"""
set -e
cd "{self.repo_root}"
source "{mock_env_path}"
source lib/security.sh
sanitize_env_for_node "{out_env_path}"
"""
            res = subprocess.run(["bash", "-c", bash_script], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f"sanitize_env_for_node failed:\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}")

            with open(out_env_path, "r") as f:
                sanitized_content = f.read()

            # Must NOT contain forbidden credentials
            self.assertNotIn("AWS_ACCESS_KEY_ID", sanitized_content)
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", sanitized_content)
            self.assertNotIn("CF_API_TOKEN", sanitized_content)
            self.assertNotIn("CF_ZONE_ID", sanitized_content)
            self.assertNotIn("secret_cf_token", sanitized_content)
            self.assertNotIn("AKIA_SENSITIVE", sanitized_content)

            # When GATEWAY_URL is present, it must be pure zero-secret: TG and GH tokens stripped!
            self.assertIn('GATEWAY_URL="https://snack-gateway.example.workers.dev"', sanitized_content)
            self.assertIn('GATEWAY_AUTH_KEY="gw_auth_token_999"', sanitized_content)
            self.assertIn('DEFAULT_CLOUD_USER="admin"', sanitized_content)
            self.assertNotIn("TG_BOT_TOKEN", sanitized_content)
            self.assertNotIn("GH_TOKEN", sanitized_content)

        finally:
            shutil.rmtree(temp_dir)

    def test_sync_node_scripts_assembly_and_upload(self):
        """sync_node_scripts should assemble runner.sh, reality_rotate.sh, reality_check.sh, tg_templates.sh, sanitized .env and push via scp."""
        temp_dir = tempfile.mkdtemp(prefix="snack_test_sync_")
        try:
            bin_dir = os.path.join(temp_dir, "bin")
            os.makedirs(bin_dir)
            log_file = os.path.join(temp_dir, "remote_ops.log")
            scp_captured_dir = os.path.join(temp_dir, "scp_captured")
            os.makedirs(scp_captured_dir)

            # Mock ssh command
            mock_ssh = os.path.join(bin_dir, "ssh")
            with open(mock_ssh, "w") as f:
                f.write(f"""#!/bin/bash
echo "SSH_CALL: $@" >> "{log_file}"
# Handle specific subcommands
cmd="$*"
if [[ "$cmd" == *"eval echo ~"* ]]; then
    echo "/home/admin"
    exit 0
fi
if [[ "$cmd" == *"-G"* ]]; then
    echo "hostname test-node.example.com"
    exit 0
fi
if [[ "$cmd" == *"curl -4"* ]]; then
    echo "198.51.100.22"
    exit 0
fi
if [[ "$cmd" == *"curl -6"* ]]; then
    echo "2001:db8:1234::1"
    exit 0
fi
if [[ "$cmd" == *"grep -E '^SERVER_HOST='"* ]]; then
    echo 'SERVER_HOST="node-v123.example.com"'
    exit 0
fi
exit 0
""")
            os.chmod(mock_ssh, 0o755)

            # Mock scp command: capture files being copied
            mock_scp = os.path.join(bin_dir, "scp")
            with open(mock_scp, "w") as f:
                f.write(f"""#!/bin/bash
echo "SCP_CALL: $@" >> "{log_file}"
# Copy file to scp_captured dir for inspection
while [[ $# -gt 0 ]]; do
    if [ -f "$1" ]; then
        cp "$1" "{scp_captured_dir}/"
    fi
    shift
done
exit 0
""")
            os.chmod(mock_scp, 0o755)

            # Mock .env file
            mock_env_path = os.path.join(temp_dir, ".env")
            with open(mock_env_path, "w") as f:
                f.write("""
TG_BOT_TOKEN="mock_bot_tok"
TG_CHAT_ID="mock_chat_id"
CF_API_TOKEN="mock_cf_token"
CF_ZONE_ID="mock_cf_zone"
AWS_ACCESS_KEY_ID="mock_aws_id"
AWS_SECRET_ACCESS_KEY="mock_aws_sec"
GATEWAY_URL="https://mock.gw"
GATEWAY_AUTH_KEY="mock_auth_key"
""")

            bash_script = f"""
set -e
cd "{self.repo_root}"
source "{mock_env_path}"
export PATH="{bin_dir}:$PATH"
# Source functions from lib and core
source lib/ui.sh
source lib/ssh.sh
source lib/security.sh
source core/update.sh

sync_node_scripts "sg_test_node"
"""
            res = subprocess.run(["bash", "-c", bash_script], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f"sync_node_scripts failed:\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}")

            # Verify files captured by mock scp
            captured_files = os.listdir(scp_captured_dir)
            self.assertIn("runner.sh", captured_files)
            self.assertIn("reality_rotate.sh", captured_files)
            self.assertIn("reality_check.sh", captured_files)
            self.assertIn("tg_templates.sh", captured_files)
            self.assertIn(".env", captured_files)

            # Inspect rendered runner.sh
            with open(os.path.join(scp_captured_dir, "runner.sh"), "r") as f:
                runner_content = f.read()
            self.assertIn('TARGET_IP="198.51.100.22"', runner_content)
            self.assertIn('SERVER_HOST="node-v123.example.com"', runner_content)
            self.assertIn('SSH_ALIAS="sg_test_node"', runner_content)
            self.assertIn('/home/admin/docker-apps/xray', runner_content)

            # Inspect sanitized .env
            with open(os.path.join(scp_captured_dir, ".env"), "r") as f:
                env_content = f.read()
            self.assertNotIn("AWS_ACCESS_KEY_ID", env_content)
            self.assertIn('GATEWAY_URL="https://mock.gw"', env_content)
            self.assertIn('GATEWAY_AUTH_KEY="mock_auth_key"', env_content)
            self.assertNotIn('TG_BOT_TOKEN', env_content)

            # Verify cron registration command targeted runner.sh
            with open(log_file, "r") as f:
                log_content = f.read()
            self.assertIn("runner.sh > /home/admin/docker-apps/xray/rotate.log", log_content)

        finally:
            shutil.rmtree(temp_dir)

    def test_cnsr_update_command_dispatch(self):
        """./cnsr.sh update <alias> should trigger sync_node_scripts."""
        temp_dir = tempfile.mkdtemp(prefix="snack_test_update_cmd_")
        try:
            bin_dir = os.path.join(temp_dir, "bin")
            os.makedirs(bin_dir)
            log_file = os.path.join(temp_dir, "dispatch.log")

            mock_ssh = os.path.join(bin_dir, "ssh")
            with open(mock_ssh, "w") as f:
                f.write(f"""#!/bin/bash
echo "SSH_CALL: $@" >> "{log_file}"
if [[ "$*" == *"eval echo ~"* ]]; then echo "/home/admin"; exit 0; fi
if [[ "$*" == *"-G"* ]]; then echo "hostname 198.51.100.99"; exit 0; fi
if [[ "$*" == *"curl -4"* ]]; then echo "198.51.100.99"; exit 0; fi
exit 0
""")
            os.chmod(mock_ssh, 0o755)

            mock_scp = os.path.join(bin_dir, "scp")
            with open(mock_scp, "w") as f:
                f.write(f"""#!/bin/bash
echo "SCP_CALL: $@" >> "{log_file}"
exit 0
""")
            os.chmod(mock_scp, 0o755)

            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}:{env['PATH']}"

            proc = subprocess.run([self.cnsr_path, "update", "sg_node_test"], cwd=self.repo_root, env=env, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, f"cnsr.sh update failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}")
            self.assertIn("全量组件与脱敏凭据热更新完成", proc.stdout)

            with open(log_file, "r") as f:
                log_content = f.read()
            self.assertIn("runner.sh", log_content)

        finally:
            shutil.rmtree(temp_dir)

    def test_guess_default_user(self):
        """Test default username inference based on alias keywords."""
        cases = [
            ("aws-sg-01", "admin"),
            ("lightsail-tokyo", "admin"),
            ("ls_debian_node", "admin"),
            ("my-ubuntu-box", "ubuntu"),
            ("oci-us-west", "ubuntu"),
            ("oracle_cloud", "ubuntu"),
            ("ec2-amazon-linux", "ec2-user"),
            ("amzn2023-node", "ec2-user"),
            ("centos-vps", "ec2-user"),
            ("rocky-linux", "ec2-user"),
            ("opc-custom", "opc"),
            ("gcp-instance", "admin"),
            ("dmit-hk-pro", "root"),
            ("bwg-la-01", "root"),
            ("vultr-sgp", "root"),
            ("custom-node", "root"),
        ]
        for alias, expected_user in cases:
            bash_cmd = f"""
set -e
cd "{self.repo_root}"
source lib/ssh.sh
guess_default_user "{alias}"
"""
            res = subprocess.run(["bash", "-c", bash_cmd], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f"Error running guess_default_user for {alias}")
            actual_user = res.stdout.strip()
            self.assertEqual(actual_user, expected_user, f"Alias '{alias}' expected '{expected_user}', got '{actual_user}'")

if __name__ == "__main__":
    unittest.main()

