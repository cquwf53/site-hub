param(
    [string]$PythonPath = '',
    [switch]$SkipDependencyInstall,
    [switch]$SkipFrontendBuild,
    [ValidateRange(15, 300)]
    [int]$StartupTimeoutSeconds = 120,
    [ValidateRange(10, 180)]
    [int]$WorkerRecoveryTimeoutSeconds = 60,
    [ValidateRange(5, 120)]
    [int]$ShutdownTimeoutSeconds = 30
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$requirementsLock = Join-Path $projectRoot 'requirements.lock'
$appUrl = 'http://127.0.0.1:8686'
$readyUrl = "$appUrl/api/v1/health/ready"
$systemTempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd('\')
$runRoot = Join-Path $systemTempRoot ("SiteHub-smoke-" + [Guid]::NewGuid().ToString('N'))
$dataRoot = Join-Path $runRoot 'data'
$mainStdout = Join-Path $runRoot 'main.stdout.log'
$mainStderr = Join-Path $runRoot 'main.stderr.log'
$secondStdout = Join-Path $runRoot 'second.stdout.log'
$secondStderr = Join-Path $runRoot 'second.stderr.log'

$mainProcess = $null
$secondProcess = $null
$mainStdoutTask = $null
$mainStderrTask = $null
$secondStdoutTask = $null
$secondStderrTask = $null
$trackedPids = New-Object 'System.Collections.Generic.HashSet[int]'
$trackedIdentity = @{}
$hadDataEnv = Test-Path Env:SITEHUB_DATA_DIR
$oldDataEnv = $env:SITEHUB_DATA_DIR
$hadBrowserEnv = Test-Path Env:SITEHUB_NO_BROWSER
$oldBrowserEnv = $env:SITEHUB_NO_BROWSER
$hadUnbufferedEnv = Test-Path Env:PYTHONUNBUFFERED
$oldUnbufferedEnv = $env:PYTHONUNBUFFERED

function Write-Step([string]$Message) {
    Write-Host "[SiteHub smoke] $Message"
}

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

function Assert-SafeTempPath([string]$Path) {
    $full = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
    $prefix = $systemTempRoot + '\'
    $insideTemp = $full.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
    $leaf = [System.IO.Path]::GetFileName($full)
    if (-not $insideTemp -or -not $leaf.StartsWith('SiteHub-smoke-', [System.StringComparison]::Ordinal)) {
        throw "Refusing recursive cleanup outside the SiteHub smoke-test temp root: $full"
    }
    return $full
}

function Resolve-Executable([string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) { return $null }
    if ([System.IO.Path]::IsPathRooted($Value)) {
        if (-not (Test-Path -LiteralPath $Value -PathType Leaf)) {
            throw "Python executable does not exist: $Value"
        }
        return [System.IO.Path]::GetFullPath($Value)
    }
    $command = Get-Command $Value -ErrorAction SilentlyContinue
    if ($null -eq $command) { throw "Python executable was not found: $Value" }
    return $command.Source
}

function Test-Python([string]$Executable, [switch]$RequireDependencies) {
    try {
        $probe = 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 14) else 1)'
        if ($RequireDependencies) {
            $probe = 'import sys, fastapi, uvicorn, httpx, playwright; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 14) else 1)'
        }
        & $Executable -c $probe *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Resolve-BasePython {
    $candidates = New-Object System.Collections.Generic.List[string]
    $bundled = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    foreach ($candidate in @('python', $bundled)) {
        try {
            $resolved = Resolve-Executable $candidate
            if ($null -ne $resolved) { $candidates.Add($resolved) }
        } catch { }
    }
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -ne $launcher) {
        foreach ($selector in @('-3.13', '-3.12', '-3.11')) {
            try {
                $resolved = (& $launcher.Source $selector -c 'import sys; print(sys.executable)' 2>$null).Trim()
                if ($resolved) { $candidates.Add($resolved) }
            } catch { }
        }
    }
    foreach ($candidate in $candidates) {
        if (Test-Python $candidate) { return $candidate }
    }
    return $null
}

function Resolve-SmokePython {
    if ($PythonPath) {
        $resolved = Resolve-Executable $PythonPath
        Assert-True (Test-Python $resolved) "Python 3.11-3.13 is required: $resolved"
        return $resolved
    }

    $venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        Assert-True (Test-Python $venvPython) "The project virtual environment uses an unsupported Python: $venvPython"
        return $venvPython
    }
    if ($SkipDependencyInstall) {
        throw 'No project Python environment exists. Pass -PythonPath for an installed environment or omit -SkipDependencyInstall.'
    }

    $basePython = Resolve-BasePython
    Assert-True ($null -ne $basePython) 'Python 3.11-3.13 is required to create the project environment.'
    Write-Step 'Creating .venv for the smoke test.'
    & $basePython -m venv (Join-Path $projectRoot '.venv')
    Assert-True ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $venvPython -PathType Leaf)) 'Unable to create .venv.'
    return $venvPython
}

