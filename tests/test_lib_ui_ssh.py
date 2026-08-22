import unittest
import subprocess
import os
import tempfile
import shutil

class TestLibUi(unittest.TestCase):
    def setUp(self):
        self.repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        self.ui_sh = os.path.join(self.repo_root, "lib", "ui.sh")

    def test_ui_syntax(self):
        """lib/ui.sh must pass bash -n static syntax check."""
        res = subprocess.run(["bash", "-n", self.ui_sh], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Syntax error in lib/ui.sh:\n{res.stderr}")

    def test_print_usage_zh(self):
        """print_usage with CNSR_LANG=zh should output Chinese help text."""
        cmd = f"""
        source "{self.ui_sh}"
        CNSR_LANG="zh" print_usage 0
        """
        res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("Snack Connoisseur (cnsr) - X-ray REALITY 全自动部署与防封锁自愈中枢", res.stdout)
        self.assertIn("用法 / USAGE:", res.stdout)

    def test_print_usage_en(self):
        """print_usage with CNSR_LANG=en should output English help text."""
        cmd = f"""
        source "{self.ui_sh}"
        CNSR_LANG="en" print_usage 0
        """
        res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("Snack Connoisseur (cnsr) - X-ray REALITY Controller & Anti-Censorship Suite", res.stdout)
        self.assertIn("USAGE:", res.stdout)

    def test_print_usage_exit_code(self):
        """print_usage should exit with provided exit code."""
        cmd = f"""
        source "{self.ui_sh}"
        print_usage 2
        """
        res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
        self.assertEqual(res.returncode, 2)

    def test_select_menu_non_interactive(self):
        """select_menu in non-interactive mode should default to MENU_CHOICE=1."""
        cmd = f"""
        source "{self.ui_sh}"
        select_menu "Test prompt" "Option 1" "Option 2"
        echo "CHOICE=$MENU_CHOICE"
        """
        res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("CHOICE=1", res.stdout)


class TestLibSsh(unittest.TestCase):
    def setUp(self):
        self.repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        self.ssh_sh = os.path.join(self.repo_root, "lib", "ssh.sh")
        self.temp_dir = tempfile.mkdtemp(prefix="snack_test_ssh_")
        self.test_ssh_config = os.path.join(self.temp_dir, "ssh_config")

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_ssh_syntax(self):
        """lib/ssh.sh must pass bash -n static syntax check."""
        res = subprocess.run(["bash", "-n", self.ssh_sh], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Syntax error in lib/ssh.sh:\n{res.stderr}")

    def test_guess_default_user(self):
        """Test username inference in lib/ssh.sh."""
        cases = [
            ("aws-sg-01", "admin"),
            ("lightsail-tokyo", "admin"),
            ("my-ubuntu-box", "ubuntu"),
            ("oci-us-west", "ubuntu"),
            ("ec2-amazon-linux", "ec2-user"),
            ("opc-custom", "opc"),
            ("gcp-instance", "admin"),
            ("dmit-hk-pro", "root"),
        ]
        for alias, expected_user in cases:
            cmd = f"""
            source "{self.ssh_sh}"
            guess_default_user "{alias}"
            """
            res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0)
            self.assertEqual(res.stdout.strip(), expected_user)

    def test_ensure_ssh_alias_creates_new_entry(self):
        """ensure_ssh_alias creates new Host block in TEST_SSH_CONFIG."""
        cmd = f"""
        export TEST_SSH_CONFIG="{self.test_ssh_config}"
        source "{self.ssh_sh}"
        ensure_ssh_alias "sg_test" "198.51.100.10" "admin" "22" "~/.ssh/id_rsa"
        """
        res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"ensure_ssh_alias failed:\nSTDERR:{res.stderr}")

        with open(self.test_ssh_config, "r") as f:
            content = f.read()

        self.assertIn("Host sg_test", content)
        self.assertIn("HostName 198.51.100.10", content)
        self.assertIn("User admin", content)
        self.assertIn("IdentityFile ~/.ssh/id_rsa", content)
        self.assertIn("StrictHostKeyChecking accept-new", content)
        self.assertNotIn("Port 22", content)  # Port 22 defaults and does not add redundant Port 22

    def test_ensure_ssh_alias_custom_port(self):
        """ensure_ssh_alias with non-22 port should include Port line."""
        cmd = f"""
        export TEST_SSH_CONFIG="{self.test_ssh_config}"
        source "{self.ssh_sh}"
        ensure_ssh_alias "sg_hardened" "198.51.100.20" "secuser" "2222"
        """
        res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)

        with open(self.test_ssh_config, "r") as f:
            content = f.read()

        self.assertIn("Host sg_hardened", content)
        self.assertIn("HostName 198.51.100.20", content)
        self.assertIn("User secuser", content)
        self.assertIn("Port 2222", content)

    def test_ensure_ssh_alias_idempotent_update(self):
        """ensure_ssh_alias updating existing block must modify values without duplicating."""
        # First creation
        cmd1 = f"""
        export TEST_SSH_CONFIG="{self.test_ssh_config}"
        source "{self.ssh_sh}"
        ensure_ssh_alias "node1" "1.1.1.1" "root" "2222"
        """
        res1 = subprocess.run(["bash", "-c", cmd1], capture_output=True, text=True)
        self.assertEqual(res1.returncode, 0)

        # Second update: change IP to 2.2.2.2, user to admin, reset port to 22
        cmd2 = f"""
        export TEST_SSH_CONFIG="{self.test_ssh_config}"
        source "{self.ssh_sh}"
        ensure_ssh_alias "node1" "2.2.2.2" "admin" "22" "~/.ssh/new_key"
        """
        res2 = subprocess.run(["bash", "-c", cmd2], capture_output=True, text=True)
        self.assertEqual(res2.returncode, 0)

        with open(self.test_ssh_config, "r") as f:
            content = f.read()

        self.assertEqual(content.count("Host node1"), 1)
        self.assertIn("HostName 2.2.2.2", content)
        self.assertNotIn("HostName 1.1.1.1", content)
        self.assertIn("User admin", content)
        self.assertNotIn("User root", content)
        self.assertNotIn("Port 2222", content)  # Reset to default 22 should remove 2222
        self.assertIn("IdentityFile ~/.ssh/new_key", content)

    def test_get_ssh_config(self):
        """get_ssh_config extracts specific attributes from TEST_SSH_CONFIG."""
        with open(self.test_ssh_config, "w") as f:
            f.write("""Host test_alias
    HostName 198.51.100.50
    User debian
    Port 2200
    IdentityFile ~/.ssh/id_test
""")

        cmd_hostname = f"""
        export TEST_SSH_CONFIG="{self.test_ssh_config}"
        source "{self.ssh_sh}"
        get_ssh_config "test_alias" "HostName"
        """
        res = subprocess.run(["bash", "-c", cmd_hostname], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), "198.51.100.50")

        cmd_user = f"""
        export TEST_SSH_CONFIG="{self.test_ssh_config}"
        source "{self.ssh_sh}"
        get_ssh_config "test_alias" "User"
        """
        res = subprocess.run(["bash", "-c", cmd_user], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), "debian")

        cmd_port = f"""
        export TEST_SSH_CONFIG="{self.test_ssh_config}"
        source "{self.ssh_sh}"
        get_ssh_config "test_alias" "Port"
        """
        res = subprocess.run(["bash", "-c", cmd_port], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), "2200")

    def test_get_real_host(self):
        """get_real_host should return HostName if in config, or fallback to alias."""
        with open(self.test_ssh_config, "w") as f:
            f.write("""Host my_node
    HostName 198.51.100.99
    User ubuntu
""")

        # When configured
        cmd = f"""
        export TEST_SSH_CONFIG="{self.test_ssh_config}"
        source "{self.ssh_sh}"
        get_real_host "my_node"
        """
        res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), "198.51.100.99")

        # When not configured
        cmd_fallback = f"""
        export TEST_SSH_CONFIG="{self.test_ssh_config}"
        source "{self.ssh_sh}"
        get_real_host "unconfigured_node"
        """
        res = subprocess.run(["bash", "-c", cmd_fallback], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), "unconfigured_node")

    def test_check_ssh_conn(self):
        """check_ssh_conn should invoke ssh with sandbox config path and return ssh exit code."""
        bin_dir = os.path.join(self.temp_dir, "bin")
        os.makedirs(bin_dir)
        mock_ssh = os.path.join(bin_dir, "ssh")
        log_file = os.path.join(self.temp_dir, "ssh_call.log")

        with open(mock_ssh, "w") as f:
            f.write(f"""#!/bin/bash
echo "SSH_ARGS: $@" >> "{log_file}"
if [[ "$*" == *"success_node"* ]]; then
    exit 0
else
    exit 255
fi
""")
        os.chmod(mock_ssh, 0o755)

        cmd = f"""
        export PATH="{bin_dir}:$PATH"
        export TEST_SSH_CONFIG="{self.test_ssh_config}"
        touch "{self.test_ssh_config}"
        source "{self.ssh_sh}"
        if check_ssh_conn "success_node" 2; then
            echo "CONN_OK"
        else
            echo "CONN_FAIL"
        fi
        if check_ssh_conn "fail_node" 2; then
            echo "FAIL_WAS_OK"
        else
            echo "FAIL_EXPECTED"
        fi
        """
        res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("CONN_OK", res.stdout)
        self.assertIn("FAIL_EXPECTED", res.stdout)

        with open(log_file, "r") as f:
            ssh_log = f.read()
        self.assertIn(f"-F {self.test_ssh_config}", ssh_log)
        self.assertIn("-o ConnectTimeout=2", ssh_log)

if __name__ == "__main__":
    unittest.main()
