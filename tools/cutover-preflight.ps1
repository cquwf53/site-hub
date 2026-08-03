param(
    [string]$SiteHubProject = '',
    [string]$VShopProject = 'C:\Users\31587\.gemini\antigravity\scratch\vshop_monitor',
    [string]$AutoCheckinProject = 'E:\myprojects\auto-checkin-hub',
    [string]$VShopDatabase = '',
    [string]$AutoCheckinDatabase = '',
    [string]$ProfileSource = '',
    [string]$TargetData = '',
    [string]$LegacyTaskName = 'AutoCheckin Hub',
    [string]$SiteHubTaskName = 'SiteHub',
    [switch]$SkipProfileInventory,
    [string]$ReceiptPath = '',
    [ValidateRange(1, 60)][int]$ReceiptTtlMinutes = 30
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

if (-not $SiteHubProject) { $SiteHubProject = Join-Path $PSScriptRoot '..' }
$SiteHubProject = [System.IO.Path]::GetFullPath($SiteHubProject)
$VShopProject = [System.IO.Path]::GetFullPath($VShopProject)
$AutoCheckinProject = [System.IO.Path]::GetFullPath($AutoCheckinProject)
if (-not $VShopDatabase) { $VShopDatabase = Join-Path $VShopProject 'data\vshop.db' }
if (-not $AutoCheckinDatabase) { $AutoCheckinDatabase = Join-Path $AutoCheckinProject 'data\checkin.db' }
if (-not $ProfileSource) { $ProfileSource = Join-Path $AutoCheckinProject 'data\browser-profiles' }
if (-not $TargetData) { $TargetData = Join-Path $SiteHubProject 'data' }
$VShopDatabase = [System.IO.Path]::GetFullPath($VShopDatabase)
$AutoCheckinDatabase = [System.IO.Path]::GetFullPath($AutoCheckinDatabase)
$ProfileSource = [System.IO.Path]::GetFullPath($ProfileSource)
$TargetData = [System.IO.Path]::GetFullPath($TargetData)
$defaultTargetData = [System.IO.Path]::GetFullPath((Join-Path $SiteHubProject 'data'))
$targetDatabase = Join-Path $TargetData 'site-hub.db'
$targetProfiles = Join-Path $TargetData 'browser-profiles'
$profileBytes = [int64]0
$profileInventoryAvailable = $false
$receiptFullPath = if ([string]::IsNullOrWhiteSpace($ReceiptPath)) {
    ''
} else {
    [System.IO.Path]::GetFullPath($ReceiptPath)
}

$script:results = @()
$script:databaseHashes = @{}

function Add-Result([string]$Level, [string]$Name, [string]$Message) {
    $script:results += [pscustomobject]@{
        Level = $Level.ToUpperInvariant()
        Name = $Name
        Message = $Message
    }
}

function Normalize-ForComparison([string]$Path) {
    return [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
}

function Test-IsInside([string]$Candidate, [string]$Parent) {
    $candidateFull = Normalize-ForComparison $Candidate
    $parentFull = Normalize-ForComparison $Parent
    return $candidateFull.Equals($parentFull, [System.StringComparison]::OrdinalIgnoreCase) -or
        $candidateFull.StartsWith($parentFull + '\', [System.StringComparison]::OrdinalIgnoreCase)
}

function Test-Database([string]$Name, [string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        Add-Result 'BLOCK' $Name "Database is missing: $Path"
        return
    }
    try {
        $item = Get-Item -LiteralPath $Path
        $hash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
        $script:databaseHashes[(Normalize-ForComparison $Path)] = $hash
        Add-Result 'PASS' $Name "Readable; bytes=$($item.Length); sha256=$hash; path=$Path"
    } catch {
        Add-Result 'BLOCK' $Name "Database cannot be read or hashed: $Path ($($_.Exception.Message))"
        return
    }

    $wal = "$Path-wal"
    $shm = "$Path-shm"
    if (Test-Path -LiteralPath $wal -PathType Leaf) {
        $walItem = Get-Item -LiteralPath $wal
        if ($walItem.Length -gt 0) {
            Add-Result 'BLOCK' "$Name WAL" "Non-empty WAL exists ($($walItem.Length) bytes). Stop the old service before the frozen snapshot: $wal"
        } else {
            Add-Result 'WARN' "$Name WAL" "An empty WAL sidecar exists. Recheck after the old service is stopped: $wal"
        }
    }
    if (Test-Path -LiteralPath $shm -PathType Leaf) {
        Add-Result 'WARN' "$Name SHM" "A shared-memory sidecar exists. Its presence can indicate a recently open database: $shm"
    }
}

function Get-GitState([string]$Name, [string]$Project) {
    if (-not (Test-Path -LiteralPath (Join-Path $Project '.git'))) {
        Add-Result 'BLOCK' "$Name Git" "Git repository is missing: $Project"
        return
    }
    $git = Get-Command git.exe -ErrorAction SilentlyContinue
    if ($null -eq $git) {
        Add-Result 'BLOCK' "$Name Git" 'git.exe is unavailable.'
        return
    }
    try {
        $oid = (& $git.Source -C $Project rev-parse HEAD 2>$null).Trim()
        $changes = @(& $git.Source -C $Project status --short --untracked-files=all 2>$null)
        if ($LASTEXITCODE -ne 0) { throw 'git status returned a non-zero exit code' }
        if ($changes.Count -eq 0) {
            Add-Result 'PASS' "$Name Git" "Clean; oid=$oid; path=$Project"
        } else {
            Add-Result 'BLOCK' "$Name Git" "Working tree is not clean ($($changes.Count) entries): $Project"
            foreach ($change in $changes | Select-Object -First 20) {
                Add-Result 'BLOCK' "$Name Git detail" ([string]$change)
            }
        }
    } catch {
        Add-Result 'BLOCK' "$Name Git" "Unable to inspect repository: $($_.Exception.Message)"
    }
}

function Test-LikelyWritable([string]$Path) {
    $probe = $Path
    while (-not (Test-Path -LiteralPath $probe -PathType Container)) {
        $parent = [System.IO.Directory]::GetParent($probe)
        if ($null -eq $parent) { return $false }
        $probe = $parent.FullName
    }
    try {
        $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
        $tokenSids = @($identity.User.Value) + @($identity.Groups | ForEach-Object { $_.Value })
        $acl = Get-Acl -LiteralPath $probe
        $rules = $acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])
        $writeMask = [System.Security.AccessControl.FileSystemRights]::Write -bor
            [System.Security.AccessControl.FileSystemRights]::Modify -bor
            [System.Security.AccessControl.FileSystemRights]::FullControl -bor
            [System.Security.AccessControl.FileSystemRights]::CreateFiles -bor
            [System.Security.AccessControl.FileSystemRights]::CreateDirectories
        $allowed = $false
        foreach ($rule in $rules) {
            if ($tokenSids -notcontains $rule.IdentityReference.Value) { continue }
            if (($rule.FileSystemRights -band $writeMask) -eq 0) { continue }
            if ($rule.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Deny) {
                return $false
            }
            if ($rule.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Allow) {
                $allowed = $true
            }
        }
        return $allowed
    } catch {
        return $false
    }
}

function Get-AllTcpListeners {
    $netstat = Join-Path $env:WINDIR 'System32\netstat.exe'
    if (-not (Test-Path -LiteralPath $netstat -PathType Leaf)) { return }
    foreach ($line in @(& $netstat -ano -p tcp 2>$null)) {
        if ($line -match '^\s*TCP\s+(\S+):(\d+)\s+(\S+):(\d+)\s+(\S+)\s+(\d+)\s*$') {
            $remotePort = [int]$Matches[4]
            $state = [string]$Matches[5]
            if ($remotePort -eq 0 -or $state -match 'LISTEN') {
                [pscustomobject]@{
                    Address = [string]$Matches[1]
                    Port = [int]$Matches[2]
                    ProcessId = [int]$Matches[6]
                }
            }
        }
    }
}

function Test-ScheduledTaskState([string]$Name, [bool]$MustBeAbsentOrDisabled) {
    if ($null -eq (Get-Command Get-ScheduledTask -ErrorAction SilentlyContinue)) {
        $level = if ($MustBeAbsentOrDisabled) { 'BLOCK' } else { 'WARN' }
        Add-Result $level "Scheduled task $Name" 'ScheduledTasks module is unavailable; task state cannot be proven.'
        return
    }
    try {
        $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
        if ($null -eq $task) {
            Add-Result 'PASS' "Scheduled task $Name" 'Task is absent.'
            return
        }
        $enabled = $task.State -ne 'Disabled'
        if ($MustBeAbsentOrDisabled -and $enabled) {
            Add-Result 'BLOCK' "Scheduled task $Name" "Task exists and is $($task.State). Disable it before the frozen migration window."
        } else {
            Add-Result 'PASS' "Scheduled task $Name" "Task exists with state=$($task.State)."
        }
    } catch {
        $level = if ($MustBeAbsentOrDisabled) { 'BLOCK' } else { 'WARN' }
        Add-Result $level "Scheduled task $Name" "Unable to query task state: $($_.Exception.Message)"
    }
}

function Test-RequiredFile([string]$RelativePath) {
    $path = Join-Path $SiteHubProject $RelativePath
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        Add-Result 'PASS' "SiteHub file" $RelativePath
    } else {
        Add-Result 'BLOCK' "SiteHub file" "Missing: $RelativePath"
    }
}

