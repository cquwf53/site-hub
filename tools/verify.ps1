[CmdletBinding()]
param(
    [string]$PythonPath = '',
    [switch]$SkipDependencyInstall,
    [switch]$SkipSmoke,
    [ValidateRange(15, 300)]
    [int]$StartupTimeoutSeconds = 120
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$venvDir = Join-Path $projectRoot '.venv'
$venvPython = Join-Path $venvDir 'Scripts\python.exe'
$productionInputs = Join-Path $projectRoot 'requirements.txt'
$productionLock = Join-Path $projectRoot 'requirements.lock'
$developmentInputs = Join-Path $projectRoot 'requirements-dev.txt'
$developmentLock = Join-Path $projectRoot 'requirements-dev.lock'
$systemTempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd('\')
$runRoot = Join-Path $systemTempRoot ("SiteHub-verify-" + [Guid]::NewGuid().ToString('N'))
$isolatedData = Join-Path $runRoot 'data'

function Write-Step([string]$Message) {
    Write-Host "[SiteHub verify] $Message"
}

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Description
    )

    Write-Step $Description
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

function Test-SupportedPython([string]$Executable) {
    try {
        if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) { return $false }
        & $Executable -c 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 14) else 1)' *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Resolve-Executable([string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) { return $null }
    if ([System.IO.Path]::IsPathRooted($Value)) {
        if (-not (Test-Path -LiteralPath $Value -PathType Leaf)) {
            throw "Executable does not exist: $Value"
        }
        return [System.IO.Path]::GetFullPath($Value)
    }
    $command = Get-Command $Value -ErrorAction SilentlyContinue
    if ($null -eq $command) { throw "Executable was not found: $Value" }
    return $command.Source
}

function Resolve-BasePython {
    $bundled = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    $candidates = @(
        [pscustomobject]@{ File = 'py.exe'; Args = @('-3.13') },
        [pscustomobject]@{ File = 'py.exe'; Args = @('-3.12') },
        [pscustomobject]@{ File = 'py.exe'; Args = @('-3.11') },
        [pscustomobject]@{ File = 'python.exe'; Args = @() },
        [pscustomobject]@{ File = $bundled; Args = @() }
    )
    foreach ($candidate in $candidates) {
        try {
            $probeArgs = @($candidate.Args) + @(
                '-c',
                'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 14) else 1)'
            )
            & $candidate.File @probeArgs *> $null
            if ($LASTEXITCODE -eq 0) { return $candidate }
        } catch { }
    }
    return $null
}

function Resolve-VerificationPython {
    if (-not [string]::IsNullOrWhiteSpace($PythonPath)) {
        $resolved = Resolve-Executable $PythonPath
        Assert-True (Test-SupportedPython $resolved) "Python 3.11-3.13 is required: $resolved"
        return $resolved
    }
    if (Test-SupportedPython $venvPython) { return $venvPython }
    if ($SkipDependencyInstall) {
        throw 'The project .venv is unavailable or invalid. Omit -SkipDependencyInstall to recreate it, or pass -PythonPath for a prepared environment.'
    }

    $base = Resolve-BasePython
    Assert-True ($null -ne $base) 'Python 3.11-3.13 is required to create the project environment.'
    $expected = [System.IO.Path]::GetFullPath((Join-Path $projectRoot '.venv'))
    $resolvedVenv = [System.IO.Path]::GetFullPath($venvDir)
    Assert-True ($resolvedVenv.Equals($expected, [System.StringComparison]::OrdinalIgnoreCase)) "Refusing to replace unexpected environment path: $resolvedVenv"
    if (Test-Path -LiteralPath $resolvedVenv) {
        Write-Step 'Removing the invalid project .venv before recreation.'
        Remove-Item -LiteralPath $resolvedVenv -Recurse -Force
    }
    Write-Step 'Creating the project Python environment.'
    $venvArgs = @($base.Args) + @('-m', 'venv', $resolvedVenv)
    & $base.File @venvArgs
    Assert-True ($LASTEXITCODE -eq 0 -and (Test-SupportedPython $venvPython)) 'Unable to create a supported project Python environment.'
    return $venvPython
}

