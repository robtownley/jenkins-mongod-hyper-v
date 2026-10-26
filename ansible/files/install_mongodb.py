#!/usr/bin/env python3
"""Logged, bounded MongoDB installation. Never forcibly kill an RPM transaction."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

LOG = Path('/var/log/mongodb-hyperv-install.log')
LOCK = Path('/run/lock/mongodb-hyperv-install.lock')
DNF = ['dnf', '-y', '-v', '--setopt=ip_resolve=4', '--setopt=timeout=30',
       '--setopt=minrate=1024', '--setopt=retries=2', '--setopt=exit_on_lock=True',
       '--setopt=max_parallel_downloads=1', '--setopt=install_weak_deps=False',
       'install', 'mongodb-org']
REPO = 'https://repo.mongodb.org/yum/redhat/9/mongodb-org/8.0/x86_64/repodata/repomd.xml'


def tail(path, lines=80):
    return '\n'.join(path.read_text(errors='replace').splitlines()[-lines:]) if path.exists() else ''


def inventory():
    result = subprocess.run(['rpm', '-qa', '--qf', '%{NAME}=%{VERSION}-%{RELEASE}\\n'],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError('Cannot read RPM database: ' + result.stderr)
    return sorted(result.stdout.splitlines())


def wait_bounded(process, log, deadline_seconds=900, monotonic=time.monotonic):
    deadline = monotonic() + deadline_seconds
    while True:
        try:
            return process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if monotonic() >= deadline:
                raise RuntimeError(
                    'MongoDB DNF install exceeded %ss. PID %s is left running to avoid '
                    'forcibly interrupting RPM. No duplicate installer will be started. '
                    'Inspect %s before retrying.\n%s' % (deadline_seconds, process.pid, log, tail(log)))


def main():
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('A previous MongoDB installer is still running.\n' + tail(LOG))
        # Read-only package inspection fails promptly if the RPM database is stuck.
        before = inventory()
        with LOG.open('a', buffering=1) as log:
            log.write('\n=== MongoDB install attempt %s ===\n' % time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
            log.write('Checking MongoDB repository over IPv4\n')
            log.flush()
            probe = subprocess.run(['curl', '--ipv4', '--fail', '--location', '--silent', '--show-error',
                                    '--connect-timeout', '10', '--max-time', '45', '--output', '/dev/null', REPO],
                                   stdout=log, stderr=subprocess.STDOUT, timeout=60)
            if probe.returncode:
                raise RuntimeError('MongoDB repository IPv4 check failed.\n' + tail(LOG))
            log.write('Starting: ' + ' '.join(DNF) + '\n')
            log.flush()
            # The child retains the lock if this supervisor hits its time limit.
            # It uses an independent session and writes directly to a durable log.
            process = subprocess.Popen(DNF, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True, pass_fds=(lock.fileno(),))
            log.write('DNF PID: %s\n' % process.pid)
            log.flush()
            code = wait_bounded(process, LOG)
            if code:
                raise RuntimeError('DNF exited with code %s.\n%s' % (code, tail(LOG)))
        after = inventory()
        print(json.dumps({'changed': before != after, 'log': str(LOG), 'tail': tail(LOG, 35)}))


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
