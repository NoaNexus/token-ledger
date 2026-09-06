Option Explicit

Dim shell, files, base, scriptFile
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
base = files.GetParentFolderName(WScript.ScriptFullName)
scriptFile = base & "\native_app.pyw"

If files.FileExists(scriptFile) Then
    shell.Run "pythonw.exe """ & scriptFile & """", 0, False
Else
    shell.Run "python.exe """ & base & "\run.py""", 0, False
End If
