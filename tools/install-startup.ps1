param([switch]$Force)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
. (Join-Path $PSScriptRoot 'startup-task-common.ps1')

$context = Get-SiteHubStartupContext
$taskName = $context.TaskName
$expectedLauncherArgument = "`"$($context.Launcher)`""

$tasks = @(Get-SiteHubNamedStartupTasks $taskName)
$split = Split-SiteHubStartupTasks $tasks $context
if ($split.Foreign.Count -gt 0 -and -not $Force) {
    throw "A scheduled task named '$taskName' exists outside this SiteHub project. Review it first; -Force only permits replacing the root task and never modifies same-name tasks in other folders."
}
if ($split.Foreign.Count -gt 0) {
    Write-Warning "Ignoring $($split.Foreign.Count) same-name task(s) outside this project; only a root task can be replaced."
}

$action = New-ScheduledTaskAction -Execute $context.Wscript -Argument $expectedLauncherArgument
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $context.CurrentUser
$principal = New-ScheduledTaskPrincipal -UserId $context.CurrentUser -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -Hidden

# Close the check/use window as far as Task Scheduler's API permits. Without
# explicit -Force, a root task that changed ownership during preparation must
# never be overwritten by the unconditional Register-ScheduledTask -Force call.
$currentRootTask = Get-SiteHubRootStartupTask $taskName $context.TaskPath
if ($null -ne $currentRootTask -and -not $Force -and -not (Test-SiteHubStartupTaskOwned $currentRootTask $context)) {
    throw "The root '$taskName' task changed ownership during registration preparation; nothing was replaced."
}

Register-ScheduledTask `
    -TaskName $taskName `
    -TaskPath $context.TaskPath `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description 'Start SiteHub after Windows logon.' `
    -Force | Out-Null

$registered = Get-SiteHubRootStartupTask $taskName $context.TaskPath
if ($null -eq $registered) { throw 'The SiteHub startup task was not present after registration.' }
if (-not (Test-SiteHubStartupTaskOwned $registered $context)) {
    throw 'The registered task failed the SiteHub ownership verification.'
}
if ([bool]$registered.Settings.DisallowStartIfOnBatteries -or [bool]$registered.Settings.StopIfGoingOnBatteries) {
    throw 'The registered task has unsafe battery settings; expected start and continued operation on battery power.'
}
if ([string]$registered.Settings.MultipleInstances -ne 'IgnoreNew') {
    throw 'The registered task must reject overlapping instances.'
}

Write-Host "Registered startup task: $taskName"
