#!/usr/bin/env python3
"""Jenkins/Linux controller for the Windows Hyper-V lab. No AWS dependencies."""
import argparse
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shlex
import socket
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
NODES = ('mongo1', 'mongo2', 'mongo3')
WINDOWS_SCRIPT = 'C:/ProgramData/MongoHyperVLab/Manage-Lab.ps1'
WINDOWS_STAGING = 'C:/ProgramData/MongoHyperVLab/incoming'


def read_config(path):
    config = json.loads(Path(path).read_text())
    for key in ('hyperv_host', 'hyperv_user', 'hyperv_port', 'host_ssh_key', 'host_known_hosts',
                'guest_ssh_key', 'guest_known_hosts', 'guest_user', 'address_network',
                'address_overrides', 'vm_switch', 'vm_root', 'template_vhdx',
                'memory_mb', 'vcpus', 'disk_gb', 'macs'):
        if key not in config:
            raise ValueError(f'Missing config value: {key}')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.\-]*', config['hyperv_host']):
        raise ValueError('Use a Windows host IPv4 address or DNS name')
    if not re.fullmatch(r'[A-Za-z0-9_.\-]+', config['hyperv_user']) or 'YOUR_' in config['hyperv_user']:
        raise ValueError('Set hyperv_user to an existing local Windows administrator SSH account')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,30}', config['guest_user']) or config['guest_user'] == 'root':
        raise ValueError('Use a non-root Linux guest username')
    if set(config['macs']) != set(NODES):
        raise ValueError('Supply MACs for exactly mongo1, mongo2 and mongo3')
    macs = list(config['macs'].values())
    if len(set(macs)) != 3 or any(not re.fullmatch(r'00155D[0-9A-F]{6}', m) for m in macs):
        raise ValueError('MACs must be distinct uppercase Hyper-V addresses: 00155D + six hex digits')
    if not isinstance(config['hyperv_port'], int) or not 1 <= config['hyperv_port'] <= 65535:
        raise ValueError('Invalid Windows SSH port')
    for key, low, high in (('memory_mb', 2048, 65536), ('vcpus', 1, 16), ('disk_gb', 20, 2048)):
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise ValueError(f'Invalid {key}')
    for key in ('vm_root', 'template_vhdx'):
        winpath = PureWindowsPath(config[key])
        if not winpath.is_absolute() or not winpath.drive or str(winpath) == winpath.anchor or '..' in winpath.parts:
            raise ValueError(f'{key} must be an absolute local Windows path, not a drive root')
        if str(winpath).startswith('\\\\'):
            raise ValueError('Use local disks, not UNC shares')
    if 'YOUR_' in config['vm_switch'] or not config['vm_switch']:
        raise ValueError('Set vm_switch to the existing external Hyper-V switch')
    for key in ('host_ssh_key', 'host_known_hosts', 'guest_ssh_key', 'guest_known_hosts'):
        value = config[key]
        if not value.startswith('/') or any(c in value for c in ('\n', '\r', '\x00')):
            raise ValueError(f'{key} must be an absolute Linux path')
    network = ipaddress.IPv4Network(config['address_network'], strict=True)
    if set(config['address_overrides']) - set(NODES):
        raise ValueError('Unknown address override node')
    for address in config['address_overrides'].values():
        if ipaddress.IPv4Address(address) not in network:
            raise ValueError('Address override must be in address_network')
    return config


def public_key(config):
    value = Path(config['guest_ssh_key'] + '.pub').read_text().strip()
    parts = value.split()
    if len(parts) < 2 or parts[0] not in ('ssh-ed25519', 'ssh-rsa', 'ecdsa-sha2-nistp256'):
        raise ValueError('Invalid guest public key')
    base64.b64decode(parts[1], validate=True)
    # Comments do not change guest identity or cause provisioning drift.
    return ' '.join(parts[:2])


def host_config(config):
    return {key: config[key] for key in ('vm_switch', 'vm_root', 'template_vhdx',
            'memory_mb', 'vcpus', 'disk_gb', 'macs', 'guest_user')}


