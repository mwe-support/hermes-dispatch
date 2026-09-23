[CmdletBinding()]
param(
    [switch]$DryRun,
    [ValidateRange(0, 86400)][int]$WaitActiveSeconds = 3600,
    [ValidateRange(1, 3600)][int]$PollSeconds = 15
)

$ErrorActionPreference = "Stop"
$Remote = if ($env:HERMES_DISPATCH_REMOTE) { $env:HERMES_DISPATCH_REMOTE } else { "https://github.com/mwe-support/hermes-dispatch.git" }
$Ref = if ($env:HERMES_DISPATCH_REF) { $env:HERMES_DISPATCH_REF } else { "main" }
if (-not $PSBoundParameters.ContainsKey("WaitActiveSeconds") -and $env:HERMES_DISPATCH_WAIT_SECONDS) {
    $WaitActiveSeconds = [int]$env:HERMES_DISPATCH_WAIT_SECONDS
}
if (-not $PSBoundParameters.ContainsKey("PollSeconds") -and $env:HERMES_DISPATCH_POLL_SECONDS) {
    $PollSeconds = [int]$env:HERMES_DISPATCH_POLL_SECONDS
}

function Resolve-NativeCommand([string]$Name) {
    $command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue
    if (-not $command) { throw "$Name is required" }
    return $command.Source
}

$Git = Resolve-NativeCommand "git"
$HermesRoot = if ($env:HERMES_DISPATCH_ROOT) {
    $env:HERMES_DISPATCH_ROOT
} else {
    if (-not $env:LOCALAPPDATA) { throw "LOCALAPPDATA is required" }
    Join-Path $env:LOCALAPPDATA "hermes"
}
if (-not (Test-Path -LiteralPath $HermesRoot -PathType Container)) { throw "Hermes root is missing: $HermesRoot" }
if ((Get-Item -LiteralPath $HermesRoot -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw "Hermes root must not be a reparse point: $HermesRoot"
}

$Python = $null
foreach ($candidate in @(
    (Join-Path $HermesRoot "hermes-agent\venv\Scripts\python.exe"),
    (Join-Path $HermesRoot "hermes-agent\.venv\Scripts\python.exe")
)) {
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { $Python = $candidate; break }
}
if (-not $Python) {
    foreach ($name in @("python3", "python")) {
        $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue
        if ($command) { $Python = $command.Source; break }
    }
}
if (-not $Python) { throw "Python 3.9 or newer is required" }
& $Python -c "import sys; raise SystemExit(sys.version_info < (3, 9))"
if ($LASTEXITCODE) { throw "Python 3.9 or newer is required" }

$HermesCommand = Get-Command "hermes" -CommandType Application -ErrorAction SilentlyContinue
if ($HermesCommand) {
    $Hermes = $HermesCommand.Source
} else {
    $Hermes = Join-Path $HermesRoot "hermes-agent\hermes"
    if (-not (Test-Path -LiteralPath $Hermes -PathType Leaf)) { throw "hermes is required" }
}

$TempRoot = $null
try {
    if ($env:HERMES_DISPATCH_SOURCE_DIR) {
        $Source = (Resolve-Path -LiteralPath $env:HERMES_DISPATCH_SOURCE_DIR).Path
    } else {
        $TempRoot = Join-Path ([IO.Path]::GetTempPath()) ("hermes-dispatch-all-" + [guid]::NewGuid().ToString("N"))
        $Source = Join-Path $TempRoot "source"
        New-Item -ItemType Directory -Path $TempRoot | Out-Null
        & $Git clone --quiet --depth 1 --branch $Ref $Remote $Source
        if ($LASTEXITCODE) { throw "git clone failed with exit $LASTEXITCODE" }
    }

    $Updater = Join-Path $Source "ops\hermes_dispatch_update.py"
    if (-not (Test-Path -LiteralPath $Updater -PathType Leaf)) { throw "updater not found in fetched source: $Updater" }
    $Commit = (& $Git -C $Source rev-parse HEAD).Trim()
    if ($LASTEXITCODE -or $Commit -notmatch "^[0-9a-f]{40}$") { throw "invalid fetched commit: $Commit" }

    $Profiles = [Collections.Generic.List[string]]::new()
    $Failures = [Collections.Generic.List[string]]::new()
    $Updated = [Collections.Generic.List[string]]::new()
    $Profiles.Add("default")
    $ProfilesRoot = Join-Path $HermesRoot "profiles"
    if (Test-Path -LiteralPath $ProfilesRoot -PathType Container) {
        foreach ($item in Get-ChildItem -LiteralPath $ProfilesRoot -Directory -Force | Sort-Object Name) {
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                $Failures.Add("$($item.Name):unsafe-profile-path")
            } elseif ($item.Name -match "^[a-z0-9][a-z0-9_-]{0,63}$" -and $item.Name -ne "default") {
                $Profiles.Add($item.Name)
            }
        }
    }

    Write-Host "Updating $($Profiles.Count) Hermes profile(s) to $Commit from $Ref"
    foreach ($profileName in $Profiles) {
        Write-Host "`n==> $profileName"
        $deadline = (Get-Date).AddSeconds($WaitActiveSeconds)
        while ($true) {
            $resultBase = Join-Path ([IO.Path]::GetTempPath()) ("hermes-dispatch-result-" + [guid]::NewGuid().ToString("N"))
            $outFile = "$resultBase.out"
            $errFile = "$resultBase.err"
            try {
                $arguments = @($Updater, "run", "--profile", $profileName, "--remote", $Remote, "--ref", $Commit, "--hermes-cli", $Hermes, "--git-cli", $Git)
                if (-not $DryRun) { $arguments += "--apply" }
                & $Python @arguments 1> $outFile 2> $errFile
                $code = $LASTEXITCODE
                $stdout = if (Test-Path $outFile) { Get-Content -LiteralPath $outFile -Raw } else { "" }
                $stderr = if (Test-Path $errFile) { Get-Content -LiteralPath $errFile -Raw } else { "" }
                if ($stdout) { [Console]::Out.Write($stdout) }
                if ($stderr) { [Console]::Error.Write($stderr) }
                $status = ""
                if ($code -eq 0 -and $stdout) {
                    try { $status = ($stdout | ConvertFrom-Json).status } catch { $status = "" }
                }
            } finally {
                Remove-Item -LiteralPath $outFile, $errFile -Force -ErrorAction SilentlyContinue
            }

            if ($status -eq "deferred" -and (Get-Date) -lt $deadline) {
                Write-Host "Profile $profileName has active agents; retrying in ${PollSeconds}s"
                Start-Sleep -Seconds $PollSeconds
                continue
            }
            $accepted = if ($DryRun) {
                $status -in @("dry-run-passed", "already-tested", "no-live-change")
            } else {
                $status -in @("updated", "no-live-change")
            }
            if ($accepted) {
                $Updated.Add($profileName)
            } else {
                $failureStatus = if ($status) { $status } else { "exit-$code" }
                $Failures.Add("${profileName}:$failureStatus")
            }
            break
        }
    }

    Write-Host "`nCommit: $Commit"
    Write-Host "Updated: $(if ($Updated.Count) { $Updated -join ' ' } else { '(none)' })"
    if ($Failures.Count) { throw "Failed/deferred: $($Failures -join ' ')" }
    Write-Host "All Hermes profiles are current."
} finally {
    if ($TempRoot -and (Test-Path -LiteralPath $TempRoot)) {
        Remove-Item -LiteralPath $TempRoot -Recurse -Force
    }
}
