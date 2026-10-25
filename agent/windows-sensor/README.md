# Windows sensor

Owner: member 2.

Read keyboard/mouse idle time and host CPU usage on Windows, then report readings
to the Linux node agent over localhost. This folder will also hold sensor
packaging and startup support.

## Lending indicator

A tray icon shows green while the PC is lent out and grey while not (hover for
the reason). Each change is also logged as one line, such as `Lending compute`.
Set `IDLECLOUD_TRAY=0` for the log line only.

With no console (the .exe), logs go to `%LOCALAPPDATA%\idlecloud\sensor.log`.
If the sensor hits an unexpected error, the icon turns grey with
"sensor stopped, restart it" and the traceback is in that log.

## Build the .exe (developers only)

Lab PCs don't run these steps; they get the .exe through `install-worker.ps1`.

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
