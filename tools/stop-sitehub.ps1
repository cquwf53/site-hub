[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [ValidateRange(5, 120)][int]$TimeoutSeconds = 30,
    [switch]$AuditOnly,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
. (Join-Path $PSScriptRoot 'startup-task-common.ps1')

function Write-Step([string]$Message) {
    Write-Host "[SiteHub stop] $Message"
}

function Get-ListenerRows {
    param([int]$Port = 8686)

    $netstat = Join-Path $env:WINDIR 'System32\netstat.exe'
    if (-not (Test-Path -LiteralPath $netstat -PathType Leaf)) {
        throw 'Windows netstat.exe is unavailable.'
    }
    $rows = New-Object System.Collections.Generic.List[object]
    foreach ($line in @(& $netstat -ano -p tcp)) {
        if ($line -match '^\s*TCP\s+(\S+):(\d+)\s+(\S+):(\d+)\s+(\S+)\s+(\d+)\s*$') {
            $remotePort = [int]$Matches[4]
            $state = [string]$Matches[5]
            if ([int]$Matches[2] -eq $Port -and ($remotePort -eq 0 -or $state -match 'LISTEN')) {
                $rows.Add([pscustomobject]@{
                    LocalAddress = [string]$Matches[1]
                    LocalPort = [int]$Matches[2]
                    OwningProcess = [int]$Matches[6]
                })
            }
        }
    }
    foreach ($row in $rows) { Write-Output $row }
}

function Get-ProcessSnapshot {
    return @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop)
}

function Get-ProcessCreationStamp {
    param([Parameter(Mandatory = $true)]$Record)

    if ($null -eq $Record.CreationDate) { return '' }
    return ([DateTime]$Record.CreationDate).ToUniversalTime().Ticks.ToString()
}