def request(config, action, **fields):
    script_hash = hashlib.sha256((ROOT / 'windows/Manage-Lab.ps1').read_bytes()).hexdigest()
    return dict(protocol=1, action=action, script_sha256=script_hash,
                config=host_config(config), public_key=public_key(config), **fields)


def encoded_command(payload):
    data = base64.b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode()
    command = ("$ErrorActionPreference='Stop'; & '" + WINDOWS_SCRIPT +
               "' -RequestBase64 '" + data + "'")
    encoded = base64.b64encode(command.encode('utf-16le')).decode()
    if len(encoded) > 7400:
        raise ValueError('Windows command too long; shorten configured Windows paths/switch/user names')
    return encoded


def ssh_options(config, scp=False):
    return ['-P' if scp else '-p', str(config['hyperv_port']),
            '-i', config['host_ssh_key'], '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=15',
            '-o', f"UserKnownHostsFile={config['host_known_hosts']}"]


def call_host(config, payload):
    target = config['hyperv_user'] + '@' + config['hyperv_host']
    command = ['ssh', *ssh_options(config), target,
               'powershell.exe -NoLogo -NoProfile -NonInteractive -OutputFormat Text -EncodedCommand ' + encoded_command(payload)]
    result = subprocess.run(command, text=True, capture_output=True, timeout=1800)
    if result.returncode:
        raise RuntimeError(f'Hyper-V host operation failed:\n{result.stderr.strip()}\n{result.stdout.strip()}')
    try:
        return json.loads(result.stdout.lstrip('\ufeff').strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError('Expected JSON from Windows. Check SSH login banners/profile output: ' + result.stdout[:1000]) from exc


def seed_documents(config, node):
    # JSON is a valid YAML subset and avoids interpolating key/user strings into YAML.
    cloud = {
        'hostname': node, 'fqdn': node + '.mongodb.home.arpa', 'manage_etc_hosts': True,
        'ssh_pwauth': False, 'disable_root': True,
        'users': [{'name': config['guest_user'], 'groups': ['wheel'], 'shell': '/bin/bash',
                   'sudo': ['ALL=(ALL) NOPASSWD:ALL'], 'lock_passwd': True,
                   'ssh_authorized_keys': [public_key(config)]}],
        'growpart': {'mode': 'auto', 'devices': ['/'], 'ignore_growroot_disabled': False},
        'resize_rootfs': True,
        'packages': ['hyperv-daemons'],
        'runcmd': [['sh', '-ec', (ROOT / 'ansible/files/kvp-start.sh').read_text()]],
    }
    mac = ':'.join(config['macs'][node][i:i+2].lower() for i in range(0, 12, 2))
    network = {'version': 2, 'ethernets': {'eth0': {
        'match': {'macaddress': mac}, 'set-name': 'eth0', 'dhcp4': True, 'dhcp6': False}}}
    metadata = {'instance-id': 'mongo-hyperv-' + node + '-' + uuid.uuid4().hex,
                'local-hostname': node}
    return {'user-data': '#cloud-config\n' + json.dumps(cloud, indent=2) + '\n',
            'meta-data': json.dumps(metadata, indent=2) + '\n',
            'network-config': json.dumps(network, indent=2) + '\n'}


def build_seeds(config, plan, build):
    hashes = {}
    for row in plan['plan']['nodes']:
        if row['operation'] != 'Create':
            continue
        node = row['node']
        folder = build / 'seeds' / node
        folder.mkdir(parents=True, exist_ok=True)
        documents = seed_documents(config, node)
        for name, text in documents.items():
            (folder / name).write_text(text)
        iso = build / (node + '.iso')
        subprocess.run(['genisoimage', '-quiet', '-output', str(iso.resolve()), '-volid', 'cidata',
                        '-joliet', '-rock', 'user-data', 'meta-data', 'network-config'], cwd=folder, check=True)
        hashes[node] = hashlib.sha256(iso.read_bytes()).hexdigest()
    return hashes


def pick_addresses(config, status):
    if set(status) != set(NODES):
        raise ValueError('Hyper-V did not return exactly three nodes')
    network = ipaddress.IPv4Network(config['address_network'])
    result = {}
    for node in NODES:
        item = status[node]
        if item['mac'].upper() != config['macs'][node] or item['state'] != 'Running':
            raise ValueError(f'{node} is not running with the expected MAC')
        values = []
        if node in config['address_overrides']:
            values = [config['address_overrides'][node]]
        else:
            for address in item['ips']:
                try:
                    ip = ipaddress.IPv4Address(address)
                except ipaddress.AddressValueError:
                    continue
                if ip in network and not ip.is_loopback and not ip.is_link_local and not ip.is_unspecified:
                    values.append(str(ip))
        values = sorted(set(values))
        if len(values) != 1:
            raise ValueError(f'{node}: expected one DHCP address in {network}; found {values}. Check KVP/router leases or address_overrides.')
        result[node] = {**item, 'private_ip': values[0], 'hostname': node + '.mongodb.home.arpa'}
    if len({item['private_ip'] for item in result.values()}) != 3:
        raise ValueError('Duplicate guest IP addresses')
    return result



def arp_addresses(config, status, text):
    """Accept only resolved ARP entries matching the owned VMs' configured MACs."""
    network = ipaddress.IPv4Network(config['address_network'])
    result = {node: [] for node in NODES}
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 6:
            continue
        try:
            ip = ipaddress.IPv4Address(fields[0])
            flags = int(fields[2], 16)
        except ValueError:
            continue
        if ip not in network or not flags & 2:
            continue
        mac = fields[3].replace(':', '').upper()
        for node in NODES:
            if (mac == config['macs'][node] == status[node]['mac'].upper()
                    and status[node]['state'] == 'Running'):
                result[node].append(str(ip))
    return {node: sorted(set(values)) for node, values in result.items()}


def probe_lan(config):
    """Unprivileged UDP probes populate Linux ARP for this directly connected LAN.

    No root/raw socket/arp-scan dependency. Limit to <=1024 addresses; only the
    project's expected MACs will be accepted from the resulting neighbor table.
    """
    network = ipaddress.IPv4Network(config['address_network'])
    if network.num_addresses > 1024:
        raise ValueError('ARP bootstrap fallback requires a LAN subnet of /22 or smaller')
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect((str(network.network_address + 1), 65500))
        if ipaddress.IPv4Address(route.getsockname()[0]) not in network:
            raise ValueError('ARP bootstrap requires Jenkins on the same LAN; use DHCP reservation address_overrides for routed networks')
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setblocking(False)
        for ip in network.hosts():
            try:
                sock.sendto(b'\x00', (str(ip), 65500))
            except OSError:
                pass


def with_arp_fallback(config, status, text):
    addresses = arp_addresses(config, status, text)
    merged = {node: dict(item, ips=list(item['ips'])) for node, item in status.items()}
    network = ipaddress.IPv4Network(config['address_network'])
    for node in NODES:
        if node in config['address_overrides']:
            continue
        usable = []
        for value in merged[node]['ips']:
            try:
                if ipaddress.IPv4Address(value) in network:
                    usable.append(value)
            except ValueError:
                continue
        if not usable:
            merged[node]['ips'] = addresses[node]
    return merged


def inventory(config, nodes):
    return {'all': {'vars': {
        'ansible_user': config['guest_user'], 'ansible_ssh_private_key_file': config['guest_ssh_key'],
        # Nodes are a disposable lab; initial guest identity uses TOFU. The Windows host is pre-verified.
        'ansible_ssh_common_args': '-o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=' + shlex.quote(config['guest_known_hosts']),
    }, 'children': {'mongodb': {'hosts': {node: {
        'ansible_host': nodes[node]['private_ip'], 'private_ip': nodes[node]['private_ip'],
        'mongo_hostname': nodes[node]['hostname'],
    } for node in NODES}}}}}


def print_plan(result):
    lines = [f"{result['plan']['intent']} — Hyper-V MongoDB lab", 'VM root: ' + result['plan']['vm_root'], '']
    for node in result['plan']['nodes']:
        lines.append(f"{node['vm_name']}: {node['operation']} ({node['state']}); MAC {node['mac']}")
    lines += ['', 'DESTROY deletes only the three owned VMs and their node directories, including database disks.',
              'No existing VM is automatically rebuilt for a settings/template/key change.']
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/var/lib/jenkins/mongodb-hyperv/lab.json')
    sub = parser.add_subparsers(dest='command', required=True)
    plan_parser = sub.add_parser('plan')
    plan_parser.add_argument('--intent', choices=['APPLY', 'DESTROY'], default='APPLY')
    sub.add_parser('execute')
    sub.add_parser('discover')
    args = parser.parse_args()
    config = read_config(args.config)
    build = ROOT / 'build'
    build.mkdir(exist_ok=True)
    if args.command == 'plan':
        result = call_host(config, request(config, 'PLAN', intent=args.intent))
        (build / 'plan.json').write_text(json.dumps(result, indent=2) + '\n')
        text = print_plan(result)
        (build / 'plan.txt').write_text(text)
        print(text)
    elif args.command == 'execute':
        plan = json.loads((build / 'plan.json').read_text())
        intent = plan['plan']['intent']
        if intent not in ('APPLY', 'DESTROY'):
            raise ValueError('Invalid saved plan')
        if intent == 'DESTROY':
            call_host(config, request(config, 'DESTROY', review_token=plan['review_token']))
        else:
            hashes = build_seeds(config, plan, build)
            stage = uuid.uuid4().hex
            call_host(config, request(config, 'STAGE', stage=stage))
            for node in hashes:
                dest = config['hyperv_user'] + '@' + config['hyperv_host'] + ':' + WINDOWS_STAGING + '/' + stage + '/' + node + '.iso'
                subprocess.run(['scp', *ssh_options(config, scp=True), str(build / (node + '.iso')), dest], check=True, timeout=180)
            call_host(config, request(config, 'APPLY', review_token=plan['review_token'], stage=stage, seed_hashes=hashes))
        print(intent + ' completed on Hyper-V')
    else:
        deadline = time.monotonic() + 900
        last_message = ''
        next_probe = time.monotonic() + 30
        while True:
            status = call_host(config, request(config, 'STATUS'))
            try:
                nodes = pick_addresses(config, status)
                break
            except ValueError as exc:
                # KVP itself may be the failed first-boot step. Discover the same
                # owned MACs on the local LAN so Ansible can repair the guest.
                if Path('/proc/net/arp').exists():
                    try:
                        recovered = with_arp_fallback(config, status, Path('/proc/net/arp').read_text())
                        nodes = pick_addresses(config, recovered)
                        print('Using MAC-verified LAN neighbor addresses for bootstrap repair', flush=True)
                        break
                    except ValueError:
                        pass
                    if time.monotonic() >= next_probe:
                        print('Refreshing local LAN neighbors to find the three expected VM MACs', flush=True)
                        probe_lan(config)
                        next_probe = time.monotonic() + 30
                message = str(exc)
                if message != last_message:
                    print(message, flush=True)
                    last_message = message
                if time.monotonic() >= deadline:
                    raise RuntimeError('Timed out waiting for guest DHCP/KVP. ' + message) from exc
                time.sleep(5)
        # A newly created VM legitimately has a new SSH host key. Forget only
        # addresses of nodes created by this reviewed build, never existing nodes.
        known = Path(config['guest_known_hosts'])
        known.parent.mkdir(parents=True, exist_ok=True)
        known.touch(mode=0o600, exist_ok=True)
        saved_plan = json.loads((build / 'plan.json').read_text())
        for row in saved_plan['plan']['nodes']:
            if row['operation'] == 'Create':
                subprocess.run(['ssh-keygen', '-R', nodes[row['node']]['private_ip'], '-f', str(known)],
                               check=True, capture_output=True, text=True)
        (build / 'nodes.json').write_text(json.dumps(nodes, indent=2) + '\n')
        (build / 'inventory.json').write_text(json.dumps(inventory(config, nodes), indent=2) + '\n')
        lines = [f"{node}: {nodes[node]['private_ip']} / {nodes[node]['hostname']} / MAC {nodes[node]['mac']}" for node in NODES]
        (build / 'nodes.txt').write_text('\n'.join(lines) + '\n')
        print('\n'.join(lines))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
