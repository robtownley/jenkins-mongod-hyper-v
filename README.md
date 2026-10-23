# Jenkins MongoDB on home Hyper-V

A separate local project for Rob's existing Linux Jenkins VM on a Windows Hyper-V host. It creates three Rocky Linux 9 Generation 2 VMs, obtains their DHCP addresses, and installs a MongoDB 8.0 replica set with one primary and two secondaries.

**Start with [docs/SETUP.md](docs/SETUP.md).** It has the one-time Windows SSH setup, Rocky template preparation and Jenkins job instructions. Your actual Windows host IP/user, external switch name, LAN subnet and disk paths belong in `config/lab.example.json`, copied to `/var/lib/jenkins/mongodb-hyperv/lab.json` on the Linux Jenkins VM. Placeholder values must be changed before PLAN.

## What it uses

- Jenkins → SSH → Windows PowerShell/Hyper-V for provisioning; Jenkins → Ansible/SSH for MongoDB.
- Direct supported Hyper-V PowerShell cmdlets, without a third-party Terraform provider. There is no Terraform requirement or AWS provider/state in this project.
- An existing external Hyper-V switch and your router's DHCP service. Fixed configurable virtual MAC addresses allow DHCP reservations. The project does not create/alter host switches or reboot the Windows host.
- A verified Rocky 9 GenericCloud **Base** QCOW2 converted once to a standalone VHDX. The template is copied for each node and is never booted/modified by the pipeline.
- Per-node cloud-init ISO: DHCP by MAC, public SSH key and a sudo-enabled guest account. No MongoDB secrets go into the image or ISO.
- Default VMs `mongo-hv-mongo1`, `mongo-hv-mongo2`, `mongo-hv-mongo3`, each 2 vCPU / 4 GiB fixed RAM / 40 GiB dynamic VHDX. Total 12 GiB guest RAM and up to 120 GiB virtual disk capacity, plus Windows/Jenkins/template space. No physical resources have been checked on your host.
- MongoDB RPM repository, stock Rocky 9 / 5.14 kernel preflight, persistent keyfile authentication, official SELinux policy. No kernel/GRUB changes. OS upgrades and image refreshes are deliberate separate work.

## Jenkins actions

| Action | Behaviour |
| --- | --- |
| PLAN | Read host/template/VM configuration and produce archived `build/plan.txt`; creates no VMs |
| APPLY | Review plan → create/start owned VMs → discover DHCP → configure MongoDB → verify a write on all three members |
| DESTROY | Review plan → power off/delete the three owned lab VMs and their node directories/data disks |

PLAN defaults to the APPLY intent. The saved plan is rechecked on Windows before execution; a changed VM ID/state/configuration requires a fresh plan. Identical APPLY runs keep existing VMs and data. Hardware/template/user/public-key changes fail instead of automatically rebuilding a populated set. Use an explicit reviewed DESTROY/rebuild for this disposable lab after exporting any data you want.

The Windows script uses a named mutex for host provisioning operations and records each VM's identity in `<vm_root>/<node>/.managed.json`, plus a root owner marker. It checks the VM name, GUID, project notes, attached disk path and owned directory before destruction. It never enumerates/deletes arbitrary VMs or the lab root/template. This protects against accidental targeting; the trusted Windows admin SSH account itself still has full admin capabilities. Use one Jenkins job for this one lab.

No AWS resources, IAM permissions, Route53 records, AWS SSH keys, AWS MongoDB credentials or existing Jenkins VM are managed. The AWS lab remains a separate project.

## DHCP and DNS

The VM MACs default to:

| VM | MAC for router reservations |
| --- | --- |
| mongo-hv-mongo1 | `00:15:5D:71:01:01` |
| mongo-hv-mongo2 | `00:15:5D:71:01:02` |
| mongo-hv-mongo3 | `00:15:5D:71:01:03` |

Confirm these do not collide with existing adapters; choose different addresses before first APPLY if needed. Reserve addresses on your router for reliable MongoDB networking. DHCP is still used; no static IP is configured inside the guests. Hyper-V KVP reports the addresses; the controller accepts one IPv4 per node inside your configured LAN subnet. There is a 15-minute discovery timeout.

Guests install `hyperv-daemons` on first boot. If KVP cannot report an address, inspect the router/guest console and add that node's **DHCP reservation** to `address_overrides` in the local config. This is only a discovery fallback, not a static guest-network setting.

MongoDB member names are `mongo1.mongodb.home.arpa`, `mongo2.mongodb.home.arpa`, `mongo3.mongodb.home.arpa`. Ansible maps these names to discovered LAN addresses on **all three guests** before configuration. The project does not create DNS records on your router. For home clients/Compass replica discovery, add equivalent records to your local DNS or client hosts file. Direct connections can use the discovered IP and `directConnection=true`.

