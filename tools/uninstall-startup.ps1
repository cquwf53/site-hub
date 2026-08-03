param([switch]$Force)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
. (Join-Path $PSScriptRoot 'startup-task-common.ps1')

$context = Get-SiteHubStartupContext
$taskName = $context.TaskName
$tasks = @(Get-SiteHubNamedStartupTasks $taskName)
$split = Split-SiteHubStartupTasks $tasks $context
if ($split.Foreign.Count -gt 0 -and -not $Force) {
    throw "A scheduled task named '$taskName' exists outside this SiteHub project. Review it first; this script will never remove it."
}
if ($split.Foreign.Count -gt 0) {
    Write-Warning "Ignoring $($split.Foreign.Count) same-name task(s) not owned by this SiteHub project."
}
if ($split.Owned.Count -eq 0) {
    Write-Host "Startup task does not exist: $taskName"
    exit 0
}
$currentRootTask = Get-SiteHubRootStartupTask $taskName $context.TaskPath
if ($null -eq $currentRootTask -or -not (Test-SiteHubStartupTaskOwned $currentRootTask $context)) {
    throw "The root '$taskName' task changed ownership during removal preparation; nothing was removed."
}
Unregister-ScheduledTask -TaskName $taskName -TaskPath $context.TaskPath -Confirm:$false
Write-Host "Removed startup task: $taskName"
