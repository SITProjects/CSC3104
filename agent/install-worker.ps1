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
   then starts the sensor and WSL tasks. A PC with a discrete NVIDIA GPU, or
   an AMD one ROCm supports under WSL, joins as a GPU worker (infra/README.md,
   "GPU workers"). WSL can't tell which AMD GPU Windows has, so this script
   finds it and passes it on. -GpuVendor nvidia|amd|none picks one, or none;
   -AllowIntegratedGpu accepts an AMD APU, for testing only.

The easiest way: download the PC's setup zip from the admin pages (Workers,
Add worker) and pass it with -Bundle. It holds the node name, wg0.conf, the
join token and the node agent's kubeconfig. Once the admin pages show the PC
as joined, download its agent credentials and run this again with
-AgentCredentials, which only installs the heartbeat token.

Or pass the files from /etc/wireguard/peers/<name>/ on the hub, after
`add-peer.sh worker <name>`, with -WgConfig and -TokenFile.

The join token expires: 24 hours for a setup zip (request a new one on the
admin pages), 10 minutes from add-peer.sh. Running this again skips the join if the K3s agent is already
active (use -Rejoin to force it) and refreshes the sensor, tasks and node agent.

Run in Windows PowerShell from the repo, as the PC's normal user:

  powershell -ExecutionPolicy Bypass -File .\agent\install-worker.ps1 `
      -Bundle .\pc1-setup.zip

  powershell -ExecutionPolicy Bypass -File .\agent\install-worker.ps1 `
      -AgentCredentials .\pc1-agent-credentials.zip

  powershell -ExecutionPolicy Bypass -File .\agent\install-worker.ps1 `
      -NodeName pc1 -WgConfig .\wg0.conf -TokenFile .\agent-token `
      -CoordinatorUrl http://<cluster-ip>:8001
