' Silent launcher for AutoMoGuDingCheckIn scheduler
' - No window is shown
' - Refuses to start a second instance (prevents duplicate clock-ins)
' - Output appended to a daily log file
Option Explicit

Dim wmi, procs, shell, root, py, logFile, cmd

root = "C:\Users\Administrator\AutoMoGuDingCheckIn"
py = root & "\.venv\Scripts\python.exe"

' --- single instance guard ---
Set wmi = GetObject("winmgmts:\\.\root\cimv2")
Set procs = wmi.ExecQuery( _
    "SELECT ProcessId FROM Win32_Process WHERE Name='python.exe' " & _
    "AND CommandLine LIKE '%scheduled_runner.py%'")
If procs.Count > 0 Then WScript.Quit

' --- launch ---
logFile = root & "\logs\run_" & Year(Now) & "-" & _
          Right("0" & Month(Now), 2) & "-" & Right("0" & Day(Now), 2) & ".log"

cmd = "cmd /c """"" & py & """ """ & root & "\scheduled_runner.py"" >> """ & logFile & """ 2>&1"""

Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = root
shell.Run cmd, 0, False
