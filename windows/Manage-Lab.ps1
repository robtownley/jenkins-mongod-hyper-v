#Requires -Modules Hyper-V
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$RequestBase64)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$LabId = 'jenkins-mongodb-hyperv'
$StagingRoot = 'C:\ProgramData\MongoHyperVLab\incoming'
$NodeNames = @('mongo1','mongo2','mongo3')

function Hash-Text([string]$Text) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text)))).Replace('-','').ToLowerInvariant() }
    finally { $sha.Dispose() }
}
function Read-Json([string]$Path) { return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json }
function Write-Json([string]$Path, $Value) {
    $temp = "$Path.tmp"
    $Value | ConvertTo-Json -Depth 15 | Set-Content -LiteralPath $temp -Encoding UTF8
    Move-Item -LiteralPath $temp -Destination $Path -Force
}
function Assert-PlainPath([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    $check = $full
    while ($check) {
        if (Test-Path -LiteralPath $check) {
            if ((Get-Item -LiteralPath $check -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Reparse point refused: $check" }
        }
        $parent = Split-Path -Parent $check
        if ($parent -eq $check) { break }
        $check = $parent
    }
    return $full
}
function Marker-Path([string]$Node) { return Join-Path (Join-Path $script:VmRoot $Node) '.managed.json' }
function Desired([string]$Node) {
    return [ordered]@{
        lab_id=$LabId; node=$Node; vm_name="mongo-hv-$Node"; mac=$script:Cfg.macs.$Node
        switch=$script:Cfg.vm_switch; memory_mb=[int]$script:Cfg.memory_mb; vcpus=[int]$script:Cfg.vcpus
        disk_gb=[int]$script:Cfg.disk_gb; template=$script:Cfg.template_vhdx
        template_bytes=$script:Template.Length; template_modified=$script:Template.LastWriteTimeUtc.ToString('o')
        guest_public_key_sha256=(Hash-Text $script:Request.public_key)
        guest_user=$script:Cfg.guest_user; protocol=1
    }
}
function Assert-Owned([string]$Node, $Vm, $Record) {
    if ($null -eq $Record -or $Record.lab_id -ne $LabId -or $Record.node -ne $Node) { throw "Missing or mismatched ownership record for $Node" }
    if ($Vm.Notes -ne "$LabId/$Node") { throw "VM notes mismatch: $($Vm.Name); refusing to touch it" }
    if ($Record.vm_id -and $Record.vm_id -ne $Vm.Id.ToString()) { throw "VM ID mismatch: $($Vm.Name)" }
    if (-not $Record.vm_id -and $Record.phase -ne 'creating') { throw 'Missing recorded VM ID' }
    $disk = Join-Path (Join-Path $script:VmRoot $Node) 'disk.vhdx'
    $attached = @(Get-VMHardDiskDrive -VM $Vm)
    if ($attached.Count -ne 1 -or $attached[0].Path -ne $disk) { throw "Unexpected disks attached to $($Vm.Name)" }
}
function Plan-Lab([string]$Intent) {
    $rows = @()
    foreach ($node in $NodeNames) {
        $vmName = "mongo-hv-$node"
        $vm = Get-VM -Name $vmName -ErrorAction SilentlyContinue
        $marker = Marker-Path $node
        $record = $null
        if (Test-Path -LiteralPath $marker) { $record = Read-Json $marker }
        $dir = Join-Path $script:VmRoot $node
        if (Test-Path -LiteralPath $dir) { [void](Assert-PlainPath $dir) }
        if ($record -and ($record.lab_id -ne $LabId -or $record.node -ne $node)) { throw "Unowned directory: $dir" }
        if ($vm) { Assert-Owned $node $vm $record }
        if (-not $vm -and (Test-Path -LiteralPath $dir) -and -not $record) { throw "Unowned directory exists: $dir" }
        $operation = 'None'
        $desiredHash = ''
        if ($Intent -eq 'APPLY') {
            $desiredHash = Hash-Text ((Desired $node) | ConvertTo-Json -Compress)
            if ($record -and $record.config_hash -ne $desiredHash) { throw "Configuration changed for $node. Existing VM/disk will not be rebuilt automatically. Review data and use DESTROY/rebuild explicitly." }
            if ($vm) {
                if ($vm.Generation -ne 2 -or $vm.ProcessorCount -ne [int]$script:Cfg.vcpus -or $vm.MemoryStartup -ne ([long]$script:Cfg.memory_mb * 1MB)) { throw "Hardware drift for $vmName; repair explicitly" }
                $nic = @(Get-VMNetworkAdapter -VM $vm)
                if ($nic.Count -ne 1 -or $nic[0].SwitchName -ne $script:Cfg.vm_switch -or $nic[0].MacAddress -ne $script:Cfg.macs.$node) { throw "Network drift for $vmName" }
                if ($vm.State.ToString() -notin @('Running','Off')) { throw "Unsupported VM state for $vmName : $($vm.State)" }
                $operation = if ($vm.State.ToString() -eq 'Off') { 'Start' } else { 'Keep' }
            } elseif ($record) { throw "Incomplete provisioning or missing VM for $node. Review marker and use DESTROY to clean this owned node before retrying." }
            else { $operation = 'Create' }
        } elseif ($vm -or $record) { $operation = 'Delete' }
        $rows += [ordered]@{
            node=$node; vm_name=$vmName; operation=$operation
            vm_id=$(if ($vm) { $vm.Id.ToString() } else { '' })
            state=$(if ($vm) { $vm.State.ToString() } else { 'Absent' })
            config_hash=$desiredHash; mac=$script:Cfg.macs.$node
        }
    }
    $body = [ordered]@{lab_id=$LabId; intent=$Intent; vm_root=$script:VmRoot; nodes=$rows}
    return [ordered]@{plan=$body; review_token=(Hash-Text ($body | ConvertTo-Json -Depth 12 -Compress))}
}

$mutex = New-Object System.Threading.Mutex($false, 'Global\JenkinsMongoHyperVLab')
$locked = $false
try {
    $locked = $mutex.WaitOne(0)
    if (-not $locked) { throw 'Another Hyper-V lab operation is running' }
    $script:Request = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($RequestBase64)) | ConvertFrom-Json
    if ($Request.protocol -ne 1) { throw 'Unsupported request protocol' }
    $installedHash = (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($installedHash -ne $Request.script_sha256) { throw 'Windows management script differs from Git. Run windows/Install-Host.ps1 from this revision on Windows first.' }
    if ($Request.action -notin @('PLAN','STAGE','APPLY','DESTROY','STATUS')) { throw 'Unknown action' }
    $script:Cfg = $Request.config
    $script:VmRoot = Assert-PlainPath $Cfg.vm_root
    if ($VmRoot -eq [IO.Path]::GetPathRoot($VmRoot) -or $VmRoot.Length -lt 8) { throw 'Use a dedicated VM subdirectory, not a drive root' }
    if (@($Cfg.macs.PSObject.Properties.Name | Sort-Object) -join ',' -ne 'mongo1,mongo2,mongo3') { throw 'Exactly three node MACs are required' }
    if (@($Cfg.macs.PSObject.Properties.Value | Select-Object -Unique).Count -ne 3) { throw 'Duplicate MAC addresses' }
    foreach ($node in $NodeNames) {
        if ($Cfg.macs.$node -notmatch '^00155D[0-9A-F]{6}$') { throw 'Use twelve uppercase hex digits in the Hyper-V MAC range 00155D' }
    }
    if ([int]$Cfg.memory_mb -lt 2048 -or [int]$Cfg.memory_mb -gt 65536 -or [int]$Cfg.vcpus -lt 1 -or [int]$Cfg.vcpus -gt 16 -or [int]$Cfg.disk_gb -lt 20 -or [int]$Cfg.disk_gb -gt 2048) { throw 'VM sizing outside supported lab bounds' }
    $rootMarker = Join-Path $VmRoot '.lab-owner.json'
    if (Test-Path -LiteralPath $VmRoot) {
        if (Test-Path -LiteralPath $rootMarker) {
            if ((Read-Json $rootMarker).lab_id -ne $LabId) { throw 'VM root belongs to another project' }
        } elseif (@(Get-ChildItem -LiteralPath $VmRoot -Force).Count -gt 0) { throw 'VM root must be new/empty or owned by this project' }
    }
    $intent = if ($Request.action -eq 'PLAN') { $Request.intent } elseif ($Request.action -eq 'DESTROY') { 'DESTROY' } else { 'APPLY' }
    if ($intent -notin @('APPLY','DESTROY')) { throw 'Invalid plan intent' }
    if ($intent -eq 'APPLY' -and $Request.action -notin @('STAGE','STATUS')) {
        $switch = Get-VMSwitch -Name $Cfg.vm_switch
        if ($switch.SwitchType.ToString() -ne 'External') { throw 'Select an existing EXTERNAL virtual switch with LAN DHCP' }
        $templatePath = Assert-PlainPath $Cfg.template_vhdx
        if ($templatePath.StartsWith($VmRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Keep the template outside the lab root' }
        $script:Template = Get-Item -LiteralPath $templatePath
        $vhd = Get-VHD -Path $templatePath
        if ($vhd.VhdFormat.ToString() -ne 'VHDX' -or $vhd.VhdType.ToString() -eq 'Differencing') { throw 'Use a standalone VHDX template' }
        if ($vhd.Attached -or $vhd.Size -gt ([long]$Cfg.disk_gb * 1GB)) { throw 'Template must be detached and no larger than target disk' }
        foreach ($node in $NodeNames) {
            $collisions = @(Get-VMNetworkAdapter -All | Where-Object { $_.MacAddress -eq $Cfg.macs.$node -and $_.VMName -ne "mongo-hv-$node" })
            if ($collisions.Count) { throw "MAC collision for $node" }
        }
    }
    if ($Request.action -eq 'STAGE') {
        if ($Request.stage -notmatch '^[0-9a-f]{32}$') { throw 'Invalid stage identifier' }
        $stagePath = Join-Path $StagingRoot $Request.stage
        New-Item -ItemType Directory -Path $stagePath -ErrorAction Stop | Out-Null
        @{stage=$Request.stage} | ConvertTo-Json -Compress | Write-Output
    } elseif ($Request.action -eq 'STATUS') {
        $nodes = [ordered]@{}
        foreach ($node in $NodeNames) {
            $vm = Get-VM -Name "mongo-hv-$node" -ErrorAction SilentlyContinue
            if (-not $vm) { throw "Missing VM $node" }
            $record = Read-Json (Marker-Path $node)
            Assert-Owned $node $vm $record
            $nic = @(Get-VMNetworkAdapter -VM $vm)[0]
            $nodes[$node] = @{vm_id=$vm.Id.ToString(); vm_name=$vm.Name; mac=$nic.MacAddress; ips=@($nic.IPAddresses); state=$vm.State.ToString()}
        }
        $nodes | ConvertTo-Json -Depth 8 -Compress | Write-Output
    } else {
        $result = Plan-Lab $intent
        if ($Request.action -eq 'PLAN') { $result | ConvertTo-Json -Depth 15 -Compress | Write-Output }
        else {
            if ($result.review_token -ne $Request.review_token) { throw 'Plan changed since review. Run PLAN again and approve the new plan.' }
            if ($intent -eq 'APPLY') {
                if ($Request.stage -notmatch '^[0-9a-f]{32}$') { throw 'Invalid seed stage' }
                $stagePath = Assert-PlainPath (Join-Path $StagingRoot $Request.stage)
                # Check all required seed bytes BEFORE creating any VM.
                foreach ($row in $result.plan.nodes) {
                    if ($row.operation -eq 'Create') {
                        $seed = Join-Path $stagePath "$($row.node).iso"
                        if ((Get-FileHash -LiteralPath $seed -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Request.seed_hashes.($row.node)) { throw "Seed checksum mismatch for $($row.node)" }
                    }
                }
                New-Item -ItemType Directory -Path $VmRoot -Force | Out-Null
                Write-Json $rootMarker @{lab_id=$LabId; protocol=1}
            }
            foreach ($row in $result.plan.nodes) {
                $node = $row.node
                $dir = Join-Path $VmRoot $node
                $marker = Marker-Path $node
                if ($row.operation -eq 'Create') {
                    New-Item -ItemType Directory -Path $dir | Out-Null
                    $record = [ordered]@{lab_id=$LabId; node=$node; vm_id=''; phase='creating'; config_hash=$row.config_hash}
                    Write-Json $marker $record
                    $disk = Join-Path $dir 'disk.vhdx'
                    Copy-Item -LiteralPath $Cfg.template_vhdx -Destination $disk
                    Set-ItemProperty -LiteralPath $disk -Name IsReadOnly -Value $false
                    $diskInfo = Get-VHD -Path $disk
                    if ($diskInfo.Size -lt ([long]$Cfg.disk_gb * 1GB)) { Resize-VHD -Path $disk -SizeBytes ([long]$Cfg.disk_gb * 1GB) }
                    $seedPath = Join-Path $dir 'seed.iso'
                    Copy-Item -LiteralPath (Join-Path $stagePath "$node.iso") -Destination $seedPath
                    $vm = New-VM -Name $row.vm_name -Generation 2 -MemoryStartupBytes ([long]$Cfg.memory_mb * 1MB) -VHDPath $disk -Path $dir -SwitchName $Cfg.vm_switch
                    Set-VM -VM $vm -Notes "$LabId/$node" -AutomaticStartAction Start -AutomaticStopAction ShutDown
                    $record.vm_id = $vm.Id.ToString()
                    Write-Json $marker $record
                    Set-VMProcessor -VM $vm -Count ([int]$Cfg.vcpus)
                    Set-VMMemory -VM $vm -DynamicMemoryEnabled $false
                    Get-VMNetworkAdapter -VM $vm | Set-VMNetworkAdapter -StaticMacAddress $Cfg.macs.$node
                    Set-VMFirmware -VM $vm -EnableSecureBoot On -SecureBootTemplate MicrosoftUEFICertificateAuthority -FirstBootDevice (Get-VMHardDiskDrive -VM $vm)
                    Add-VMDvdDrive -VM $vm -Path $seedPath
                    Start-VM -VM $vm
                    $record.phase = 'ready'
                    Write-Json $marker $record
                } elseif ($row.operation -eq 'Start') {
                    Start-VM -Name $row.vm_name
                } elseif ($row.operation -eq 'Delete') {
                    $vm = Get-VM -Name $row.vm_name -ErrorAction SilentlyContinue
                    $record = Read-Json $marker
                    if ($vm) {
                        Assert-Owned $node $vm $record
                        if ($vm.State.ToString() -ne 'Off') { Stop-VM -VM $vm -TurnOff -Force }
                        Remove-VM -VM $vm -Force
                    }
                    # Exactly one owned node directory; never remove VM root/template/other VMs.
                    [void](Assert-PlainPath $dir)
                    Remove-Item -LiteralPath $dir -Recurse -Force
                }
            }
            if ($intent -eq 'APPLY' -and (Test-Path -LiteralPath $stagePath)) { Remove-Item -LiteralPath $stagePath -Recurse -Force }
            @{ok=$true; intent=$intent} | ConvertTo-Json -Compress | Write-Output
        }
    }
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
} finally {
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
