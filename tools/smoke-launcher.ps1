[CmdletBinding()]
param(
    [switch]$UseVerifiedSetup,
    [ValidateRange(20, 300)]
    [int]$StartupTimeoutSeconds = 120,
    [ValidateRange(5, 30)]
    [int]$SecondLauncherTimeoutSeconds = 10,
    [ValidateRange(10, 120)]
    [int]$ShutdownTimeoutSeconds = 40
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\')
$systemTempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd('\')
$runId = [Guid]::NewGuid().ToString('N')
$runRoot = Join-Path $systemTempRoot ("SiteHub launcher smoke-$runId")
$projectAlias = Join-Path $runRoot 'project path with spaces'
$dataRoot = Join-Path $runRoot 'isolated data with spaces'
$mainStdout = Join-Path $runRoot 'launcher-main.stdout.log'
$mainStderr = Join-Path $runRoot 'launcher-main.stderr.log'
$secondStdout = Join-Path $runRoot 'launcher-second.stdout.log'
$secondStderr = Join-Path $runRoot 'launcher-second.stderr.log'
$appUrl = 'http://127.0.0.1:8686'
$readyUrl = "$appUrl/api/v1/health/ready"
$testStartedUtc = [DateTime]::UtcNow

$mainLaunch = $null
$secondLaunch = $null
$selfHealLaunch = $null
$tracked = @{}
$failure = $null
$cleanupFailure = $null
$projectAliasCreated = $false
$watcherState = $null

$savedEnvironment = @{}
foreach ($name in @('SITEHUB_DATA_DIR', 'SITEHUB_NO_BROWSER', 'SITEHUB_NO_PAUSE', 'SITEHUB_SKIP_BUILD', 'PYTHONUNBUFFERED')) {
    $item = Get-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
    $savedEnvironment[$name] = if ($null -eq $item) {
        [pscustomobject]@{ Exists = $false; Value = $null }
    } else {
        [pscustomobject]@{ Exists = $true; Value = [string]$item.Value }
    }
}

function Write-Step([string]$Message) {
    Write-Host "[SiteHub launcher smoke] $Message"
}

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

function Assert-SafeRunRoot([string]$Path) {
    $full = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
    $insideTemp = $full.StartsWith($systemTempRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)
    $leaf = [System.IO.Path]::GetFileName($full)
    if (-not $insideTemp -or -not $leaf.StartsWith('SiteHub launcher smoke-', [System.StringComparison]::Ordinal)) {
        throw "Refusing launcher-smoke cleanup outside its system-temp root: $full"
    }
    return $full
}

function Get-ProcessRows {
    try {
        return @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop)
    } catch {
        return @(Get-WmiObject -Class Win32_Process -ErrorAction Stop)
    }
}

function Get-ProcessRow([int]$ProcessId) {
    $filter = "ProcessId = $ProcessId"
    try {
        return Get-CimInstance -ClassName Win32_Process -Filter $filter -ErrorAction Stop
    } catch {
        return Get-WmiObject -Class Win32_Process -Filter $filter -ErrorAction SilentlyContinue
    }
}

function Get-ProcessIdentity([int]$ProcessId) {
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $null }
    $row = Get-ProcessRow $ProcessId
    if ($null -eq $row) { return $null }
    try {
        $startTicks = $process.StartTime.ToUniversalTime().Ticks
    } catch {
        return $null
    }
    $commandLine = [string]$row.CommandLine
    if ([string]::IsNullOrWhiteSpace($commandLine)) {
        throw "Cannot safely identify PID $ProcessId because its command line is unavailable."
    }
    return [pscustomobject]@{
        Pid = $ProcessId
        StartTicks = [long]$startTicks
        CommandLine = $commandLine
        ExecutablePath = [string]$row.ExecutablePath
        Name = [string]$process.ProcessName
    }
}

function Test-SameIdentity($Expected) {
    if ($null -eq $Expected) { return $false }
    $actual = Get-ProcessIdentity ([int]$Expected.Pid)
    if ($null -eq $actual) { return $false }
    return (
        [long]$actual.StartTicks -eq [long]$Expected.StartTicks -and
        [string]$actual.CommandLine -ceq [string]$Expected.CommandLine
    )
}

function Add-TrackedIdentity([int]$ProcessId) {
    $identity = Get-ProcessIdentity $ProcessId
    if ($null -eq $identity) { return }
    $key = [string]$ProcessId
    if ($tracked.ContainsKey($key)) {
        if (-not (Test-SameIdentity $tracked[$key])) {
            throw "PID $ProcessId was reused while the launcher smoke test was running."
        }
        return
    }
    $tracked[$key] = $identity
}

