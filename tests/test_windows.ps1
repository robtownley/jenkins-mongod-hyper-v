# Run with PowerShell 7 on any OS. Uses mocks; does not call real Hyper-V.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$path = Join-Path $PSScriptRoot '../windows/Manage-Lab.ps1'
$tokens=$null; $parseErrors=$null
$ast = [Management.Automation.Language.Parser]::ParseFile((Resolve-Path $path), [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
$definitions = $ast.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] }, $false)
foreach ($definition in $definitions) { Invoke-Expression $definition.Extent.Text }
function Assert-True($Condition,[string]$Message) { if (-not $Condition) { throw $Message } }
function Assert-Throws([scriptblock]$Code,[string]$Pattern) {
    $caught=$false
    try { & $Code | Out-Null } catch { if ($_.Exception.Message -notmatch $Pattern) { throw }; $caught=$true }
    if (-not $caught) { throw "Expected failure: $Pattern" }
}
$LabId='jenkins-mongodb-hyperv'
$NodeNames=@('mongo1','mongo2','mongo3')
$script:VmRoot=Join-Path ([IO.Path]::GetTempPath()) ('hv-tests-' + [guid]::NewGuid().ToString('N'))
$script:Cfg=[pscustomobject]@{
    macs=[pscustomobject]@{mongo1='00155D710101';mongo2='00155D710102';mongo3='00155D710103'}
    vm_switch='LAN';memory_mb=4096;vcpus=2;disk_gb=40
    template_vhdx='D:\Templates\rocky.vhdx';guest_user='labadmin'
}
$script:Template=[pscustomobject]@{Length=1000;LastWriteTimeUtc=[datetime]'2026-01-01T00:00:00Z'}
$script:Request=[pscustomobject]@{public_key='ssh-ed25519 AAAA'}
$script:FakeVMs=@{}
$script:ExtraDisk=$false
function Get-VM {
    [CmdletBinding()]param([string]$Name)
    if ($Name -notmatch '^mongo-hv-mongo[123]$') { throw "Unexpected VM target: $Name" }
    return $script:FakeVMs[$Name]
}
function Get-VMHardDiskDrive {
    param($VM)
    $node=$VM.Name.Replace('mongo-hv-','')
    [pscustomobject]@{Path=(Join-Path (Join-Path $script:VmRoot $node) 'disk.vhdx')}
    if ($script:ExtraDisk) { [pscustomobject]@{Path='unrelated.vhdx'} }
}
function Get-VMNetworkAdapter {
    param($VM)
    $node=$VM.Name.Replace('mongo-hv-','')
    [pscustomobject]@{SwitchName='LAN';MacAddress=$script:Cfg.macs.$node}
}
try {
    New-Item -ItemType Directory -Path $script:VmRoot | Out-Null
    $first=Plan-Lab APPLY
    Assert-True (@($first.plan.nodes | Where-Object operation -eq 'Create').Count -eq 3) 'Fresh plan must create exactly three VMs'
    Assert-True ($first.review_token -eq (Plan-Lab APPLY).review_token) 'Unchanged plans must have the same review token'
    $node='mongo1'
    $dir=Join-Path $script:VmRoot $node
    New-Item -ItemType Directory -Path $dir | Out-Null
    $vm=[pscustomobject]@{Name='mongo-hv-mongo1';Id=[guid]::NewGuid();Notes="$LabId/mongo1";Generation=2;ProcessorCount=2;MemoryStartup=(4096MB);State='Running'}
    $script:FakeVMs[$vm.Name]=$vm
    Assert-Throws { Plan-Lab APPLY } 'Missing or mismatched ownership'
    $hash=Hash-Text ((Desired $node) | ConvertTo-Json -Compress)
    $record=[ordered]@{lab_id=$LabId;node=$node;vm_id=$vm.Id.ToString();phase='ready';config_hash=$hash}
    Write-Json (Marker-Path $node) $record
    $plan=Plan-Lab APPLY
    Assert-True ($plan.plan.nodes[0].operation -eq 'Keep') 'Existing owned VM must be kept'
    $vm.State='Off'
    Assert-True ((Plan-Lab APPLY).plan.nodes[0].operation -eq 'Start') 'Powered-off VM must be started, not recreated'
    $vm.State='Running'
    $record.vm_id=[guid]::NewGuid().ToString()
    Write-Json (Marker-Path $node) $record
    Assert-Throws { Plan-Lab DESTROY } 'VM ID mismatch'
    $record.vm_id=$vm.Id.ToString()
    Write-Json (Marker-Path $node) $record
    $vm.Notes='unrelated project'
    Assert-Throws { Plan-Lab DESTROY } 'VM notes mismatch'
    $vm.Notes="$LabId/mongo1"
    $script:ExtraDisk=$true
    Assert-Throws { Plan-Lab DESTROY } 'Unexpected disks'
    $script:ExtraDisk=$false
    $script:Cfg.vcpus=4
    Assert-Throws { Plan-Lab APPLY } 'Configuration changed'
    $script:Cfg.vcpus=2
    $destroy=Plan-Lab DESTROY
    Assert-True ($destroy.plan.nodes[0].operation -eq 'Delete') 'Owned VM must be deletable'
    Assert-True (@($destroy.plan.nodes | Where-Object operation -eq 'Delete').Count -eq 1) 'Absent/unowned targets must not be included'
    Write-Output '11 Hyper-V planning/ownership mock checks passed'
} finally {
    Remove-Item -LiteralPath $script:VmRoot -Recurse -Force
}
