# One-time setup — Windows host and existing Linux Jenkins VM

Assumption: Jenkins runs as the `jenkins` Linux user in the existing Linux VM (the `emby` environment from the AWS lab), hosted by your Windows Hyper-V machine. It will SSH over the LAN to Windows to run PowerShell, and directly to the new Linux guest VMs for Ansible. No Windows Jenkins agent is required. Use Windows Server 2019/2022/2025 or a supported Windows Pro Hyper-V installation with Generation 2 Linux support and Windows PowerShell 5.1/Hyper-V module. Do not run these Windows commands inside the Linux VM.

## 1. Confirm host capacity and LAN switch

Open **PowerShell as Administrator on the Windows host**:

```powershell
Get-VM | Select-Object Name, State, MemoryAssigned
Get-VMSwitch | Select-Object Name, SwitchType
Get-Volume | Select-Object DriveLetter, SizeRemaining, Size
```

Choose your existing **external** switch, with DHCP from your router and reachable by the Jenkins VM. Do not use the Windows client `Default Switch` (NAT/internal). If your host has no external switch, create one manually in Hyper-V Manager at a suitable maintenance time; switch creation can temporarily disrupt host connectivity. This project never creates a switch. Optional VLAN tagging is not implemented; use an untagged LAN switch/port.

Have at least 12 GiB of spare VM RAM plus space for Windows/Jenkins, three dynamic VHDX copies, their growth and a separate template. Adjust config before first creation if required; 4 GiB per node is the recommended lab default.

Create example storage directories (choose drives available on your machine):

```powershell
New-Item -ItemType Directory -Path D:\HyperV\Templates -Force
# MongoLab itself must be new/empty. Do not point vm_root at existing VM storage.
```

## 2. Prepare the Linux Jenkins runner

On the Linux Jenkins VM:

```bash
sudo apt-get update
sudo apt-get install -y python3-venv openssh-client genisoimage qemu-utils curl
sudo python3 -m venv /opt/jenkins-mongodb-hyperv-venv
sudo /opt/jenkins-mongodb-hyperv-venv/bin/pip install 'ansible-core>=2.19,<2.20'
sudo install -d -o jenkins -g jenkins -m 700 \
  /var/lib/jenkins/.ssh /var/lib/jenkins/mongodb-hyperv
sudo -u jenkins bash <<'KEYS'
set -eu
for key in mongo-hyperv-host mongo-hyperv-guest; do
  if [ ! -f "/var/lib/jenkins/.ssh/$key" ]; then
    ssh-keygen -t ed25519 -N '' -f "/var/lib/jenkins/.ssh/$key"
  fi
done
KEYS
sudo -u jenkins cat /var/lib/jenkins/.ssh/mongo-hyperv-host.pub
```

The **host** public key goes into Windows. The **guest** key is injected into cloud-init seeds automatically. Neither is an AWS key. Keep private keys and host-specific config outside Git.

## 3. Enable Windows SSH

On **Windows**, as Administrator:

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Set-Service sshd -StartupType Automatic
Start-Service sshd
```

Use an existing **local Windows administrator account** for this trusted home lab, setting that username in `hyperv_user`. That account needs key-based SSH, Hyper-V cmdlet access and write access to the VM/template directories. Passwords are not stored in the project. If you create a dedicated account, do so through your normal Windows administration process and add it to the local Administrators group; the project does not create Windows users.

For an administrator account, OpenSSH normally uses `C:\ProgramData\ssh\administrators_authorized_keys`. Append the single-line `mongo-hyperv-host.pub` key from the previous step:

```powershell
$key = Read-Host 'Paste the Jenkins mongo-hyperv-host.pub public key'
Add-Content -LiteralPath "$env:ProgramData\ssh\administrators_authorized_keys" -Value $key -Encoding ascii
icacls "$env:ProgramData\ssh\administrators_authorized_keys" /inheritance:r /grant '*S-1-5-18:F' /grant '*S-1-5-32-544:F'
```

Use a LAN firewall rule for TCP/22 from the Linux Jenkins VM. If the standard OpenSSH rule exists and this host's SSH is intended only for that runner, restrict it after replacing the example IP:

```powershell
Set-NetFirewallRule -Name OpenSSH-Server-In-TCP -RemoteAddress 192.168.1.20
```

If the named rule does not exist, create an inbound TCP/22 rule for that source through Windows Firewall. Preserve any other legitimate SSH access you need. Windows must accept both SSH commands and SFTP (the default OpenSSH configuration). The controller explicitly invokes `powershell.exe`, so it does not depend on your SSH DefaultShell setting.

On Linux, substitute your actual Windows username/address and test as Jenkins:

```bash
sudo -u jenkins -H ssh \
  -i /var/lib/jenkins/.ssh/mongo-hyperv-host \
  -o IdentitiesOnly=yes \
  -o UserKnownHostsFile=/var/lib/jenkins/mongodb-hyperv/known_hosts \
  YOUR_WINDOWS_USER@192.168.1.10 \
  'powershell.exe -NoProfile -Command "Get-VMSwitch | Select-Object Name,SwitchType"'