function Add-TrackedTree([int]$RootPid) {
    $rootIdentity = Get-ProcessIdentity $RootPid
    if ($null -eq $rootIdentity) { return }
    Add-TrackedIdentity $RootPid

    $rows = @(Get-ProcessRows)
    $children = @{}
    foreach ($row in $rows) {
        $parentKey = [string][int]$row.ParentProcessId
        if (-not $children.ContainsKey($parentKey)) {
            $children[$parentKey] = New-Object System.Collections.Generic.List[int]
        }
        $children[$parentKey].Add([int]$row.ProcessId)
    }
    $queue = New-Object System.Collections.Generic.Queue[int]
    $queue.Enqueue($RootPid)
    $seen = New-Object 'System.Collections.Generic.HashSet[int]'
    [void]$seen.Add($RootPid)
    while ($queue.Count -gt 0) {
        $parent = $queue.Dequeue()
        $parentKey = [string]$parent
        if (-not $children.ContainsKey($parentKey)) { continue }
        foreach ($childPid in $children[$parentKey]) {
            if (-not $seen.Add([int]$childPid)) { continue }
            $childIdentity = Get-ProcessIdentity ([int]$childPid)
            if ($null -eq $childIdentity) { continue }
            # Parent PID reuse cannot pull an older, unrelated process into the
            # test tree: every child must have started after the launcher root.
            if ([long]$childIdentity.StartTicks -lt [long]$rootIdentity.StartTicks) { continue }
            Add-TrackedIdentity ([int]$childPid)
            $queue.Enqueue([int]$childPid)
        }
    }
}

function Stop-ExactIdentity($Identity) {
    if (-not (Test-SameIdentity $Identity)) { return $false }
    Stop-Process -Id ([int]$Identity.Pid) -Force -ErrorAction Stop
    return $true
}

function Wait-IdentityGone($Identity, [int]$TimeoutSeconds) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (-not (Test-SameIdentity $Identity)) { return $true }
        Start-Sleep -Milliseconds 200
    }
    return -not (Test-SameIdentity $Identity)
}

function Get-ListenerRows([int]$Port = 8686) {
    $netstat = Join-Path $env:WINDIR 'System32\netstat.exe'
    Assert-True (Test-Path -LiteralPath $netstat -PathType Leaf) 'Windows netstat.exe is unavailable.'
    $result = New-Object System.Collections.Generic.List[object]
    foreach ($line in @(& $netstat -ano -p tcp)) {
        if ($line -match '^\s*TCP\s+(\S+):(\d+)\s+(\S+):(\d+)\s+(\S+)\s+(\d+)\s*$') {
            $remotePort = [int]$Matches[4]
            $state = [string]$Matches[5]
            if ([int]$Matches[2] -eq $Port -and ($remotePort -eq 0 -or $state -match 'LISTEN')) {
                $result.Add([pscustomobject]@{
                    LocalAddress = [string]$Matches[1]
                    LocalPort = [int]$Matches[2]
                    OwningProcess = [int]$Matches[6]
                })
            }
        }
    }
    foreach ($item in $result) { Write-Output $item }
}

function Invoke-Json([string]$Uri) {
    return Invoke-RestMethod -UseBasicParsing -Uri $Uri -TimeoutSec 3 -Headers @{ Host = '127.0.0.1:8686' }
}

function Start-Launcher(
    [string]$StartBat,
    [string]$WorkingDirectory,
    [switch]$AllowEarlyExitWithoutIdentity,
    [ValidateRange(1, 60)]
    [int]$IdentityTimeoutSeconds = 3
) {
    $cmd = (Get-Command cmd.exe -ErrorAction Stop).Source
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $cmd
    # The outer quote pair belongs to cmd.exe /s /c; the inner pair quotes a
    # batch path which deliberately contains spaces in this acceptance test.
    $startInfo.Arguments = '/d /s /c ""' + $StartBat + '""'
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    Assert-True $process.Start() "Unable to launch: $StartBat"
    # If start.bat reaches its interactive failure pause, EOF keeps automated
    # cleanup deterministic instead of leaving an invisible cmd.exe behind.
    $process.StandardInput.Close()
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $identity = $null
    $identityDeadline = [DateTime]::UtcNow.AddSeconds($IdentityTimeoutSeconds)
    while ($null -eq $identity -and -not $process.HasExited -and [DateTime]::UtcNow -lt $identityDeadline) {
        $identity = Get-ProcessIdentity ([int]$process.Id)
        if ($null -eq $identity) { Start-Sleep -Milliseconds 50 }
    }
    if ($null -eq $identity) {
        # Some negative-path fixtures are expected to fail before CIM can
        # observe cmd.exe. The Process object still gives those callers the
        # exact exit code and redirected output; live launchers remain strict.
        if ($AllowEarlyExitWithoutIdentity -and $process.HasExited) {
            return [pscustomobject]@{
                Process = $process
                Identity = $null
                StdoutTask = $stdoutTask
                StderrTask = $stderrTask
            }
        }
        # The Process object holds a handle to the process which was actually
        # created, so this cannot target a later PID reuse.
        if (-not $process.HasExited) {
            try { $process.Kill() } catch { }
            try { $process.WaitForExit(3000) | Out-Null } catch { }
        }
        try { $stdoutTask.Wait(3000) | Out-Null } catch { }
        try { $stderrTask.Wait(3000) | Out-Null } catch { }
        $diagnostic = ''
        if ($stdoutTask.IsCompleted) { $diagnostic += [string]$stdoutTask.Result }
        if ($stderrTask.IsCompleted) { $diagnostic += "`n" + [string]$stderrTask.Result }
        throw "Unable to capture launcher PID $($process.Id) start-time/command-line identity. Launcher output: $diagnostic"
    }
    return [pscustomobject]@{
        Process = $process
        Identity = $identity
        StdoutTask = $stdoutTask
        StderrTask = $stderrTask
    }
}

