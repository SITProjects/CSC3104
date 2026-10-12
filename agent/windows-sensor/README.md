# Windows sensor

Owner: member 2.

Read keyboard/mouse idle time and host CPU usage on Windows, then report readings
to the Linux node agent over localhost. This folder will also hold sensor
packaging and startup support.

## Build the .exe (developers only)

Lab PCs don't run these steps; they get the .exe through `install-worker.ps1`.

Run in Windows PowerShell:

```powershell
cd agent\windows-sensor
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pyinstaller --onefile --name idlecloud-sensor sensor.py
.\dist\idlecloud-sensor.exe
```