A changing lease during active operation can interrupt replication because `/etc/hosts` is reconciled on APPLY, not continuously. Use reservations. Recreating a node with the same reserved IP legitimately changes its SSH host key; the controller removes only that freshly created node's address from the dedicated guest known_hosts file. Existing-node host key changes still fail.

## MongoDB credentials and verification

Use dedicated Jenkins IDs `mongodb-hyperv-keyfile` (Secret text) and `mongodb-hyperv-admin` (Username with password; password >=16 characters). Keep their values stable between APPLY builds. Rotation is not automated. Secret Ansible tasks use `no_log`; root/Jenkins administrators can inspect process environments.

Bootstrap uses the localhost exception only on a new replica set, then authenticates on reruns. Each successful APPLY verifies exactly one primary/two healthy secondaries, writes a build-token document with `w:3`, and reads it locally from all three nodes. All VMs share one physical host, so this demonstrates member failover, not resilience to a host failure. No backup system or TLS is configured. Port 27017 is enabled in active guest firewalld for LAN use; keep the lab on your trusted LAN and do not port-forward it.

## Connect and troubleshoot

Jenkins archives `build/nodes.txt` and `build/nodes.json` with current addresses.

```bash
sudo -u jenkins ssh -i /var/lib/jenkins/.ssh/mongo-hyperv-guest labadmin@NODE_LAN_IP
mongosh --host 127.0.0.1 --username labadmin --authenticationDatabase admin --password
```

Use `db.hello()` and `rs.status()` inside mongosh. From the Linux runner, also use its dedicated `guest_known_hosts` options if checking the same host keys as Jenkins.

If DHCP discovery fails, use the Hyper-V console and inspect `cloud-init status --long`, `journalctl -u hypervkvpd`, `ip -4 addr`, and router leases. If SSH works but configuration fails, inspect `journalctl -u mongod` and `/var/log/mongodb/mongod.log`.

For an incomplete creation, markers are written before disk/VM operations. The next APPLY refuses to improvise a recovery. Review the files and run a new DESTROY plan to remove the owned partial node(s), then APPLY. Ownership mismatches, unrelated attached disks, directory junctions or missing markers deliberately stop cleanup and require manual inspection. Do not erase markers to bypass a check. Failed transfers can leave public-key-only ISOs in `C:\ProgramData\MongoHyperVLab\incoming`; after confirming no job is running, these staging subdirectories can be removed manually. Template and marker backups are your responsibility.

After changing `windows/Manage-Lab.ps1`, run `windows/Install-Host.ps1` on Windows again. The controller checks the installed script's SHA256 against Git and refuses a mismatched version. Other Python/Ansible/Jenkins changes are fetched by normal Git checkout.

## Git and validation

Unzip this into a **new** directory/repository. Keep `Jenkinsfile`, `scripts`, `windows`, `ansible`, `config` and `docs` at its root; do not copy this over the AWS repo.

```bash
git init -b main
git add .
git commit -m "Add local Hyper-V MongoDB lab"
git remote add origin https://github.com/robtownley/YOUR_NEW_REPOSITORY.git
git push -u origin main
```

The new GitHub repository should be empty for these commands. Once initialized, edit/replace files directly in this directory and commit/push normally. `.gitignore` excludes local config, VM images, seeds, build output and Python caches.

Local validation:

```bash
python3 -m unittest discover -s tests -v
node tests/test_cluster.js                      # optional Node.js
pwsh -NoProfile -File tests/test_windows.ps1    # optional PowerShell 7; mocks only
ansible-playbook -i localhost, ansible/install.yml --syntax-check
ansible-playbook -i localhost, ansible/configure.yml --syntax-check
```

The Python/controller tests, MongoDB helper tests, PowerShell parser/mock tests and Ansible syntax checks are local checks. **No real Hyper-V VM has been provisioned or booted for this deliverable.** Windows PowerShell 5.1/Hyper-V, image boot, Secure Boot, SCP/SFTP paths, KVP discovery and full MongoDB setup need the first run on your host. Start with PLAN.

Sources: [Rocky cloud images](https://download.rockylinux.org/pub/rocky/9/images/x86_64/), [Hyper-V firmware](https://learn.microsoft.com/en-us/powershell/module/hyper-v/set-vmfirmware), [Hyper-V network adapters](https://learn.microsoft.com/en-us/powershell/module/hyper-v/set-vmnetworkadapter), [NoCloud](https://docs.cloud-init.io/en/latest/reference/datasources/nocloud.html), [MongoDB EL9 installation](https://www.mongodb.com/docs/v8.0/tutorial/install-mongodb-on-red-hat/).
