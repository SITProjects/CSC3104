# Windows sensor

Owner: member 2.

Runs on the Windows host, not inside WSL. Every 5 seconds it reads how long
since the last keyboard or mouse input (`GetLastInputInfo` through `ctypes`)
and the host's CPU load (`psutil`), and POSTs them to the
[node agent](../node-agent/README.md):

```json
{"idle_seconds": 312.4, "cpu_percent": 7.5}
```

The agent replies with its decision, which the sensor shows in the tray. Lab PCs
run it as a single .exe, so they need no Python.

| Variable | Default | Meaning |
|---|---|---|
| `IDLECLOUD_AGENT_URL` | `http://localhost:9100/state` | Where to send readings. WSL2 forwards Windows `localhost` to the agent. |
| `IDLECLOUD_TRAY` | `1` | `0` turns off the tray icon and only logs changes. |

## Lending indicator

A tray icon shows green while the PC is lent out and grey while not (hover for
the reason). Each change is also logged as one line, such as `Lending compute`.
Set `IDLECLOUD_TRAY=0` for the log line only.

Right-click the icon and choose **Quit (stops lending)** to stop the sensor.
With no readings, the agent marks the PC BUSY within 30 seconds.

With no console (the .exe), logs go to `%LOCALAPPDATA%\idlecloud\sensor.log`.
If the sensor hits an unexpected error, the icon turns grey with
"sensor stopped, restart it" and the traceback is in that log.

## On a worker

[`install-worker.ps1`](../README.md#set-up-a-worker) copies the .exe to
`%LOCALAPPDATA%\idlecloud\idlecloud-sensor.exe` and registers the Task Scheduler
task `\IdleCloud\Sensor`, which starts it at every login. To restart it, for
example after quitting it from the tray:

```powershell
Start-ScheduledTask -TaskPath \IdleCloud\ -TaskName Sensor
```

## Build the .exe (developers only)

Lab PCs don't run these steps. `dist/` is not in Git. The Build Windows sensor
workflow builds the .exe whenever this folder changes on `main` and publishes it
to the `sensor-latest` release, with its SHA-256, so usually you only download
it from there. To build it yourself, do it once on a Windows machine and copy
it to each PC. Give `install-worker.ps1` either
this checkout or the built file (`-SensorExe <path>`). Rebuild it after any
change to `sensor.py` or `requirements.txt`, or workers keep running the old
code.

Run in Windows PowerShell:

```powershell
cd agent\windows-sensor
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pyinstaller --onefile --noconsole --hidden-import pystray._win32 --name idlecloud-sensor sensor.py
.\dist\idlecloud-sensor.exe
```

`--noconsole` leaves only the tray icon, with no window for the owner to close
by mistake. `pystray` picks its Windows backend at run time, so PyInstaller
needs the `--hidden-import` to include it.

To run from source instead, with log lines in the console, use
`python sensor.py` in the same venv.
