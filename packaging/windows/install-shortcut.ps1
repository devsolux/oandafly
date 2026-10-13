# Optional current-user Start Menu shortcut for the extracted portable application.
$ErrorActionPreference = 'Stop'
$Executable = Join-Path $PSScriptRoot 'oandafly.exe'
if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) { throw 'Keep this script beside oandafly.exe.' }
$Programs = [Environment]::GetFolderPath('Programs')
$ShortcutPath = Join-Path $Programs 'Oandafly.lnk'
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $Executable
$Shortcut.WorkingDirectory = $PSScriptRoot
$Shortcut.IconLocation = "$Executable,0"
$Shortcut.Description = 'Oandafly — full MaleCNS policy workspace'
$Shortcut.Save()
Write-Output 'Current-user Start Menu shortcut created. Keep the portable application folder in place.'
