import unittest
import subprocess
import os

class TestGatewayWorker(unittest.TestCase):
    def test_worker_javascript_suite(self):
        jsc_bin = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc"
        test_file = os.path.join(os.path.dirname(__file__), "..", "gateway", "test_worker.js")
        
        proc = subprocess.run([jsc_bin, "-m", test_file], capture_output=True, text=True)
        print(proc.stdout)
        self.assertEqual(proc.returncode, 0, f"Worker test suite failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
        self.assertIn("38 passed, 0 failed.", proc.stdout)

if __name__ == "__main__":
    unittest.main()
