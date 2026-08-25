<#
.SYNOPSIS
    Build the release archive for the SDR# Speech-to-Text plugin.

.DESCRIPTION
    Produces SDRSharp-STT-Plugin-<version>.zip containing the prebuilt plugin DLL and the
    Python server, for people who cannot or would rather not build it themselves.

    Two safety properties are enforced here rather than remembered:

    1. The server tree is taken from `git ls-files`, never from a directory walk. Everything
       that must not be published -- config.json, credentials.json, conversations.db, the AIS
       cache, every references*.txt and bench artefact -- is gitignored, so sourcing the file
       list from git means the archive cannot contain them even if they are sitting in the
       working tree. A directory copy with an exclusion list would be one forgotten pattern
       away from publishing received radio traffic.

    2. The archive is asserted to contain no SDR# SDK assemblies. The plugin's build output
       directory DOES contain SDRSharp.Common.dll, SDRSharp.Radio.dll and
       SDRSharp.PanView.dll, copied there from the local SDR# install by the reference
       resolution. Those are proprietary (c) Airspy, and NOTICE.md states they are not
       redistributed. Zipping bin\Release wholesale would ship them.

.PARAMETER Version
    Version string used in the archive name, e.g. v1.0.0.

.PARAMETER SdkPath
    Directory holding SDRSharp.Common.dll and SDRSharp.Radio.dll. Defaults to the csproj's
    own default.

.EXAMPLE
    pwsh tools/make-release.ps1 -Version v1.0.0
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$SdkPath = "",
    [string]$OutDir  = "dist"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo
try {
    $proj    = "SDRSharp.SttPlugin/SDRSharp.SttPlugin.csproj"
    $staging = Join-Path ([System.IO.Path]::GetTempPath()) ("sttrelease-" + [guid]::NewGuid().ToString("N"))
    $zip     = Join-Path $OutDir "SDRSharp-STT-Plugin-$Version.zip"

    # --- 1. build ---------------------------------------------------------------------
    Write-Host "==> building the plugin ($Version)" -ForegroundColor Cyan
    $buildArgs = @("build", $proj, "-c", "Release", "--nologo", "-v", "quiet")
    if ($SdkPath) { $buildArgs += "-p:SDRSharpSdkPath=$SdkPath" }
    & dotnet @buildArgs
    if ($LASTEXITCODE -ne 0) { throw "build failed" }

    $dll = Get-ChildItem "SDRSharp.SttPlugin/bin/Release" -Recurse -Filter "SDRSharp.SttPlugin.dll" |
           Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $dll) { throw "SDRSharp.SttPlugin.dll not found after a successful build" }

    # --- 2. stage ---------------------------------------------------------------------
    Write-Host "==> staging" -ForegroundColor Cyan
    $pluginDir = Join-Path $staging "Plugins/SttPlugin"
    New-Item -ItemType Directory -Force -Path $pluginDir | Out-Null
    Copy-Item $dll.FullName $pluginDir

    # Only for SDR# builds predating plugin auto-discovery. Documentation, not configuration.
    Set-Content -Path (Join-Path $pluginDir "MagicLine.txt") -Encoding utf8 -Value @(
        '<add key="SpeechToText" value="SDRSharp.SttPlugin.SttPlugin,SDRSharp.SttPlugin" />'
        ''
        'Only needed on SDR# builds old enough to require plugin registration in'
        'SDRSharp.exe.config. Modern SDR# discovers plugins by scanning Plugins\<Name>\'
        'and never reads this file.'
    )

    # The server tree, from git. See the note at the top of this file for why.
    $tracked = & git ls-files server
    if ($LASTEXITCODE -ne 0) { throw "git ls-files failed" }
    if (-not $tracked) { throw "git ls-files server returned nothing" }
    foreach ($rel in $tracked) {
        $dest = Join-Path $staging $rel
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dest) | Out-Null
        Copy-Item $rel $dest
    }

    Copy-Item "tools/INSTALL.txt" (Join-Path $staging "INSTALL.txt")
    Copy-Item "LICENSE"           (Join-Path $staging "LICENSE")
    Copy-Item "NOTICE.md"         (Join-Path $staging "NOTICE.md")

    # --- 3. assert --------------------------------------------------------------------
    Write-Host "==> checking what is about to be published" -ForegroundColor Cyan
    $staged = Get-ChildItem $staging -Recurse -File

    $proprietary = $staged | Where-Object { $_.Name -match '^SDRSharp\.(Common|Radio|PanView)\.dll$' }
    if ($proprietary) {
        throw ("PROPRIETARY SDR# SDK ASSEMBLIES IN THE ARCHIVE: " +
               ($proprietary.Name -join ", ") + ". These are (c) Airspy and must not be redistributed.")
    }

    # Belt and braces over the git-sourced list: name the file families that carry received
    # radio traffic or credentials, and fail on any of them. Mirrors the CI hygiene gate.
    # Each alternative names a family that .gitignore already excludes, so this should never
    # fire -- it is here to catch a .gitignore that silently stopped matching, which has
    # happened twice (an anchored pattern missing dated snapshots, and one missing files at
    # depth). Deliberately NARROW: bench-*.json arm results and fewshot.py are tracked source
    # and must not trip it.
    $forbidden = @(
        '[\/](config|credentials)\.json$'
        '[\/]references[^\/]*\.txt$'
        '[\/]conversations[^\/]*\.(json|db)'
        '[\/]ais_cache[^\/]*\.json$'
        '[\/]start-all\.bat$'
        '[\/]identification-labels'
        '[\/]bench-identify-(results|repeats)[^\/]*\.json$'
        '[\/]bench-correct-results[^\/]*\.json$'
        '[\/]bench-conv-correct-[^\/]*\.json$'
        '[\/]suggest-[^\/]*\.json$'
        '[\/](draft|score)-[^\/]*\.(json|html)$'
        '[^\/]*(fewshot|examples)[^\/]*\.json$'
    ) -join '|'
    $leaked = $staged | Where-Object { $_.FullName.Substring($staging.Length) -match $forbidden }
    if ($leaked) {
        throw ("FILES THAT MUST NOT BE PUBLISHED: " +
               (($leaked | ForEach-Object { $_.FullName.Substring($staging.Length) }) -join ", "))
    }

    if (-not ($staged | Where-Object { $_.Name -eq "SDRSharp.SttPlugin.dll" })) {
        throw "the plugin DLL is missing from the archive"
    }
    if (-not ($staged | Where-Object { $_.Name -eq "requirements.txt" })) {
        throw "server/requirements.txt is missing from the archive"
    }

    # --- 4. zip -----------------------------------------------------------------------
    New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Compress-Archive -Path (Join-Path $staging "*") -DestinationPath $zip -CompressionLevel Optimal

    $size = (Get-Item $zip).Length
    Write-Host ""
    Write-Host ("==> {0}" -f $zip) -ForegroundColor Green
    Write-Host ("    {0} files, {1:N0} bytes" -f $staged.Count, $size)
    Write-Host ("    plugin DLL: {0:N0} bytes" -f $dll.Length)
    Write-Host "    no SDR# SDK assemblies, no credentials, no transcripts" -ForegroundColor Green
}
finally {
    Pop-Location
    if ($staging -and (Test-Path $staging)) { Remove-Item $staging -Recurse -Force }
}
