param([switch]$Package)
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) { throw 'Windows x64 with .NET Framework is required.' }
$binaryDir = Join-Path $projectRoot 'artifacts\bin'
New-Item -ItemType Directory -Path $binaryDir -Force | Out-Null
$binary = Join-Path $binaryDir 'PCWatch.exe'
$sourceFiles = @(Get-ChildItem -LiteralPath (Join-Path $projectRoot 'src') -Filter '*.cs' | Select-Object -ExpandProperty FullName)
& $compiler /nologo /target:winexe /platform:x64 /optimize+ ("/out:" + $binary) ("/win32manifest:" + (Join-Path $projectRoot 'src\app.manifest')) /reference:System.dll /reference:System.Core.dll /reference:System.Drawing.dll /reference:System.Windows.Forms.dll /reference:System.Management.dll /reference:System.Web.Extensions.dll $sourceFiles
if ($LASTEXITCODE -ne 0) { throw 'PCWatch compilation failed.' }
Write-Output $binary
if ($Package) {
    $packageDir = Join-Path $projectRoot 'artifacts\portable'
    New-Item -ItemType Directory -Path $packageDir -Force | Out-Null
    Copy-Item -LiteralPath $binary -Destination (Join-Path $packageDir 'PCWatch.exe') -Force
    Copy-Item -LiteralPath (Join-Path $projectRoot 'README.md') -Destination (Join-Path $packageDir 'README.md') -Force
    Copy-Item -LiteralPath (Join-Path $projectRoot 'LICENSE') -Destination (Join-Path $packageDir 'LICENSE') -Force
    Get-ChildItem -LiteralPath (Join-Path $projectRoot 'scripts') -Filter '*.cmd' | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $packageDir $_.Name) -Force }
    $version = [Diagnostics.FileVersionInfo]::GetVersionInfo($binary).FileVersion
    $archive = Join-Path $projectRoot ('artifacts\PCWatch-' + $version + '-windows-x64.zip')
    Compress-Archive -Path (Join-Path $packageDir '*') -DestinationPath $archive -Force
    $hash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    [IO.File]::WriteAllText($archive + '.sha256', ($hash + '  ' + [IO.Path]::GetFileName($archive) + "`n"), (New-Object Text.UTF8Encoding $false))
    Write-Output $archive
}