$preflightMode = if ($receiptFullPath) {
    'read-only checks plus an explicitly requested PASS-only receipt write'
} else {
    'read-only'
}
Write-Host "SiteHub cutover preflight ($preflightMode)" -ForegroundColor Cyan
Write-Host "SiteHub project : $SiteHubProject"
Write-Host "VShop project  : $VShopProject"
Write-Host "AutoCheckin    : $AutoCheckinProject"
Write-Host "Target data    : $TargetData"
Write-Host ''

if ($env:OS -eq 'Windows_NT') {
    Add-Result 'PASS' 'Operating system' "Windows; user=$([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)"
} else {
    Add-Result 'BLOCK' 'Operating system' 'Formal migration requires Windows DPAPI CurrentUser.'
}

if (-not (Test-Path -LiteralPath $SiteHubProject -PathType Container)) {
    Add-Result 'BLOCK' 'SiteHub project' "Directory is missing: $SiteHubProject"
}
if (Test-IsInside $TargetData $VShopProject -or Test-IsInside $TargetData $AutoCheckinProject) {
    Add-Result 'BLOCK' 'Target isolation' 'Target data is inside a legacy project. Choose an isolated SiteHub data directory.'
} elseif ((Normalize-ForComparison $targetDatabase) -in @(
    (Normalize-ForComparison $VShopDatabase), (Normalize-ForComparison $AutoCheckinDatabase)
)) {
    Add-Result 'BLOCK' 'Target isolation' 'Target database equals a legacy source database.'
} else {
    Add-Result 'PASS' 'Target isolation' 'Target data is outside both legacy project roots.'
}
if ((Normalize-ForComparison $TargetData) -eq (Normalize-ForComparison $defaultTargetData)) {
    Add-Result 'PASS' 'Runtime data path' 'Target data matches the path used by start.bat and the startup task.'
} elseif ($env:SITEHUB_DATA_DIR -and
          (Normalize-ForComparison $env:SITEHUB_DATA_DIR) -eq (Normalize-ForComparison $TargetData)) {
    Add-Result 'WARN' 'Runtime data path' 'A custom target is selected through the current SITEHUB_DATA_DIR environment. Ensure the same persistent user environment is available to the startup task.'
} else {
    Add-Result 'BLOCK' 'Runtime data path' "Custom TargetData is not the path start.bat will use. Select $defaultTargetData or persist SITEHUB_DATA_DIR=$TargetData before migration."
}
if ($receiptFullPath) {
    $receiptParent = [System.IO.Path]::GetDirectoryName($receiptFullPath)
    $receiptRuntimeState = Join-Path $SiteHubProject '.runtime-state'
    if (Test-IsInside $receiptFullPath $TargetData) {
        Add-Result 'BLOCK' 'Cutover receipt path' 'Receipt must be outside TargetData so the verified-empty target remains empty.'
    } elseif ((Test-IsInside $receiptFullPath $VShopProject) -or
              (Test-IsInside $receiptFullPath $AutoCheckinProject) -or
              (Test-IsInside $receiptFullPath $ProfileSource)) {
        Add-Result 'BLOCK' 'Cutover receipt path' 'Receipt must not modify either frozen legacy project or the Profile source.'
    } elseif ((Test-IsInside $receiptFullPath $SiteHubProject) -and
              -not (Test-IsInside $receiptFullPath $receiptRuntimeState)) {
        Add-Result 'BLOCK' 'Cutover receipt path' 'A receipt inside SiteHub must use the ignored .runtime-state directory so the audited Git tree stays clean.'
    } elseif ((Normalize-ForComparison $receiptFullPath) -in @(
        (Normalize-ForComparison $VShopDatabase),
        (Normalize-ForComparison $AutoCheckinDatabase),
        (Normalize-ForComparison $targetDatabase)
    )) {
        Add-Result 'BLOCK' 'Cutover receipt path' 'Receipt path collides with a source or target database.'
    } elseif (-not (Test-LikelyWritable $receiptParent)) {
        Add-Result 'BLOCK' 'Cutover receipt path' "No applicable write/modify ACL could be confirmed for: $receiptParent"
    } else {
        Add-Result 'PASS' 'Cutover receipt path' "A receipt will be written only after every blocker is clear: $receiptFullPath"
    }
}

