# Update the existing Hyper-V lab without rebuilding VMs

The original seed installed the Hyper-V daemons after the KVP device already existed, then attempted to enable/start a static service. This caused a missing systemd device dependency and a cloud-init `scripts_user` failure. Reloading the new udev rule and triggering the KVP device fixes startup. Cloud-init 24.4 also retains the old final-stage error on a retry, so recovery needs to preserve the evidence and reset only that known stale record after verifying the underlying fix.

This update fixes new seeds and repairs existing VMs through Jenkins. Windows `Manage-Lab.ps1`, the VM configuration identity, disks, MACs, keys and template are unchanged. The installed Windows script does not need updating.

On the Mac, from the existing Git checkout:

```bash
cd ~/jenkins-mongod-hyper-v
unzip -o ~/Downloads/jenkins-mongodb-hyperv.zip
git add .
git diff --cached --stat
git commit -m "Repair Hyper-V KVP and cloud-init bootstrap in pipeline"
git push
```

The ZIP is flat: its files are placed directly in the current Git checkout, not a nested directory. Host-specific Jenkins configuration stays outside this checkout.

Run Jenkins ACTION=APPLY and review the plan. Existing running VMs should show Keep. The pipeline discovers their DHCP addresses (KVP or MAC-verified ARP), runs the bootstrap repair on all three, then continues installing/configuring MongoDB. No manual guest repair is required for the diagnosed failure. If a VM has unrelated cloud-init errors or unknown scripts, the pipeline stops with the reason rather than clearing them.

MAC-based ARP fallback is limited to the directly connected configured IPv4 LAN, up to 1024 addresses. For your 192.168.1.0/24 LAN it sends small UDP probes to populate the local neighbor table, then uses only entries matching the expected running VM MACs. If your Jenkins VM cannot see the guests on that LAN, use DHCP-reservation addresses in `address_overrides` instead.

Validation: controller/repair unit tests cover the old failure, successful repair, preserving errors when repair fails, rejection of unrelated errors/scripts, no-op healthy reruns, executable backup handling, MAC filtering, stale-IP ambiguity and corrected seed generation. Ansible syntax and existing MongoDB helper checks also run. Full execution on your Hyper-V host remains to be verified by your next APPLY.

## Install-step update

The update ZIP also replaces the silent MongoDB DNF task with a logged, IPv4-only, lock-aware installer. Package preparation is serial, and Ansible polls the MongoDB job every 15 seconds. On timeout/failure it reports diagnostics. Existing active package transactions are not killed, and guests are not rebooted/rebuilt automatically. Stop the previous Jenkins build before retrying; aborting a build does not guarantee that a previously launched DNF process has stopped inside a guest.

## Election-readiness update

The original per-node election check stopped as soon as a primary name appeared, even while that member was not writable. A fresh set could therefore fail the unique-primary assertion. The new coordinated probe waits for one writable primary and consistent member responses, selects exactly one existing inventory host and prints per-member details on timeout. Rerun APPLY after pushing the update; do not DESTROY or force-initiate the existing set. The hypothesis addresses a real readiness race in the prior code; a remaining network/election failure will be shown by the new diagnostics.