function Assert-LockFile([string]$Path) {
    Assert-True (Test-Path -LiteralPath $Path -PathType Leaf) "Python lock file is missing: $Path"
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $value = $line.Trim()
        if (-not $value -or $value.StartsWith('#') -or $value.StartsWith('-r ')) { continue }
        Assert-True ($value -match '^[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?==[^\s;]+(?:\s*;.*)?$') "Unpinned entry in $Path`: $value"
    }
}

function Assert-DirectInputsLocked([string]$Inputs, [string]$Lock) {
    $locked = @(Get-Content -LiteralPath $Lock -Encoding UTF8 | ForEach-Object { $_.Trim() })
    foreach ($line in Get-Content -LiteralPath $Inputs -Encoding UTF8) {
        $value = $line.Trim()
        if (-not $value -or $value.StartsWith('#') -or $value.StartsWith('-r ')) { continue }
        Assert-True ($locked -contains $value) "Direct requirement is not pinned identically in $Lock`: $value"
    }
}

function Get-Port8686Listeners {
    $netstat = Join-Path $env:WINDIR 'System32\netstat.exe'
    Assert-True (Test-Path -LiteralPath $netstat -PathType Leaf) 'Windows netstat.exe is unavailable.'
    $pids = New-Object System.Collections.Generic.List[int]
    foreach ($line in @(& $netstat -ano -p tcp)) {
        if ($line -match '^\s*TCP\s+(\S+):(\d+)\s+(\S+):(\d+)\s+(\S+)\s+(\d+)\s*$') {
            if ([int]$Matches[2] -eq 8686 -and ([int]$Matches[4] -eq 0 -or [string]$Matches[5] -match 'LISTEN')) {
                $pids.Add([int]$Matches[6])
            }
        }
    }
    return @($pids | Sort-Object -Unique)
}

function Assert-SafeRunRoot([string]$Path) {
    $full = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
    $insideTemp = $full.StartsWith($systemTempRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)
    $leaf = [System.IO.Path]::GetFileName($full)
    Assert-True ($insideTemp -and $leaf.StartsWith('SiteHub-verify-', [System.StringComparison]::Ordinal)) "Refusing temporary cleanup outside the SiteHub verify root: $full"
    return $full
}

if ($env:OS -ne 'Windows_NT') { throw 'tools\verify.ps1 only supports Windows.' }

Assert-LockFile $productionLock
Assert-LockFile $developmentLock
Assert-DirectInputsLocked $productionInputs $productionLock
Assert-DirectInputsLocked $developmentInputs $developmentLock
Assert-True ((Get-Content -LiteralPath $developmentLock -Encoding UTF8 | ForEach-Object { $_.Trim() }) -contains '-r requirements.lock') 'requirements-dev.lock must include requirements.lock.'

if (-not $SkipSmoke) {
    $listeners = @(Get-Port8686Listeners)
    if ($listeners.Count -gt 0) {
        throw "TCP port 8686 is already in use by PID(s) $($listeners -join ', '). The isolated startup smoke cannot run while SiteHub or another service is listening. Stop it, or rerun with -SkipSmoke to execute the remaining gates."
    }
}

$python = Resolve-VerificationPython
$npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
$powershell = Get-Command powershell.exe -ErrorAction SilentlyContinue
Assert-True ($null -ne $npm) 'npm.cmd is required.'
Assert-True ($null -ne $powershell) 'Windows PowerShell is required.'

$previousData = Get-Item -LiteralPath Env:SITEHUB_DATA_DIR -ErrorAction SilentlyContinue
$previousNoBrowser = Get-Item -LiteralPath Env:SITEHUB_NO_BROWSER -ErrorAction SilentlyContinue
$previousUnbuffered = Get-Item -LiteralPath Env:PYTHONUNBUFFERED -ErrorAction SilentlyContinue

