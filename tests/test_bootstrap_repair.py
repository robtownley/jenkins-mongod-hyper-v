import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('repair', ROOT / 'ansible/files/repair_bootstrap.py')
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)
FIXED = (ROOT / 'ansible/files/kvp-start.sh').read_text()
OLD = "#!/bin/sh\n'systemctl' 'enable' '--now' 'hypervkvpd.service'\n"
ERROR = "('scripts_user', RuntimeError('Runparts: 1 failures (runcmd) in 1 attempted commands'))"


class RepairTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.instance = self.root / 'instance'
        (self.instance / 'scripts').mkdir(parents=True)
        self.script = self.instance / 'scripts/runcmd'
        self.script.write_text(OLD)
        self.script.chmod(0o700)
        self.status = self.root / 'status.json'
        self.state = {'v1': {name: {'errors': [], 'recoverable_errors': {}, 'start': 1, 'finished': 2}
                            for name in ('init-local', 'init', 'modules-config', 'modules-final')}}
        self.state['v1']['modules-final']['errors'] = [ERROR]
        self.status.write_text(json.dumps(self.state))
        self.calls = []
        self.final_started = False
        self.fail_start = False

    def runner(self, argv, timeout=120):
        self.calls.append(argv)
        rc = 0
        if argv[:2] == ['cloud-init', 'status']:
            rc = 0 if self.final_started else bool(self.state['v1']['modules-final']['errors'])
        if argv == ['systemctl', 'start', 'cloud-final']:
            self.final_started = True
        if argv[:1] == ['/bin/sh'] and self.fail_start:
            rc = 1
        return subprocess.CompletedProcess(argv, int(rc), '', '')

    def invoke(self):
        return repair.repair(self.instance, self.status, self.root/'backups', FIXED, self.runner)

    def test_known_legacy_script_repaired_and_errors_reset_only_after_verification(self):
        result = self.invoke()
        self.assertTrue(result['changed'])
        after = json.loads(self.status.read_text())
        self.assertEqual(after['v1']['init'], self.state['v1']['init'])
        self.assertEqual(after['v1']['modules-final']['errors'], [])
        self.assertEqual(self.script.read_text(), FIXED)
        self.assertEqual((Path(result['backup'])/'runcmd.before').read_text(), OLD)
        self.assertLess(self.calls.index(['/bin/sh',str(self.script)]), self.calls.index(['systemctl','stop','cloud-final']))

    def test_script_failure_preserves_error_state(self):
        self.fail_start = True
        with self.assertRaises(RuntimeError): self.invoke()
        self.assertEqual(json.loads(self.status.read_text()), self.state)
        self.assertNotIn(['systemctl','start','cloud-final'], self.calls)

    def test_unrelated_stage_error_refused(self):
        self.state['v1']['init']['errors'] = ['network failure']
        self.status.write_text(json.dumps(self.state))
        with self.assertRaisesRegex(RuntimeError,'Unrelated'): self.invoke()
        self.assertEqual(self.script.read_text(), OLD)

    def test_unknown_final_error_refused(self):
        self.state['v1']['modules-final']['errors'] = ['package installation failed']
        self.status.write_text(json.dumps(self.state))
        with self.assertRaisesRegex(RuntimeError,'Unexpected'): self.invoke()

    def test_unknown_user_script_refused(self):
        other = self.instance/'scripts/custom'
        other.write_text('#!/bin/sh\nexit 1\n'); other.chmod(0o700)
        with self.assertRaisesRegex(RuntimeError,'Another executable'): self.invoke()

    def test_old_executable_backup_moved_outside_scripts(self):
        backup = self.instance/'scripts/runcmd.backup'
        backup.write_text(OLD); backup.chmod(0o700)
        result = self.invoke()
        self.assertFalse(backup.exists())
        self.assertTrue((Path(result['backup'])/'runcmd.backup').exists())

    def test_healthy_node_is_noop(self):
        self.script.write_text(FIXED)
        self.state['v1']['modules-final']['errors'] = []
        self.status.write_text(json.dumps(self.state))
        result = self.invoke()
        self.assertFalse(result['changed'])
        self.assertFalse((self.root/'backups').exists())

    def test_new_cloud_seed_shell_wrapper_recognized(self):
        wrapped = '#!/bin/sh\n' + ' '.join(shlex.quote(value) for value in ['sh','-ec',FIXED]) + '\n'
        self.assertTrue(repair.known_script(wrapped, FIXED))
        self.assertFalse(repair.known_script('#!/bin/sh\nrm -rf /\n', FIXED))

if __name__ == '__main__': unittest.main()