function Get-AllListenerRows {
    $netstat = Join-Path $env:WINDIR 'System32\netstat.exe'
    Assert-True (Test-Path -LiteralPath $netstat -PathType Leaf) 'Windows netstat.exe is unavailable.'
    $rows = New-Object System.Collections.Generic.List[object]
    foreach ($line in @(& $netstat -ano -p tcp)) {
        # The state label may be localized, so parse from the stable columns
        # and accept only the LISTENING/listen-state rows (remote port zero).
        if ($line -match '^\s*TCP\s+(\S+):(\d+)\s+(\S+):(\d+)\s+(\S+)\s+(\d+)\s*$') {
            $remotePort = [int]$Matches[4]
            $state = [string]$Matches[5]
            if ($remotePort -eq 0 -or $state -match 'LISTEN') {
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

function Get-ListenerRows([int]$Port = 8686) {
    return @(Get-AllListenerRows | Where-Object { [int]$_.LocalPort -eq $Port })
}

function Start-SiteHubProcess([string]$Executable) {
    # Start-Process in Windows PowerShell can fail when its inherited native
    # environment contains both Path and PATH. ProcessStartInfo passes the
    # native environment through without rebuilding that case-insensitive map.
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $Executable
    $startInfo.Arguments = '-m site_hub.main'
    $startInfo.WorkingDirectory = $projectRoot
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    Assert-True $process.Start() 'Failed to start the SiteHub Python process.'
    return [pscustomobject]@{
        Process = $process
        StdoutTask = $process.StandardOutput.ReadToEndAsync()
        StderrTask = $process.StandardError.ReadToEndAsync()
    }
}

function Save-ProcessLogs($Process, $StdoutTask, $StderrTask, [string]$StdoutPath, [string]$StderrPath) {
    if ($null -eq $Process -or -not $Process.HasExited) { return }
    if ($null -ne $StdoutTask -and $StdoutTask.IsCompleted) {
        Set-Content -LiteralPath $StdoutPath -Value $StdoutTask.Result -Encoding UTF8
    }
    if ($null -ne $StderrTask -and $StderrTask.IsCompleted) {
        Set-Content -LiteralPath $StderrPath -Value $StderrTask.Result -Encoding UTF8
    }
}

function Initialize-ProcessTreeApi {
    if ($null -ne ('SiteHubSmoke.NativeProcessTree' -as [type])) { return }
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;

namespace SiteHubSmoke {
    public static class NativeProcessTree {
        private const uint TH32CS_SNAPPROCESS = 0x00000002;
        private static readonly IntPtr InvalidHandle = new IntPtr(-1);

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct PROCESSENTRY32 {
            public uint dwSize;
            public uint cntUsage;
            public uint th32ProcessID;
            public IntPtr th32DefaultHeapID;
            public uint th32ModuleID;
            public uint cntThreads;
            public uint th32ParentProcessID;
            public int pcPriClassBase;
            public uint dwFlags;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)]
            public string szExeFile;
        }

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr CreateToolhelp32Snapshot(uint flags, uint processId);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool Process32FirstW(IntPtr snapshot, ref PROCESSENTRY32 entry);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool Process32NextW(IntPtr snapshot, ref PROCESSENTRY32 entry);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr handle);

        public static Dictionary<int, int> Parents() {
            IntPtr snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
            if (snapshot == InvalidHandle) {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }
            try {
                Dictionary<int, int> result = new Dictionary<int, int>();
                PROCESSENTRY32 entry = new PROCESSENTRY32();
                entry.dwSize = (uint)Marshal.SizeOf(typeof(PROCESSENTRY32));
                if (!Process32FirstW(snapshot, ref entry)) {
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                }
                do {
                    result[(int)entry.th32ProcessID] = (int)entry.th32ParentProcessID;
                    entry.dwSize = (uint)Marshal.SizeOf(typeof(PROCESSENTRY32));
                } while (Process32NextW(snapshot, ref entry));
                return result;
            } finally {
                CloseHandle(snapshot);
            }
        }
    }
}
'@
}

function Get-DescendantPids([int]$RootPid) {
    Initialize-ProcessTreeApi
    $parents = [SiteHubSmoke.NativeProcessTree]::Parents()
    $result = New-Object 'System.Collections.Generic.HashSet[int]'
    $queue = New-Object 'System.Collections.Generic.Queue[int]'
    $queue.Enqueue($RootPid)
    while ($queue.Count -gt 0) {
        $parent = $queue.Dequeue()
        foreach ($childPid in @($parents.Keys)) {
            if ([int]$parents[$childPid] -eq $parent -and $result.Add([int]$childPid)) {
                $queue.Enqueue([int]$childPid)
            }
        }
    }
    foreach ($processId in $result) { Write-Output $processId }
}

function Add-TrackedTree([int]$RootPid) {
    Add-TrackedPid $RootPid
    foreach ($pidValue in @(Get-DescendantPids $RootPid)) {
        Add-TrackedPid ([int]$pidValue)
    }
}

function Add-TrackedPid([int]$ProcessId) {
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) { return }
    try {
        $started = $process.StartTime.ToUniversalTime().Ticks
    } catch {
        return
    }
    [void]$trackedPids.Add($ProcessId)
    $trackedIdentity[[string]$ProcessId] = $started
}