```

Verify the host key fingerprint against Windows before accepting it. The pipeline uses strict checking on this dedicated known_hosts file; it will not auto-accept a replacement Windows identity.

## 4. Prepare a Rocky 9 template once

From a local checkout of this project on the **Linux runner**, download the published GenericCloud Base image and its exact checksum. The dated example is pinned, not an automatically changing `latest` image:

```bash
mkdir -p /tmp/rocky-hyperv-image
cd /tmp/rocky-hyperv-image
curl -fLO https://download.rockylinux.org/pub/rocky/9/images/x86_64/Rocky-9-GenericCloud-Base-9.8-20260525.0.x86_64.qcow2
curl -fLO https://download.rockylinux.org/pub/rocky/9/images/x86_64/Rocky-9-GenericCloud-Base-9.8-20260525.0.x86_64.qcow2.CHECKSUM
```

Run the included conversion helper using the actual path to your checkout:

```bash
python3 /PATH/TO/jenkins-mongodb-hyperv/scripts/prepare_image.py \
  --source /tmp/rocky-hyperv-image/Rocky-9-GenericCloud-Base-9.8-20260525.0.x86_64.qcow2 \
  --checksum-file /tmp/rocky-hyperv-image/Rocky-9-GenericCloud-Base-9.8-20260525.0.x86_64.qcow2.CHECKSUM \
  --output /tmp/rocky-hyperv-image/Rocky9-GenericCloud-base.vhdx
```

The helper checks the exact filename's SHA256 from Rocky's HTTPS checksum file before calling qemu-img, refuses backing-file images, and refuses to overwrite an existing output. Rocky also publishes detached checksum signatures if you use its release-signature verification process. Download and convert once, not on every build.

Copy the VHDX to your **Windows host** (replace user/IP and use an existing destination directory):

```bash
scp -i /var/lib/jenkins/.ssh/mongo-hyperv-host \
  -o IdentitiesOnly=yes \
  -o UserKnownHostsFile=/var/lib/jenkins/mongodb-hyperv/known_hosts \
  /tmp/rocky-hyperv-image/Rocky9-GenericCloud-base.vhdx \
  YOUR_WINDOWS_USER@192.168.1.10:D:/HyperV/Templates/Rocky9-GenericCloud-base.vhdx
```

Run the scp command as `jenkins` (or prefix it with `sudo -u jenkins -H`) so it uses the same key/known_hosts identity as the pipeline. Check from Windows:

```powershell
Get-VHD -Path D:\HyperV\Templates\Rocky9-GenericCloud-base.vhdx
```

It should be standalone VHDX, detached, and no larger than your configured 40 GiB target. Do not create/boot a VM from the shared template itself. Guest disks are **full copies**, so deleting the lab does not remove or modify this template. Cloud-init expands the guest root filesystem to the larger cloned disk on first boot.

## 5. Install the Windows management script

Put a copy of this project's `windows` folder on Windows, then run from it in Administrator PowerShell:

```powershell
.\Install-Host.ps1
```

This copies `Manage-Lab.ps1` into `C:\ProgramData\MongoHyperVLab` and creates its incoming staging directory. If Windows execution policy blocks your reviewed local scripts, use a process-only policy for this Administrator shell:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
Unblock-File .\Install-Host.ps1
Unblock-File .\Manage-Lab.ps1
.\Install-Host.ps1
```

For subsequent SSH sessions, if that Windows account still has a Restricted effective policy, run the following once as the **same account used for Windows SSH**, where local policy permits:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

Check with `Get-ExecutionPolicy -List`. The pipeline does not bypass execution policy. On a domain-managed machine, follow the applicable policy rather than changing machine-wide settings. Run the installer again whenever the Windows script changes in Git; the installed file must exactly match the checkout's SHA256.

## 6. Write local configuration

Copy `config/lab.example.json` from your checkout to `/var/lib/jenkins/mongodb-hyperv/lab.json`, owned by Jenkins:

```bash
sudo install -o jenkins -g jenkins -m 600 \
  config/lab.example.json /var/lib/jenkins/mongodb-hyperv/lab.json
sudo nano /var/lib/jenkins/mongodb-hyperv/lab.json
```

Set actual Windows host IPv4/username, existing external switch, template and dedicated VM root paths, LAN subnet, and optional MACs. Windows paths in JSON need double backslashes as shown in the example. Keep the default key paths unless you deliberately change them.

Create three DHCP reservations on your router for the configured MACs. The router provides gateway/DNS and the guests still use DHCP. The subnet in `address_network` must match those leases and be reachable from the Jenkins VM. Guests require outbound internet access to Rocky mirrors, MongoDB repositories and GitHub for the official SELinux policy.

## 7. Jenkins credentials/job

Add these dedicated credentials through Jenkins:

| ID | Kind | Value |
| --- | --- | --- |
| `mongodb-hyperv-admin` | Username with password | E.g. `labadmin`; password >=16 characters |
| `mongodb-hyperv-keyfile` | Secret text | Persistent output of `openssl rand -base64 512 \| tr -d '\n'` |

These are MongoDB users/secrets, separate from the Windows SSH account and Linux guest SSH identity. Do not change values between reruns; this is not a rotation workflow.

Create a **new** Pipeline job, e.g. `mongodb-hyperv-lab`, Pipeline script from SCM → your new Git repository → branch `*/main` → Script Path `Jenkinsfile`. Run it only on the existing Linux Jenkins runner containing these tools/config/keys. With multiple Jenkins agents, change `agent any` to the label of that runner.

Run PLAN first. Expect only `mongo-hv-mongo1/2/3: Create`. APPLY repeats the plan, asks for review, copies disks/seeds, starts VMs, polls for DHCP/KVP (up to 15 minutes), then runs Ansible and replication checks. It does not reboot or stop the Windows host/Jenkins VM.

For repeated APPLY, expect Keep operations; the same VM data is preserved. DESTROY requires its own plan/review and powers off/deletes the three owned VM directories, including data. Root ownership marker and shared template remain. The AWS project and its state are not contacted.
