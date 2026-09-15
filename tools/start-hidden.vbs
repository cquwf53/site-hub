Option Explicit

Dim shell, fileSystem, toolsDir, projectDir, launcher, command, exitCode, dataDir, logsDir, logFile
Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")

toolsDir = fileSystem.GetParentFolderName(WScript.ScriptFullName)
projectDir = fileSystem.GetParentFolderName(toolsDir)
launcher = fileSystem.BuildPath(projectDir, "start.ps1")
dataDir = shell.Environment("Process")("SITEHUB_DATA_DIR")
If Len(Trim(dataDir)) = 0 Then dataDir = fileSystem.BuildPath(projectDir, "data")
shell.CurrentDirectory = projectDir
dataDir = fileSystem.GetAbsolutePathName(dataDir)
logsDir = fileSystem.BuildPath(dataDir, "logs")
EnsureFolder dataDir
EnsureFolder logsDir
logFile = fileSystem.BuildPath(logsDir, "startup.log")

' Startup tasks must not steal focus by opening a browser. Output is retained for diagnosis.
shell.Environment("Process")("SITEHUB_NO_BROWSER") = "1"
command = "cmd.exe /d /s /c ""powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ & launcher & """ >> """ & logFile & """ 2>&1"""

exitCode = shell.Run(command, 0, True)
WScript.Quit exitCode

Sub EnsureFolder(folderPath)
    Dim parentPath
    If fileSystem.FolderExists(folderPath) Then Exit Sub
    parentPath = fileSystem.GetParentFolderName(folderPath)
    If Len(parentPath) > 0 And parentPath <> folderPath Then EnsureFolder parentPath
    fileSystem.CreateFolder folderPath
End Sub
