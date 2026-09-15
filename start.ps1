$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$projectRoot = $PSScriptRoot
$venvDir = Join-Path $projectRoot '.venv'
$venvPython = Join-Path $venvDir 'Scripts\python.exe'
$requirementsLock = Join-Path $projectRoot 'requirements.lock'
$runtimeState = Join-Path $projectRoot '.runtime-state'
$frontendIndex = Join-Path $projectRoot 'frontend\dist\index.html'
$appUrl = 'http://127.0.0.1:8686'

function Write-Step([string]$message) {
    Write-Host "[SiteHub] $message"
}

function Start-ReadyBrowserHelper([string]$helperPath, [string]$url) {
    # Windows PowerShell's Start-Process can rebuild the inherited environment
    # as a case-insensitive dictionary and fail when a parent supplied both
    # Path and PATH. ProcessStartInfo preserves that native environment. Quote
    # the script path explicitly so installations below a directory with spaces
    # work as well.
    $powershell = (Get-Command powershell.exe -ErrorAction Stop).Source
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $powershell
    $startInfo.Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$helperPath`" -Url `"$url`" -TimeoutSeconds 120"
    $startInfo.WorkingDirectory = $projectRoot
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        throw 'Unable to start the browser readiness helper.'
    }
    $process.Dispose()
}

function Get-SiteHubMutexKey([string]$dataPath) {
    if (-not [System.IO.Path]::IsPathRooted($dataPath)) {
        $dataPath = Join-Path $projectRoot $dataPath
    }
    $canonical = [System.IO.Path]::GetFullPath($dataPath).TrimEnd('\').ToLowerInvariant()
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($canonical)
        $digest = $sha.ComputeHash($bytes)
        return ([System.BitConverter]::ToString($digest).Replace('-', '').ToLowerInvariant()).Substring(0, 24)
    } finally {
        $sha.Dispose()
    }
}

function Test-Python([string]$pythonPath) {
    try {
        if ($pythonPath -match '[\\/]' -and -not (Test-Path -LiteralPath $pythonPath)) {
            return $false
        }
        & $pythonPath -c "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 14) else 1)" *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Resolve-Python {
    $bundled = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    $candidates = @(
        @{ File = 'py'; Args = @('-3.13') },
        @{ File = 'py'; Args = @('-3.12') },
        @{ File = 'py'; Args = @('-3.11') },
        @{ File = 'python'; Args = @() },
        @{ File = $bundled; Args = @() }
    )

    foreach ($candidate in $candidates) {
        try {
            $probeArgs = @($candidate.Args) + @('-c', "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 14) else 1)")
            & $candidate.File @probeArgs *> $null
            if ($LASTEXITCODE -eq 0) {
                return $candidate
            }
        } catch { }
    }
    return $null
}

$configuredData = if ([string]::IsNullOrWhiteSpace($env:SITEHUB_DATA_DIR)) {
    Join-Path $projectRoot 'data'
} else {
    $env:SITEHUB_DATA_DIR
}
$mutexKey = Get-SiteHubMutexKey $configuredData
$bootstrapCreated = $false
$bootstrapMutex = New-Object System.Threading.Mutex($true, "Local\SiteHub-Bootstrap-$mutexKey", [ref]$bootstrapCreated)
if (-not $bootstrapCreated) {
    $bootstrapMutex.Dispose()
    Write-Step 'SiteHub is already running or another launcher is preparing it.'
    exit 0
}

# Probe the application mutex before any pip/npm/build write. Keep ownership
# through dependency preparation so a direct Python launch cannot overlap it.
$instanceCreated = $false
$instanceProbe = New-Object System.Threading.Mutex($true, "Local\SiteHub-$mutexKey", [ref]$instanceCreated)
if (-not $instanceCreated) {
    $instanceProbe.Dispose()
    $bootstrapMutex.ReleaseMutex()
    $bootstrapMutex.Dispose()
    Write-Step 'SiteHub already owns this data directory.'
    exit 0
}

New-Item -ItemType Directory -Force -Path $runtimeState | Out-Null

$venvCreated = $false
if (-not (Test-Python $venvPython)) {
    Write-Step 'Creating an isolated Python environment.'
    $python = Resolve-Python
    if ($null -eq $python) {
        throw 'Python 3.11-3.13 is required. Install Python 3.12 and run start.bat again.'
    }
    if (Test-Path -LiteralPath $venvDir) {
        $trimSeparators = [char[]]@(
            [System.IO.Path]::DirectorySeparatorChar,
            [System.IO.Path]::AltDirectorySeparatorChar
        )
        $resolvedRoot = [System.IO.Path]::GetFullPath($projectRoot).TrimEnd($trimSeparators)
        $expectedVenv = [System.IO.Path]::GetFullPath((Join-Path $resolvedRoot '.venv'))
        $resolvedVenv = [System.IO.Path]::GetFullPath($venvDir)
        if (-not $resolvedVenv.Equals($expectedVenv, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to replace unexpected environment path: $resolvedVenv"
        }
        Remove-Item -Recurse -Force -LiteralPath $resolvedVenv
    }
    $venvArgs = @($python.Args) + @('-m', 'venv', $venvDir)
    & $python.File @venvArgs
    if ($LASTEXITCODE -ne 0 -or -not (Test-Python $venvPython)) {
        throw 'Unable to create the Python virtual environment.'
    }
    $venvCreated = $true
}

$requirementsHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $requirementsLock).Hash
$requirementsMarker = Join-Path $runtimeState 'requirements-lock.sha256'
$installedHash = if (Test-Path -LiteralPath $requirementsMarker) {
    (Get-Content -LiteralPath $requirementsMarker -Raw).Trim()
} else { '' }

if (-not $venvCreated -and $requirementsHash -eq $installedHash) {
    & $venvPython -m pip check *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Step 'The Python environment is incomplete; reinstalling the locked dependency set.'
        $installedHash = ''
    }
}

if ($venvCreated -or $requirementsHash -ne $installedHash) {
    Write-Step 'Installing Python dependencies.'
    & $venvPython -m pip install --disable-pip-version-check --no-deps '--only-binary=:all:' -r $requirementsLock
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
    & $venvPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'The locked Python dependency set is incomplete or inconsistent.' }
    Set-Content -LiteralPath $requirementsMarker -Value $requirementsHash -Encoding ascii
}
& $venvPython -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw 'Python Playwright Chromium installation failed.' }

