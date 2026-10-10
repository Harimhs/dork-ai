' Starts Dork in the background on Windows (no window). Output goes to data\dork.log.
' A copy of this file in the Windows Startup folder starts Dork at login (run.ps1 -Autostart sets that up).
' Dork starts its own backup brain (Ollama) and C++ engine, so nothing else needs launching here.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = here
Set env = sh.Environment("PROCESS")
env("PYTHONIOENCODING") = "utf-8"
env("PYTHONUNBUFFERED") = "1"
If Not fso.FolderExists(here & "\data") Then fso.CreateFolder(here & "\data")
sh.Run "cmd /c "".venv\Scripts\python.exe"" -m dork >> data\dork.log 2>&1", 0, False