function Test-TrackedProcessAlive([int]$ProcessId) {
    $expected = $trackedIdentity[[string]$ProcessId]
    if ($null -eq $expected) { return $false }
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $false }
    try {
        return $process.StartTime.ToUniversalTime().Ticks -eq [long]$expected
    } catch {
        return $false
    }
}

function Stop-TrackedProcess([int]$ProcessId) {
    if (-not (Test-TrackedProcessAlive $ProcessId)) { return $false }
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $false }
    try {
        $expected = [long]$trackedIdentity[[string]$ProcessId]
        if ($process.StartTime.ToUniversalTime().Ticks -ne $expected) { return $false }
        Stop-Process -InputObject $process -Force -ErrorAction Stop
        return $true
    } catch {
        if (-not (Test-TrackedProcessAlive $ProcessId)) { return $true }
        throw
    }
}

function Wait-TrackedProcessGone([int]$ProcessId, [int]$TimeoutSeconds) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (-not (Test-TrackedProcessAlive $ProcessId)) { return $true }
        Start-Sleep -Milliseconds 200
    }
    return -not (Test-TrackedProcessAlive $ProcessId)
}

function Invoke-Json([string]$Uri) {
    return Invoke-RestMethod -UseBasicParsing -Uri $Uri -TimeoutSec 3 -Headers @{ Host = '127.0.0.1:8686' }
}

function Wait-Ready([int]$TimeoutSeconds) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $lastError = ''
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($null -ne $mainProcess -and $mainProcess.HasExited) {
            throw "SiteHub exited before readiness (exit code $($mainProcess.ExitCode))."
        }
        try {
            $response = Invoke-Json $readyUrl
            if ($response.status -eq 'ready') { return $response }
        } catch {
            $lastError = $_.Exception.Message
        }
        Start-Sleep -Milliseconds 300
    }
    throw "SiteHub did not become ready within $TimeoutSeconds seconds. Last error: $lastError"
}