Test-Database 'VShop database' $VShopDatabase
Test-Database 'AutoCheckin database' $AutoCheckinDatabase
Get-GitState 'VShop' $VShopProject
Get-GitState 'AutoCheckin' $AutoCheckinProject
Get-GitState 'SiteHub' $SiteHubProject

if (Test-Path -LiteralPath $ProfileSource -PathType Container) {
    $activeLocks = @(Get-ChildItem -LiteralPath $ProfileSource -Recurse -Force -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -in @('SingletonLock', 'SingletonCookie', 'SingletonSocket', 'DevToolsActivePort') })
    if ($activeLocks.Count -gt 0) {
        Add-Result 'BLOCK' 'Browser profiles' "Found $($activeLocks.Count) active Chromium lock/DevTools markers. Close the owning browser before profile copy."
    } else {
        Add-Result 'PASS' 'Browser profiles' "No top-level Chromium singleton/DevTools lock markers found: $ProfileSource"
    }
    if (-not $SkipProfileInventory) {
        $files = @(Get-ChildItem -LiteralPath $ProfileSource -Recurse -Force -File -ErrorAction SilentlyContinue)
        $bytes = ($files | Measure-Object -Property Length -Sum).Sum
        if ($null -eq $bytes) { $bytes = 0 }
        $profileBytes = [int64]$bytes
        $profileInventoryAvailable = $true
        $profiles = @(Get-ChildItem -LiteralPath $ProfileSource -Directory -Force -ErrorAction SilentlyContinue)
        Add-Result 'PASS' 'Profile inventory' "profiles=$($profiles.Count); files=$($files.Count); bytes=$bytes"
    } else {
        Add-Result 'WARN' 'Profile inventory' 'Skipped by -SkipProfileInventory.'
    }
} else {
    Add-Result 'BLOCK' 'Browser profiles' "Profile source is missing: $ProfileSource"
}