function Save-LaunchLogs($Launch, [string]$StdoutPath, [string]$StderrPath) {
    if ($null -eq $Launch) { return }
    if ($Launch.StdoutTask.IsCompleted) {
        Set-Content -LiteralPath $StdoutPath -Value ([string]$Launch.StdoutTask.Result) -Encoding UTF8
    }
    if ($Launch.StderrTask.IsCompleted) {
        Set-Content -LiteralPath $StderrPath -Value ([string]$Launch.StderrTask.Result) -Encoding UTF8
    }
}

function Show-LaunchTail($Launch, [string]$Label) {
    if ($null -eq $Launch) { return }
    foreach ($entry in @(
        @{ Task = $Launch.StdoutTask; Stream = 'stdout' },
        @{ Task = $Launch.StderrTask; Stream = 'stderr' }
    )) {
        if (-not $entry.Task.IsCompleted) { continue }
        $lines = @(([string]$entry.Task.Result) -split "`r?`n")
        if ($lines.Count -gt 0) {
            Write-Warning "$Label $($entry.Stream) (last 25 lines):"
            $lines | Select-Object -Last 25 | ForEach-Object { if ($_ ) { Write-Warning $_ } }
        }
    }
}

function Wait-Ready($Launch, [int]$TimeoutSeconds) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $lastError = ''
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($Launch.Process.HasExited) {
            throw "start.bat exited before readiness (exit code $($Launch.Process.ExitCode))."
        }
        try {
            $ready = Invoke-Json $readyUrl
            if ($ready.status -eq 'ready') { return $ready }
        } catch {
            $lastError = $_.Exception.Message
        }
        Start-Sleep -Milliseconds 300
    }
    throw "SiteHub did not become ready in $TimeoutSeconds seconds. Last error: $lastError"
}

function Test-PythonEnvironment([string]$PythonPath) {
    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) { return $false }
    & $PythonPath -c 'import sys, fastapi, uvicorn, httpx, playwright, aiosqlite, apscheduler, ddddocr, psutil; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 14) else 1)' *> $null
    if ($LASTEXITCODE -ne 0) { return $false }
    & $PythonPath -m pip check *> $null
    return $LASTEXITCODE -eq 0
}

function Initialize-VerifiedSetup {
    $python = Join-Path $projectRoot '.venv\Scripts\python.exe'
    Assert-True (Test-PythonEnvironment $python) 'The existing .venv is not complete; run the full launcher setup first.'
    Assert-True (Test-Path -LiteralPath (Join-Path $projectRoot 'frontend\dist\index.html') -PathType Leaf) 'The verified frontend build is missing.'

    $node = Get-Command node.exe -ErrorAction SilentlyContinue
    $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    Assert-True ($null -ne $node -and $null -ne $npm) 'Node.js and npm.cmd are required.'
    $nodeVersion = (& $node.Source --version).Trim()
    Assert-True ($nodeVersion -match '^v?(\d+)\.(\d+)\.(\d+)') "Unable to parse the Node.js version: $nodeVersion"
    $nodeMajor = [int]$Matches[1]
    $nodeMinor = [int]$Matches[2]
    $nodeSupported = (($nodeMajor -eq 20 -and $nodeMinor -ge 19) -or ($nodeMajor -eq 22 -and $nodeMinor -ge 12) -or $nodeMajor -gt 22)
    Assert-True $nodeSupported "Node.js 20.19+ or 22.12+ is required; found $nodeVersion."
    Push-Location $projectRoot
    try {
        & $npm.Source ls --workspaces --depth=0 --omit=optional *> $null
        Assert-True ($LASTEXITCODE -eq 0) 'The existing Node workspace dependencies are incomplete.'
        & $node.Source -e "const fs=require('fs');const {chromium}=require('playwright');if(!fs.existsSync(chromium.executablePath()))process.exit(2)"
        Assert-True ($LASTEXITCODE -eq 0) 'The Node Playwright Chromium runtime is unavailable.'
    } finally {
        Pop-Location
    }
    & $python -c "from pathlib import Path; from playwright.sync_api import sync_playwright; p=sync_playwright().start(); e=p.chromium.executable_path; p.stop(); raise SystemExit(0 if Path(e).is_file() else 2)"
    Assert-True ($LASTEXITCODE -eq 0) 'The Python Playwright Chromium runtime is unavailable.'

    # The launcher owns these markers. Seeding them is safe only after the
    # checks above prove this exact environment is already usable; start.ps1
    # still runs from its first line and still acquires both mutexes.
    $runtimeState = Join-Path $projectRoot '.runtime-state'
    New-Item -ItemType Directory -Path $runtimeState -Force | Out-Null
    $requirementsHash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $projectRoot 'requirements.lock')).Hash
    $packageHash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $projectRoot 'package-lock.json')).Hash
    Set-Content -LiteralPath (Join-Path $runtimeState 'requirements-lock.sha256') -Value $requirementsHash -Encoding ascii
    Set-Content -LiteralPath (Join-Path $runtimeState 'package.sha256') -Value $packageHash -Encoding ascii
    $env:SITEHUB_SKIP_BUILD = '1'
}

