# Shared helpers for Windows PowerShell 5.1 tests. All runtime data stays in artifacts/.
function Initialize-TestSandbox([string]$Name, [string]$BinaryPath) {
    $script:binary = (Resolve-Path -LiteralPath $BinaryPath).Path
    $script:caseRoot = Join-Path (Split-Path -Parent $PSScriptRoot) ('artifacts/tests/' + $Name + '-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $script:caseRoot -Force | Out-Null
    $script:results = New-Object 'System.Collections.Generic.List[object]'
    $script:ownedProcesses = New-Object 'System.Collections.Generic.List[System.Diagnostics.Process]'
    $script:clock = [Diagnostics.Stopwatch]::StartNew()
    $script:failure = $null
}

function Assert-Test([string]$Name, [bool]$Passed, [object]$Details) {
    $script:results.Add([pscustomobject]@{ test = $Name; status = $(if ($Passed) { 'passed' } else { 'failed' }); details = $Details })
    if (-not $Passed) { throw ('Test failed: ' + $Name) }
}

function Skip-Test([string]$Name, [string]$Reason) {
    $script:results.Add([pscustomobject]@{ test = $Name; status = 'skipped'; details = @{ reason = $Reason } })
}

function Start-TestProcess([string[]]$Arguments) {
    $process = Start-Process -FilePath $script:binary -ArgumentList $Arguments -WorkingDirectory $script:caseRoot -WindowStyle Hidden -PassThru
    $script:ownedProcesses.Add($process)
    return $process
}

function Start-TestMonitor([string]$DataDir, [int]$MaxSeconds = 12) {
    return Start-TestProcess -Arguments @('--data-dir', ('"' + $DataDir + '"'), '--interval', '2', '--details', '4', '--max-seconds', [string]$MaxSeconds, '--no-tray')
}

function Wait-TestCondition([scriptblock]$Condition, [int]$TimeoutSeconds, [string]$Description) {
    $started = $script:clock.Elapsed.TotalSeconds
    while ($true) {
        if (& $Condition) { return }
        if ($script:clock.Elapsed.TotalSeconds -ge 70) { throw 'Test exceeded its global 70-second limit.' }
        if ($script:clock.Elapsed.TotalSeconds - $started -ge $TimeoutSeconds) { throw ('Timed out waiting for ' + $Description) }
        Start-Sleep -Milliseconds 200
    }
}

function Wait-TestExit([Diagnostics.Process]$Process, [int]$TimeoutSeconds = 12) {
    Wait-TestCondition { $Process.Refresh(); $Process.HasExited } $TimeoutSeconds ('owned process PID ' + $Process.Id + ' to exit')
    $Process.WaitForExit()
}

function Read-TestStatus([string]$DataDir) {
    $path = Join-Path $DataDir 'status.json'
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    try { return Get-Content -LiteralPath $path -Raw | ConvertFrom-Json }
    catch { return $null }
}

function Read-TestJsonLines([string]$DataDir, [string]$Kind) {
    $logs = Join-Path $DataDir 'Logs'
    if (-not (Test-Path -LiteralPath $logs)) { return }
    foreach ($file in @(Get-ChildItem -LiteralPath $logs -Filter ($Kind + '-*.jsonl') | Sort-Object Name)) {
        foreach ($line in @(Get-Content -LiteralPath $file.FullName)) {
            if (-not [string]::IsNullOrWhiteSpace($line)) { $line | ConvertFrom-Json }
        }
    }
}

function Read-TestCsv([string]$DataDir) {
    $logs = Join-Path $DataDir 'Logs'
    if (-not (Test-Path -LiteralPath $logs)) { return }
    foreach ($file in @(Get-ChildItem -LiteralPath $logs -Filter 'resources-*.csv' | Sort-Object Name)) { Import-Csv -LiteralPath $file.FullName }
}

function Test-FiniteNumber([string]$Value, [bool]$AllowBlank = $false) {
    if ([string]::IsNullOrWhiteSpace($Value)) { return $AllowBlank }
    $number = 0.0
    return ([double]::TryParse($Value, [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$number) -and (-not [double]::IsNaN($number)) -and (-not [double]::IsInfinity($number)))
}

function Complete-TestSandbox([string]$Name) {
    foreach ($process in $script:ownedProcesses) {
        try {
            $process.Refresh()
            if (-not $process.HasExited) {
                # Only this exact Process object, started by this test, can be killed.
                $process.Kill()
                [void]$process.WaitForExit(2000)
            }
        }
        catch { }
        finally { $process.Dispose() }
    }
    if ($null -ne $script:failure -and @($script:results | Where-Object { $_.status -eq 'failed' }).Count -eq 0) {
        $script:results.Add([pscustomobject]@{ test = 'test-execution'; status = 'failed'; details = @{ errorType = $script:failure.Exception.GetType().Name } })
    }
    $report = [pscustomobject]@{
        utc = [DateTime]::UtcNow.ToString('o')
        binarySha256 = (Get-FileHash -LiteralPath $script:binary -Algorithm SHA256).Hash
        caseRoot = $script:caseRoot
        elapsedSeconds = [Math]::Round($script:clock.Elapsed.TotalSeconds, 2)
        passedCount = @($script:results | Where-Object { $_.status -eq 'passed' }).Count
        skippedCount = @($script:results | Where-Object { $_.status -eq 'skipped' }).Count
        failedCount = @($script:results | Where-Object { $_.status -eq 'failed' }).Count
        failureType = $(if ($null -ne $script:failure) { $script:failure.Exception.GetType().Name } else { $null })
        tests = $script:results.ToArray()
    }
    $path = Join-Path $script:caseRoot 'results.json'
    [IO.File]::WriteAllText($path, ($report | ConvertTo-Json -Depth 10), (New-Object Text.UTF8Encoding $false))
    Write-Output ('{0}: {1} passed, {2} skipped, {3} failed. Results: {4}' -f $Name, $report.passedCount, $report.skippedCount, $report.failedCount, $path)
}