$legacyLock = Join-Path $AutoCheckinProject 'data\auto-checkin-hub.lock'
if (Test-Path -LiteralPath $legacyLock -PathType Leaf) {
    Add-Result 'BLOCK' 'AutoCheckin process lock' "Legacy lock file exists. Confirm its PID and stop the old service: $legacyLock"
} else {
    Add-Result 'PASS' 'AutoCheckin process lock' 'Legacy lock file is absent.'
}

$listeners8686 = @(Get-AllTcpListeners | Where-Object { $_.Port -eq 8686 })
if ($listeners8686.Count -eq 0) {
    Add-Result 'PASS' 'TCP 8686' 'Port is free.'
} else {
    $owners = ($listeners8686 | ForEach-Object { "$($_.Address):$($_.Port) pid=$($_.ProcessId)" }) -join '; '
    Add-Result 'BLOCK' 'TCP 8686' "Port is already listening: $owners"
}

Test-ScheduledTaskState $LegacyTaskName $true
Test-ScheduledTaskState $SiteHubTaskName $true

$processGroups = @(
    @{ Label = 'Chrome'; Names = @('chrome', 'msedge') },
    @{ Label = 'Node'; Names = @('node') },
    @{ Label = 'OCR'; Names = @('tesseract', 'tesseract-ocr', 'ddddocr') }
)
foreach ($group in $processGroups) {
    $processes = @(Get-Process -Name $group.Names -ErrorAction SilentlyContinue)
    if ($processes.Count -eq 0) {
        Add-Result 'PASS' "$($group.Label) processes" 'None detected.'
    } else {
        $pids = ($processes | Select-Object -ExpandProperty Id | Sort-Object | Select-Object -First 30) -join ','
        Add-Result 'WARN' "$($group.Label) processes" "Detected $($processes.Count) process(es), pids=$pids. Attribute them before stopping anything; ddddocr may appear as python.exe."
    }
}