function Invoke-StaleVenvRecovery {
    $fixtureRoot = Join-Path $runRoot 'stale venv project with spaces'
    $fixtureData = Join-Path $runRoot 'stale venv isolated data'
    $fixtureVenv = Join-Path $fixtureRoot '.venv'
    $sentinel = Join-Path $fixtureVenv 'stale-environment.sentinel'
    $fixtureStdout = Join-Path $runRoot 'stale-venv.stdout.log'
    $fixtureStderr = Join-Path $runRoot 'stale-venv.stderr.log'
    $realPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
    $realConfig = Join-Path $projectRoot '.venv\pyvenv.cfg'
    Assert-True (Test-Path -LiteralPath $realPython -PathType Leaf) 'The real project .venv is unavailable before the isolation test.'
    Assert-True (Test-Path -LiteralPath $realConfig -PathType Leaf) 'The real project pyvenv.cfg is unavailable before the isolation test.'
    $realPythonHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $realPython).Hash
    $realConfigHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $realConfig).Hash

    New-Item -ItemType Directory -Path $fixtureVenv -Force | Out-Null
    New-Item -ItemType Directory -Path $fixtureData -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $projectRoot 'start.ps1') -Destination (Join-Path $fixtureRoot 'start.ps1')
    Copy-Item -LiteralPath (Join-Path $projectRoot 'start.bat') -Destination (Join-Path $fixtureRoot 'start.bat')
    # An empty lock file makes the fixture deterministic and offline.
    # It intentionally fails later at the absent Playwright module, after the
    # stale environment has been replaced and before any Node setup is reached.
    Set-Content -LiteralPath (Join-Path $fixtureRoot 'requirements.lock') -Value '' -Encoding ascii
    Set-Content -LiteralPath $sentinel -Value 'invalid virtual environment' -Encoding ascii
    Assert-True ($fixtureRoot.Contains(' ')) 'The stale-.venv fixture path must contain spaces.'

    $previousData = Get-Item -LiteralPath Env:SITEHUB_DATA_DIR -ErrorAction SilentlyContinue
    $previousNoBrowser = Get-Item -LiteralPath Env:SITEHUB_NO_BROWSER -ErrorAction SilentlyContinue
    $previousNoPause = Get-Item -LiteralPath Env:SITEHUB_NO_PAUSE -ErrorAction SilentlyContinue
    $previousSkipBuild = Get-Item -LiteralPath Env:SITEHUB_SKIP_BUILD -ErrorAction SilentlyContinue
    try {
        $env:SITEHUB_DATA_DIR = $fixtureData
        $env:SITEHUB_NO_BROWSER = '1'
        $env:SITEHUB_NO_PAUSE = '1'
        $env:SITEHUB_SKIP_BUILD = '1'
        Write-Step 'Exercising stale .venv recovery in an independent path-with-spaces fixture.'
        # Creating a fresh venv can outlive the normal three-second identity
        # window on a cold Windows CIM provider. Let this negative-path fixture
        # finish naturally instead of killing cmd.exe while its child still
        # owns the redirected streams.
        $script:selfHealLaunch = Start-Launcher `
            (Join-Path $fixtureRoot 'start.bat') `
            $fixtureRoot `
            -AllowEarlyExitWithoutIdentity `
            -IdentityTimeoutSeconds 30
        if ($null -ne $script:selfHealLaunch.Identity) {
            $tracked[[string]$script:selfHealLaunch.Identity.Pid] = $script:selfHealLaunch.Identity
        }
        $deadline = [DateTime]::UtcNow.AddSeconds(60)
        while (-not $script:selfHealLaunch.Process.HasExited -and [DateTime]::UtcNow -lt $deadline) {
            if ($null -ne $script:selfHealLaunch.Identity) {
                try { Add-TrackedTree ([int]$script:selfHealLaunch.Identity.Pid) } catch { }
            }
            Start-Sleep -Milliseconds 100
        }
        if (-not $script:selfHealLaunch.Process.HasExited) {
            if ($null -ne $script:selfHealLaunch.Identity) {
                [void](Stop-ExactIdentity $script:selfHealLaunch.Identity)
            } else {
                # This branch is defensive: Start-Launcher only permits a null
                # identity after the process has already exited.
                try { $script:selfHealLaunch.Process.Kill() } catch { }
            }
            throw 'The stale-.venv recovery fixture did not exit within 60 seconds.'
        }
        $script:selfHealLaunch.StdoutTask.Wait(5000) | Out-Null
        $script:selfHealLaunch.StderrTask.Wait(5000) | Out-Null
        Save-LaunchLogs $script:selfHealLaunch $fixtureStdout $fixtureStderr
        Assert-True ($script:selfHealLaunch.Process.ExitCode -ne 0) 'start.bat did not preserve the expected start.ps1 failure code.'
        Assert-True (-not (Test-Path -LiteralPath $sentinel)) 'start.ps1 did not remove the stale .venv contents.'
        $fixturePython = Join-Path $fixtureVenv 'Scripts\python.exe'
        Assert-True (Test-Path -LiteralPath $fixturePython -PathType Leaf) 'start.ps1 did not recreate .venv in the fixture.'
        & $fixturePython -c 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 14) else 1)' *> $null
        Assert-True ($LASTEXITCODE -eq 0) 'The recreated fixture .venv is not a valid supported Python environment.'
        $fixtureOutput = ([string]$script:selfHealLaunch.StdoutTask.Result) + "`n" + ([string]$script:selfHealLaunch.StderrTask.Result)
        Assert-True ($fixtureOutput -match 'Creating an isolated Python environment') 'The fixture did not exercise the stale-.venv recreation branch.'
        # start.bat's interactive-failure pause consumes redirected stdin EOF
        # and can normalize the wrapper exit code to zero. Reaching the actual
        # app start and opening 8686 are therefore the authoritative checks.
        Assert-True ($fixtureOutput -notmatch '\[SiteHub\] Starting http://127\.0\.0\.1:8686') 'The intentionally incomplete fixture unexpectedly reached application startup.'
        Assert-True (@(Get-ListenerRows).Count -eq 0) 'The stale-.venv fixture unexpectedly opened port 8686.'
    } finally {
        if ($null -eq $previousData) { Remove-Item Env:SITEHUB_DATA_DIR -ErrorAction SilentlyContinue } else { $env:SITEHUB_DATA_DIR = [string]$previousData.Value }
        if ($null -eq $previousNoBrowser) { Remove-Item Env:SITEHUB_NO_BROWSER -ErrorAction SilentlyContinue } else { $env:SITEHUB_NO_BROWSER = [string]$previousNoBrowser.Value }
        if ($null -eq $previousNoPause) { Remove-Item Env:SITEHUB_NO_PAUSE -ErrorAction SilentlyContinue } else { $env:SITEHUB_NO_PAUSE = [string]$previousNoPause.Value }
        if ($null -eq $previousSkipBuild) { Remove-Item Env:SITEHUB_SKIP_BUILD -ErrorAction SilentlyContinue } else { $env:SITEHUB_SKIP_BUILD = [string]$previousSkipBuild.Value }
    }

    Assert-True ((Get-FileHash -Algorithm SHA256 -LiteralPath $realPython).Hash -ceq $realPythonHash) 'The stale-.venv fixture modified the real project Python executable.'
    Assert-True ((Get-FileHash -Algorithm SHA256 -LiteralPath $realConfig).Hash -ceq $realConfigHash) 'The stale-.venv fixture modified the real project pyvenv.cfg.'
    Write-Step 'Verified stale .venv replacement without touching the real project environment.'
}