function Wait-WorkerRecovery([int]$OldPid, [string]$OldInstanceId, [int]$TimeoutSeconds) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $status = Invoke-Json "$appUrl/api/v1/worker/status"
            $newPid = [int]$status.pid
            if ($status.state -eq 'ready' -and $newPid -gt 0 -and $newPid -ne $OldPid -and $status.instance_id -ne $OldInstanceId) {
                return $status
            }
        } catch { }
        Start-Sleep -Milliseconds 300
    }
    throw "Node Worker did not recover within $TimeoutSeconds seconds."
}

function Stop-TrackedProcesses {
    if ($null -ne $mainProcess -and -not $mainProcess.HasExited) {
        try { Add-TrackedTree ([int]$mainProcess.Id) } catch { }
    }
    if ($null -ne $secondProcess -and -not $secondProcess.HasExited) {
        try { Add-TrackedTree ([int]$secondProcess.Id) } catch { }
    }
    foreach ($pidValue in @($trackedPids)) {
        try { [void](Stop-TrackedProcess ([int]$pidValue)) } catch { }
    }
}

function Show-LogTail([string]$Path, [string]$Label) {
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        Write-Warning "$Label (last 30 lines):"
        Get-Content -LiteralPath $Path -Tail 30 | ForEach-Object { Write-Warning $_ }
    }
}