function Test-SameProcessAlive {
    param([Parameter(Mandatory = $true)]$Record)

    $processIdValue = [int]$Record.ProcessId
    $current = @(Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $processIdValue" -ErrorAction SilentlyContinue)
    if ($current.Count -ne 1) { return $false }
    return (Get-ProcessCreationStamp $current[0]) -eq (Get-ProcessCreationStamp $Record)
}

function Get-DescendantRecords {
    param(
        [Parameter(Mandatory = $true)][object[]]$Snapshot,
        [Parameter(Mandatory = $true)][int[]]$RootIds
    )

    $seen = @{}
    $queue = New-Object System.Collections.Generic.Queue[int]
    foreach ($rootId in $RootIds) {
        if (-not $seen.ContainsKey($rootId)) {
            $seen[$rootId] = $true
            $queue.Enqueue($rootId)
        }
    }
    $result = New-Object System.Collections.Generic.List[object]
    while ($queue.Count -gt 0) {
        $parentId = $queue.Dequeue()
        foreach ($child in @($Snapshot | Where-Object { [int]$_.ParentProcessId -eq $parentId })) {
            $childId = [int]$child.ProcessId
            if (-not $seen.ContainsKey($childId)) {
                $seen[$childId] = $true
                $queue.Enqueue($childId)
                $result.Add($child)
            }
        }
    }
    foreach ($record in $result) { Write-Output $record }
}

function Get-CleanupRole {
    param([Parameter(Mandatory = $true)]$Record)

    $name = ([string]$Record.Name).ToLowerInvariant()
    $commandLine = ([string]$Record.CommandLine).ToLowerInvariant()
    if ($name -eq 'node.exe') { return 'Node' }
    if ($name -in @('chrome.exe', 'chromium.exe', 'msedge.exe', 'msedgewebview2.exe', 'firefox.exe')) {
        return 'Chrome/browser'
    }
    if ($commandLine -match 'ddddocr(?:_cli)?\.py' -or $name -match 'ocr') { return 'OCR' }
    return $null
}

function Get-TaskLauncherRecords {
    param(
        [Parameter(Mandatory = $true)][object[]]$Snapshot,
        [Parameter(Mandatory = $true)]$Context
    )

    $escapedLauncher = [regex]::Escape($Context.Launcher)
    $launcherArgumentPattern = '(?:^|\s)(?:"' + $escapedLauncher + '"|' + $escapedLauncher + ')(?:\s|$)'
    return @($Snapshot | Where-Object {
        (Test-SiteHubPathEqual ([string]$_.ExecutablePath) $Context.Wscript) -and
        -not [string]::IsNullOrWhiteSpace([string]$_.CommandLine) -and
        [regex]::IsMatch(
            [string]$_.CommandLine,
            $launcherArgumentPattern,
            [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
        )
    })
}

function Test-ControlPlaneCommand {
    param([Parameter(Mandatory = $true)]$Record)

    $commandLine = [string]$Record.CommandLine
    if ([string]::IsNullOrWhiteSpace($commandLine)) { return $false }
    return [regex]::IsMatch(
        $commandLine,
        '(?:^|\s)-m\s+(?:"site_hub\.main"|site_hub\.main)(?:\s|$)',
        [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
    )
}

function Test-ControlPlaneOwner {
    param(
        [Parameter(Mandatory = $true)]$Record,
        [Parameter(Mandatory = $true)][string]$ExpectedPython,
        [Parameter(Mandatory = $true)][object[]]$Snapshot
    )

    if (-not (Test-ControlPlaneCommand $Record)) { return $false }
    if (Test-SiteHubPathEqual ([string]$Record.ExecutablePath) $ExpectedPython) {
        return $true
    }

    # Python 3.13's Windows venv launcher can create a second process whose
    # WMI ExecutablePath is the base interpreter even though argv[0] and the
    # direct parent are the project venv. Accept only that exact, verified
    # parent relationship; an unrelated base-Python listener remains blocked.
    $parentId = [int]$Record.ParentProcessId
    if ($parentId -le 0) { return $false }
    $parents = @($Snapshot | Where-Object { [int]$_.ProcessId -eq $parentId })
    if ($parents.Count -ne 1) { return $false }
    $parent = $parents[0]
    if (-not (Test-SiteHubPathEqual ([string]$parent.ExecutablePath) $ExpectedPython)) {
        return $false
    }
    if (-not (Test-ControlPlaneCommand $parent)) { return $false }

    $ownerStamp = Get-ProcessCreationStamp $Record
    $parentStamp = Get-ProcessCreationStamp $parent
    if ($ownerStamp -and $parentStamp -and [int64]$ownerStamp -lt [int64]$parentStamp) {
        return $false
    }
    return $true
}

function Stop-VerifiedProcess {
    param(
        [Parameter(Mandatory = $true)]$Record,
        [Parameter(Mandatory = $true)][string]$Reason,
        [Parameter(Mandatory = $true)]$Caller
    )

    if (-not (Test-SameProcessAlive $Record)) { return }
    $processIdValue = [int]$Record.ProcessId
    if ($Caller.ShouldProcess("PID $processIdValue", $Reason)) {
        Stop-Process -Id $processIdValue -Force -ErrorAction Stop
    }
}

if ($env:OS -ne 'Windows_NT') {
    throw 'stop-sitehub.ps1 only supports Windows.'
}

$context = Get-SiteHubStartupContext
$venvPython = Join-Path $context.ProjectRoot '.venv\Scripts\python.exe'
$tasks = @(Get-SiteHubNamedStartupTasks $context.TaskName)
$split = Split-SiteHubStartupTasks $tasks $context
if ($split.Foreign.Count -gt 0 -and -not $Force) {
    throw "A same-name '$($context.TaskName)' scheduled task is not owned by this project. Nothing was stopped. Review the task or re-run with -Force to ignore it; foreign tasks are never stopped."
}
if ($split.Foreign.Count -gt 0) {
    Write-Warning "Ignoring $($split.Foreign.Count) same-name scheduled task(s) outside this project."
}

$ownedTask = if ($split.Owned.Count -eq 1) { $split.Owned[0] } else { $null }
$taskIsRunning = $null -ne $ownedTask -and [string]$ownedTask.State -eq 'Running'
$listeners = @(Get-ListenerRows 8686)
if ($null -ne $ownedTask) {
    Write-Step "Verified owned startup task $($context.TaskPath)$($context.TaskName) for $($context.CurrentUser)."
}
if (-not $taskIsRunning -and $listeners.Count -eq 0) {
    Write-Step 'SiteHub is already stopped; port 8686 has no listener.'
    if ($AuditOnly) { Write-Step 'Audit PASS: no changes were made.' }
    exit 0
}

$snapshot = @(Get-ProcessSnapshot)
$launcherRecords = @()
if ($taskIsRunning) {
    $launcherRecords = @(Get-TaskLauncherRecords $snapshot $context)
    if ($launcherRecords.Count -ne 1) {
        throw "The owned startup task reports Running, but exactly one verified wscript launcher was not found (found $($launcherRecords.Count)). Nothing was stopped."
    }
}

$ownerRecord = $null
if ($listeners.Count -gt 0) {
    $ownerIds = @($listeners | ForEach-Object { [int]$_.OwningProcess } | Sort-Object -Unique)
    if ($ownerIds.Count -ne 1) {
        throw "Port 8686 has more than one owner. Nothing was stopped."
    }
    $ownerId = [int]$ownerIds[0]
    $owners = @($snapshot | Where-Object { [int]$_.ProcessId -eq $ownerId })
    if ($owners.Count -ne 1 -or -not (Test-ControlPlaneOwner $owners[0] $venvPython $snapshot)) {
        $ownerPath = if ($owners.Count -eq 1 -and -not [string]::IsNullOrWhiteSpace([string]$owners[0].ExecutablePath)) {
            [string]$owners[0].ExecutablePath
        } else { '<unavailable>' }
        throw "Port 8686 owner PID $ownerId is not this project's verified .venv SiteHub control-plane chain (executable: $ownerPath). Nothing was stopped."
    }
    $ownerRecord = $owners[0]
    Write-Step "Verified port 8686 owner PID $ownerId from this project's .venv process chain."
}

$rootRecords = New-Object System.Collections.Generic.List[object]
foreach ($record in @($launcherRecords)) { $rootRecords.Add($record) }
if ($null -ne $ownerRecord) { $rootRecords.Add($ownerRecord) }
$rootIds = @($rootRecords | ForEach-Object { [int]$_.ProcessId } | Sort-Object -Unique)
$descendants = if ($rootIds.Count -gt 0) { @(Get-DescendantRecords $snapshot $rootIds) } else { @() }
$cleanupRecords = @($descendants | Where-Object { $null -ne (Get-CleanupRole $_) })

Write-Step "Tracked $($cleanupRecords.Count) Node/Chrome/OCR descendant process(es) for cleanup verification."
if ($AuditOnly -or $WhatIfPreference) {
    Write-Step 'Audit PASS: ownership, executable paths, command lines, and process ancestry are valid. No changes were made.'
    exit 0
}

if ($taskIsRunning -and $PSCmdlet.ShouldProcess(
    "$($context.TaskPath)$($context.TaskName)",
    'Stop the verified SiteHub scheduled-task instance'
)) {
    $taskForStop = Get-SiteHubRootStartupTask $context.TaskName $context.TaskPath
    if ($null -eq $taskForStop -or
        -not (Test-SiteHubStartupTaskOwned $taskForStop $context) -or
        [string]$taskForStop.State -ne 'Running') {
        throw 'The root SiteHub task changed identity or state after validation; nothing was stopped.'
    }
    Stop-ScheduledTask -TaskName $context.TaskName -TaskPath $context.TaskPath -ErrorAction Stop
}

# Give Task Scheduler and the Python/Node shutdown jobs a short opportunity to
# close the verified tree before the exact-PID fallback is used.
$graceSeconds = [Math]::Min(5, $TimeoutSeconds)
$graceDeadline = [DateTime]::UtcNow.AddSeconds($graceSeconds)
while ([DateTime]::UtcNow -lt $graceDeadline) {
    $ownerAlive = $null -ne $ownerRecord -and (Test-SameProcessAlive $ownerRecord)
    $cleanupAlive = @($cleanupRecords | Where-Object { Test-SameProcessAlive $_ })
    if (-not $ownerAlive -and $cleanupAlive.Count -eq 0) { break }
    Start-Sleep -Milliseconds 250
}

if ($null -ne $ownerRecord -and (Test-SameProcessAlive $ownerRecord)) {
    Stop-VerifiedProcess $ownerRecord 'Stop the verified SiteHub control plane after task shutdown' $PSCmdlet
}
foreach ($record in @($cleanupRecords | Where-Object { Test-SameProcessAlive $_ })) {
    $role = Get-CleanupRole $record
    Stop-VerifiedProcess $record "Remove the verified SiteHub $role descendant after control-plane shutdown" $PSCmdlet
}

$deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
do {
    $remainingPortRows = @(Get-ListenerRows 8686)
    $remainingCleanup = @($cleanupRecords | Where-Object { Test-SameProcessAlive $_ })
    $currentOwnedTask = if ($null -ne $ownedTask) {
        Get-SiteHubRootStartupTask $context.TaskName $context.TaskPath
    } else { $null }
    if ($null -ne $ownedTask -and $null -ne $currentOwnedTask -and
        -not (Test-SiteHubStartupTaskOwned $currentOwnedTask $context)) {
        throw 'The owned root SiteHub task changed ownership during shutdown; refusing further action.'
    }
    $stillRunning = $null -ne $currentOwnedTask -and [string]$currentOwnedTask.State -eq 'Running'
    if ($remainingPortRows.Count -eq 0 -and $remainingCleanup.Count -eq 0 -and -not $stillRunning) { break }
    Start-Sleep -Milliseconds 250
} while ([DateTime]::UtcNow -lt $deadline)

$remainingPortRows = @(Get-ListenerRows 8686)
$remainingCleanup = @($cleanupRecords | Where-Object { Test-SameProcessAlive $_ })
$finalTask = if ($null -ne $ownedTask) {
    Get-SiteHubRootStartupTask $context.TaskName $context.TaskPath
} else { $null }
$finalTaskRunning = $null -ne $finalTask -and [string]$finalTask.State -eq 'Running'
if ($remainingPortRows.Count -gt 0) {
    throw 'Port 8686 remains open after the verified SiteHub shutdown.'
}
if ($remainingCleanup.Count -gt 0) {
    $residue = @($remainingCleanup | ForEach-Object { "$(Get-CleanupRole $_):$([int]$_.ProcessId)" }) -join ', '
    throw "Verified SiteHub Node/Chrome/OCR descendants remain alive: $residue"
}
if ($finalTaskRunning) {
    throw 'The verified SiteHub scheduled-task instance still reports Running.'
}

Write-Step 'PASS: SiteHub stopped; port 8686 and verified Node/Chrome/OCR descendants are clear.'
