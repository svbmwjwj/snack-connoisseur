import unittest
import subprocess
import os
import tempfile
import shutil

class TestLibSecurity(unittest.TestCase):
    def setUp(self):
        self.repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        self.security_sh = os.path.join(self.repo_root, "lib", "security.sh")
        self.temp_dir = tempfile.mkdtemp(prefix="snack_test_sec_")
        self.test_ssh_config = os.path.join(self.temp_dir, "ssh_config")

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_security_syntax(self):
        """lib/security.sh must pass bash -n static syntax check."""
        res = subprocess.run(["bash", "-n", self.security_sh], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Syntax error in lib/security.sh:\n{res.stderr}")

    def test_sanitize_env_for_node_missing_param(self):
        """sanitize_env_for_node without output file parameter must fail with error message."""
        cmd = f"""
        source "{self.security_sh}"
        sanitize_env_for_node
        """
        res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("sanitize_env_for_node 需要指定输出文件路径", res.stdout)

    def test_sanitize_env_for_node_gateway_mode(self):
        """sanitize_env_for_node in Gateway mode (zero-secret) must strip cloud keys and TG/GH tokens."""
        out_file = os.path.join(self.temp_dir, "gateway.env")
        cmd = f"""
        export GATEWAY_URL="https://gateway.example.com"
        export GATEWAY_AUTH_KEY="gw_secret_auth_123"
        export DEFAULT_CLOUD_USER="admin"
        export CNSR_LANG="en"
        export TG_BOT_TOKEN="123456:sensitive_tg_token"
        export TG_CHAT_ID="888888"
        export GH_TOKEN="ghp_sensitive_gh_token"
        export AWS_ACCESS_KEY_ID="AKIA_LEAKED_AWS_KEY"
        export AWS_SECRET_ACCESS_KEY="sensitive_aws_secret"
        export CF_API_TOKEN="sensitive_cf_token"
        export CF_ZONE_ID="sensitive_cf_zone"

        source "{self.security_sh}"
        sanitize_env_for_node "{out_file}"
        """
        res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"sanitize_env_for_node failed:\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}")

        with open(out_file, "r") as f:
            content = f.read()

        # Retained variables
        self.assertIn('GATEWAY_URL="https://gateway.example.com"', content)
        self.assertIn('GATEWAY_AUTH_KEY="gw_secret_auth_123"', content)
        self.assertIn('DEFAULT_CLOUD_USER="admin"', content)
        self.assertIn('CNSR_LANG="en"', content)

        # Forbidden cloud credentials
        self.assertNotIn("AWS_ACCESS_KEY_ID", content)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", content)
        self.assertNotIn("CF_API_TOKEN", content)
        self.assertNotIn("CF_ZONE_ID", content)
        self.assertNotIn("AKIA_LEAKED_AWS_KEY", content)

        # Zero-secret mode: TG/GH tokens must be stripped
        self.assertNotIn("TG_BOT_TOKEN", content)
        self.assertNotIn("TG_CHAT_ID", content)
        self.assertNotIn("GH_TOKEN", content)

    def test_sanitize_env_for_node_direct_mode(self):
        """sanitize_env_for_node in Direct mode (no Gateway) must retain TG/GH tokens but strip AWS/CF secrets."""
        out_file = os.path.join(self.temp_dir, "direct.env")
        cmd = f"""
        unset GATEWAY_URL
        unset GATEWAY_AUTH_KEY
        export DEFAULT_CLOUD_USER="ubuntu"
        export CNSR_LANG="zh"
        export TG_BOT_TOKEN="123456:valid_tg_token"
        export TG_CHAT_ID="888888"
        export GH_TOKEN="ghp_valid_gh_token"
        export AWS_ACCESS_KEY_ID="AKIA_LEAKED_AWS_KEY"
        export AWS_SECRET_ACCESS_KEY="sensitive_aws_secret"
        export CF_API_TOKEN="sensitive_cf_token"
        export CF_ZONE_ID="sensitive_cf_zone"

        source "{self.security_sh}"
        sanitize_env_for_node "{out_file}"
        """
        res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"sanitize_env_for_node failed:\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}")

        with open(out_file, "r") as f:
            content = f.read()

        # Retained direct variables
        self.assertIn('TG_BOT_TOKEN="123456:valid_tg_token"', content)
        self.assertIn('TG_CHAT_ID="888888"', content)
        self.assertIn('GH_TOKEN="ghp_valid_gh_token"', content)
        self.assertIn('DEFAULT_CLOUD_USER="ubuntu"', content)
        self.assertIn('CNSR_LANG="zh"', content)

        # Forbidden cloud credentials
        self.assertNotIn("AWS_ACCESS_KEY_ID", content)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", content)
        self.assertNotIn("CF_API_TOKEN", content)
        self.assertNotIn("CF_ZONE_ID", content)

    def test_module_harden_system_missing_alias(self):
        """module_harden_system without alias must return error."""
        cmd = f"""
        source "{self.security_sh}"
        module_harden_system ""
        """
        res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)

    def test_module_harden_system_execution(self):
        """module_harden_system executes remote hardening script and updates SSH config."""
        bin_dir = os.path.join(self.temp_dir, "bin")
        os.makedirs(bin_dir)
        log_file = os.path.join(self.temp_dir, "remote_ssh.log")

        # Mock initial SSH config
        with open(self.test_ssh_config, "w") as f:
            f.write("""Host test_node
    HostName 198.51.100.55
    User initial_admin
""")

        # Mock ssh command
        mock_ssh = os.path.join(bin_dir, "ssh")
        with open(mock_ssh, "w") as f:
            f.write(f"""#!/bin/bash
echo "SSH_ARGS: $@" >> "{log_file}"
# If evaluating home or host
if [[ "$*" == *"eval echo ~"* ]]; then
    echo "/home/initial_admin"
    exit 0
fi
if [[ "$*" == *"sshd -t"* ]]; then
    exit 0
fi
exit 0
""")
        os.chmod(mock_ssh, 0o755)

        cmd = f"""
        export PATH="{bin_dir}:$PATH"
        export TEST_SSH_CONFIG="{self.test_ssh_config}"
        source "{self.security_sh}"
        module_harden_system "test_node" "198.51.100.55" "28392" "arrakis"
        """
        res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"module_harden_system failed:\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}")

        # Check SSH config updated
        with open(self.test_ssh_config, "r") as f:
            ssh_conf = f.read()

        self.assertIn("Host test_node", ssh_conf)
        self.assertIn("User arrakis", ssh_conf)
        self.assertIn("Port 28392", ssh_conf)
        self.assertNotIn("User initial_admin", ssh_conf)

        # Check that SSH was invoked with remote hardening script
        with open(log_file, "r") as f:
            ssh_log = f.read()

        self.assertIn("ufw", ssh_log)
        self.assertIn("fail2ban", ssh_log)
        self.assertIn("arrakis", ssh_log)

    def test_remote_sshd_and_ufw_hardening_idempotency(self):
        """Remote sshd_config and UFW modifications must be idempotent when executed repeatedly."""
        mock_sshd_dir = os.path.join(self.temp_dir, "etc_ssh")
        os.makedirs(mock_sshd_dir)
        mock_sshd_config = os.path.join(mock_sshd_dir, "sshd_config")

        # Initial sshd_config with default standard comments and port 22
        with open(mock_sshd_config, "w") as f:
            f.write("""# Package generated configuration file
# See the sshd_config(5) manpage for details

# What ports, IPs and protocols we listen for
#Port 22
# AddressFamily any
#ListenAddress 0.0.0.0

# Authentication:
#LoginGraceTime 2m
#PermitRootLogin prohibit-password
#StrictModes yes
#MaxAuthTries 6

# To disable tunneled clear text passwords, change to no here!
#PasswordAuthentication yes
#PermitEmptyPasswords no
""")

        # Script simulating remote execution of sshd modification block
        harden_snippet = f"""
        NEW_SSH_PORT="39999"
        SHADOW_USER="cyberdyne"
        SSHD_CONFIG_FILE="{mock_sshd_config}"

        # 1. Port
        if grep -qE '^[#[:space:]]*Port[[:space:]]+' "$SSHD_CONFIG_FILE"; then
            sed -i '' -E 's/^[#[:space:]]*Port[[:space:]]+.*/Port '"$NEW_SSH_PORT"'/' "$SSHD_CONFIG_FILE" 2>/dev/null || sed -i -E 's/^[#[:space:]]*Port[[:space:]]+.*/Port '"$NEW_SSH_PORT"'/' "$SSHD_CONFIG_FILE"
        else
            echo "Port $NEW_SSH_PORT" >> "$SSHD_CONFIG_FILE"
        fi

        # 2. PermitRootLogin
        if grep -qE '^[#[:space:]]*PermitRootLogin[[:space:]]+' "$SSHD_CONFIG_FILE"; then
            sed -i '' -E 's/^[#[:space:]]*PermitRootLogin[[:space:]]+.*/PermitRootLogin no/' "$SSHD_CONFIG_FILE" 2>/dev/null || sed -i -E 's/^[#[:space:]]*PermitRootLogin[[:space:]]+.*/PermitRootLogin no/' "$SSHD_CONFIG_FILE"
        else
            echo "PermitRootLogin no" >> "$SSHD_CONFIG_FILE"
        fi

        # 3. PasswordAuthentication
        if grep -qE '^[#[:space:]]*PasswordAuthentication[[:space:]]+' "$SSHD_CONFIG_FILE"; then
            sed -i '' -E 's/^[#[:space:]]*PasswordAuthentication[[:space:]]+.*/PasswordAuthentication no/' "$SSHD_CONFIG_FILE" 2>/dev/null || sed -i -E 's/^[#[:space:]]*PasswordAuthentication[[:space:]]+.*/PasswordAuthentication no/' "$SSHD_CONFIG_FILE"
        else
            echo "PasswordAuthentication no" >> "$SSHD_CONFIG_FILE"
        fi

        # 4. AllowUsers
        if grep -qE '^[#[:space:]]*AllowUsers[[:space:]]+' "$SSHD_CONFIG_FILE"; then
            if ! grep -E '^[[:space:]]*AllowUsers[[:space:]]+' "$SSHD_CONFIG_FILE" | grep -qw "$SHADOW_USER"; then
                sed -i '' -E 's/^[#[:space:]]*AllowUsers[[:space:]]+.*/AllowUsers '"$SHADOW_USER"'/' "$SSHD_CONFIG_FILE" 2>/dev/null || sed -i -E 's/^[#[:space:]]*AllowUsers[[:space:]]+.*/AllowUsers '"$SHADOW_USER"'/' "$SSHD_CONFIG_FILE"
            fi
        else
            echo "AllowUsers $SHADOW_USER" >> "$SSHD_CONFIG_FILE"
        fi
        """

        # First run
        res1 = subprocess.run(["bash", "-c", harden_snippet], capture_output=True, text=True)
        self.assertEqual(res1.returncode, 0)

        with open(mock_sshd_config, "r") as f:
            content1 = f.read()

        self.assertEqual(content1.count("Port 39999"), 1)
        self.assertEqual(content1.count("PermitRootLogin no"), 1)
        self.assertEqual(content1.count("PasswordAuthentication no"), 1)
        self.assertEqual(content1.count("AllowUsers cyberdyne"), 1)

        # Second run (Idempotency check)
        res2 = subprocess.run(["bash", "-c", harden_snippet], capture_output=True, text=True)
        self.assertEqual(res2.returncode, 0)

        with open(mock_sshd_config, "r") as f:
            content2 = f.read()

        self.assertEqual(content2.count("Port 39999"), 1)
        self.assertEqual(content2.count("PermitRootLogin no"), 1)
        self.assertEqual(content2.count("PasswordAuthentication no"), 1)
        self.assertEqual(content2.count("AllowUsers cyberdyne"), 1)

    def test_module_harden_system_default_port_and_user_generation(self):
        """module_harden_system without port and user should generate valid defaults."""
        bin_dir = os.path.join(self.temp_dir, "bin")
        os.makedirs(bin_dir)

        with open(self.test_ssh_config, "w") as f:
            f.write("""Host auto_node
    HostName 198.51.100.88
    User root
""")

        mock_ssh = os.path.join(bin_dir, "ssh")
        with open(mock_ssh, "w") as f:
            f.write("""#!/bin/bash
if [[ "$*" == *"eval echo ~"* ]]; then echo "/root"; exit 0; fi
exit 0
""")
        os.chmod(mock_ssh, 0o755)

        cmd = f"""
        export PATH="{bin_dir}:$PATH"
        export TEST_SSH_CONFIG="{self.test_ssh_config}"
        source "{self.security_sh}"
        module_harden_system "auto_node" "198.51.100.88"
        """
        res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"module_harden_system auto generation failed:\n{res.stderr}")

        with open(self.test_ssh_config, "r") as f:
            content = f.read()

        valid_users = ["arrakis", "replicant", "skynet", "gotham", "trantor", "jedi", "cyberdyne", "matrix", "nostromo", "tesseract"]
        found_user = any(f"User {u}" in content for u in valid_users)
        self.assertTrue(found_user, f"Config does not contain a generated sci-fi shadow user:\n{content}")

        # Check port is non-standard high port (> 1024)
        import re
        port_match = re.search(r'Port\s+(\d+)', content)
        self.assertIsNotNone(port_match, f"Config missing generated Port:\n{content}")
        port_val = int(port_match.group(1))
        self.assertTrue(10000 <= port_val <= 65535, f"Port {port_val} out of expected range 10000-65535")

    def test_module_harden_system_remote_ssh_failure(self):
        """module_harden_system must return non-zero if remote SSH commands fail."""
        bin_dir = os.path.join(self.temp_dir, "bin")
        os.makedirs(bin_dir)

        mock_ssh = os.path.join(bin_dir, "ssh")
        with open(mock_ssh, "w") as f:
            f.write("""#!/bin/bash
exit 255
""")
        os.chmod(mock_ssh, 0o755)

        cmd = f"""
        export PATH="{bin_dir}:$PATH"
        export TEST_SSH_CONFIG="{self.test_ssh_config}"
        source "{self.security_sh}"
        module_harden_system "failing_node" "198.51.100.99" "2222" "arrakis"
        """
        res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("远端加固流程执行失败", res.stdout)

    def test_module_harden_system_preserves_existing_identity_file(self):
        """module_harden_system must preserve existing IdentityFile in SSH config."""
        bin_dir = os.path.join(self.temp_dir, "bin")
        os.makedirs(bin_dir)

        with open(self.test_ssh_config, "w") as f:
            f.write("""Host key_node
    HostName 198.51.100.77
    User old_user
    IdentityFile ~/.ssh/custom_prod_key
    IdentitiesOnly yes
""")

        mock_ssh = os.path.join(bin_dir, "ssh")
        with open(mock_ssh, "w") as f:
            f.write("""#!/bin/bash
exit 0
""")
        os.chmod(mock_ssh, 0o755)

        cmd = f"""
        export PATH="{bin_dir}:$PATH"
        export TEST_SSH_CONFIG="{self.test_ssh_config}"
        source "{self.security_sh}"
        module_harden_system "key_node" "198.51.100.77" "33445" "matrix"
        """
        res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)

        with open(self.test_ssh_config, "r") as f:
            content = f.read()

        self.assertIn("Host key_node", content)
        self.assertIn("User matrix", content)
        self.assertIn("Port 33445", content)
        self.assertIn("IdentityFile ~/.ssh/custom_prod_key", content)
        self.assertIn("IdentitiesOnly yes", content)

    def test_module_harden_system_aws_cloud_firewall_probe(self):
        """module_harden_system opens port via AWS CLI when running on Lightsail."""
        bin_dir = os.path.join(self.temp_dir, "bin")
        os.makedirs(bin_dir)
        log_file = os.path.join(self.temp_dir, "aws_cli.log")

        mock_ssh = os.path.join(bin_dir, "ssh")
        with open(mock_ssh, "w") as f:
            f.write("""#!/bin/bash
if [[ "$*" == *"169.254.169.254"* ]]; then
    echo "ap-southeast-1"
    exit 0
fi
exit 0
""")
        os.chmod(mock_ssh, 0o755)

        mock_aws = os.path.join(bin_dir, "aws")
        with open(mock_aws, "w") as f:
            f.write(f"""#!/bin/bash
echo "AWS_ARGS: $@" >> "{log_file}"
if [[ "$*" == *"get-instances"* ]]; then
    echo '{{"instances": [{{"name": "snack-lightsail-node", "publicIpAddress": "198.51.100.66"}}]}}'
    exit 0
fi
if [[ "$*" == *"open-instance-port"* ]]; then
    exit 0
fi
exit 0
""")
        os.chmod(mock_aws, 0o755)

        cmd = f"""
        export PATH="{bin_dir}:$PATH"
        export TEST_SSH_CONFIG="{self.test_ssh_config}"
        source "{self.security_sh}"
        module_harden_system "aws_node" "198.51.100.66" "45678" "tesseract"
        """
        res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("已自动调用 AWS API 在 Lightsail 云防火墙中放行 45678 端口", res.stdout)

        with open(log_file, "r") as f:
            aws_log = f.read()

        self.assertIn("open-instance-port", aws_log)
        self.assertIn("fromPort=45678", aws_log)
        self.assertIn("--instance-name snack-lightsail-node", aws_log)

if __name__ == "__main__":
    unittest.main()
