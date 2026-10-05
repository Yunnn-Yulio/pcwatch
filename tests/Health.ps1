[CmdletBinding()]
param(
    [string]$BinaryPath = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
if ([string]::IsNullOrWhiteSpace($BinaryPath)) { $BinaryPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'artifacts/bin/PCWatch.exe' }
. (Join-Path $PSScriptRoot 'TestHelpers.ps1')
Initialize-TestSandbox 'health' $BinaryPath
$dataDir = Join-Path $caseRoot 'data'
$lockedStream = $null

try {
    $monitor = Start-TestMonitor $dataDir 35
    Wait-TestCondition { $status = Read-TestStatus $dataDir; $null -ne $status -and $status.running } 35 'first health status'
    $status = Read-TestStatus $dataDir
    Assert-Test 'health-contract-present' ('logging_healthy' -in @($status.PSObject.Properties.Name) -and 'last_sample_utc' -in @($status.PSObject.Properties.Name) -and 'log_error_components' -in @($status.PSObject.Properties.Name)) @{ fields = @($status.PSObject.Properties.Name) }
    Assert-Test 'initial-logging-healthy' ($status.logging_healthy -eq $true -and -not [string]::IsNullOrWhiteSpace($status.last_sample_utc)) @{ loggingHealthy = $status.logging_healthy; lastSampleUtc = $status.last_sample_utc }
    $initialSampleUtc = $status.last_sample_utc
    $resourceFile = @(Get-ChildItem -LiteralPath (Join-Path $dataDir 'Logs') -Filter 'resources-*.csv' | Sort-Object Name)[-1]

    # Deliberately deny access to this test's CSV only; no ACL or production data changes.
    $lockedStream = [IO.File]::Open($resourceFile.FullName, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    Wait-TestCondition { $status = Read-TestStatus $dataDir; $null -ne $status -and $status.logging_healthy -eq $false -and 'resources' -in @($status.log_error_components) } 7 'visible resource-write failure in status.json'
    $failedStatus = Read-TestStatus $dataDir
    Assert-Test 'write-failure-visible-without-writing-csv' ($failedStatus.running -and $failedStatus.logging_healthy -eq $false -and 'resources' -in @($failedStatus.log_error_components)) @{ loggingHealthy = $failedStatus.logging_healthy; components = @($failedStatus.log_error_components); lastSampleUtc = $failedStatus.last_sample_utc }
    $lockedStream.Dispose()
    $lockedStream = $null

    Wait-TestCondition { $status = Read-TestStatus $dataDir; $null -ne $status -and $status.logging_healthy -eq $true -and $status.last_sample_utc -ne $initialSampleUtc } 8 'logging recovery after unlocking CSV'
    $recovered = Read-TestStatus $dataDir
    Assert-Test 'write-failure-recovers' ($recovered.logging_healthy -eq $true -and 'resources' -notin @($recovered.log_error_components)) @{ loggingHealthy = $recovered.logging_healthy; components = @($recovered.log_error_components); lastSampleUtc = $recovered.last_sample_utc }
    $samples = @(Read-TestCsv $dataDir)
    Assert-Test 'recovered-csv-remains-parseable' ($samples.Count -ge 2 -and @($samples | Where-Object { -not (Test-FiniteNumber $_.ram_total_MiB) }).Count -eq 0) @{ samples = $samples.Count }
    $stopper = Start-TestProcess -Arguments @('--stop', '--data-dir', ('"' + $dataDir + '"'))
    Wait-TestExit $stopper 5
    Wait-TestExit $monitor 12
    $finalStatus = Read-TestStatus $dataDir
    Assert-Test 'health-test-stops-cleanly' ($stopper.ExitCode -eq 0 -and $monitor.ExitCode -eq 0 -and $null -ne $finalStatus -and $finalStatus.running -eq $false) @{ exitCode = $monitor.ExitCode; running = $finalStatus.running }
}
catch { $failure = $_ }
finally {
    if ($null -ne $lockedStream) { $lockedStream.Dispose() }
    Complete-TestSandbox 'Health'
}
if ($null -ne $failure) { throw $failure }
