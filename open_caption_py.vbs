' Open the Python always-on-top caption window without any extra console window.
' Keep run_live.bat running first. Uses conda env pythonw.exe (GUI subsystem, no console).
Dim shell, fso, scriptDir, pythonw, script
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = "C:\Users\ayore\.conda\envs\live-jp-zh\pythonw.exe"
script = scriptDir & "\caption_window.py"
' Fallback to python.exe if pythonw.exe is missing (a console may then appear)
If Not fso.FileExists(pythonw) Then pythonw = "C:\Users\ayore\.conda\envs\live-jp-zh\python.exe"
' Window style 0 = hidden; False = do not wait for the process to finish
shell.Run """" & pythonw & """ """ & script & """", 0, False
