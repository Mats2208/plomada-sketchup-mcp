# Opens a model in the isolated dev SketchUp (port 7884): the current model is saved first to
# %LOCALAPPDATA%\Plomada-dev\<SaveAs> (when given), then the file opens through a UI timer. Prompts the dev
# instance raises meanwhile are answered by UI Automation in that process's own windows only: "Purge Unused?"
# and "Save changes" get No, anything else Escape. The user's SketchUp is never touched.
#   powershell -File scripts/dev_open.ps1 -Path <model.skp> [-SaveAs generado.skp]
param([Parameter(Mandatory = $true)][string]$Path, [string]$SaveAs = '')
$ErrorActionPreference = 'Continue'  # dev.py prints a traceback while SketchUp is busy loading
$root = Split-Path $PSScriptRoot -Parent
$devpid = (Get-Content (Join-Path $env:LOCALAPPDATA 'Plomada-dev\endpoint.json') | ConvertFrom-Json).pid
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
Add-Type -TypeDefinition @'
using System; using System.Collections.Generic; using System.Runtime.InteropServices;
public class DevDlg {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc p, IntPtr l);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern IntPtr PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  public struct RECT { public int L,T,R,B; }
  public static List<IntPtr> Small(uint pid){ var l=new List<IntPtr>(); EnumWindows((h,x)=>{uint p; GetWindowThreadProcessId(h,out p); RECT r; GetWindowRect(h,out r); if(p==pid && IsWindowVisible(h) && (r.R-r.L)<900 && (r.B-r.T)<700) l.Add(h); return true;}, IntPtr.Zero); return l; }
}
'@
function Answer([IntPtr]$h) {
  $el = [System.Windows.Automation.AutomationElement]::FromHandle($h)
  $names = @($el.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition) | ForEach-Object { $_.Current.Name })
  $no = $el.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, 'No')))
  if ($no -and (($names -match 'Purge|Save changes').Count -gt 0)) {
    $no.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke(); return
  }
  [void][DevDlg]::PostMessage($h, 0x0100, [IntPtr]0x1B, [IntPtr]0x00010001)
  [void][DevDlg]::PostMessage($h, 0x0101, [IntPtr]0x1B, [IntPtr]0xC0010001)
}
$save = if ($SaveAs) { "Sketchup.active_model.save(File.join(ENV['LOCALAPPDATA'], 'Plomada-dev', '$SaveAs')); " } else { '' }
$file = (Resolve-Path $Path).Path.Replace('\', '/')
$code = "UI.start_timer(0.5, false) { ${save}Sketchup.open_file('$file') }; :scheduled"
Push-Location $root
& uv run python scripts/dev.py ruby $code | Out-Null
$want = [IO.Path]::GetFileNameWithoutExtension($file)
for ($i = 0; $i -lt 120; $i++) {
  Start-Sleep -Seconds 2
  foreach ($h in [DevDlg]::Small([uint32]$devpid)) { Answer $h }
  $title = (& uv run python scripts/dev.py ruby 'Sketchup.active_model.title' 2>$null | Select-String '"result"') -replace '.*"result": "\\?"?([^"\\]*).*', '$1'
  if ($title -eq $want) { Pop-Location; "open: $want"; exit 0 }
}
Pop-Location
throw "the dev SketchUp did not open $file"