try {
    $safeRunRoot = Assert-SafeRunRoot $runRoot
    New-Item -ItemType Directory -Path $isolatedData -Force | Out-Null
    Assert-True (-not ([System.IO.Path]::GetFullPath($isolatedData).Equals(
        [System.IO.Path]::GetFullPath((Join-Path $projectRoot 'data')),
        [System.StringComparison]::OrdinalIgnoreCase
    ))) 'Verification data must never use the project data directory.'
    $env:SITEHUB_DATA_DIR = $isolatedData
    $env:SITEHUB_NO_BROWSER = '1'
    $env:PYTHONUNBUFFERED = '1'

    if (-not $SkipDependencyInstall) {
        Invoke-Checked $python @(
            '-m', 'pip', 'install', '--disable-pip-version-check', '--no-deps',
            '--only-binary=:all:', '-r', $developmentLock
        ) 'Installing the locked Python development dependency set'
        Invoke-Checked $python @('-m', 'pip', 'check') 'Checking the locked Python dependency set'
        Invoke-Checked $npm.Source @('ci', '--include=dev') 'Installing locked Node workspace dependencies'
        if (-not $SkipSmoke) {
            Invoke-Checked $python @('-m', 'playwright', 'install', 'chromium') 'Installing the Python Playwright Chromium runtime'
            Invoke-Checked $npm.Source @('exec', '--workspace=worker-node', '--', 'playwright', 'install', 'chromium') 'Installing the Node Playwright Chromium runtime'
        }
    } else {
        Invoke-Checked $python @('-m', 'pip', 'check') 'Checking the prepared Python dependency set'
        Invoke-Checked $python @('-c', 'import pytest, pytest_asyncio, fastapi, playwright') 'Checking required Python test imports'
        Invoke-Checked $npm.Source @('ls', '--workspaces', '--depth=0', '--omit=optional') 'Checking prepared Node workspace dependencies'
    }

    Push-Location $projectRoot
    try {
        Invoke-Checked $python @(
            '-m', 'pytest', '-q', '-m', 'not integration and not profile',
            '--basetemp', (Join-Path $runRoot 'pytest')
        ) 'Running the Python test suite'
        Invoke-Checked $python @((Join-Path $PSScriptRoot 'generate-api-types.py'), '--check') 'Checking generated OpenAPI TypeScript contracts'
        Invoke-Checked $npm.Source @('run', 'test:worker') 'Running the Node Worker test suite'
        Invoke-Checked $npm.Source @('run', 'test:frontend') 'Running the Vue test suite'
        Invoke-Checked $npm.Source @('run', 'typecheck') 'Running Vue TypeScript checks'
        Invoke-Checked $npm.Source @('run', 'build') 'Building the production frontend'
    } finally {
        Pop-Location
    }

    if (-not $SkipSmoke) {
        Invoke-Checked $powershell.Source @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', (Join-Path $PSScriptRoot 'smoke-windows.ps1'),
            '-PythonPath', $python,
            '-SkipDependencyInstall',
            '-SkipFrontendBuild',
            '-StartupTimeoutSeconds', [string]$StartupTimeoutSeconds
        ) 'Running the isolated Windows startup and health smoke test'
    }

    Write-Host '[SiteHub verify] PASS: Python, Worker, Vue, typecheck, build, and requested smoke gates completed.' -ForegroundColor Green
} finally {
    if ($null -eq $previousData) { Remove-Item Env:SITEHUB_DATA_DIR -ErrorAction SilentlyContinue } else { $env:SITEHUB_DATA_DIR = [string]$previousData.Value }
    if ($null -eq $previousNoBrowser) { Remove-Item Env:SITEHUB_NO_BROWSER -ErrorAction SilentlyContinue } else { $env:SITEHUB_NO_BROWSER = [string]$previousNoBrowser.Value }
    if ($null -eq $previousUnbuffered) { Remove-Item Env:PYTHONUNBUFFERED -ErrorAction SilentlyContinue } else { $env:PYTHONUNBUFFERED = [string]$previousUnbuffered.Value }
    try {
        if (Test-Path -LiteralPath $runRoot) {
            $safeRunRoot = Assert-SafeRunRoot $runRoot
            Remove-Item -LiteralPath $safeRunRoot -Recurse -Force
        }
    } catch {
        Write-Warning "Temporary verification cleanup warning: $($_.Exception.Message)"
    }
}