if ($null -ne (Get-Command Get-CimInstance -ErrorAction SilentlyContinue)) {
    try {
        $ownedProcesses = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
            $commandLine = [string]$_.CommandLine
            $_.ProcessId -ne $PID -and $commandLine -and (
                $commandLine.IndexOf($VShopProject, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -or
                $commandLine.IndexOf($AutoCheckinProject, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -or
                $commandLine -match '(?i)(?:^|[\s"''])app\.py(?:[\s"'']|$)' -or
                $commandLine -match '(?i)server[\\/]server\.js(?:[\s"'']|$)' -or
                $commandLine -match 'site_hub[\\/.](?:ocr_worker|main)'
            )
        })
        if ($ownedProcesses.Count -eq 0) {
            Add-Result 'PASS' 'Project-owned processes' 'No VShop/AutoCheckin/SiteHub Python or Node command lines detected.'
        } else {
            $details = ($ownedProcesses | Select-Object -First 20 | ForEach-Object { "$($_.Name) pid=$($_.ProcessId)" }) -join '; '
            Add-Result 'BLOCK' 'Project-owned processes' "Detected processes that must be attributed and stopped before the frozen snapshot: $details"
        }
    } catch {
        Add-Result 'BLOCK' 'Project-owned processes' "Unable to inspect process command lines; the frozen snapshot cannot be proven safe: $($_.Exception.Message)"
    }
} else {
    Add-Result 'BLOCK' 'Project-owned processes' 'Get-CimInstance is unavailable; the frozen snapshot cannot be proven safe.'
}

