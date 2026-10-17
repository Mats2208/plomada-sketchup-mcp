# Restarts the isolated dev SketchUp (port 7884, %LOCALAPPDATA%\Plomada-dev) and waits until it listens.
# Only the process named in Plomada-dev\endpoint.json is ever stopped; the user's SketchUp is never touched.
# A "recovered version" or "file version" prompt in the new instance is dismissed with Escape, posted to that
# dialog's own window.
$ErrorActionPreference = 'Stop'
$home_dev = Join-Path $env:LOCALAPPDATA 'Plomada-dev'
$endpoint = Join-Path $home_dev 'endpoint.json'
if (Test-Path $endpoint) {
  $old = (Get-Content $endpoint | ConvertFrom-Json).pid
  $p = Get-Process -Id $old -ErrorAction SilentlyContinue
  if ($p -and $p.ProcessName -eq 'SketchUp') { Stop-Process -Id $old -Force; Start-Sleep -Seconds 2 }
  Remove-Item $endpoint -Force
}
$before = @(Get-Process SketchUp -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
& cmd.exe /c (Join-Path $PSScriptRoot 'sketchup-dev.cmd')
Add-Type -TypeDefinition @'
using System; using System.Text; using System.Collections.Generic; using System.Runtime.InteropServices;
public class DevWin {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc p, IntPtr l);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern IntPtr PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  public struct RECT { public int L,T,R,B; }
  public static List<IntPtr> Dialogs(uint pid){ var l=new List<IntPtr>(); EnumWindows((h,x)=>{uint p; GetWindowThreadProcessId(h,out p); RECT r; GetWindowRect(h,out r); if(p==pid && IsWindowVisible(h) && (r.R-r.L)<800 && (r.B-r.T)<400) l.Add(h); return true;}, IntPtr.Zero); return l; }
}
'@
for ($i = 0; $i -lt 90; $i++) {
  Start-Sleep -Seconds 2
  if (Test-Path $endpoint) { Get-Content $endpoint; exit 0 }
  $new = Get-Process SketchUp -ErrorAction SilentlyContinue | Where-Object { $before -notcontains $_.Id }
  foreach ($proc in $new) {
    foreach ($h in [DevWin]::Dialogs([uint32]$proc.Id)) {
      [void][DevWin]::PostMessage($h, 0x0100, [IntPtr]0x1B, [IntPtr]0x00010001)
      [void][DevWin]::PostMessage($h, 0x0101, [IntPtr]0x1B, [IntPtr]0xC0010001)
    }
  }
}
throw 'the dev SketchUp did not start listening on 7884'
