' 产品信息自动抓取工具 -- 无窗口启动器
' 双击此文件启动程序，不会弹出黑色命令窗口。
Option Explicit
Dim oFso, oShell, sDir, sBat
Set oFso = CreateObject("Scripting.FileSystemObject")
Set oShell = CreateObject("WScript.Shell")
sDir = oFso.GetParentFolderName(WScript.ScriptFullName)
oShell.CurrentDirectory = sDir
sBat = sDir & "\启动产品信息抓取工具.bat"
If oFso.FileExists(sBat) Then
    oShell.Run """" & sBat & """", 0, False
Else
    MsgBox "未找到启动脚本：" & sBat, vbExclamation, "产品信息自动抓取工具"
End If