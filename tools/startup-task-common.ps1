Set-StrictMode -Version 2.0

function ConvertTo-SiteHubNormalizedPath {
    param([AllowNull()][string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) { return $null }
    try {
        $expanded = [Environment]::ExpandEnvironmentVariables($Path)
        if (-not [System.IO.Path]::IsPathRooted($expanded)) { return $null }
        return [System.IO.Path]::GetFullPath($expanded).TrimEnd('\')
    } catch {
        return $null
    }
}

function Test-SiteHubPathEqual {
    param(
        [AllowNull()][string]$Left,
        [AllowNull()][string]$Right
    )

    $normalizedLeft = ConvertTo-SiteHubNormalizedPath $Left
    $normalizedRight = ConvertTo-SiteHubNormalizedPath $Right
    return $null -ne $normalizedLeft -and
        $null -ne $normalizedRight -and
        $normalizedLeft.Equals($normalizedRight, [System.StringComparison]::OrdinalIgnoreCase)
}

function ConvertTo-SiteHubSid {
    param([AllowNull()][string]$Identity)

    if ([string]::IsNullOrWhiteSpace($Identity)) { return $null }
    try {
        if ($Identity -match '^S-\d(?:-\d+)+$') {
            return ([System.Security.Principal.SecurityIdentifier]$Identity).Value
        }
        $account = [System.Security.Principal.NTAccount]$Identity
        return $account.Translate([System.Security.Principal.SecurityIdentifier]).Value
    } catch {
        return $null
    }
}

function Get-SiteHubStartupContext {
    $projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\')
    $launcher = Join-Path $PSScriptRoot 'start-hidden.vbs'
    $wscript = Join-Path $env:WINDIR 'System32\wscript.exe'
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    return [pscustomobject]@{
        ProjectRoot = $projectRoot
        Launcher = [System.IO.Path]::GetFullPath($launcher)
        Wscript = [System.IO.Path]::GetFullPath($wscript)
        CurrentUser = $identity.Name
        CurrentUserSid = $identity.User.Value
        TaskName = 'SiteHub'
        TaskPath = '\'
    }
}

function Get-SiteHubNamedStartupTasks {
    param([string]$TaskName = 'SiteHub')

    try {
        return @(Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop)
    } catch {
        if ([string]$_.CategoryInfo.Category -eq 'ObjectNotFound') { return @() }
        throw "Unable to audit scheduled tasks named '$TaskName': $($_.Exception.Message)"
    }
}

function Get-SiteHubRootStartupTask {
    param(
        [string]$TaskName = 'SiteHub',
        [string]$TaskPath = '\'
    )

    try {
        $tasks = @(Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction Stop)
    } catch {
        if ([string]$_.CategoryInfo.Category -eq 'ObjectNotFound') { return $null }
        throw "Unable to audit scheduled task '$TaskPath$TaskName': $($_.Exception.Message)"
    }
    if ($tasks.Count -gt 1) {
        throw "More than one scheduled task exists at '$TaskPath$TaskName'."
    }
    if ($tasks.Count -eq 1) { return $tasks[0] }
    return $null
}

function Test-SiteHubStartupTaskOwned {
    param(
        [Parameter(Mandatory = $true)]$Task,
        [Parameter(Mandatory = $true)]$Context
    )

    if ([string]$Task.TaskPath -ne $Context.TaskPath) { return $false }
    $actions = @($Task.Actions)
    if ($actions.Count -ne 1) { return $false }
    $action = $actions[0]
    if (-not (Test-SiteHubPathEqual ([string]$action.Execute) $Context.Wscript)) { return $false }
    if ([string]$action.Arguments -cne "`"$($Context.Launcher)`"") { return $false }
    if (-not [string]::IsNullOrWhiteSpace([string]$action.WorkingDirectory)) { return $false }

    $principalSid = ConvertTo-SiteHubSid ([string]$Task.Principal.UserId)
    if ($null -eq $principalSid -or $principalSid -ne $Context.CurrentUserSid) { return $false }
    return $true
}

function Split-SiteHubStartupTasks {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$Tasks,
        [Parameter(Mandatory = $true)]$Context
    )

    $owned = @($Tasks | Where-Object { Test-SiteHubStartupTaskOwned $_ $Context })
    $foreign = @($Tasks | Where-Object { -not (Test-SiteHubStartupTaskOwned $_ $Context) })
    if ($owned.Count -gt 1) {
        throw "More than one owned SiteHub startup task was found; refusing an ambiguous operation."
    }
    return [pscustomobject]@{ Owned = $owned; Foreign = $foreign }
}
