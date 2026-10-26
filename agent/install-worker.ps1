<#
.SYNOPSIS
Turns this Windows PC into an IdleCloud worker in one step.

.DESCRIPTION
1. Turns on systemd in WSL2. K3s, WireGuard and graceful shutdown need it.
2. Installs the Windows sensor .exe and registers it, and WSL, to start at
   every login.
3. Installs the node agent in Ubuntu as a systemd service, so it starts at
   boot and is restarted if it stops.
4. Runs infra/join-worker.sh inside Ubuntu to join the cluster over WireGuard,
   then starts the sensor and WSL tasks.

Get wg0.conf and agent-token from /etc/wireguard/peers/<name>/ on the hub,
after member 1 runs `add-peer.sh worker <name>`. The token lasts 10 minutes,
so run this soon after. Running it again skips the join if the K3s agent is
already active (use -Rejoin to force it) and refreshes the sensor, tasks and
node agent.

Run in Windows PowerShell from the repo, as the PC's normal user:

  powershell -ExecutionPolicy Bypass -File .\agent\install-worker.ps1 `
      -NodeName pc1 -WgConfig .\wg0.conf -TokenFile .\agent-token `
      -CoordinatorUrl http://<cluster-ip>:8001
#>
param(
    [Parameter(Mandatory)] [string] $NodeName,
    [string] $WgConfig,
    [string] $TokenFile,
    [string] $CoordinatorUrl,
    [string] $Distro = 'Ubuntu-24.04',
    [string] $SensorExe,
    [switch] $Rejoin
)

$ErrorActionPreference = 'Stop'
# Without this, wsl.exe prints UTF-16 and `wsl -l` output cannot be compared.
$env:WSL_UTF8 = '1'

