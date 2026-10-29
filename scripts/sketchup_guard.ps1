<#
.SYNOPSIS
  Keeps a SketchUp that an agent drives from hanging on a modal dialog.

.DESCRIPTION
  Waits for Plomada's port. While it is closed, it looks for the modal dialogs
  of the SketchUp process: Win32 ones (#32770) and the Qt message boxes
  SketchUp 2025 draws inside its main window (QMessageBox). It answers:
  "the recovered version of this file is newer... open it instead?" with No
  (open the file as saved: an agent never resumes an autosave it did not
  make); any other dialog with one SAFE button (OK, Aceptar, Continue,
  Continuar, Close, Cerrar): the version warning of an old template, "Some
  Extensions Failed to Load". It never answers a save prompt (a dialog with a
  Save / Guardar / Don't Save button): that one stays for the person.

  -Launch starts the isolated development SketchUp (scripts\sketchup-dev.cmd,
  port 7884) and the guard owns that process: when the port never opens it is
  killed and the reason reported. A SketchUp the guard did not start is never
  killed; for it the guard only presses safe buttons and reports.

  Exit codes: 0 port open, 1 port never opened (process killed if owned),
  2 a dialog needs a person.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\sketchup_guard.ps1 -Launch
  powershell -ExecutionPolicy Bypass -File scripts\sketchup_guard.ps1 -Port 7883 -TimeoutSec 30
#>
param(
    [int]$Port = 7884,
    [int]$TimeoutSec = 120,
    [switch]$Launch
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$Repo = Split-Path -Parent $PSScriptRoot
$Safe = @('OK', 'Aceptar', 'Continue', 'Continuar', 'Close', 'Cerrar')
$SavePrompt = @('Save', 'Guardar', "Don't Save", 'No guardar')

function Port-Open { Test-NetConnection 127.0.0.1 -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue }

function Dialogs([int[]]$Ids) {
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $class = [System.Windows.Automation.AutomationElement]::ClassNameProperty
    $win32 = New-Object System.Windows.Automation.PropertyCondition $class, '#32770'
    $qt = New-Object System.Windows.Automation.PropertyCondition $class, 'QMessageBox'
    $any = New-Object System.Windows.Automation.OrCondition $win32, $qt
    $root.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition) |
        Where-Object { $Ids -contains $_.Current.ProcessId } |
        ForEach-Object { $_.FindAll([System.Windows.Automation.TreeScope]::Subtree, $any) }
}

function Answer($dialog) {
    $btnCond = New-Object System.Windows.Automation.PropertyCondition ([System.Windows.Automation.AutomationElement]::ControlTypeProperty), ([System.Windows.Automation.ControlType]::Button)
    $buttons = $dialog.FindAll([System.Windows.Automation.TreeScope]::Descendants, $btnCond)
    $names = @($buttons | ForEach-Object { $_.Current.Name.Trim('&') })
    $title = $dialog.Current.Name
    if ($title -match 'recovered version|versión recuperada') {
        $no = $buttons | Where-Object { @('No') -contains $_.Current.Name.Trim('&') } | Select-Object -First 1
        if ($no) {
            $no.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
            Write-Host "  dialog '$($title -replace '\s+', ' ')': pressed No (open the saved file, not the autosave)"
            return $true
        }
    }
    if ($names | Where-Object { $SavePrompt -contains $_ }) {
        Write-Host "  dialog '$title' asks to save ($($names -join ', ')): left for a person"
        return $false
    }
    foreach ($b in $buttons) {
        if ($Safe -contains $b.Current.Name.Trim('&')) {
            $b.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
            Write-Host "  dialog '$title': pressed $($b.Current.Name)"
            return $true
        }
    }
    Write-Host "  dialog '$title' has no safe button ($($names -join ', ')): left for a person"
    $false
}

$owned = $null
if ($Launch) {
    $before = @(Get-Process SketchUp -ErrorAction SilentlyContinue | ForEach-Object Id)
    $home_ = Join-Path $env:LOCALAPPDATA 'Plomada-dev'
    New-Item -ItemType Directory -Force $home_ | Out-Null
    # A model this SketchUp saved itself opens without the version warning; until one exists the
    # guard answers that warning below.
    Start-Process cmd.exe -ArgumentList '/c', (Join-Path $Repo 'scripts\sketchup-dev.cmd') -WindowStyle Minimized
    for ($i = 0; $i -lt 30 -and -not $owned; $i++) {
        Start-Sleep 1
        $owned = Get-Process SketchUp -ErrorAction SilentlyContinue | Where-Object { $before -notcontains $_.Id } | Select-Object -First 1
    }
    if (-not $owned) { Write-Host "SketchUp did not start"; exit 1 }
    Write-Host "started SketchUp pid $($owned.Id) for port $Port"
}

$deadline = (Get-Date).AddSeconds($TimeoutSec)
$stuck = $false
while ((Get-Date) -lt $deadline) {
    if (Port-Open) { Write-Host "port $Port open"; exit 0 }
    $ids = if ($owned) { @($owned.Id) } else { @(Get-Process SketchUp -ErrorAction SilentlyContinue | ForEach-Object Id) }
    foreach ($d in (Dialogs $ids)) { if (-not (Answer $d)) { $stuck = $true } }
    Start-Sleep 2
}

if ($owned) {
    Stop-Process -Id $owned.Id -Force
    Write-Host "port $Port never opened in $TimeoutSec s: killed SketchUp pid $($owned.Id), which this guard started"
    exit 1
}
if ($stuck) { Write-Host "port $Port closed and a dialog needs a person"; exit 2 }
Write-Host "port $Port never opened in $TimeoutSec s (SketchUp not started by this guard: left running)"
exit 1