try {
    Assert-True ($env:OS -eq 'Windows_NT') 'This acceptance script only supports Windows.'
    Assert-True (@(Get-ListenerRows).Count -eq 0) 'TCP port 8686 is already in use; refusing to disturb an existing service.'

    $python = Resolve-SmokePython
    if (-not $SkipDependencyInstall) {
        Write-Step 'Installing Python dependencies and browser runtime.'
        & $python -m pip install --disable-pip-version-check --no-deps '--only-binary=:all:' -r $requirementsLock
        Assert-True ($LASTEXITCODE -eq 0) 'Python dependency installation failed.'
        & $python -m pip check
        Assert-True ($LASTEXITCODE -eq 0) 'The locked Python dependency set is incomplete or inconsistent.'
        & $python -m playwright install chromium
        Assert-True ($LASTEXITCODE -eq 0) 'Python Playwright Chromium installation failed.'

        $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
        Assert-True ($null -ne $npm) 'npm.cmd is required.'
        if (Test-Path -LiteralPath (Join-Path $projectRoot 'package-lock.json')) {
            & $npm.Source ci --prefix $projectRoot
            if ($LASTEXITCODE -ne 0) {
                # A copied Windows workspace can retain stale absolute junction
                # records in package-lock.json. Restore from the workspace
                # manifests without rewriting that lock; the acceptance must
                # still prove that all declared dependencies are installable.
                Write-Step 'npm ci failed; retrying from package manifests without changing package-lock.json.'
                & $npm.Source install --package-lock=false --prefix $projectRoot
            }
        } else {
            & $npm.Source install --prefix $projectRoot
        }
        Assert-True ($LASTEXITCODE -eq 0) 'Node dependency installation failed.'
        Push-Location $projectRoot
        try {
            & $npm.Source exec '--workspace=worker-node' -- playwright install chromium
            Assert-True ($LASTEXITCODE -eq 0) 'Node Playwright Chromium installation failed.'
        } finally {
            Pop-Location
        }
    }
    Assert-True (Test-Python $python -RequireDependencies) "Required Python packages are unavailable in: $python"

    if (-not $SkipFrontendBuild) {
        $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
        Assert-True ($null -ne $npm) 'npm.cmd is required to build the frontend.'
        Push-Location $projectRoot
        try {
            Write-Step 'Building the frontend.'
            & $npm.Source run build
            Assert-True ($LASTEXITCODE -eq 0) 'Frontend build failed.'
        } finally {
            Pop-Location
        }
    }
    Assert-True (Test-Path -LiteralPath (Join-Path $projectRoot 'frontend\dist\index.html') -PathType Leaf) 'Frontend build output is missing.'

    [void](Assert-SafeTempPath $runRoot)
    New-Item -ItemType Directory -Path $dataRoot -Force | Out-Null
    $env:SITEHUB_DATA_DIR = $dataRoot
    $env:SITEHUB_NO_BROWSER = '1'
    $env:PYTHONUNBUFFERED = '1'

    Write-Step "Starting SiteHub with isolated data: $dataRoot"
    $mainLaunch = Start-SiteHubProcess $python
    $mainProcess = $mainLaunch.Process
    $mainStdoutTask = $mainLaunch.StdoutTask
    $mainStderrTask = $mainLaunch.StderrTask
    Add-TrackedPid ([int]$mainProcess.Id)
    [void](Wait-Ready $StartupTimeoutSeconds)

    Add-TrackedTree ([int]$mainProcess.Id)
    $listeners = @(Get-ListenerRows)
    Assert-True ($listeners.Count -eq 1) "Expected exactly one listener on port 8686; found $($listeners.Count)."
    Assert-True ($listeners[0].LocalAddress -eq '127.0.0.1') "Port 8686 is not restricted to 127.0.0.1: $($listeners[0].LocalAddress)"
    $treePids = @([int]$mainProcess.Id) + @(Get-DescendantPids ([int]$mainProcess.Id))
    Assert-True ($treePids -contains [int]$listeners[0].OwningProcess) "Port 8686 owner $($listeners[0].OwningProcess) is outside the launched SiteHub process tree rooted at $($mainProcess.Id)."
    $treeListeners = @(Get-AllListenerRows | Where-Object { $treePids -contains [int]$_.OwningProcess })
    Assert-True ($treeListeners.Count -eq 1) "The SiteHub process tree opened unexpected listeners: $($treeListeners.Count) total."
    Assert-True ([int]$treeListeners[0].LocalPort -eq 8686 -and $treeListeners[0].LocalAddress -eq '127.0.0.1') 'The SiteHub process tree opened a listener other than 127.0.0.1:8686.'
    Write-Step 'Verified the process tree only listens on 127.0.0.1:8686.'

    Write-Step 'Verifying a second instance is rejected.'
    $secondLaunch = Start-SiteHubProcess $python
    $secondProcess = $secondLaunch.Process
    $secondStdoutTask = $secondLaunch.StdoutTask
    $secondStderrTask = $secondLaunch.StderrTask
    Add-TrackedPid ([int]$secondProcess.Id)
    Assert-True ($secondProcess.WaitForExit(15000)) 'The second SiteHub instance was not rejected within 15 seconds.'
    Assert-True ($secondProcess.ExitCode -ne 0) 'The second SiteHub instance unexpectedly exited successfully.'
    $secondStdoutTask.Wait()
    $secondStderrTask.Wait()
    $secondLog = [string]$secondStderrTask.Result
    Save-ProcessLogs $secondProcess $secondStdoutTask $secondStderrTask $secondStdout $secondStderr
    Assert-True ($secondLog -match 'another SiteHub instance already owns this data directory') 'The second instance did not fail through the single-instance guard.'

    $node = Get-Command node.exe -ErrorAction SilentlyContinue
    Assert-True ($null -ne $node) 'node.exe is required for the browser UI smoke test.'
    $uiSmoke = Join-Path $projectRoot 'tools\ui-smoke.mjs'
    Assert-True (Test-Path -LiteralPath $uiSmoke -PathType Leaf) 'The browser UI smoke script is missing.'
    Write-Step 'Running the browser UI acceptance.'
    Push-Location $projectRoot
    try {
        & $node.Source $uiSmoke --url $appUrl
        $uiSmokeExitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    Assert-True ($uiSmokeExitCode -eq 0) 'The browser UI acceptance failed.'

    $worker = Invoke-Json "$appUrl/api/v1/worker/status"
    Assert-True ($worker.state -eq 'ready' -and [int]$worker.pid -gt 0) 'Node Worker is not ready before the recovery test.'
    $oldWorkerPid = [int]$worker.pid
    $oldWorkerInstance = [string]$worker.instance_id
    Add-TrackedTree $oldWorkerPid

    Write-Step "Killing Node Worker PID $oldWorkerPid and waiting for automatic recovery."
    Assert-True (Stop-TrackedProcess $oldWorkerPid) 'The original Node Worker identity changed before the recovery test.'
    Assert-True (Wait-TrackedProcessGone $oldWorkerPid 10) 'The original Node Worker did not exit.'

    $live = Invoke-Json "$appUrl/api/v1/health/live"
    Assert-True ($live.status -eq 'ok') 'The control-plane live endpoint failed after Worker loss.'
    $page = Invoke-WebRequest -UseBasicParsing -Uri $appUrl -TimeoutSec 3 -Headers @{ Host = '127.0.0.1:8686' }
    Assert-True ($page.StatusCode -eq 200) 'The Vue page became unavailable after Worker loss.'
    $products = Invoke-Json "$appUrl/api/v1/products?limit=1"
    Assert-True ($null -ne $products.items) 'The product API became unavailable after Worker loss.'

    $recovered = Wait-WorkerRecovery $oldWorkerPid $oldWorkerInstance $WorkerRecoveryTimeoutSeconds
    $newWorkerPid = [int]$recovered.pid
    Add-TrackedTree $newWorkerPid
    [void](Wait-Ready $WorkerRecoveryTimeoutSeconds)
    Write-Step "Node Worker recovered as PID $newWorkerPid."

    Write-Step 'Stopping the SiteHub control process and checking for residue.'
    Add-TrackedTree ([int]$mainProcess.Id)
    Assert-True (Stop-TrackedProcess ([int]$mainProcess.Id)) 'The SiteHub control process identity changed before shutdown.'
    Assert-True (Wait-TrackedProcessGone ([int]$mainProcess.Id) $ShutdownTimeoutSeconds) 'The SiteHub control process did not stop.'

    $deadline = [DateTime]::UtcNow.AddSeconds($ShutdownTimeoutSeconds)
    do {
        $alive = @($trackedPids | Where-Object { Test-TrackedProcessAlive ([int]$_) })
        $portRows = @(Get-ListenerRows)
        if ($alive.Count -eq 0 -and $portRows.Count -eq 0) { break }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)

    $alive = @($trackedPids | Where-Object { Test-TrackedProcessAlive ([int]$_) })
    Assert-True ($alive.Count -eq 0) "Tracked Node/Chrome/OCR descendants remain alive: $($alive -join ', ')"
    Assert-True (@(Get-ListenerRows).Count -eq 0) 'Port 8686 remains open after SiteHub stopped.'

    Write-Host '[SiteHub smoke] PASS: listener, instance guard, browser UI, Worker recovery, API continuity, and process cleanup verified.' -ForegroundColor Green
} catch {
    Save-ProcessLogs $mainProcess $mainStdoutTask $mainStderrTask $mainStdout $mainStderr
    Save-ProcessLogs $secondProcess $secondStdoutTask $secondStderrTask $secondStdout $secondStderr
    Show-LogTail $mainStderr 'SiteHub stderr'
    Show-LogTail $secondStderr 'Second-instance stderr'
    throw
} finally {
    try { Stop-TrackedProcesses } catch { Write-Warning "Process cleanup warning: $($_.Exception.Message)" }

    if ($hadDataEnv) { $env:SITEHUB_DATA_DIR = $oldDataEnv } else { Remove-Item Env:SITEHUB_DATA_DIR -ErrorAction SilentlyContinue }
    if ($hadBrowserEnv) { $env:SITEHUB_NO_BROWSER = $oldBrowserEnv } else { Remove-Item Env:SITEHUB_NO_BROWSER -ErrorAction SilentlyContinue }
    if ($hadUnbufferedEnv) { $env:PYTHONUNBUFFERED = $oldUnbufferedEnv } else { Remove-Item Env:PYTHONUNBUFFERED -ErrorAction SilentlyContinue }

    try {
        if (Test-Path -LiteralPath $runRoot) {
            $safeRunRoot = Assert-SafeTempPath $runRoot
            Remove-Item -LiteralPath $safeRunRoot -Recurse -Force
        }
    } catch {
        Write-Warning "Temporary directory cleanup warning: $($_.Exception.Message)"
    }
}
