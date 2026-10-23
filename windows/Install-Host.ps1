#Requires -RunAsAdministrator
#Requires -Modules Hyper-V
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$root = 'C:\ProgramData\MongoHyperVLab'
New-Item -ItemType Directory -Path $root -Force | Out-Null
New-Item -ItemType Directory -Path "$root\incoming" -Force | Out-Null
Copy-Item (Join-Path $PSScriptRoot 'Manage-Lab.ps1') "$root\Manage-Lab.ps1" -Force
# The host management script is executed by a trusted Windows administrator.
# No host networking, Hyper-V switches or existing VMs are altered by this installer.
Write-Output "Installed $root\Manage-Lab.ps1"
Write-Output 'Configure Windows OpenSSH and the administrator public key as described in docs/SETUP.md.'