#>
[CmdletBinding(DefaultParameterSetName = 'Files')]
param(
    [Parameter(Mandatory, ParameterSetName = 'Files')] [string] $NodeName,
    [Parameter(ParameterSetName = 'Files')] [string] $WgConfig,
    [Parameter(ParameterSetName = 'Files')] [string] $TokenFile,
    [Parameter(ParameterSetName = 'Files')] [string] $CoordinatorUrl,
    [Parameter(Mandatory, ParameterSetName = 'Bundle')] [string] $Bundle,
    [Parameter(Mandatory, ParameterSetName = 'Credentials')] [string] $AgentCredentials,
    [string] $Distro = 'Ubuntu-24.04',
    [Parameter(ParameterSetName = 'Files')]
    [Parameter(ParameterSetName = 'Bundle')] [string] $SensorExe,
    [Parameter(ParameterSetName = 'Files')]
    [Parameter(ParameterSetName = 'Bundle')] [switch] $Rejoin,
    [Parameter(ParameterSetName = 'Files')]
    [Parameter(ParameterSetName = 'Bundle')]
    [ValidateSet('auto', 'nvidia', 'amd', 'none')] [string] $GpuVendor = 'auto',
    [Parameter(ParameterSetName = 'Files')]
    [Parameter(ParameterSetName = 'Bundle')] [switch] $AllowIntegratedGpu
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

function Write-ToStage([string] $Name, [string] $Text) {
    # Drop CRLF, so a Windows checkout or editor cannot break bash or the token.
    [IO.File]::WriteAllText("$StageUnc\$Name", ($Text -replace "`r`n", "`n"),
        (New-Object Text.UTF8Encoding $false))
}

function Copy-ToStage([string] $Source, [string] $Name) {
    Write-ToStage $Name ([IO.File]::ReadAllText((Resolve-Path $Source).Path))
}

function Read-Zip([string] $Path) {
    # A zip from the admin pages: manifest.json names the files beside it. They
    # are read into memory only, since they hold the WireGuard key and tokens.
    Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead((Resolve-Path $Path).Path)
    try {
        function Read-Entry([string] $Name) {
            if ($Name -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$') { throw "Bad file name in ${Path}: $Name" }
            $entry = $zip.GetEntry($Name)
            if (-not $entry) { throw "$Path has no $Name." }
            $reader = New-Object IO.StreamReader($entry.Open(), (New-Object Text.UTF8Encoding $false))
            try { $reader.ReadToEnd() } finally { $reader.Dispose() }
        }
        $manifest = (Read-Entry 'manifest.json') | ConvertFrom-Json
        if ($manifest.version -ne 1) { throw "$Path is not a setup zip this installer knows (version $($manifest.version))." }
        if ($manifest.node_name -notmatch '^[a-z0-9]([a-z0-9-]*[a-z0-9])?$') { throw "Bad node name in $Path." }
        $files = @{}
        foreach ($key in 'wg_config', 'join_token', 'kubeconfig', 'coordinator_token') {
            $name = $manifest.files.$key
            if ($name) { $files[$key] = Read-Entry $name }
        }
        @{ NodeName = $manifest.node_name; CoordinatorUrl = $manifest.coordinator_url; Files = $files }
    } finally {
        $zip.Dispose()
    }
}

# PCI device IDs of AMD APUs' integrated graphics, which never make a GPU
# worker: Raven/Picasso (15DD, 15D8), Renoir/Cezanne/Lucienne/Barcelo (1636,
# 1638, 164C, 15E7), Van Gogh (163F), Rembrandt (1681), Raphael/Granite Ridge
# (164E, 13C0), Mendocino (1506), Phoenix/Hawk Point (15BF, 15C8, 1900, 1901),
# Strix Point (150E), Strix Halo (1586), Krackan (1114), Carrizo (9874),
# Stoney (98E4). infra/join-worker.sh skips the same chips on Linux.
$AmdApuDeviceIds = @('15DD', '15D8', '1636', '1638', '164C', '15E7', '163F', '1681',
    '164E', '13C0', '1506', '15BF', '15C8', '1900', '1901', '150E', '1586', '1114',
    '9874', '98E4')

function Get-AmdGpu {
    # The discrete AMD GPU with the most memory, as @{ Name; Gib }, or $null.
    # Memory comes from the display driver's registry key, because
    # Win32_VideoController.AdapterRAM stops at 4 GB.
    $present = @(Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue |
        ForEach-Object { if ($_.PNPDeviceID -match 'VEN_1002&DEV_([0-9A-F]{4})') { $Matches[1].ToUpper() } })
    $class = 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}'
    $best = $null
    foreach ($key in (Get-ChildItem $class -ErrorAction SilentlyContinue)) {
        $props = Get-ItemProperty $key.PSPath -ErrorAction SilentlyContinue
        if (-not $props -or "$($props.MatchingDeviceId)" -notmatch 'ven_1002&dev_([0-9a-f]{4})') { continue }
        $device = $Matches[1].ToUpper()
        if ($present -notcontains $device) { continue }  # a driver key left from an old card
        $raw = $props.'HardwareInformation.qwMemorySize'
        if (-not $raw) { $raw = $props.'HardwareInformation.MemorySize' }  # older drivers, 4 GB at most
        $bytes = 0
        if ($raw -is [byte[]] -and $raw.Length -ge 8) { $bytes = [BitConverter]::ToUInt64($raw, 0) }
        elseif ($raw -is [byte[]] -and $raw.Length -ge 4) { $bytes = [BitConverter]::ToUInt32($raw, 0) }
        elseif ($raw -is [int]) { $bytes = [int64]$raw -band 0xFFFFFFFFL }  # a DWORD over 2 GB reads as negative
        elseif ($raw) { $bytes = [int64]$raw }
        $gib = [int][Math]::Floor($bytes / 1GB)
        # an APU's own memory is a small carve-out; under 2 GB it is no dGPU
        if (($AmdApuDeviceIds -contains $device) -or $gib -lt 2) {
            if (-not $AllowIntegratedGpu) {
                Write-Host "Integrated AMD GPU ($($props.DriverDesc), $device): not a GPU worker for it."
                continue
            }
            Write-Host "Integrated AMD GPU ($($props.DriverDesc)) accepted (-AllowIntegratedGpu)."
        }
        if (-not $best -or $gib -gt $best.Gib) { $best = @{ Name = "$($props.DriverDesc)"; Gib = $gib } }
    }
    $best
}

$distros = (& wsl.exe -l -q) | ForEach-Object { ($_ -replace "`0", '').Trim() }
if ($distros -notcontains $Distro) {
    throw "$Distro is not installed. Run 'wsl --install -d $Distro', create the user, then run this again."
}

# Agent credentials only: the heartbeat token once the PC has joined
if ($PSCmdlet.ParameterSetName -eq 'Credentials') {
    $creds = Read-Zip $AgentCredentials
    if (-not $creds.Files.coordinator_token) { throw "$AgentCredentials has no coordinator token." }
    $installed = (Get-WslOutput sed -n 's/^NODE_NAME=//p' /etc/idlecloud/node-agent.env)
    if ($installed -ne $creds.NodeName) {
        throw "These credentials are for $($creds.NodeName), but this PC's node agent is '$installed'."
    }
    Write-Host "Installing the agent credentials for $($creds.NodeName)..."
    Initialize-Stage
    try {
        Write-ToStage 'coordinator.token' $creds.Files.coordinator_token
        Invoke-Wsl install -m 600 "$Stage/coordinator.token" /etc/idlecloud/coordinator.token
        if ($creds.Files.kubeconfig) {
            Write-ToStage 'node-agent.kubeconfig' $creds.Files.kubeconfig
            Invoke-Wsl install -m 600 "$Stage/node-agent.kubeconfig" /etc/idlecloud/node-agent.kubeconfig
        }
        Invoke-Wsl systemctl restart idlecloud-node-agent
    } finally {
        & wsl.exe -d $Distro -u root --exec rm -rf $Stage
    }
    Write-Host "Done. The node agent restarted and now sends heartbeats."
    return
}

$setup = $null
if ($PSCmdlet.ParameterSetName -eq 'Bundle') {
    $setup = Read-Zip $Bundle
    $NodeName = $setup.NodeName
    $CoordinatorUrl = $setup.CoordinatorUrl
}

foreach ($path in @($JoinScript, $SensorExe, (Join-Path $AgentDir 'install-service.sh'))) {
    if (-not (Test-Path $path -PathType Leaf)) { throw "File not found: $path" }
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
    if ($setup -and $setup.Files.kubeconfig) {
        Write-ToStage 'node-agent.kubeconfig' $setup.Files.kubeconfig
        $agentArgs += '--kubeconfig', "$Stage/node-agent.kubeconfig"
    }
    if ($setup -and $setup.Files.coordinator_token) {
        Write-ToStage 'coordinator.token' $setup.Files.coordinator_token
        $agentArgs += '--coordinator-token', "$Stage/coordinator.token"
    }
    Invoke-Wsl bash "$Stage/install-service.sh" @agentArgs
} finally {
    & wsl.exe -d $Distro -u root --exec rm -rf $Stage
}

# 4. Join the cluster
if ((Get-WslOutput systemctl is-active k3s-agent) -eq 'active' -and -not $Rejoin) {
    Write-Host 'K3s agent is already running, skipping the join (use -Rejoin to force it).'
} else {
    if ($setup) {
        if (-not $setup.Files.wg_config -or -not $setup.Files.join_token) {
            throw "$Bundle has no wg0.conf or join token. If $NodeName already joined, use -Rejoin only with a new setup zip."
        }
    } elseif (-not $WgConfig -or -not $TokenFile) {
        throw 'Joining needs -Bundle, or -WgConfig and -TokenFile from add-peer.sh on the hub.'
    }
    Write-Host "Joining the cluster as $NodeName..."
    Initialize-Stage
    try {
        Copy-ToStage $JoinScript 'join-worker.sh'
        if ($setup) {
            Write-ToStage 'wg0.conf' $setup.Files.wg_config
            Write-ToStage 'agent-token' $setup.Files.join_token
        } else {
            Copy-ToStage $WgConfig 'wg0.conf'
            Copy-ToStage $TokenFile 'agent-token'
        }
        # GPU settings for join-worker.sh, through env since sudo isn't involved
        $gpuEnv = @("IDLECLOUD_GPU_VENDOR=$GpuVendor")
        if ($AllowIntegratedGpu) { $gpuEnv += 'IDLECLOUD_ALLOW_INTEGRATED_GPU=1' }
        if ($GpuVendor -in 'auto', 'amd') {
            $amd = Get-AmdGpu
            if ($amd) {
                Write-Host "AMD GPU found: $($amd.Name), $($amd.Gib) GB."
                $gpuEnv += 'IDLECLOUD_WSL_GPU=amd', "IDLECLOUD_WSL_GPU_VRAM_GIB=$($amd.Gib)"
            }
        }
        Invoke-Wsl env @gpuEnv bash "$Stage/join-worker.sh" $NodeName "$Stage/wg0.conf" "$Stage/agent-token"
    } finally {
        # The stage holds the WireGuard private key and join token.
        & wsl.exe -d $Distro -u root --exec rm -rf $Stage
    }
}

Start-ScheduledTask -TaskPath $TaskPath -TaskName 'WSL'
Start-ScheduledTask -TaskPath $TaskPath -TaskName 'Sensor'

if ($setup -and $setup.Files.kubeconfig) {
    Write-Host @"

Done. $NodeName should show as joined on the admin pages within a minute.
Then download its agent credentials there and run this again with
-AgentCredentials. Until then the coordinator gets no heartbeats from it and
treats it as offline, so it runs no tasks.
"@
} else {
    Write-Host @"

Done. $NodeName should show as Ready in 'kubectl get nodes' within a minute.
The node gets no tasks until the node agent runs and sees the PC is idle. On an
admin machine, run agent/node-agent/make-kubeconfig.sh $NodeName and copy its
two files to /etc/idlecloud/ (see agent/README.md). The node agent then starts by itself
within 10 seconds, and at every boot after that.
"@
}
