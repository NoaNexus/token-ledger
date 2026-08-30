Option Explicit

Dim shell, files, base, executable, fallback
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
base = files.GetParentFolderName(WScript.ScriptFullName)
executable = base & "\dist\TokenLedger.exe"
fallback = base & "\native_app.pyw"

If files.FileExists(executable) Then
    shell.Run Chr(34) & executable & Chr(34), 1, False
ElseIf files.FileExists(fallback) Then
    shell.Run Chr(34) & fallback & Chr(34), 1, False
Else
    MsgBox "TokenLedger.exe was not found.", 16, "Token Ledger"
End If
