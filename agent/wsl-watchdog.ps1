<#
.SYNOPSIS
Restarts WSL when this worker's Linux side is stuck. install-worker.ps1 runs
it every 2 minutes as the IdleCloud\Watchdog task.

.DESCRIPTION
WSL can keep running while the worker inside it is dead. When its disk hits
an error, Linux remounts it read-only, and K3s and the node agent then stop
on their next write. Nothing on the PC notices, so it stays out of the
cluster until someone restarts WSL by hand.

Each run checks, inside the distro:
  - /var/lib/rancher is writable (a small probe file is written there);
  - k3s-agent and idlecloud-node-agent, where enabled, are active (or still
    starting).
After two failed checks in a row (about 4 minutes), and at most once every
10 minutes, it saves the kernel's disk messages (dmesg) to the log, runs
wsl --shutdown, which stops every WSL distro on this PC, and starts the
IdleCloud\WSL task again, which brings the disk back read-write.

The log is watchdog.log beside this script (%LOCALAPPDATA%\idlecloud).
#>
param(
    [string] $Distro = 'Ubuntu-24.04',
    [string] $TaskPath = '\IdleCloud\'
)

$ErrorActionPreference = 'Stop'
$env:WSL_UTF8 = '1'

$Dir = $PSScriptRoot
$LogFile = Join-Path $Dir 'watchdog.log'
$StateFile = Join-Path $Dir 'watchdog.state.json'
$FailuresBeforeRestart = 2
$MinMinutesBetweenRestarts = 10
$CheckTimeoutSeconds = 60

$Check = @'
probe=/var/lib/rancher/.idlecloud-watchdog
if ! date +%s > "$probe" 2>/dev/null; then echo "READONLY $probe is not writable"; exit 1; fi
for unit in k3s-agent idlecloud-node-agent; do
    systemctl is-enabled --quiet "$unit" 2>/dev/null || continue
    state=$(systemctl is-active "$unit")
    case "$state" in
        active|activating|reloading) ;;
        *) echo "DOWN $unit is $state"; exit 1 ;;
    esac
done
echo OK
'@ -replace "`r`n", "`n"

function Write-Log([string] $Message) {
    if ((Test-Path $LogFile) -and (Get-Item $LogFile).Length -gt 1MB) {
        Move-Item $LogFile "$LogFile.1" -Force
    }
    Add-Content -Path $LogFile -Value ("{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $Message)
}

function Read-State {
    if (Test-Path $StateFile) {
        try { return Get-Content $StateFile -Raw | ConvertFrom-Json } catch { }
    }
    [pscustomobject]@{ failures = 0; lastRestart = $null }
}

function Save-State($State) {
    $State | ConvertTo-Json | Set-Content $StateFile
}

function Invoke-WslScript([string] $Script, [int] $TimeoutSeconds) {
    # Runs a shell script in the distro as root. It travels base64-encoded, as
    # Start-Process passes one command line that Windows would re-split. A
    # wedged VM can make wsl.exe hang, so give up after a timeout.
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Script))
    $arguments = "-d $Distro -u root --exec sh -c `"echo $b64 | base64 -d | sh`""
    $out = [IO.Path]::GetTempFileName()
    try {
        $p = Start-Process wsl.exe -ArgumentList $arguments -NoNewWindow -PassThru `
            -RedirectStandardOutput $out
        if (-not $p.WaitForExit($TimeoutSeconds * 1000)) {
            try { $p.Kill() } catch { }
            return @{ ok = $false; text = "TIMEOUT wsl.exe did not answer in $TimeoutSeconds s" }
        }
        $text = (Get-Content $out -Raw)
        if ($null -eq $text) { $text = '' }
        return @{ ok = ($p.ExitCode -eq 0); text = $text.Trim() }
    } finally {
        Remove-Item $out -ErrorAction SilentlyContinue
    }
}

$running = (& wsl.exe --list --running --quiet | Out-String) -split "`r?`n" |
    ForEach-Object { $_.Trim() }
if ($running -notcontains $Distro) {
    # Not running at all: the WSL task keeps it up, so start that.
    Write-Log "$Distro is not running; starting the WSL task."
    Start-ScheduledTask -TaskPath $TaskPath -TaskName 'WSL' -ErrorAction SilentlyContinue
    exit 0
}

$state = Read-State
$result = Invoke-WslScript $Check $CheckTimeoutSeconds
if ($result.ok) {
    if ($state.failures -gt 0) { Write-Log "Healthy again after $($state.failures) failed check(s)." }
    $state.failures = 0
    Save-State $state
    exit 0
}

$state.failures = [int] $state.failures + 1
Write-Log "Check failed ($($state.failures)/$FailuresBeforeRestart): $($result.text)"
$recent = $state.lastRestart -and
    ((Get-Date) - [datetime] $state.lastRestart).TotalMinutes -lt $MinMinutesBetweenRestarts
if ($state.failures -lt $FailuresBeforeRestart -or $recent) {
    Save-State $state
    exit 0
}

# Keep the kernel's account of the disk error: it is gone after the restart.
$dmesg = Invoke-WslScript 'dmesg | grep -iE "ext4|read-only|I/O error|blk_update" | tail -30' 20
if ($dmesg.text) { Write-Log "dmesg before restart:`n$($dmesg.text)" }

Write-Log 'Restarting WSL (wsl --shutdown, then the WSL task).'
& wsl.exe --shutdown
Start-Sleep -Seconds 5
Start-ScheduledTask -TaskPath $TaskPath -TaskName 'WSL'
$state.failures = 0
$state.lastRestart = (Get-Date).ToString('o')
Save-State $state
