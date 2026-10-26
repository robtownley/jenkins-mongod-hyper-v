import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('installer',ROOT/'ansible/files/install_mongodb.py')
installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)

class InstallerTest(unittest.TestCase):
    def test_dnf_has_finite_network_retries_and_ipv4(self):
        for option in ('--setopt=ip_resolve=4','--setopt=timeout=30','--setopt=retries=2','--setopt=exit_on_lock=True'):
            self.assertIn(option,installer.DNF)
        self.assertEqual(installer.DNF[-2:],['install','mongodb-org'])
    def test_normal_completion_returns_exit_code(self):
        class Process:
            def wait(self,timeout): return 0
        self.assertEqual(installer.wait_bounded(Process(),Path('/nonexistent')),0)
    def test_failed_process_is_reported(self):
        class Process:
            def wait(self,timeout): return 1
        self.assertEqual(installer.wait_bounded(Process(),Path('/nonexistent')),1)
    def test_deadline_does_not_kill_active_transaction(self):
        class Process:
            pid=1234
            def wait(self,timeout): raise subprocess.TimeoutExpired('dnf',timeout)
            def kill(self): raise AssertionError('Must not kill RPM')
            def terminate(self): raise AssertionError('Must not interrupt RPM')
        with self.assertRaisesRegex(RuntimeError,'left running'):
            installer.wait_bounded(Process(),Path('/nonexistent'),deadline_seconds=10,monotonic=iter([0,11]).__next__)
    def test_tail_retains_latest_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'log';p.write_text('old\nnew\nlatest\n')
            self.assertEqual(installer.tail(p,2),'new\nlatest')
    def test_rpm_query_timeout_propagates(self):
        with patch.object(installer.subprocess,'run',side_effect=subprocess.TimeoutExpired('rpm',30)):
            with self.assertRaises(subprocess.TimeoutExpired): installer.inventory()

if __name__=='__main__': unittest.main()
