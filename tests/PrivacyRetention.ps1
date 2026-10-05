[CmdletBinding()]
param(
    [string]$BinaryPath = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$repoRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($BinaryPath)) { $BinaryPath = Join-Path $repoRoot 'artifacts/bin/PCWatch.exe' }
$binary = (Resolve-Path -LiteralPath $BinaryPath).Path
$caseRoot = Join-Path $repoRoot ('artifacts/tests/privacy-retention-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $caseRoot -Force | Out-Null
$results = New-Object 'System.Collections.Generic.List[object]'
$failure = $null

function Assert-Fixture([string]$Name, [bool]$Passed, [object]$Details) {
    $results.Add([pscustomobject]@{ test = $Name; status = $(if ($Passed) { 'passed' } else { 'failed' }); details = $Details })
    if (-not $Passed) { throw ('Fixture failed: ' + $Name) }
}

function Skip-Fixture([string]$Name, [string]$Reason) {
    $results.Add([pscustomobject]@{ test = $Name; status = 'skipped'; details = @{ reason = $Reason } })
}

function Write-Fixture([string]$Path, [int]$Bytes, [datetime]$LastWrite) {
    [IO.File]::WriteAllBytes($Path, (New-Object byte[] $Bytes))
    [IO.File]::SetLastWriteTimeUtc($Path, $LastWrite)
}

function Run-Cleanup([string]$DataDir, [string]$Name, [int]$ExpectedExit = 0) {
    $process = Start-Process -FilePath $binary -ArgumentList @('--cleanup-only', '--data-dir', ('"' + $DataDir + '"'), '--max-log-mb', '1', '--retention-days', '14') -WorkingDirectory $caseRoot -WindowStyle Hidden -PassThru
    try {
        if (-not $process.WaitForExit(70000)) {
            # Only terminate the process started by this test, never a process-name match.
            $process.Kill()
            $process.WaitForExit()
            throw 'Cleanup process exceeded the 70-second test limit.'
        }
        Assert-Fixture ($Name + '-cleanup-process-exit') ($process.ExitCode -eq $ExpectedExit) @{ exitCode = $process.ExitCode; expectedExit = $ExpectedExit }
    }
    finally { $process.Dispose() }
}

try {
    $ageRoot = Join-Path $caseRoot 'retention-age'
    $ageLogs = Join-Path $ageRoot 'Logs'
    New-Item -ItemType Directory -Path $ageLogs -Force | Out-Null
    $oldDate = [DateTime]::UtcNow.AddDays(-20)
    $nowDate = [DateTime]::UtcNow
    $oldOwn = Join-Path $ageLogs 'resources-20260101-001.csv'
    $recentOwn = Join-Path $ageLogs 'events-20260102-001.jsonl'
    $other = Join-Path $ageLogs 'unrelated-fixture.txt'
    $backup = Join-Path $ageLogs 'resources-20260101-001.csv.bak'
    $outsideDir = Join-Path $caseRoot 'outside'
    New-Item -ItemType Directory -Path $outsideDir | Out-Null
    $outside = Join-Path $outsideDir 'resources-20260101-001.csv'
    Write-Fixture $oldOwn 128 $oldDate
    Write-Fixture $recentOwn 128 $nowDate
    Write-Fixture $other 128 $oldDate
    Write-Fixture $backup 128 $oldDate
    Write-Fixture $outside 128 $oldDate
    Run-Cleanup $ageRoot 'age'
    Assert-Fixture 'age-removes-old-own' (-not (Test-Path -LiteralPath $oldOwn)) @{ expected = 'removed' }
    Assert-Fixture 'age-keeps-recent-own' (Test-Path -LiteralPath $recentOwn) @{ expected = 'preserved' }
    Assert-Fixture 'age-preserves-other-backup-and-outside' ((Test-Path -LiteralPath $other) -and (Test-Path -LiteralPath $backup) -and (Test-Path -LiteralPath $outside)) @{ expected = 'all three preserved' }

    $capRoot = Join-Path $caseRoot 'retention-cap'
    $capLogs = Join-Path $capRoot 'Logs'
    New-Item -ItemType Directory -Path $capLogs -Force | Out-Null
    $capOld = Join-Path $capLogs 'events-20260102-001.jsonl'
    $capMiddle = Join-Path $capLogs 'resources-20260102-001.csv'
    $capNew = Join-Path $capLogs 'activity-20260102-001.jsonl'
    $capOther = Join-Path $capLogs 'unrelated-archive.bin'
    Write-Fixture $capOld (800 * 1024) ([datetime]::UtcNow.AddHours(-3))
    Write-Fixture $capMiddle (800 * 1024) ([datetime]::UtcNow.AddHours(-2))
    Write-Fixture $capNew (800 * 1024) ([datetime]::UtcNow.AddHours(-1))
    Write-Fixture $capOther (800 * 1024) ([datetime]::UtcNow.AddHours(-4))
    Run-Cleanup $capRoot 'cap'
    Assert-Fixture 'cap-removes-oldest-own-first' ((-not (Test-Path -LiteralPath $capOld)) -and (-not (Test-Path -LiteralPath $capMiddle)) -and (Test-Path -LiteralPath $capNew)) @{ survivingOwnBytes = $(if (Test-Path -LiteralPath $capNew) { (Get-Item -LiteralPath $capNew).Length } else { 0 }); limitBytes = 1MB }
    Assert-Fixture 'cap-keeps-other-files' (Test-Path -LiteralPath $capOther) @{ expected = 'preserved' }

    $junctionRoot = Join-Path $caseRoot 'retention-junction'
    New-Item -ItemType Directory -Path $junctionRoot | Out-Null
    $junctionCreated = $false
    try {
        New-Item -ItemType Junction -Path (Join-Path $junctionRoot 'Logs') -Target $outsideDir -ErrorAction Stop | Out-Null
        $junctionCreated = $true
    }
    catch {
        # Some CI file systems cannot create junctions. This is a skip, not a pass.
        $reason = 'Junction creation is not supported here: ' + $_.Exception.GetType().Name
        Skip-Fixture 'junction-cleanup-process-exit' $reason
        Skip-Fixture 'junction-rejected-and-target-preserved' $reason
    }
    if ($junctionCreated) {
        Run-Cleanup $junctionRoot 'junction' 1
        $junctionError = Get-Content -LiteralPath (Join-Path $junctionRoot 'last-error.json') -Raw | ConvertFrom-Json
        Assert-Fixture 'junction-rejected-and-target-preserved' (($junctionError.error_type -eq 'InvalidDataException') -and (Test-Path -LiteralPath $outside)) @{ errorType = $junctionError.error_type; outsideFilePreserved = (Test-Path -LiteralPath $outside) }
    }

    $assembly = [Reflection.Assembly]::LoadFrom($binary)
    $collector = $assembly.GetType('PCWatch.EventCollector', $true)
    $flags = [Reflection.BindingFlags]::NonPublic -bor [Reflection.BindingFlags]::Static
    $addField = $collector.GetMethod('AddAllowedField', $flags)
    $summary = $collector.GetMethod('AddCperSummary', $flags)
    $privacy = New-Object 'System.Collections.Generic.Dictionary[string,object]'
    # Every value in this block is synthetic. These checks test filtering behavior.
    $fieldCases = @(
        @('RawData', 'synthetic-private-raw-payload'),
        @('DeviceSerialNumber', '1122334455667788'),
        @('CommandLine', 'C:\Users\example\secret.exe --token=synthetic-fixture-secret'),
        @('AppPath', 'C:\Users\example\private-file.exe'),
        @('UserName', 'example'),
        @('AppName', 'C:\Users\example\private-folder\demo.exe'),
        @('ModuleName', 'C:\Users\example\private-folder\graphics.dll'),
        @('ProcessId', '0xAB'),
        @('ErrorType', 'synthetic-nonnumeric-value'),
        @('EventName', 'LiveKernelEvent')
    )
    foreach ($fieldCase in $fieldCases) { [void]$addField.Invoke($null, [object[]]@($privacy.PSObject.BaseObject, [string]$fieldCase[0], [string]$fieldCase[1])) }
    Assert-Fixture 'privacy-denies-sensitive-fields' ((-not $privacy.ContainsKey('RawData')) -and (-not $privacy.ContainsKey('DeviceSerialNumber')) -and (-not $privacy.ContainsKey('CommandLine')) -and (-not $privacy.ContainsKey('AppPath')) -and (-not $privacy.ContainsKey('UserName')) -and (-not $privacy.ContainsKey('ErrorType'))) @{ capturedFieldNames = @($privacy.Keys) }
    Assert-Fixture 'privacy-basename-only' (($privacy['AppName'] -eq 'demo.exe') -and ($privacy['ModuleName'] -eq 'graphics.dll')) @{ AppName = $privacy['AppName']; ModuleName = $privacy['ModuleName'] }
    Assert-Fixture 'privacy-keeps-useful-technical-fields' (($privacy['ProcessId'] -eq '0xAB') -and ($privacy['EventName'] -eq 'LiveKernelEvent')) @{ ProcessId = $privacy['ProcessId']; EventName = $privacy['EventName'] }

    $cper = New-Object byte[] 400
    [Array]::Copy([Text.Encoding]::ASCII.GetBytes('CPER'), 0, $cper, 0, 4)
    [Array]::Copy([BitConverter]::GetBytes([uint16]1), 0, $cper, 10, 2)
    [Array]::Copy([BitConverter]::GetBytes([uint32]1), 0, $cper, 12, 4)
    [Array]::Copy([BitConverter]::GetBytes([uint32]66), 0, $cper, 104, 4)
    $notifyGuid = [Guid]'00000000-0000-0000-0000-000000000001'
    $sectionGuid = [Guid]'00000000-0000-0000-0000-000000000002'
    [Array]::Copy($notifyGuid.ToByteArray(), 0, $cper, 80, 16)
    [Array]::Copy($sectionGuid.ToByteArray(), 0, $cper, 144, 16)
    $payload = [Text.Encoding]::ASCII.GetBytes('SYNTHETIC-CPER-PAYLOAD-DO-NOT-LOG')
    [Array]::Copy($payload, 0, $cper, 220, $payload.Length)
    for ($byteIndex = 96; $byteIndex -lt 104; $byteIndex++) { $cper[$byteIndex] = 255 }
    $rawCper = [BitConverter]::ToString($cper).Replace('-', '')
    $cperSummary = New-Object 'System.Collections.Generic.Dictionary[string,object]'
    [void]$summary.Invoke($null, [object[]]@($cperSummary.PSObject.BaseObject, $rawCper))
    $summaryJson = $cperSummary | ConvertTo-Json -Depth 6 -Compress
    $allowedSummary = @('cper_severity', 'cper_flags', 'cper_previous_error', 'cper_sections_truncated', 'cper_section_count', 'cper_notification_type', 'cper_section_types')
    $unexpected = @($cperSummary.Keys | Where-Object { $_ -notin $allowedSummary })
    Assert-Fixture 'cper-header-summary-correct' (($cperSummary['cper_severity'] -eq 1) -and ($cperSummary['cper_flags'] -eq 66) -and $cperSummary['cper_previous_error'] -and $cperSummary['cper_sections_truncated'] -and ($cperSummary['cper_section_count'] -eq 1) -and ($cperSummary['cper_notification_type'] -eq $notifyGuid.ToString()) -and ($cperSummary['cper_section_types'][0] -eq $sectionGuid.ToString())) @{ summary = $cperSummary }
    Assert-Fixture 'cper-no-payload-or-record-id' (($unexpected.Count -eq 0) -and (-not $summaryJson.Contains('CPER-PAYLOAD')) -and (-not $summaryJson.Contains($rawCper)) -and (-not $summaryJson.Contains('FFFFFFFFFFFFFFFF'))) @{ capturedFieldNames = @($cperSummary.Keys) }
    $emptySummary = New-Object 'System.Collections.Generic.Dictionary[string,object]'
    [void]$summary.Invoke($null, [object[]]@($emptySummary.PSObject.BaseObject, 'synthetic-invalid-data'))
    Assert-Fixture 'cper-invalid-short-value-ignored' ($emptySummary.Count -eq 0) @{ capturedCount = $emptySummary.Count }
}
catch { $failure = $_ }
finally {
    if ($null -ne $failure -and @($results | Where-Object { $_.status -eq 'failed' }).Count -eq 0) {
        $results.Add([pscustomobject]@{ test = 'test-execution'; status = 'failed'; details = @{ errorType = $failure.Exception.GetType().Name } })
    }
    $report = [pscustomobject]@{
        utc = [DateTime]::UtcNow.ToString('o')
        binarySha256 = (Get-FileHash -LiteralPath $binary -Algorithm SHA256).Hash
        caseRoot = $caseRoot
        validation = 'synthetic fixtures only; no production secret or leak claim'
        passedCount = @($results | Where-Object { $_.status -eq 'passed' }).Count
        skippedCount = @($results | Where-Object { $_.status -eq 'skipped' }).Count
        failedCount = @($results | Where-Object { $_.status -eq 'failed' }).Count
        tests = $results.ToArray()
    }
    $reportJson = $report | ConvertTo-Json -Depth 10
    $resultFile = Join-Path $caseRoot 'results.json'
    [IO.File]::WriteAllText($resultFile, $reportJson, (New-Object Text.UTF8Encoding $false))
    Write-Output ('Privacy/retention: {0} passed, {1} skipped, {2} failed. Results: {3}' -f $report.passedCount, $report.skippedCount, $report.failedCount, $resultFile)
}
if ($null -ne $failure) { throw $failure }