# Windows PowerShell 5.1 has no $PSScriptRoot in param defaults, so set it here.
if (-not $SensorExe) {
    $SensorExe = Join-Path $PSScriptRoot 'windows-sensor\dist\idlecloud-sensor.exe'
}
$JoinScript = Join-Path $PSScriptRoot '..\infra\join-worker.sh'
$AgentDir = Join-Path $PSScriptRoot 'node-agent'
$Stage = '/tmp/idlecloud-install'
$StageUnc = "\\wsl$\$Distro" + ($Stage -replace '/', '\')
$TaskPath = '\IdleCloud\'

function Invoke-Wsl {
    # Runs one command as root with no shell in between, so nothing is re-quoted.
    & wsl.exe -d $Distro -u root --exec @args
    if ($LASTEXITCODE -ne 0) { throw "Command failed in WSL ($LASTEXITCODE): $args" }
}

function Get-WslOutput {
    (& wsl.exe -d $Distro -u root --exec @args | Out-String).Trim()
}

function Initialize-Stage {
    # Owned by the default user so Windows can write into it through \\wsl$.
    $user = (& wsl.exe -d $Distro --exec id -un | Out-String).Trim()
    Invoke-Wsl rm -rf $Stage
    Invoke-Wsl install -d -m 700 -o $user $Stage
}

function Copy-ToStage([string] $Source, [string] $Name) {
    # Drop CRLF, so a Windows checkout or editor cannot break bash or the token.
    $text = [IO.File]::ReadAllText((Resolve-Path $Source).Path) -replace "`r`n", "`n"
    [IO.File]::WriteAllText("$StageUnc\$Name", $text, (New-Object Text.UTF8Encoding $false))
}

foreach ($path in @($JoinScript, $SensorExe, (Join-Path $AgentDir 'install-service.sh'))) {
    if (-not (Test-Path $path -PathType Leaf)) { throw "File not found: $path" }
}

$distros = (& wsl.exe -l -q) | ForEach-Object { ($_ -replace "`0", '').Trim() }
if ($distros -notcontains $Distro) {
    throw "$Distro is not installed. Run 'wsl --install -d $Distro', create the user, then run this again."
}

# 1. systemd in WSL2
if ((Get-WslOutput ps -p 1 -o comm=) -ne 'systemd') {
    Write-Host 'Turning on systemd in WSL...'
    $script = @'
set -eu
conf=/etc/wsl.conf
touch "$conf"
sed -i '/^[[:space:]]*systemd[[:space:]]*=/d' "$conf"
if grep -q '^\[boot\]' "$conf"; then
    sed -i '/^\[boot\]/a systemd=true' "$conf"
else
    printf '\n[boot]\nsystemd=true\n' >> "$conf"
fi
'@
    Initialize-Stage
    [IO.File]::WriteAllText("$StageUnc\enable-systemd.sh", ($script -replace "`r`n", "`n"),
        (New-Object Text.UTF8Encoding $false))
    Invoke-Wsl bash "$Stage/enable-systemd.sh"

    & wsl.exe --terminate $Distro | Out-Null
    if ((Get-WslOutput ps -p 1 -o comm=) -ne 'systemd') {
        throw "systemd did not start. Run 'wsl --update' and try again."
    }
}

# 2. Sensor and WSL at login. Done before the join, so a failure here (such as
# a PC that blocks task creation) does not spend the 10-minute token.
Write-Host 'Installing the sensor and login tasks...'
$installDir = Join-Path $env:LOCALAPPDATA 'idlecloud'
$installedExe = Join-Path $installDir 'idlecloud-sensor.exe'
New-Item -ItemType Directory -Force $installDir | Out-Null

# A running sensor locks the .exe.
Stop-ScheduledTask -TaskPath $TaskPath -TaskName 'Sensor' -ErrorAction SilentlyContinue
Get-Process idlecloud-sensor -ErrorAction SilentlyContinue | Stop-Process -Force
Copy-Item $SensorExe $installedExe -Force

$me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $me
$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew

# WSL stops its VM soon after the last wsl.exe exits, even with systemd
# services running, so keep one open. conhost --headless hides its window.
$wslAction = New-ScheduledTaskAction -Execute 'conhost.exe' `
    -Argument "--headless wsl.exe -d $Distro --exec sleep infinity"
$sensorAction = New-ScheduledTaskAction -Execute $installedExe

Register-ScheduledTask -TaskPath $TaskPath -TaskName 'WSL' -Action $wslAction `
    -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Register-ScheduledTask -TaskPath $TaskPath -TaskName 'Sensor' -Action $sensorAction `
    -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null

# 3. Node agent as a systemd service. Also before the join, as it downloads
# packages and can fail on a PC without internet access.
Write-Host 'Installing the node agent service...'
Initialize-Stage
try {
    $agentFiles = @(Get-ChildItem $AgentDir -Filter *.py) + @(
        'requirements.txt', 'idlecloud-node-agent.service', 'install-service.sh' |
            ForEach-Object { Get-Item (Join-Path $AgentDir $_) })
    foreach ($file in $agentFiles) { Copy-ToStage $file.FullName $file.Name }
    $agentArgs = @($NodeName)
    if ($CoordinatorUrl) { $agentArgs += $CoordinatorUrl }
    Invoke-Wsl bash "$Stage/install-service.sh" @agentArgs
} finally {
    & wsl.exe -d $Distro -u root --exec rm -rf $Stage
}

# 4. Join the cluster
if ((Get-WslOutput systemctl is-active k3s-agent) -eq 'active' -and -not $Rejoin) {
    Write-Host 'K3s agent is already running, skipping the join (use -Rejoin to force it).'
} else {
    if (-not $WgConfig -or -not $TokenFile) {
        throw 'Joining needs -WgConfig and -TokenFile from add-peer.sh on the hub.'
    }
    Write-Host "Joining the cluster as $NodeName..."
    Initialize-Stage
    try {
        Copy-ToStage $JoinScript 'join-worker.sh'
        Copy-ToStage $WgConfig 'wg0.conf'
        Copy-ToStage $TokenFile 'agent-token'
        Invoke-Wsl bash "$Stage/join-worker.sh" $NodeName "$Stage/wg0.conf" "$Stage/agent-token"
    } finally {
        # The stage holds the WireGuard private key and join token.
        & wsl.exe -d $Distro -u root --exec rm -rf $Stage
    }
}

Start-ScheduledTask -TaskPath $TaskPath -TaskName 'WSL'
Start-ScheduledTask -TaskPath $TaskPath -TaskName 'Sensor'

Write-Host @"

Done. $NodeName should show as Ready in 'kubectl get nodes' within a minute.
The node is not protected from task pods until the node agent runs. On an
admin machine, run agent/node-agent/make-kubeconfig.sh $NodeName and copy its
two files to /etc/idlecloud/ (see agent/README.md). The node agent then starts
by itself within 10 seconds, and at every boot after that.
"@
