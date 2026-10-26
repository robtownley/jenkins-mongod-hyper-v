#!/usr/bin/env python3
"""Repair only this project's known KVP/runcmd cloud-init failure, in place."""
import json
from pathlib import Path
import os
import shlex
import shutil
import subprocess
import sys
import time


def run(argv, timeout=120):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def checked(runner, argv, timeout=120):
    result = runner(argv, timeout=timeout)
    if result.returncode:
        raise RuntimeError('Command failed: ' + ' '.join(argv) + '\n' + result.stdout + result.stderr)
    return result


def known_script(text, fixed):
    tokens = shlex.split(text, comments=True)
    return (tokens == ['systemctl', 'enable', '--now', 'hypervkvpd.service']
            or tokens == shlex.split(fixed, comments=True)
            or tokens in (['sh', '-ec', fixed], ['/bin/sh', '-ec', fixed]))


def validate_status(status):
    stages = status['v1']
    for name in ('init-local', 'init', 'modules-config'):
        if stages.get(name, {}).get('errors'):
            raise RuntimeError('Unrelated cloud-init errors in ' + name + '; refusing to clear them')
    errors = stages.get('modules-final', {}).get('errors', [])
    if any('scripts_user' not in str(error) or 'runcmd' not in str(error) for error in errors):
        raise RuntimeError('Unexpected final-stage error; refusing to clear it')
    return bool(errors)


def repair(instance, status_path, backup_root, fixed, runner=run):
    waiting = runner(['cloud-init', 'status', '--wait'], timeout=900)
    if waiting.returncode not in (0, 1, 2):
        raise RuntimeError('Could not read cloud-init completion status: ' + waiting.stderr)
    status = json.loads(status_path.read_text())
    had_errors = validate_status(status)
    scripts = instance / 'scripts'
    current = scripts / 'runcmd'
    if not known_script(current.read_text(), fixed):
        raise RuntimeError('runcmd differs from the known project scripts; refusing to replace it')
    # A previous manual fix placed an executable backup in this directory.
    # Only this known backup may be moved automatically; never rerun unknown scripts.
    old_backup = scripts / 'runcmd.backup'
    if old_backup.exists() and not known_script(old_backup.read_text(), fixed):
        raise RuntimeError('Unknown runcmd.backup contents; inspect manually')
    for item in scripts.iterdir():
        if item.name not in ('runcmd', 'runcmd.backup') and os.access(item, os.X_OK):
            raise RuntimeError('Another executable cloud-init script exists: ' + str(item))
    active = runner(['systemctl', 'is-active', '--quiet', 'hypervkvpd'])
    needs_change = (had_errors or waiting.returncode != 0 or old_backup.exists()
                    or current.read_text() != fixed or active.returncode != 0)
    if not needs_change:
        return {'changed': False, 'message': 'KVP/cloud-init already healthy'}
    backup = backup_root / str(time.time_ns())
    backup.mkdir(parents=True, mode=0o700)
    shutil.copy2(current, backup / 'runcmd.before')
    shutil.copy2(status_path, backup / 'status.before.json')
    if old_backup.exists():
        shutil.move(str(old_backup), str(backup / 'runcmd.backup'))
    current.write_text(fixed)
    current.chmod(0o700)
    checked(runner, ['/bin/sh', str(current)])
    checked(runner, ['systemctl', 'is-active', '--quiet', 'hypervkvpd'])
    # Only AFTER the corrected script and daemon pass, reset the stale failed-stage
    # record. Other stage state and original evidence remain preserved.
    checked(runner, ['systemctl', 'stop', 'cloud-final'])
    if had_errors:
        status = json.loads(status_path.read_text())
        validate_status(status)
        status['v1']['modules-final'] = {
            'errors': [], 'recoverable_errors': {}, 'start': None, 'finished': None}
        temporary = status_path.with_name(status_path.name + '.kvp-repair.tmp')
        temporary.write_text(json.dumps(status))
        temporary.chmod(status_path.stat().st_mode & 0o777)
        temporary.replace(status_path)
    checked(runner, ['systemctl', 'reset-failed', 'cloud-final'])
    checked(runner, ['systemctl', 'start', 'cloud-final'], timeout=900)
    checked(runner, ['cloud-init', 'status', '--wait'], timeout=900)
    checked(runner, ['systemctl', 'is-active', '--quiet', 'hypervkvpd'])
    return {'changed': True, 'message': 'KVP and cloud-init repaired and verified', 'backup': str(backup)}


if __name__ == '__main__':
    try:
        result = repair(Path('/var/lib/cloud/instance'), Path('/var/lib/cloud/data/status.json'),
                        Path('/var/lib/mongodb-hyperv-bootstrap-backups'),
                        Path(__file__).with_name('kvp-start.sh').read_text())
        print(json.dumps(result))
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
