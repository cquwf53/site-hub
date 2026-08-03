param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^http://127\.0\.0\.1:8686$')]
    [string]$Url,
    [ValidateRange(5, 300)]
    [int]$TimeoutSeconds = 120
)

$ErrorActionPreference = 'SilentlyContinue'
$deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
$readyUrl = "$Url/api/v1/health/ready"
while ([DateTime]::UtcNow -lt $deadline) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $readyUrl -TimeoutSec 2
        if ($response.StatusCode -eq 200) {
            Start-Process $Url
            exit 0
        }
    } catch { }
    Start-Sleep -Milliseconds 500
}
exit 1