if (Test-Path -LiteralPath $TargetData -PathType Container) {
    $targetEntries = @(
        Get-ChildItem -LiteralPath $TargetData -Force -ErrorAction Stop |
            Where-Object {
                -not (-not $_.PSIsContainer -and $_.Name -ceq '.gitkeep' -and $_.Length -le 2)
            }
    )
    if ($targetEntries.Count -eq 0) {
        Add-Result 'PASS' 'Target data emptiness' "Existing directory has no data (an empty .gitkeep marker is allowed): $TargetData"
    } else {
        Add-Result 'BLOCK' 'Target data emptiness' "Directory contains $($targetEntries.Count) entries. Use a fresh target or explicitly review every entry: $TargetData"
    }
} elseif (Test-Path -LiteralPath $TargetData) {
    Add-Result 'BLOCK' 'Target data path' "Path exists but is not a directory: $TargetData"
} else {
    Add-Result 'PASS' 'Target data emptiness' "Directory does not exist yet and is therefore empty: $TargetData"
}
if (Test-LikelyWritable $TargetData) {
    Add-Result 'PASS' 'Target data ACL' 'Current user has an applicable write/modify ACL on the target or nearest existing parent (no probe file was created).'
} else {
    Add-Result 'BLOCK' 'Target data ACL' 'No applicable write/modify ACL could be confirmed without writing. Review permissions manually.'
}
if ($profileInventoryAvailable) {
    try {
        $sourceDbBytes = [int64]0
        foreach ($sourceDb in @($VShopDatabase, $AutoCheckinDatabase)) {
            if (Test-Path -LiteralPath $sourceDb -PathType Leaf) {
                $sourceDbBytes += [int64](Get-Item -LiteralPath $sourceDb).Length
            }
        }
        $requiredBytes = [int64][Math]::Ceiling($profileBytes * 1.25 + $sourceDbBytes * 3 + 512MB)
        $driveRoot = [System.IO.Path]::GetPathRoot($TargetData)
        $drive = New-Object System.IO.DriveInfo -ArgumentList $driveRoot
        if ($drive.AvailableFreeSpace -ge $requiredBytes) {
            Add-Result 'PASS' 'Target free space' "free=$($drive.AvailableFreeSpace); required=$requiredBytes; drive=$driveRoot"
        } else {
            Add-Result 'BLOCK' 'Target free space' "Insufficient free space: free=$($drive.AvailableFreeSpace); required=$requiredBytes; drive=$driveRoot"
        }
    } catch {
        Add-Result 'BLOCK' 'Target free space' "Unable to confirm target free space: $($_.Exception.Message)"
    }
} else {
    Add-Result 'WARN' 'Target free space' 'Profile inventory was skipped, so the copy-space requirement could not be calculated.'
}

foreach ($required in @(
    'start.bat', 'start.ps1', 'requirements.txt', 'package.json', 'package-lock.json',
    'frontend\dist\index.html', 'worker-node\src\main.js',
    'tools\migrate_legacy.py', 'tools\smoke-windows.ps1',
    'tools\preflight_probe.py', 'tools\smoke-launcher.ps1',
    'tools\install-startup.ps1', 'tools\uninstall-startup.ps1',
    'tools\stop-sitehub.ps1', 'tools\startup-task-common.ps1',
    'tools\start-hidden.vbs', 'docs\legacy-migration.md', 'docs\cutover.md'
)) { Test-RequiredFile $required }

if (Test-Path -LiteralPath (Join-Path $SiteHubProject 'node_modules') -PathType Container) {
    $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if ($null -eq $npm) {
        Add-Result 'BLOCK' 'Node dependencies' 'npm.cmd is unavailable, so the dependency tree cannot be verified.'
    } else {
        & $npm.Source --prefix $SiteHubProject ls --all --json *> $null
        if ($LASTEXITCODE -eq 0) {
            Add-Result 'PASS' 'Node dependencies' 'npm ls --all completed successfully.'
        } else {
            Add-Result 'BLOCK' 'Node dependencies' 'npm ls --all found missing or invalid dependencies.'
        }
    }
} else {
    Add-Result 'BLOCK' 'Node dependencies' 'node_modules is missing; prepare dependencies before the freeze window.'
}
$node = Get-Command node.exe -ErrorAction SilentlyContinue
if ($null -eq $node) {
    Add-Result 'BLOCK' 'Node runtime' 'node.exe is unavailable.'
} else {
    try {
        $nodeVersion = (& $node.Source --version).Trim()
        $nodeSemver = [version]($nodeVersion.TrimStart('v').Split('-')[0])
        $supported = (
            ($nodeSemver.Major -eq 20 -and $nodeSemver.Minor -ge 19) -or
            ($nodeSemver.Major -ge 22 -and (
                $nodeSemver.Major -gt 22 -or $nodeSemver.Minor -ge 12
            ))
        )
        if ($supported) {
            Add-Result 'PASS' 'Node runtime' $nodeVersion
        } else {
            Add-Result 'BLOCK' 'Node runtime' "Node ^20.19.0 or >=22.12.0 is required; found $nodeVersion"
        }
    } catch {
        Add-Result 'BLOCK' 'Node runtime' "Unable to query Node version: $($_.Exception.Message)"
    }
}

