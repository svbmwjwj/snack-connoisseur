import unittest
import subprocess
import os
import tempfile
import shutil
import json

class TestCoreOperations(unittest.TestCase):
    def setUp(self):
        self.repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        self.core_dir = os.path.join(self.repo_root, "core")
        self.lib_dir = os.path.join(self.repo_root, "lib")
        self.temp_dir = tempfile.mkdtemp(prefix="snack_test_core_")
        self.test_ssh_config = os.path.join(self.temp_dir, "ssh_config")

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def _setup_bin_dir(self, extra_tools=None):
        bin_dir = os.path.join(self.temp_dir, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        # Mock uv to execute python3 without triggering sandbox ~/.local/share/uv path block
        mock_uv = os.path.join(bin_dir, "uv")
        with open(mock_uv, "w") as f:
            f.write("""#!/bin/bash
if [ "$1" = "run" ]; then
    shift
    exec "$@"
fi
exec "$@"
""")
        os.chmod(mock_uv, 0o755)
        return bin_dir

    # ------------------------------------------------------------------
    # 1. Static syntax validation (bash -n)
    # ------------------------------------------------------------------
    def test_core_scripts_exist_and_pass_syntax(self):
        """All core operations scripts must exist and pass bash -n static check."""
        for script_name in ["check.sh", "update.sh", "rotate.sh", "test.sh"]:
            script_path = os.path.join(self.core_dir, script_name)
            self.assertTrue(os.path.exists(script_path), f"File {script_path} does not exist!")
            res = subprocess.run(["bash", "-n", script_path], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f"Syntax error in {script_name}:\n{res.stderr}")

    # ------------------------------------------------------------------
    # 2. Test core/update.sh
    # ------------------------------------------------------------------
    def test_module_update_missing_alias(self):
        """module_update without alias must exit with error message."""
        update_sh = os.path.join(self.core_dir, "update.sh")
        cmd = f"""
        source "{update_sh}"
        module_update ""
        """
        res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("请指定节点别名", res.stdout)

    def test_module_update_execution(self):
        """module_update should invoke sync_node_scripts and print success message."""
        update_sh = os.path.join(self.core_dir, "update.sh")
        bin_dir = self._setup_bin_dir()
        log_file = os.path.join(self.temp_dir, "update_ssh.log")

        mock_ssh = os.path.join(bin_dir, "ssh")
        with open(mock_ssh, "w") as f:
            f.write(f"""#!/bin/bash
echo "SSH: $@" >> "{log_file}"
if [[ "$*" == *"eval echo ~"* ]]; then echo "/home/admin"; exit 0; fi
if [[ "$*" == *"curl -4"* ]]; then echo "198.51.100.33"; exit 0; fi
if [[ "$*" == *"-G"* ]]; then echo "hostname 198.51.100.33"; exit 0; fi
exit 0
""")
        os.chmod(mock_ssh, 0o755)

        mock_scp = os.path.join(bin_dir, "scp")
        with open(mock_scp, "w") as f:
            f.write(f"""#!/bin/bash
echo "SCP: $@" >> "{log_file}"
exit 0
""")
        os.chmod(mock_scp, 0o755)

        cmd = f"""
        export PATH="{bin_dir}:$PATH"
        export TEST_SSH_CONFIG="{self.test_ssh_config}"
        touch "{self.test_ssh_config}"
        source "{update_sh}"
        module_update "sg_node_test"
        """
        res = subprocess.run(["bash", "-c", cmd], cwd=self.repo_root, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"module_update failed:\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}")
        self.assertIn("全量组件与脱敏凭据热更新完成", res.stdout)

        with open(log_file, "r") as f:
            log_content = f.read()
        self.assertIn("runner.sh", log_content)

    # ------------------------------------------------------------------
    # 3. Test core/check.sh
    # ------------------------------------------------------------------
    def test_module_check_missing_alias(self):
        """module_check without alias must fail."""
        check_sh = os.path.join(self.core_dir, "check.sh")
        cmd = f"""
        source "{check_sh}"
        module_check ""
        """
        res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("请指定节点别名", res.stdout)

    def test_module_check_execution(self):
        """module_check should sync scripts and invoke reality_check.sh --notify remotely."""
        check_sh = os.path.join(self.core_dir, "check.sh")
        bin_dir = self._setup_bin_dir()
        log_file = os.path.join(self.temp_dir, "check_ssh.log")

        mock_ssh = os.path.join(bin_dir, "ssh")
        with open(mock_ssh, "w") as f:
            f.write(f"""#!/bin/bash
echo "SSH: $@" >> "{log_file}"
if [[ "$*" == *"eval echo ~"* ]]; then echo "/home/admin"; exit 0; fi
if [[ "$*" == *"curl -4"* ]]; then echo "198.51.100.44"; exit 0; fi
if [[ "$*" == *"-G"* ]]; then echo "hostname 198.51.100.44"; exit 0; fi
if [[ "$*" == *"reality_check.sh --notify"* ]]; then
    echo "MOCK_CHECK_DONE"
    exit 0
fi
exit 0
""")
        os.chmod(mock_ssh, 0o755)

        mock_scp = os.path.join(bin_dir, "scp")
        with open(mock_scp, "w") as f:
            f.write(f"""#!/bin/bash
echo "SCP: $@" >> "{log_file}"
exit 0
""")
        os.chmod(mock_scp, 0o755)

        cmd = f"""
        export PATH="{bin_dir}:$PATH"
        export TEST_SSH_CONFIG="{self.test_ssh_config}"
        touch "{self.test_ssh_config}"
        source "{check_sh}"
        module_check "sg_node_check"
        """
        res = subprocess.run(["bash", "-c", cmd], cwd=self.repo_root, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"module_check failed:\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}")
        self.assertIn("远端体检完成", res.stdout)

        with open(log_file, "r") as f:
            log_content = f.read()
        self.assertIn("reality_check.sh --notify", log_content)

    # ------------------------------------------------------------------
    # 4. Test core/rotate.sh
    # ------------------------------------------------------------------
    def test_module_rotate_sni_execution(self):
        """module_rotate_sni should invoke reality_rotate.sh --force on the remote host."""
        rotate_sh = os.path.join(self.core_dir, "rotate.sh")
        bin_dir = self._setup_bin_dir()
        log_file = os.path.join(self.temp_dir, "rotate_sni.log")

        mock_ssh = os.path.join(bin_dir, "ssh")
        with open(mock_ssh, "w") as f:
            f.write(f"""#!/bin/bash
echo "SSH: $@" >> "{log_file}"
if [[ "$*" == *"eval echo ~"* ]]; then echo "/home/admin"; exit 0; fi
if [[ "$*" == *"curl -4"* ]]; then echo "198.51.100.55"; exit 0; fi
if [[ "$*" == *"-G"* ]]; then echo "hostname 198.51.100.55"; exit 0; fi
if [[ "$*" == *"reality_rotate.sh --force"* ]]; then
    echo "ROTATION_SUCCESS"
    exit 0
fi
exit 0
""")
        os.chmod(mock_ssh, 0o755)

        mock_scp = os.path.join(bin_dir, "scp")
        with open(mock_scp, "w") as f:
            f.write("exit 0\n")
        os.chmod(mock_scp, 0o755)

        cmd = f"""
        export PATH="{bin_dir}:$PATH"
        export TEST_SSH_CONFIG="{self.test_ssh_config}"
        touch "{self.test_ssh_config}"
        source "{rotate_sh}"
        module_rotate_sni "sg_node_rotate"
        """
        res = subprocess.run(["bash", "-c", cmd], cwd=self.repo_root, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"module_rotate_sni failed:\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}")
        self.assertIn("指纹轮换完毕", res.stdout)

        with open(log_file, "r") as f:
            log_content = f.read()
        self.assertIn("reality_rotate.sh --force", log_content)

    def test_module_rotate_sni_dry_run(self):
        """module_rotate_sni with DRY_RUN=true / --dry-run should run in test mode."""
        rotate_sh = os.path.join(self.core_dir, "rotate.sh")
        bin_dir = self._setup_bin_dir()
        log_file = os.path.join(self.temp_dir, "rotate_dry_run.log")

        mock_ssh = os.path.join(bin_dir, "ssh")
        with open(mock_ssh, "w") as f:
            f.write(f"""#!/bin/bash
echo "SSH: $@" >> "{log_file}"
if [[ "$*" == *"eval echo ~"* ]]; then echo "/home/admin"; exit 0; fi
if [[ "$*" == *"curl -4"* ]]; then echo "198.51.100.55"; exit 0; fi
if [[ "$*" == *"-G"* ]]; then echo "hostname 198.51.100.55"; exit 0; fi
exit 0
""")
        os.chmod(mock_ssh, 0o755)

        mock_scp = os.path.join(bin_dir, "scp")
        with open(mock_scp, "w") as f:
            f.write("exit 0\n")
        os.chmod(mock_scp, 0o755)

        cmd = f"""
        export PATH="{bin_dir}:$PATH"
        export TEST_SSH_CONFIG="{self.test_ssh_config}"
        touch "{self.test_ssh_config}"
        source "{rotate_sh}"
        DRY_RUN=true module_rotate_sni "sg_node_rotate"
        """
        res = subprocess.run(["bash", "-c", cmd], cwd=self.repo_root, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("Dry-Run", res.stdout)

        with open(log_file, "r") as f:
            log_content = f.read()
        self.assertIn("DRY_RUN=1", log_content)

    def test_module_rotate_dns_execution(self):
        """module_rotate_dns should invoke Cloudflare API, update remote host, and upgrade SSH config."""
        rotate_sh = os.path.join(self.core_dir, "rotate.sh")
        bin_dir = self._setup_bin_dir()
        log_file = os.path.join(self.temp_dir, "rotate_dns.log")

        with open(self.test_ssh_config, "w") as f:
            f.write("""Host sg_dns_node
    HostName 198.51.100.77
    User admin
""")

        mock_ssh = os.path.join(bin_dir, "ssh")
        with open(mock_ssh, "w") as f:
            f.write(f"""#!/bin/bash
echo "SSH: $@" >> "{log_file}"
if [[ "$*" == *"curl -4"* ]]; then echo "198.51.100.77"; exit 0; fi
if [[ "$*" == *"curl -6"* ]]; then echo "none"; exit 0; fi
if [[ "$*" == *"eval echo ~"* ]]; then echo "/home/admin"; exit 0; fi
exit 0
""")
        os.chmod(mock_ssh, 0o755)

        mock_curl = os.path.join(bin_dir, "curl")
        with open(mock_curl, "w") as f:
            f.write(r'''#!/bin/bash
# Mock Cloudflare API responses
cmd="$*"
if [[ "$cmd" == *"zones/mock_zone_id/dns_records"* ]]; then
    if [[ "$cmd" == *"DELETE"* ]]; then
        echo '{"success":true}'
        exit 0
    elif [[ "$cmd" == *"POST"* ]]; then
        echo '{"success":true,"result":{"id":"rec123"}}'
        exit 0
    else
        echo '{"success":true,"result":[{"id":"old_rec_1","type":"A","content":"198.51.100.77"}]}'
        exit 0
    fi
elif [[ "$cmd" == *"zones/mock_zone_id"* ]]; then
    echo '{"success":true,"result":{"name":"snackdomain.com"}}'
    exit 0
fi
exit 0
''')
        os.chmod(mock_curl, 0o755)

        cmd = f"""
        export PATH="{bin_dir}:$PATH"
        export TEST_SSH_CONFIG="{self.test_ssh_config}"
        export CF_API_TOKEN="mock_token"
        export CF_ZONE_ID="mock_zone_id"
        source "{rotate_sh}"
        module_rotate_dns "sg_dns_node"
        """
        res = subprocess.run(["bash", "-c", cmd], cwd=self.repo_root, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"module_rotate_dns failed:\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}")
        self.assertIn("伪装域名已更新", res.stdout)

        with open(log_file, "r") as f:
            log_content = f.read()
        self.assertIn("SERVER_HOST=", log_content)

    def test_module_rotate_dns_cloudflare_api_failure(self):
        """module_rotate_dns should fail and not report success if Cloudflare API returns error."""
        rotate_sh = os.path.join(self.core_dir, "rotate.sh")
        bin_dir = self._setup_bin_dir()

        mock_ssh = os.path.join(bin_dir, "ssh")
        with open(mock_ssh, "w") as f:
            f.write("""#!/bin/bash
if [[ "$*" == *"curl -4"* ]]; then echo "198.51.100.77"; exit 0; fi
if [[ "$*" == *"curl -6"* ]]; then echo "none"; exit 0; fi
exit 0
""")
        os.chmod(mock_ssh, 0o755)

        mock_curl = os.path.join(bin_dir, "curl")
        with open(mock_curl, "w") as f:
            f.write(r'''#!/bin/bash
cmd="$*"
if [[ "$cmd" == *"zones/mock_zone_id/dns_records"* ]]; then
    if [[ "$cmd" == *"POST"* ]]; then
        echo '{"success":false,"errors":[{"message":"Authentication error: invalid token"}]}'
        exit 0
    fi
elif [[ "$cmd" == *"zones/mock_zone_id"* ]]; then
    echo '{"success":true,"result":{"name":"snackdomain.com"}}'
    exit 0
fi
exit 0
''')
        os.chmod(mock_curl, 0o755)

        cmd = f"""
        export PATH="{bin_dir}:$PATH"
        export TEST_SSH_CONFIG="{self.test_ssh_config}"
        export CF_API_TOKEN="invalid_token"
        export CF_ZONE_ID="mock_zone_id"
        source "{rotate_sh}"
        module_rotate_dns "sg_dns_fail"
        """
        res = subprocess.run(["bash", "-c", cmd], cwd=self.repo_root, capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        combined_out = res.stdout + res.stderr
        self.assertIn("Cloudflare DNS A 记录创建失败", combined_out)
        self.assertNotIn("伪装域名已更新", combined_out)

    def test_check_cf_credentials_idempotent_env_write(self):
        """set_env_var and check_cf_credentials should write to REPO_DIR/.env idempotently without duplicate lines."""
        rotate_sh = os.path.join(self.core_dir, "rotate.sh")
        mock_env = os.path.join(self.repo_root, ".env")
        env_existed = os.path.exists(mock_env)
        orig_env_content = ""
        if env_existed:
            with open(mock_env, "r") as f:
                orig_env_content = f.read()

        try:
            cmd = f"""
            source "{rotate_sh}"
            set_env_var "TEST_CF_TOKEN" "token_v1"
            set_env_var "TEST_CF_TOKEN" "token_v2"
            """
            res = subprocess.run(["bash", "-c", cmd], cwd=self.temp_dir, capture_output=True, text=True)
            self.assertEqual(res.returncode, 0)

            with open(mock_env, "r") as f:
                env_content = f.read()

            self.assertEqual(env_content.count("TEST_CF_TOKEN="), 1)
            self.assertIn('TEST_CF_TOKEN="token_v2"', env_content)
        finally:
            if env_existed:
                with open(mock_env, "w") as f:
                    f.write(orig_env_content)
            else:
                if os.path.exists(mock_env):
                    os.remove(mock_env)

    def test_module_rotate_ip_stateless_probe(self):
        """module_rotate_ip on non-AWS instance should intercept with manual node message."""
        rotate_sh = os.path.join(self.core_dir, "rotate.sh")
        bin_dir = self._setup_bin_dir()

        mock_ssh = os.path.join(bin_dir, "ssh")
        with open(mock_ssh, "w") as f:
            f.write("""#!/bin/bash
# Simulates a non-AWS node where metadata service times out or fails
if [[ "$*" == *"169.254.169.254"* ]]; then
    exit 1
fi
if [[ "$*" == *"curl -4"* ]]; then
    echo "198.51.100.88"
    exit 0
fi
exit 0
""")
        os.chmod(mock_ssh, 0o755)

        cmd = f"""
        export PATH="{bin_dir}:$PATH"
        export TEST_SSH_CONFIG="{self.test_ssh_config}"
        touch "{self.test_ssh_config}"
        source "{rotate_sh}"
        module_rotate_ip "manual_node"
        """
        res = subprocess.run(["bash", "-c", cmd], cwd=self.repo_root, capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertTrue("纯手动节点" in res.stdout or "无法获取实例所在地域" in res.stdout)

    # ------------------------------------------------------------------
    # 5. Test core/test.sh
    # ------------------------------------------------------------------
    def test_module_test_tg_execution(self):
        """module_test_tg should sync node scripts and execute reality_check.sh --test-tg with IS_TEST_MODE=1."""
        test_sh = os.path.join(self.core_dir, "test.sh")
        bin_dir = self._setup_bin_dir()
        log_file = os.path.join(self.temp_dir, "test_tg.log")

        mock_ssh = os.path.join(bin_dir, "ssh")
        with open(mock_ssh, "w") as f:
            f.write(f"""#!/bin/bash
echo "SSH: $@" >> "{log_file}"
if [[ "$*" == *"eval echo ~"* ]]; then echo "/home/admin"; exit 0; fi
if [[ "$*" == *"curl -4"* ]]; then echo "198.51.100.99"; exit 0; fi
if [[ "$*" == *"-G"* ]]; then echo "hostname 198.51.100.99"; exit 0; fi
if [[ "$*" == *"reality_check.sh --test-tg"* ]]; then
    echo "MOCK_TG_PREVIEW_SENT"
    exit 0
fi
exit 0
""")
        os.chmod(mock_ssh, 0o755)

        mock_scp = os.path.join(bin_dir, "scp")
        with open(mock_scp, "w") as f:
            f.write("exit 0\n")
        os.chmod(mock_scp, 0o755)

        cmd = f"""
        export PATH="{bin_dir}:$PATH"
        export TEST_SSH_CONFIG="{self.test_ssh_config}"
        touch "{self.test_ssh_config}"
        source "{test_sh}"
        module_test_tg "sg_node_tg"
        """
        res = subprocess.run(["bash", "-c", cmd], cwd=self.repo_root, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"module_test_tg failed:\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}")
        self.assertIn("样张预览", res.stdout)

        with open(log_file, "r") as f:
            log_content = f.read()
        self.assertIn("reality_check.sh --test-tg", log_content)
        self.assertIn("IS_TEST_MODE=1", log_content)

    def test_module_test_sni_execution(self):
        """module_test_sni should invoke reality_rotate.sh --force in dry-run test mode."""
        test_sh = os.path.join(self.core_dir, "test.sh")
        bin_dir = self._setup_bin_dir()
        log_file = os.path.join(self.temp_dir, "test_sni.log")

        mock_ssh = os.path.join(bin_dir, "ssh")
        with open(mock_ssh, "w") as f:
            f.write(f"""#!/bin/bash
echo "SSH: $@" >> "{log_file}"
if [[ "$*" == *"eval echo ~"* ]]; then echo "/home/admin"; exit 0; fi
if [[ "$*" == *"curl -4"* ]]; then echo "198.51.100.99"; exit 0; fi
if [[ "$*" == *"-G"* ]]; then echo "hostname 198.51.100.99"; exit 0; fi
if [[ "$*" == *"reality_rotate.sh --force"* ]]; then
    echo "MOCK_SNI_TEST_EVALUATED"
    exit 0
fi
exit 0
""")
        os.chmod(mock_ssh, 0o755)

        mock_scp = os.path.join(bin_dir, "scp")
        with open(mock_scp, "w") as f:
            f.write("exit 0\n")
        os.chmod(mock_scp, 0o755)

        cmd = f"""
        export PATH="{bin_dir}:$PATH"
        export TEST_SSH_CONFIG="{self.test_ssh_config}"
        touch "{self.test_ssh_config}"
        source "{test_sh}"
        module_test_sni "sg_node_sni"
        """
        res = subprocess.run(["bash", "-c", cmd], cwd=self.repo_root, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"module_test_sni failed:\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}")
        self.assertTrue("选域演练" in res.stdout or "SNI scores" in res.stdout)

        with open(log_file, "r") as f:
            log_content = f.read()
        self.assertIn("DRY_RUN=1", log_content)

    def test_module_test_dispatcher(self):
        """module_test should dispatch to tg or sni based on arguments."""
        test_sh = os.path.join(self.core_dir, "test.sh")
        bin_dir = self._setup_bin_dir()
        log_file = os.path.join(self.temp_dir, "test_disp.log")

        mock_ssh = os.path.join(bin_dir, "ssh")
        with open(mock_ssh, "w") as f:
            f.write(f"""#!/bin/bash
echo "SSH: $@" >> "{log_file}"
if [[ "$*" == *"eval echo ~"* ]]; then echo "/home/admin"; exit 0; fi
if [[ "$*" == *"curl -4"* ]]; then echo "198.51.100.99"; exit 0; fi
if [[ "$*" == *"-G"* ]]; then echo "hostname 198.51.100.99"; exit 0; fi
exit 0
""")
        os.chmod(mock_ssh, 0o755)

        mock_scp = os.path.join(bin_dir, "scp")
        with open(mock_scp, "w") as f:
            f.write("exit 0\n")
        os.chmod(mock_scp, 0o755)

        # 1. module_test "alias" "tg"
        cmd1 = f"""
        export PATH="{bin_dir}:$PATH"
        export TEST_SSH_CONFIG="{self.test_ssh_config}"
        touch "{self.test_ssh_config}"
        source "{test_sh}"
        module_test "sg_disp_node" "tg"
        """
        res1 = subprocess.run(["bash", "-c", cmd1], cwd=self.repo_root, capture_output=True, text=True)
        self.assertEqual(res1.returncode, 0)
        with open(log_file, "r") as f:
            self.assertIn("reality_check.sh --test-tg", f.read())

        # 2. module_test "alias" "sni"
        open(log_file, "w").close()
        cmd2 = f"""
        export PATH="{bin_dir}:$PATH"
        export TEST_SSH_CONFIG="{self.test_ssh_config}"
        touch "{self.test_ssh_config}"
        source "{test_sh}"
        module_test "sg_disp_node" "sni"
        """
        res2 = subprocess.run(["bash", "-c", cmd2], cwd=self.repo_root, capture_output=True, text=True)
        self.assertEqual(res2.returncode, 0)
        with open(log_file, "r") as f:
            self.assertIn("reality_rotate.sh --force", f.read())

if __name__ == "__main__":
    unittest.main()
