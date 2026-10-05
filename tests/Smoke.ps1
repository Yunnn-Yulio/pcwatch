[CmdletBinding()]
param(
    [string]$BinaryPath = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
if ([string]::IsNullOrWhiteSpace($BinaryPath)) { $BinaryPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'artifacts/bin/PCWatch.exe' }
. (Join-Path $PSScriptRoot 'TestHelpers.ps1')
Initialize-TestSandbox 'smoke' $BinaryPath
$dataDir = Join-Path $caseRoot 'data'

try {
    $monitor = Start-TestMonitor $dataDir
    Wait-TestCondition { $status = Read-TestStatus $dataDir; $null -ne $status -and $status.running } 35 'first resource sample'
    $firstStatus = Read-TestStatus $dataDir
    Assert-Test 'status-identifies-owned-process' ($firstStatus.monitor_pid -eq $monitor.Id) @{ pid = $monitor.Id; reportedPid = $firstStatus.monitor_pid }

    $duplicate = Start-TestMonitor $dataDir
    Wait-TestExit $duplicate 5
    Assert-Test 'duplicate-instance-exits-cleanly' ($duplicate.ExitCode -eq 0) @{ exitCode = $duplicate.ExitCode }
    $monitor.Refresh()
    Assert-Test 'first-instance-survives-duplicate' (-not $monitor.HasExited) @{ pid = $monitor.Id }

    $waitStarted = $clock.Elapsed.TotalSeconds
    Wait-TestCondition { $monitor.Refresh(); $monitor.HasExited -or ($clock.Elapsed.TotalSeconds - $waitStarted -ge 6) } 10 'additional resource samples'
    $stopper = Start-TestProcess -Arguments @('--stop', '--data-dir', ('"' + $dataDir + '"'))
    Wait-TestExit $stopper 5
    Wait-TestExit $monitor 12
    Assert-Test 'stop-exits-cleanly' ($stopper.ExitCode -eq 0 -and $monitor.ExitCode -eq 0) @{ stopExit = $stopper.ExitCode; monitorExit = $monitor.ExitCode }
    $status = Read-TestStatus $dataDir
    Assert-Test 'status-is-stopped' ($null -ne $status -and $status.running -eq $false) @{ running = $(if ($null -ne $status) { $status.running } else { $null }) }

    $samples = @(Read-TestCsv $dataDir)
    Assert-Test 'resource-csv-has-multiple-samples' ($samples.Count -ge 2) @{ count = $samples.Count }
    $requiredColumns = @('utc', 'cpu_pct', 'ram_total_MiB', 'ram_used_MiB', 'ram_available_MiB', 'ram_used_pct', 'monitor_pid', 'monitor_working_MiB', 'disk_read_ms', 'disk_write_ms', 'gpu_dedicated_MiB', 'gpu_shared_MiB')
    $actualColumns = @($samples[0].PSObject.Properties.Name)
    Assert-Test 'resource-csv-has-required-columns' (@($requiredColumns | Where-Object { $_ -notin $actualColumns }).Count -eq 0) @{ columns = $actualColumns }
    $numericValid = $true
    $timestampsValid = $true
    $optionalBlanks = @{}
    foreach ($sample in $samples) {
        $timestamp = [DateTime]::MinValue
        if (-not [DateTime]::TryParse($sample.utc, [ref]$timestamp)) { $timestampsValid = $false }
        foreach ($column in @('ram_total_MiB', 'ram_used_MiB', 'ram_available_MiB', 'ram_used_pct', 'monitor_working_MiB')) {
            if (-not (Test-FiniteNumber $sample.$column)) { $numericValid = $false }
        }
        # CPU's first delta and unavailable disk/GPU counters are valid empty cells.
        foreach ($column in @('cpu_pct', 'disk_queue', 'disk_read_ms', 'disk_write_ms', 'disk_read_Bps', 'disk_write_Bps', 'gpu_dedicated_MiB', 'gpu_shared_MiB', 'pages_input_per_sec', 'pages_output_per_sec')) {
            if (-not (Test-FiniteNumber $sample.$column $true)) { $numericValid = $false }
            if ([string]::IsNullOrWhiteSpace($sample.$column)) { $optionalBlanks[$column] = $true }
        }
        if ([int]$sample.monitor_pid -ne $monitor.Id) { $numericValid = $false }
    }
    Assert-Test 'resource-values-parse-and-optional-blanks-allowed' ($numericValid -and $timestampsValid) @{ optionalBlankColumns = @($optionalBlanks.Keys) }
    $last = $samples[-1]
    $ramSum = [double]::Parse($last.ram_used_MiB, [Globalization.CultureInfo]::InvariantCulture) + [double]::Parse($last.ram_available_MiB, [Globalization.CultureInfo]::InvariantCulture)
    $ramTotal = [double]::Parse($last.ram_total_MiB, [Globalization.CultureInfo]::InvariantCulture)
    Assert-Test 'physical-memory-values-consistent' ($ramTotal -gt 0 -and [Math]::Abs($ramSum - $ramTotal) -lt 0.1) @{ totalMiB = $ramTotal; usedPlusAvailableMiB = $ramSum }

    $activity = @(Read-TestJsonLines $dataDir 'activity')
    $processSnapshots = @(Read-TestJsonLines $dataDir 'processes')
    $eventsBefore = @(Read-TestJsonLines $dataDir 'events')
    Assert-Test 'activity-jsonl-has-start-baseline-stop' ((@($activity | Where-Object { $_.type -eq 'start' }).Count -eq 1) -and (@($activity | Where-Object { $_.type -eq 'baseline' }).Count -eq 1) -and (@($activity | Where-Object { $_.type -eq 'stop' }).Count -eq 1)) @{ records = $activity.Count }
    Assert-Test 'process-snapshots-parse' ($processSnapshots.Count -gt 0 -and @($processSnapshots | Where-Object { $_.type -ne 'process_snapshot' }).Count -eq 0 -and @($processSnapshots[0].processes).Count -gt 0) @{ snapshots = $processSnapshots.Count }
    foreach ($log in @('System', 'Application')) {
        $coverage = @($eventsBefore | Where-Object { $_.type -eq 'event_coverage' -and $_.log -eq $log })
        $validCoverage = $coverage.Count -gt 0
        foreach ($item in $coverage) {
            if ($item.status -notin @('available', 'unavailable', 'error', 'reset')) { $validCoverage = $false }
            if ($item.status -in @('unavailable', 'error')) {
                if ([string]::IsNullOrWhiteSpace($item.error_type) -or 'error_code' -notin @($item.PSObject.Properties.Name)) { $validCoverage = $false }
            }
        }
        Assert-Test ('event-coverage-' + $log.ToLowerInvariant()) $validCoverage @{ records = $coverage }
    }
    $cursors = Get-Content -LiteralPath (Join-Path $dataDir 'event-cursors.json') -Raw | ConvertFrom-Json
    Assert-Test 'event-cursors-persist' ($cursors.Version -eq 1 -and 'System' -in @($cursors.Logs.PSObject.Properties.Name) -and 'Application' -in @($cursors.Logs.PSObject.Properties.Name)) @{ version = $cursors.Version }

    $initialRecordCount = $eventsBefore.Count
    $initialEventKeys = @($eventsBefore | Where-Object { $_.type -eq 'event' } | ForEach-Object { $_.log + ':' + [string]$_.record_id })
    $restart = Start-TestMonitor $dataDir
    Wait-TestCondition { $status = Read-TestStatus $dataDir; $null -ne $status -and $status.running -and $status.monitor_pid -eq $restart.Id } 25 'restart sample'
    $restartStarted = $clock.Elapsed.TotalSeconds
    Wait-TestCondition { $restart.Refresh(); $restart.HasExited -or ($clock.Elapsed.TotalSeconds - $restartStarted -ge 4) } 8 'restart event poll'
    $restartStopper = Start-TestProcess -Arguments @('--stop', '--data-dir', ('"' + $dataDir + '"'))
    Wait-TestExit $restartStopper 5
    Wait-TestExit $restart 12
    $eventsAfter = @(Read-TestJsonLines $dataDir 'events')
    $newEvents = @($eventsAfter | Select-Object -Skip $initialRecordCount | Where-Object { $_.type -eq 'event' })
    $replayedKeys = @($newEvents | ForEach-Object { $_.log + ':' + [string]$_.record_id } | Where-Object { $_ -in $initialEventKeys })
    if ($initialEventKeys.Count -gt 0) {
        Assert-Test 'restart-does-not-replay-matching-events' ($replayedKeys.Count -eq 0) @{ previousMatchingEvents = $initialEventKeys.Count; newMatchingEvents = $newEvents.Count; replayedCount = $replayedKeys.Count }
    }
    else {
        Skip-Test 'restart-does-not-replay-matching-events' 'No matching current Windows events were present during this run; cursor persistence was checked, replay behavior cannot be proven here.'
    }
    $afterActivity = @(Read-TestJsonLines $dataDir 'activity')
    $finalStatus = Read-TestStatus $dataDir
    Assert-Test 'restart-finishes-cleanly-without-unclean-session-warning' ($restart.ExitCode -eq 0 -and $restartStopper.ExitCode -eq 0 -and $null -ne $finalStatus -and $finalStatus.running -eq $false -and @($afterActivity | Where-Object { $_.type -eq 'previous_monitor_session_without_stop' }).Count -eq 0) @{ running = $finalStatus.running; restartExit = $restart.ExitCode }
}
catch { $failure = $_ }
finally { Complete-TestSandbox 'Smoke' }
if ($null -ne $failure) { throw $failure }