$venvPython = Join-Path $SiteHubProject '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
    & $venvPython -c 'import fastapi,uvicorn,httpx,bs4,lxml,apscheduler,aiosqlite,playwright,ddddocr,psutil,pydantic,multipart' *> $null
    if ($LASTEXITCODE -eq 0) {
        Add-Result 'PASS' 'Python dependencies' $venvPython
    } else {
        Add-Result 'BLOCK' 'Python dependencies' "The SiteHub virtual environment cannot import required packages: $venvPython"
    }
} else {
    Add-Result 'BLOCK' 'Python dependencies' "Virtual environment is missing: $venvPython"
}

if ((Test-Path -LiteralPath $venvPython -PathType Leaf) -and
    (Test-Path -LiteralPath $AutoCheckinDatabase -PathType Leaf)) {
    $probeScript = Join-Path $SiteHubProject 'tools\preflight_probe.py'
    $nonterminal = @(& $venvPython $probeScript legacy-nonterminal $AutoCheckinDatabase 2>$null)
    if ($LASTEXITCODE -ne 0 -or $nonterminal.Count -eq 0) {
        Add-Result 'BLOCK' 'Legacy nonterminal jobs' 'Unable to inspect AutoCheckin batch and batch-item state.'
    } elseif ([int]$nonterminal[-1] -gt 0) {
        Add-Result 'BLOCK' 'Legacy nonterminal jobs' "Found $($nonterminal[-1]) nonterminal batch or batch-item record(s). Let the old run settle or explicitly archive it before the frozen snapshot."
    } else {
        Add-Result 'PASS' 'Legacy nonterminal jobs' 'All legacy batches and batch items are terminal.'
    }
}

