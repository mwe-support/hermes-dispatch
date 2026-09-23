$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Temp = Join-Path ([IO.Path]::GetTempPath()) ("dispatch-all-test-" + [guid]::NewGuid().ToString("N"))
$oldPath = $env:PATH
$oldRoot = $env:HERMES_DISPATCH_ROOT
$oldSource = $env:HERMES_DISPATCH_SOURCE_DIR
$oldLog = $env:CALL_LOG
$oldFail = $env:FAIL_PROFILE
try {
    $HermesRoot = Join-Path $Temp "hermes"
    $Source = Join-Path $Temp "source"
    $Bin = Join-Path $Temp "bin"
    New-Item -ItemType Directory -Path (Join-Path $HermesRoot "profiles\alpha"), (Join-Path $HermesRoot "profiles\beta"), (Join-Path $Source "ops"), $Bin -Force | Out-Null
    & git -C $Source init -q -b main
    & git -C $Source config user.email test@example.com
    & git -C $Source config user.name Test
    Copy-Item (Join-Path $Root "scripts\update-hermes-dispatch-all.ps1") (Join-Path $Source "update.ps1")
    @'
import json, os, sys
profile = sys.argv[sys.argv.index("--profile") + 1]
ref = sys.argv[sys.argv.index("--ref") + 1]
with open(os.environ["CALL_LOG"], "a", encoding="utf-8") as handle:
    handle.write(f"{profile} {ref} {'--apply' in sys.argv}\n")
status = "blocked" if profile == os.environ.get("FAIL_PROFILE") else "updated" if "--apply" in sys.argv else "dry-run-passed"
print(json.dumps({"status": status}))
'@ | Set-Content -LiteralPath (Join-Path $Source "ops\hermes_dispatch_update.py") -Encoding UTF8
    & git -C $Source add .
    & git -C $Source commit -qm fixture
    $Commit = (& git -C $Source rev-parse HEAD).Trim()

    "@exit /b 0" | Set-Content -LiteralPath (Join-Path $Bin "hermes.cmd") -Encoding ASCII
    $env:PATH = "$Bin;$oldPath"
    $env:HERMES_DISPATCH_ROOT = $HermesRoot
    $env:HERMES_DISPATCH_SOURCE_DIR = $Source
    $env:CALL_LOG = Join-Path $Temp "calls"

    & (Join-Path $Source "update.ps1") | Out-Null
    $expected = @("default $Commit True", "alpha $Commit True", "beta $Commit True")
    if ((@(Get-Content $env:CALL_LOG) -join "`n") -ne ($expected -join "`n")) { throw "apply calls differed" }

    Clear-Content $env:CALL_LOG
    & (Join-Path $Source "update.ps1") -DryRun | Out-Null
    $expected = @("default $Commit False", "alpha $Commit False", "beta $Commit False")
    if ((@(Get-Content $env:CALL_LOG) -join "`n") -ne ($expected -join "`n")) { throw "dry-run calls differed" }

    Clear-Content $env:CALL_LOG
    $env:FAIL_PROFILE = "alpha"
    try {
        & (Join-Path $Source "update.ps1") | Out-Null
        throw "blocked profile did not fail"
    } catch {
        if ($_ -notmatch "alpha:blocked") { throw }
    }
    $expected = @("default $Commit True", "alpha $Commit True", "beta $Commit True")
    if ((@(Get-Content $env:CALL_LOG) -join "`n") -ne ($expected -join "`n")) { throw "aggregate failure stopped early" }

    Write-Host "PowerShell all-profile discovery, pinned commit, apply, dry-run and aggregate failure: PASS"
} finally {
    $env:PATH = $oldPath
    $env:HERMES_DISPATCH_ROOT = $oldRoot
    $env:HERMES_DISPATCH_SOURCE_DIR = $oldSource
    $env:CALL_LOG = $oldLog
    $env:FAIL_PROFILE = $oldFail
    Remove-Item -LiteralPath $Temp -Recurse -Force -ErrorAction SilentlyContinue
}
