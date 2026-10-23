import base64
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('lab', ROOT / 'scripts/lab.py')
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)
spec2 = importlib.util.spec_from_file_location('image', ROOT / 'scripts/prepare_image.py')
image = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(image)


class LabTest(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads((ROOT / 'config/lab.example.json').read_text())
        self.cfg.update(hyperv_user='hvadmin', vm_switch='LAN')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / 'guest'
        (Path(str(path) + '.pub')).write_text('ssh-ed25519 ' + base64.b64encode(b'public-test-key').decode() + ' comment\n')
        self.cfg['guest_ssh_key'] = str(path)

    def config_file(self, cfg=None):
        path = Path(self.tmp.name) / 'config.json'
        path.write_text(json.dumps(cfg or self.cfg))
        return path

    def status(self):
        return {node: {'mac': self.cfg['macs'][node], 'state': 'Running', 'vm_id': str(i),
                      'vm_name': 'mongo-hv-' + node, 'ips': [f'192.168.1.{100+i}', 'fe80::123', '169.254.1.2']}
                for i, node in enumerate(lab.NODES, 1)}

    def test_valid_config(self):
        self.assertEqual(lab.read_config(self.config_file())['memory_mb'], 4096)

    def test_refuse_drive_root(self):
        self.cfg['vm_root'] = 'D:\\'
        with self.assertRaises(ValueError): lab.read_config(self.config_file())

    def test_refuse_windows_path_traversal(self):
        self.cfg['vm_root'] = 'D:\\HyperV\\..\\Other'
        with self.assertRaises(ValueError): lab.read_config(self.config_file())

    def test_duplicate_macs_rejected(self):
        self.cfg['macs']['mongo2'] = self.cfg['macs']['mongo1']
        with self.assertRaises(ValueError): lab.read_config(self.config_file())

    def test_dhcp_filters_wrong_network_and_ipv6(self):
        result = lab.pick_addresses(self.cfg, self.status())
        self.assertEqual(result['mongo1']['private_ip'], '192.168.1.101')

    def test_ambiguous_addresses_rejected(self):
        status = self.status(); status['mongo1']['ips'].append('192.168.1.201')
        with self.assertRaises(ValueError): lab.pick_addresses(self.cfg, status)

    def test_no_kvp_address_rejected(self):
        status = self.status(); status['mongo1']['ips'] = []
        with self.assertRaises(ValueError): lab.pick_addresses(self.cfg, status)

    def test_duplicate_ips_rejected(self):
        status = self.status(); status['mongo2']['ips'] = status['mongo1']['ips']
        with self.assertRaises(ValueError): lab.pick_addresses(self.cfg, status)

    def test_manual_reservation_fallback(self):
        status = self.status(); status['mongo1']['ips'] = []
        self.cfg['address_overrides']['mongo1'] = '192.168.1.151'
        self.assertEqual(lab.pick_addresses(self.cfg, status)['mongo1']['private_ip'], '192.168.1.151')

    def test_cloud_seed_uses_dhcp_mac_public_key_no_password(self):
        documents = lab.seed_documents(self.cfg, 'mongo1')
        userdata = json.loads(documents['user-data'].split('\n', 1)[1])
        network = json.loads(documents['network-config'])
        self.assertFalse(userdata['ssh_pwauth'])
        self.assertEqual(userdata['users'][0]['name'], 'labadmin')
        self.assertNotIn('password', userdata)
        self.assertEqual(network['ethernets']['eth0']['match']['macaddress'], '00:15:5d:71:01:01')
        self.assertTrue(network['ethernets']['eth0']['dhcp4'])

    def test_cloud_instance_ids_unique(self):
        a = lab.seed_documents(self.cfg, 'mongo1')['meta-data']
        b = lab.seed_documents(self.cfg, 'mongo1')['meta-data']
        self.assertNotEqual(a, b)

    def test_windows_command_base64_no_shell_interpolation(self):
        payload = {'public_key': "ssh-ed25519 AA== comment $(touch injected) `oops` 'quoted'"}
        encoded = lab.encoded_command(payload)
        command = base64.b64decode(encoded).decode('utf-16le')
        self.assertNotIn('touch injected', command)
        self.assertIn('-RequestBase64', command)
        roundtrip = json.loads(base64.b64decode(command.split("-RequestBase64 '")[1][:-1]))
        self.assertEqual(roundtrip, payload)

    def test_inventory_uses_lan_and_home_arpa(self):
        nodes = lab.pick_addresses(self.cfg, self.status())
        inv = lab.inventory(self.cfg, nodes)
        hosts = inv['all']['children']['mongodb']['hosts']
        self.assertEqual(hosts['mongo2']['ansible_host'], '192.168.1.102')
        self.assertEqual(hosts['mongo2']['mongo_hostname'], 'mongo2.mongodb.home.arpa')

    def test_plan_lists_destroy_consequence(self):
        plan = {'plan': {'intent':'DESTROY', 'vm_root':r'D:\HyperV\MongoLab', 'nodes':[
            {'vm_name':'mongo-hv-mongo1','operation':'Delete','state':'Running','mac':'00155D710101'}]}}
        self.assertIn('data', lab.print_plan(plan))

    def test_exact_checksum_filename(self):
        path = Path(self.tmp.name) / 'CHECKSUM'
        path.write_text('SHA256 (image.qcow2) = ' + 'a'*64 + '\n')
        self.assertEqual(image.checksum_from_file(path, 'image.qcow2'), 'a'*64)
        with self.assertRaises(ValueError): image.checksum_from_file(path, 'other.qcow2')

if __name__ == '__main__':
    unittest.main()