if ($receiptFullPath) {
    $preReceiptBlockers = @($script:results | Where-Object { $_.Level -eq 'BLOCK' })
    if ($preReceiptBlockers.Count -gt 0) {
        Add-Result 'WARN' 'Cutover receipt' "Not generated because $($preReceiptBlockers.Count) blocker(s) remain."
    } else {
        $temporaryReceipt = "$receiptFullPath.tmp-$([guid]::NewGuid().ToString('N'))"
        try {
            $issuedAt = [DateTimeOffset]::UtcNow
            $expiresAt = $issuedAt.AddMinutes($ReceiptTtlMinutes)
            $receipt = [ordered]@{
                kind = 'sitehub-cutover-preflight'
                version = 1
                issued_at = $issuedAt.ToString('o')
                expires_at = $expiresAt.ToString('o')
                sitehub_project = $SiteHubProject
                sources = [ordered]@{
                    vshop = [ordered]@{
                        database = $VShopDatabase
                        sha256 = $script:databaseHashes[(Normalize-ForComparison $VShopDatabase)]
                    }
                    autocheckin = [ordered]@{
                        database = $AutoCheckinDatabase
                        sha256 = $script:databaseHashes[(Normalize-ForComparison $AutoCheckinDatabase)]
                    }
                }
                target_database = $targetDatabase
                profile_source = $ProfileSource
                profile_target = $targetProfiles
            }
            $receiptJson = $receipt | ConvertTo-Json -Depth 6
            [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($receiptFullPath))
            $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
            [System.IO.File]::WriteAllText($temporaryReceipt, $receiptJson, $utf8NoBom)
            Move-Item -LiteralPath $temporaryReceipt -Destination $receiptFullPath -Force
            Add-Result 'PASS' 'Cutover receipt' "Written; expires=$($expiresAt.ToString('o')); path=$receiptFullPath"
        } catch {
            Add-Result 'BLOCK' 'Cutover receipt' "Unable to write receipt: $($_.Exception.Message)"
        } finally {
            if (Test-Path -LiteralPath $temporaryReceipt -PathType Leaf) {
                Remove-Item -LiteralPath $temporaryReceipt -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

Write-Host ''
foreach ($result in $script:results) {
    $color = switch ($result.Level) {
        'PASS' { 'Green' }
        'WARN' { 'Yellow' }
        default { 'Red' }
    }
    Write-Host ("[{0}] {1}: {2}" -f $result.Level, $result.Name, $result.Message) -ForegroundColor $color
}

$blockers = @($script:results | Where-Object { $_.Level -eq 'BLOCK' })
$warnings = @($script:results | Where-Object { $_.Level -eq 'WARN' })
Write-Host ''
Write-Host "Summary: blockers=$($blockers.Count), warnings=$($warnings.Count), checks=$($script:results.Count)" -ForegroundColor Cyan

Write-Host ''
Write-Host 'Suggested commands (printed only; this script did not execute them):' -ForegroundColor Cyan
$suggestedReceipt = if ($receiptFullPath) {
    $receiptFullPath
} else {
    Join-Path $SiteHubProject '.runtime-state\cutover-receipt.json'
}
Write-Host "1. Re-run this preflight after resolving every blocker:"
Write-Host "   powershell -NoProfile -ExecutionPolicy Bypass -File `"$SiteHubProject\tools\cutover-preflight.ps1`" -SiteHubProject `"$SiteHubProject`" -VShopProject `"$VShopProject`" -AutoCheckinProject `"$AutoCheckinProject`" -TargetData `"$TargetData`" -ReceiptPath `"$suggestedReceipt`""
Write-Host '2. Read-only migration analysis:'
Write-Host "   & `"$venvPython`" `"$SiteHubProject\tools\migrate_legacy.py`" analyze --vshop-db `"$VShopDatabase`" --autocheckin-db `"$AutoCheckinDatabase`" --include-profile-inventory --profile-source `"$ProfileSource`""
Write-Host '3. Full disposable rehearsal:'
Write-Host "   & `"$venvPython`" `"$SiteHubProject\tools\migrate_legacy.py`" dry-run --vshop-db `"$VShopDatabase`" --autocheckin-db `"$AutoCheckinDatabase`" --target-db `"$targetDatabase`" --include-profile-inventory --profile-source `"$ProfileSource`""
Write-Host '4. Formal apply (ONLY after freeze, approvals, clean preflight, and successful rehearsal):'
Write-Host "   & `"$venvPython`" `"$SiteHubProject\tools\migrate_legacy.py`" apply --vshop-db `"$VShopDatabase`" --autocheckin-db `"$AutoCheckinDatabase`" --target-db `"$targetDatabase`" --preflight-receipt `"$suggestedReceipt`" --copy-profiles --profile-source `"$ProfileSource`" --profile-target `"$targetProfiles`""
Write-Host '5. Windows acceptance before enabling the new schedule/task:'
Write-Host "   powershell -NoProfile -ExecutionPolicy Bypass -File `"$SiteHubProject\tools\smoke-windows.ps1`" -PythonPath `"$venvPython`" -SkipDependencyInstall -SkipFrontendBuild"
Write-Host '6. Register SiteHub startup only after data/UI/Profile acceptance:'
Write-Host "   powershell -NoProfile -ExecutionPolicy Bypass -File `"$SiteHubProject\tools\install-startup.ps1`""

if ($blockers.Count -gt 0) { exit 2 }
exit 0
