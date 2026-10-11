' Silent launcher for work-cloud dashboard (read-only local web UI)
' - No window is shown (pythonw + hidden)
' - Refuses to start a second instance
Option Explicit

Dim wmi, procs, shell, root, py, cmd

root = "C:\Users\Administrator\AutoMoGuDingCheckIn"
py = root & "\.venv\Scripts\pythonw.exe"

' --- single instance guard (port 8792 listener OR existing process) ---
Set wmi = GetObject("winmgmts:\\.\root\cimv2")
Set procs = wmi.ExecQuery( _
    "SELECT ProcessId FROM Win32_Process WHERE Name='pythonw.exe' " & _
    "AND CommandLine LIKE '%dashboard%app.py%'")
If procs.Count > 0 Then WScript.Quit

' --- launch (hidden, no window) ---
cmd = """" & py & """ """ & root & "\dashboard\app.py"""

Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = root
shell.Run cmd, 0, False