function Get-SetupFingerprint {
    $relativeFiles = @(
        'requirements.txt',
        'requirements.lock',
        'package.json',
        'package-lock.json',
        '.runtime-state\requirements-lock.sha256',
        '.runtime-state\package.sha256',
        '.venv\pyvenv.cfg',
        'node_modules\.package-lock.json',
        'frontend\dist\index.html'
    )
    $lines = New-Object System.Collections.Generic.List[string]
    foreach ($relative in $relativeFiles) {
        $path = Join-Path $projectRoot $relative
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            $lines.Add("$relative|missing")
            continue
        }
        $item = Get-Item -LiteralPath $path
        $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash
        $lines.Add("$relative|$($item.Length)|$($item.LastWriteTimeUtc.Ticks)|$hash")
    }
    return ($lines -join "`n")
}

function Test-IsSetupPath([string]$FullPath) {
    $full = [System.IO.Path]::GetFullPath($FullPath)
    foreach ($directory in @(
        (Join-Path $projectRoot '.venv'),
        (Join-Path $projectRoot 'node_modules'),
        (Join-Path $projectRoot 'frontend\node_modules'),
        (Join-Path $projectRoot 'worker-node\node_modules'),
        (Join-Path $projectRoot 'frontend\dist'),
        (Join-Path $projectRoot '.runtime-state')
    )) {
        $prefix = [System.IO.Path]::GetFullPath($directory).TrimEnd('\') + '\'
        if ($full.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
    foreach ($file in @('package.json', 'package-lock.json', 'requirements.txt', 'requirements.lock')) {
        if ($full.Equals((Join-Path $projectRoot $file), [System.StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
    return $false
}

function Start-SetupWatcher {
    $queue = New-Object 'System.Collections.Concurrent.ConcurrentQueue[string]'
    $watcher = New-Object System.IO.FileSystemWatcher
    $watcher.Path = $projectRoot
    $watcher.Filter = '*'
    $watcher.IncludeSubdirectories = $true
    $watcher.NotifyFilter = [System.IO.NotifyFilters]'FileName, DirectoryName, LastWrite, Size, CreationTime'
    $subscriptions = New-Object System.Collections.Generic.List[object]
    foreach ($eventName in @('Changed', 'Created', 'Deleted', 'Renamed')) {
        $source = "SiteHubLauncherSmoke-$runId-$eventName"
        $subscription = Register-ObjectEvent -InputObject $watcher -EventName $eventName -SourceIdentifier $source -MessageData $queue -Action {
            [void]$event.MessageData.Enqueue([string]$event.SourceEventArgs.FullPath)
        }
        $subscriptions.Add($subscription)
    }
    $watcher.EnableRaisingEvents = $true
    $subscriptionArray = @()
    foreach ($subscription in $subscriptions) { $subscriptionArray += $subscription }
    return [pscustomobject]@{ Watcher = $watcher; Queue = $queue; Subscriptions = $subscriptionArray }
}

function Stop-SetupWatcher($State) {
    if ($null -eq $State) { return @() }
    $State.Watcher.EnableRaisingEvents = $false
    Start-Sleep -Milliseconds 300
    foreach ($subscription in $State.Subscriptions) {
        Unregister-Event -SourceIdentifier $subscription.Name -ErrorAction SilentlyContinue
        Remove-Job -Id $subscription.Id -Force -ErrorAction SilentlyContinue
    }
    $State.Watcher.Dispose()
    $changes = New-Object System.Collections.Generic.List[string]
    $path = ''
    while ($State.Queue.TryDequeue([ref]$path)) {
        if (Test-IsSetupPath $path) { $changes.Add($path) }
        $path = ''
    }
    $changes | Sort-Object -Unique
}

function Stop-TestProcesses {
    if ($null -ne $mainLaunch -and (Test-SameIdentity $mainLaunch.Identity)) {
        try { Add-TrackedTree ([int]$mainLaunch.Identity.Pid) } catch { Write-Warning $_.Exception.Message }
    }
    if ($null -ne $secondLaunch -and (Test-SameIdentity $secondLaunch.Identity)) {
        try { Add-TrackedTree ([int]$secondLaunch.Identity.Pid) } catch { Write-Warning $_.Exception.Message }
    }

    # Stop the listener (the Python control plane) first. Closing that process
    # also closes its Windows Job Object, which terminates the exact Worker /
    # Chrome / OCR subtree. Never stop a PID unless both start time and command
    # line still equal the captured identity.
    foreach ($listener in @(Get-ListenerRows)) {
        $key = [string][int]$listener.OwningProcess
        if ($tracked.ContainsKey($key)) {
            try { [void](Stop-ExactIdentity $tracked[$key]) } catch { Write-Warning $_.Exception.Message }
        }
    }
    Start-Sleep -Milliseconds 800

    $remaining = @($tracked.Values | Sort-Object StartTicks -Descending)
    foreach ($identity in $remaining) {
        if (Test-SameIdentity $identity) {
            try { [void](Stop-ExactIdentity $identity) } catch { Write-Warning $_.Exception.Message }
        }
    }
}

function Get-UniqueResidue {
    $result = New-Object System.Collections.Generic.List[object]
    foreach ($row in @(Get-ProcessRows)) {
        $commandLine = [string]$row.CommandLine
        if ([string]::IsNullOrWhiteSpace($commandLine)) { continue }
        if (
            $commandLine.IndexOf($dataRoot, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -or
            $commandLine.IndexOf($projectAlias, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
        ) {
            $result.Add($row)
        }
    }
    foreach ($item in $result) { Write-Output $item }
}

function Remove-ProjectAlias {
    if (-not (Test-Path -LiteralPath $projectAlias)) { return }
    $item = Get-Item -LiteralPath $projectAlias -Force
    Assert-True (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) "Refusing to remove a non-junction project alias: $projectAlias"
    [System.IO.Directory]::Delete($projectAlias)
    Assert-True (-not (Test-Path -LiteralPath $projectAlias)) 'The temporary project junction was not removed.'
    Assert-True (Test-Path -LiteralPath $projectRoot -PathType Container) 'The real project root was disturbed while removing its junction.'
}

try {
    Assert-True ($env:OS -eq 'Windows_NT') 'This launcher acceptance only supports Windows.'
    Assert-True (@(Get-ListenerRows).Count -eq 0) 'TCP port 8686 is already in use; refusing to disturb an existing service.'
    [void](Assert-SafeRunRoot $runRoot)
    New-Item -ItemType Directory -Path $runRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $dataRoot -Force | Out-Null
    New-Item -ItemType Junction -Path $projectAlias -Target $projectRoot | Out-Null
    $projectAliasCreated = $true
    $aliasItem = Get-Item -LiteralPath $projectAlias -Force
    Assert-True (($aliasItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) 'The path-with-spaces project junction was not created.'

    Invoke-StaleVenvRecovery

    if ($UseVerifiedSetup) {
        Write-Step 'Verifying existing dependencies and build without reinstalling them.'
        Initialize-VerifiedSetup
    }

    $env:SITEHUB_DATA_DIR = $dataRoot
    $env:SITEHUB_NO_BROWSER = '1'
    $env:SITEHUB_NO_PAUSE = '1'
    $env:PYTHONUNBUFFERED = '1'

    $startBat = Join-Path $projectAlias 'start.bat'
    Assert-True (Test-Path -LiteralPath $startBat -PathType Leaf) "start.bat is missing through the path-with-spaces alias: $startBat"
    Write-Step "Launching the real start.bat through a path containing spaces; data is isolated at $dataRoot"
    $mainLaunch = Start-Launcher $startBat $projectAlias
    $tracked[[string]$mainLaunch.Identity.Pid] = $mainLaunch.Identity
    Assert-True ($mainLaunch.Identity.CommandLine.IndexOf($projectAlias, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) 'The launcher command line did not retain the path-with-spaces alias.'
    [void](Wait-Ready $mainLaunch $StartupTimeoutSeconds)
    Add-TrackedTree ([int]$mainLaunch.Identity.Pid)

    $listeners = @(Get-ListenerRows)
    Assert-True ($listeners.Count -eq 1) "Expected exactly one listener on 8686; found $($listeners.Count)."
    Assert-True ($listeners[0].LocalAddress -eq '127.0.0.1') "Launcher exposed 8686 on $($listeners[0].LocalAddress), not 127.0.0.1."
    $listenerKey = [string][int]$listeners[0].OwningProcess
    Assert-True ($tracked.ContainsKey($listenerKey)) "Port owner $listenerKey is outside the launcher process tree."

    $workerBefore = Invoke-Json "$appUrl/api/v1/worker/status"
    Assert-True ($workerBefore.state -eq 'ready' -and [int]$workerBefore.pid -gt 0) 'The Node Worker was not ready after start.bat completed setup.'
    Add-TrackedTree ([int]$workerBefore.pid)
    $listenerPidBefore = [int]$listeners[0].OwningProcess
    $setupBefore = Get-SetupFingerprint

    Write-Step 'Launching start.bat a second time and watching setup paths for writes.'
    $watcherState = Start-SetupWatcher
    $secondStarted = [DateTime]::UtcNow
    $secondLaunch = Start-Launcher $startBat $projectAlias
    $tracked[[string]$secondLaunch.Identity.Pid] = $secondLaunch.Identity
    Assert-True ($secondLaunch.Process.WaitForExit($SecondLauncherTimeoutSeconds * 1000)) "The second launcher did not exit within $SecondLauncherTimeoutSeconds seconds."
    $secondElapsed = [DateTime]::UtcNow - $secondStarted
    $secondLaunch.StdoutTask.Wait(5000) | Out-Null
    $secondLaunch.StderrTask.Wait(5000) | Out-Null
    $setupChanges = @(Stop-SetupWatcher $watcherState)
    $watcherState = $null
    $setupAfter = Get-SetupFingerprint
    Save-LaunchLogs $secondLaunch $secondStdout $secondStderr
    $secondOutput = ([string]$secondLaunch.StdoutTask.Result) + "`n" + ([string]$secondLaunch.StderrTask.Result)
    Assert-True ($secondLaunch.Process.ExitCode -eq 0) "The duplicate launcher returned exit code $($secondLaunch.Process.ExitCode)."
    Assert-True ($secondOutput -match 'already running|already owns this data directory|another launcher is preparing') 'The duplicate launcher did not exit through the launcher mutex.'
    Assert-True ($setupBefore -ceq $setupAfter) 'A dependency/build fingerprint changed during the duplicate launcher attempt.'
    Assert-True ($setupChanges.Count -eq 0) ("The duplicate launcher wrote dependency/build paths before exiting: " + ($setupChanges -join ', '))

    $readyAfter = Invoke-Json $readyUrl
    $workerAfter = Invoke-Json "$appUrl/api/v1/worker/status"
    $listenersAfter = @(Get-ListenerRows)
    Assert-True ($readyAfter.status -eq 'ready') 'The running instance lost readiness after the duplicate launcher.'
    Assert-True ($listenersAfter.Count -eq 1 -and [int]$listenersAfter[0].OwningProcess -eq $listenerPidBefore) 'The duplicate launcher replaced or disturbed the listener process.'
    Assert-True ([string]$workerAfter.instance_id -eq [string]$workerBefore.instance_id -and [int]$workerAfter.pid -eq [int]$workerBefore.pid) 'The duplicate launcher replaced or disturbed the Node Worker.'
    Write-Step ("Duplicate launcher exited in {0:N2}s before setup writes and left the first instance intact." -f $secondElapsed.TotalSeconds)

    Write-Step 'Stopping only identities captured from this launcher process tree.'
    Add-TrackedTree ([int]$mainLaunch.Identity.Pid)
    Stop-TestProcesses
    $shutdownDeadline = [DateTime]::UtcNow.AddSeconds($ShutdownTimeoutSeconds)
    do {
        $alive = @($tracked.Values | Where-Object { Test-SameIdentity $_ })
        $portRows = @(Get-ListenerRows)
        $residue = @(Get-UniqueResidue)
        if ($alive.Count -eq 0 -and $portRows.Count -eq 0 -and $residue.Count -eq 0) { break }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $shutdownDeadline)

    $alive = @($tracked.Values | Where-Object { Test-SameIdentity $_ })
    $residue = @(Get-UniqueResidue)
    Assert-True ($alive.Count -eq 0) ("Tracked launcher/Node/Chrome/OCR identities remain: " + (($alive | ForEach-Object { "$($_.Name):$($_.Pid)" }) -join ', '))
    Assert-True (@(Get-ListenerRows).Count -eq 0) 'Port 8686 remains open after the test instance stopped.'
    Assert-True ($residue.Count -eq 0) ("Processes still reference the isolated data/project alias: " + (($residue | ForEach-Object { $_.ProcessId }) -join ', '))
    Save-LaunchLogs $mainLaunch $mainStdout $mainStderr
} catch {
    $failure = $_
    try { Show-LaunchTail $mainLaunch 'Main launcher' } catch { }
    try { Show-LaunchTail $secondLaunch 'Duplicate launcher' } catch { }
} finally {
    if ($null -ne $watcherState) {
        try { [void](Stop-SetupWatcher $watcherState) } catch { Write-Warning "Watcher cleanup warning: $($_.Exception.Message)" }
        $watcherState = $null
    }
    try { Stop-TestProcesses } catch { Write-Warning "Process cleanup warning: $($_.Exception.Message)" }

    foreach ($name in $savedEnvironment.Keys) {
        $saved = $savedEnvironment[$name]
        if ($saved.Exists) {
            Set-Item -LiteralPath "Env:$name" -Value $saved.Value
        } else {
            Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
        }
    }

    try {
        if ($projectAliasCreated -or (Test-Path -LiteralPath $projectAlias)) {
            Remove-ProjectAlias
        }
        if (Test-Path -LiteralPath $runRoot) {
            $safeRoot = Assert-SafeRunRoot $runRoot
            Remove-Item -LiteralPath $safeRoot -Recurse -Force
        }
    } catch {
        $cleanupFailure = $_
    }
}

if ($null -eq $failure) {
    try {
        Assert-True (-not (Test-Path -LiteralPath $projectAlias)) 'The path-with-spaces project junction remains after cleanup.'
        Assert-True (-not (Test-Path -LiteralPath $runRoot)) 'The isolated launcher-smoke temporary directory remains after cleanup.'
        Assert-True (@(Get-ListenerRows).Count -eq 0) 'Port 8686 was reopened before final cleanup verification.'
        Assert-True (@(Get-UniqueResidue).Count -eq 0) 'A launcher process survived final cleanup verification.'
    } catch {
        $failure = $_
    }
}
if ($null -ne $cleanupFailure -and $null -eq $failure) { $failure = $cleanupFailure }
if ($null -ne $failure) { throw $failure }

Write-Host '[SiteHub launcher smoke] PASS: stale-.venv recovery, real start.bat path-with-spaces launch, early duplicate rejection, readiness, exact process cleanup, port cleanup, and temp cleanup verified.' -ForegroundColor Green