$node = Get-Command node -ErrorAction SilentlyContinue
$npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
if ($null -eq $node -or $null -eq $npm) {
    throw 'Node.js 20 or newer is required.'
}
$nodeVersion = (& $node.Source --version).Trim()
if ($nodeVersion -notmatch '^v?(\d+)\.(\d+)\.(\d+)') {
    throw "Unable to parse the Node.js version: $nodeVersion"
}
$nodeMajor = [int]$Matches[1]
$nodeMinor = [int]$Matches[2]
$nodeSupported = (
    ($nodeMajor -eq 20 -and $nodeMinor -ge 19) -or
    ($nodeMajor -eq 22 -and $nodeMinor -ge 12) -or
    $nodeMajor -gt 22
)
if (-not $nodeSupported) {
    throw "Node.js 20.19+ or 22.12+ is required; found $nodeVersion."
}

$lockFile = Join-Path $projectRoot 'package-lock.json'
$packageFile = Join-Path $projectRoot 'package.json'
$nodeMarker = Join-Path $runtimeState 'package.sha256'
$nodeInput = if (Test-Path -LiteralPath $lockFile) { $lockFile } else { $packageFile }
$nodeHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $nodeInput).Hash
$installedNodeHash = if (Test-Path -LiteralPath $nodeMarker) {
    (Get-Content -LiteralPath $nodeMarker -Raw).Trim()
} else { '' }

if ($nodeHash -ne $installedNodeHash -or -not (Test-Path -LiteralPath (Join-Path $projectRoot 'node_modules'))) {
    Write-Step 'Installing Node dependencies.'
    if (Test-Path -LiteralPath $lockFile) {
        & $npm.Source ci '--include=dev'
    } else {
        & $npm.Source install '--include=dev'
    }
    if ($LASTEXITCODE -ne 0) { throw 'Node dependency installation failed.' }
    $nodeInput = if (Test-Path -LiteralPath $lockFile) { $lockFile } else { $packageFile }
    $nodeHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $nodeInput).Hash
    Set-Content -LiteralPath $nodeMarker -Value $nodeHash -Encoding ascii
}
& $npm.Source exec '--workspace=worker-node' -- playwright install chromium
if ($LASTEXITCODE -ne 0) { throw 'Node Playwright Chromium installation failed.' }

if ($env:SITEHUB_SKIP_BUILD -ne '1') {
    Write-Step 'Building the Vue application.'
    & $npm.Source run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
}
if (-not (Test-Path -LiteralPath $frontendIndex)) {
    throw 'Frontend build is missing: frontend\dist\index.html'
}

Write-Step "Starting $appUrl"
if ($env:SITEHUB_NO_BROWSER -ne '1') {
    $readyHelper = Join-Path $projectRoot 'tools\open-when-ready.ps1'
    Start-ReadyBrowserHelper $readyHelper $appUrl
}
$instanceProbe.ReleaseMutex()
$instanceProbe.Dispose()
& $venvPython -m site_hub.main
$exitCode = $LASTEXITCODE
$bootstrapMutex.ReleaseMutex()
$bootstrapMutex.Dispose()
exit $exitCode
