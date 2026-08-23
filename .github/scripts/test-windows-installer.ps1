$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

public static class KiroInstallerNative {
  [StructLayout(LayoutKind.Sequential)]
  public struct Rect {
    public int Left;
    public int Top;
    public int Right;
    public int Bottom;
  }

  [StructLayout(LayoutKind.Sequential)]
  public struct MonitorInfo {
    public int Size;
    public Rect Monitor;
    public Rect Work;
    public int Flags;
  }

  [DllImport("user32.dll")]
  public static extern bool GetWindowRect(IntPtr handle, out Rect rect);

  [DllImport("user32.dll")]
  public static extern IntPtr GetDlgItem(IntPtr parent, int id);

  [DllImport("user32.dll")]
  public static extern IntPtr SendMessage(
    IntPtr handle,
    uint message,
    IntPtr wParam,
    IntPtr lParam
  );

  [DllImport("user32.dll", CharSet = CharSet.Unicode, EntryPoint = "SendMessageW")]
  public static extern IntPtr SendMessageString(
    IntPtr handle,
    uint message,
    IntPtr wParam,
    string lParam
  );

  [DllImport("user32.dll")]
  public static extern bool PostMessage(
    IntPtr handle,
    uint message,
    IntPtr wParam,
    IntPtr lParam
  );

  [DllImport("user32.dll", CharSet = CharSet.Unicode)]
  public static extern bool SetWindowText(IntPtr handle, string text);

  [DllImport("user32.dll", CharSet = CharSet.Unicode)]
  private static extern int GetWindowText(
    IntPtr handle,
    StringBuilder text,
    int capacity
  );

  [DllImport("user32.dll")]
  public static extern int GetWindowLong(IntPtr handle, int index);

  [DllImport("user32.dll")]
  public static extern IntPtr GetParent(IntPtr handle);

  [DllImport("user32.dll")]
  public static extern IntPtr GetAncestor(IntPtr handle, uint flags);

  [DllImport("user32.dll")]
  public static extern bool SetForegroundWindow(IntPtr handle);

  [DllImport("user32.dll")]
  private static extern bool SetCursorPos(int x, int y);

  [DllImport("user32.dll")]
  private static extern void mouse_event(
    uint flags,
    uint dx,
    uint dy,
    uint data,
    UIntPtr extraInfo
  );

  [DllImport("user32.dll")]
  public static extern int GetDlgCtrlID(IntPtr handle);

  [DllImport("user32.dll", CharSet = CharSet.Unicode)]
  private static extern int GetClassName(
    IntPtr handle,
    StringBuilder className,
    int capacity
  );

  public delegate bool EnumWindowsProc(IntPtr handle, IntPtr data);

  [DllImport("user32.dll")]
  private static extern bool EnumChildWindows(
    IntPtr parent,
    EnumWindowsProc callback,
    IntPtr data
  );

  [DllImport("user32.dll")]
  public static extern IntPtr GetLastActivePopup(IntPtr owner);

  [DllImport("user32.dll")]
  public static extern bool IsWindowVisible(IntPtr handle);

  [DllImport("user32.dll")]
  public static extern bool IsWindowEnabled(IntPtr handle);

  [DllImport("user32.dll")]
  public static extern uint GetDpiForWindow(IntPtr handle);

  [DllImport("user32.dll")]
  public static extern IntPtr MonitorFromWindow(IntPtr handle, uint flags);

  [DllImport("user32.dll")]
  public static extern bool GetMonitorInfo(IntPtr monitor, ref MonitorInfo info);

  public static string ReadText(IntPtr handle) {
    StringBuilder text = new StringBuilder(4096);
    GetWindowText(handle, text, text.Capacity);
    return text.ToString();
  }

  public static void ClickPoint(int x, int y) {
    if (!SetCursorPos(x, y)) {
      throw new InvalidOperationException("Could not position the test pointer");
    }
    mouse_event(0x0002, 0, 0, 0, UIntPtr.Zero);
    mouse_event(0x0004, 0, 0, 0, UIntPtr.Zero);
  }

  public static IntPtr[] FindControls(
    IntPtr parent,
    string expectedClass,
    int styleMask,
    int expectedStyle
  ) {
    List<IntPtr> controls = new List<IntPtr>();
    EnumChildWindows(parent, delegate (IntPtr handle, IntPtr data) {
      StringBuilder className = new StringBuilder(64);
      GetClassName(handle, className, className.Capacity);
      int style = GetWindowLong(handle, -16);
      if (
        String.Equals(
          className.ToString(),
          expectedClass,
          StringComparison.OrdinalIgnoreCase
        ) &&
        (styleMask == 0 || (style & styleMask) == expectedStyle)
      ) {
        controls.Add(handle);
      }
      return true;
    }, IntPtr.Zero);
    return controls.ToArray();
  }

}
'@

function Get-KiroRoot {
  param([System.Diagnostics.Process]$Process)

  $Process.Refresh()
  if ($Process.HasExited -or $Process.MainWindowHandle -eq 0) {
    throw 'The installer window is no longer available'
  }
  return [System.Windows.Automation.AutomationElement]::FromHandle(
    $Process.MainWindowHandle
  )
}

function Get-KiroNativeControls {
  param(
    [System.Diagnostics.Process]$Process,
    [string]$ClassName,
    [int]$StyleMask = 0,
    [int]$ExpectedStyle = 0
  )

  return @(
    [KiroInstallerNative]::FindControls(
      $Process.MainWindowHandle,
      $ClassName,
      $StyleMask,
      $ExpectedStyle
    )
  )
}

function Wait-KiroWindow {
  param([System.Diagnostics.Process]$Process, [string]$Stage)

  for ($attempt = 0; $attempt -lt 120; $attempt++) {
    Start-Sleep -Milliseconds 250
    $Process.Refresh()
    if ($Process.HasExited) {
      throw "Installer exited before showing the $Stage window"
    }
    if ($Process.MainWindowHandle -ne 0) {
      return Get-KiroRoot $Process
    }
  }
  throw "$Stage installer window did not appear"
}

function Wait-KiroControls {
  param(
    [System.Diagnostics.Process]$Process,
    [string]$ClassName,
    [int]$StyleMask,
    [int]$ExpectedStyle,
    [int]$Minimum,
    [string]$Stage
  )

  for ($attempt = 0; $attempt -lt 120; $attempt++) {
    $Process.Refresh()
    if ($Process.HasExited) {
      throw "Installer exited while waiting for $Stage"
    }
    $controls = @(
      Get-KiroNativeControls $Process $ClassName $StyleMask $ExpectedStyle
    )
    if ($controls.Count -ge $Minimum) {
      return $controls
    }
    Start-Sleep -Milliseconds 100
  }
  throw "$Stage did not expose $Minimum native $ClassName controls"
}

function Get-KiroNativeRect {
  param([IntPtr]$Handle)

  $rect = New-Object KiroInstallerNative+Rect
  if (-not [KiroInstallerNative]::GetWindowRect($Handle, [ref]$rect)) {
    throw "Could not read bounds for native control $Handle"
  }
  return [pscustomobject]@{
    Left = $rect.Left
    Top = $rect.Top
    Right = $rect.Right
    Bottom = $rect.Bottom
    Width = $rect.Right - $rect.Left
    Height = $rect.Bottom - $rect.Top
  }
}

function Get-KiroElementRect {
  param([IntPtr]$Handle)

  return Get-KiroNativeRect $Handle
}

function Assert-KiroNoOverlap {
  param($First, $Second, [string]$Message)

  $overlaps = (
    $First.Left -lt $Second.Right -and
    $First.Right -gt $Second.Left -and
    $First.Top -lt $Second.Bottom -and
    $First.Bottom -gt $Second.Top
  )
  if ($overlaps) { throw $Message }
}

function Assert-KiroWindow {
  param([System.Diagnostics.Process]$Process, [string]$Stage)

  $handle = $Process.MainWindowHandle
  $rect = Get-KiroNativeRect $handle
  $dpi = [KiroInstallerNative]::GetDpiForWindow($handle)
  if ($dpi -eq 0) { $dpi = 96 }
  $logicalWidth = $rect.Width * 96 / $dpi
  $logicalHeight = $rect.Height * 96 / $dpi
  if (
    $logicalWidth -lt 700 -or $logicalWidth -gt 1282 -or
    $logicalHeight -lt 470 -or $logicalHeight -gt 862
  ) {
    throw "$Stage is not the fitted custom installer: $([int]$logicalWidth)x$([int]$logicalHeight)"
  }
  $aspectError = [Math]::Abs(($rect.Width / $rect.Height) - (1280 / 860))
  if ($aspectError -gt 0.01) {
    throw "$Stage does not preserve the 1280x860 scene aspect ratio"
  }
  $style = [KiroInstallerNative]::GetWindowLong($handle, -16)
  if (($style -band 0x00100000) -ne 0 -or ($style -band 0x00200000) -ne 0) {
    throw "$Stage unexpectedly exposes a scrollable outer window"
  }

  $monitor = [KiroInstallerNative]::MonitorFromWindow($handle, 2)
  $info = New-Object KiroInstallerNative+MonitorInfo
  $info.Size = [Runtime.InteropServices.Marshal]::SizeOf($info)
  if (-not [KiroInstallerNative]::GetMonitorInfo($monitor, [ref]$info)) {
    throw "Could not read the monitor work area for $Stage"
  }
  if (
    $rect.Left -lt $info.Work.Left -or $rect.Top -lt $info.Work.Top -or
    $rect.Right -gt $info.Work.Right -or $rect.Bottom -gt $info.Work.Bottom
  ) {
    throw "$Stage extends outside the monitor work area"
  }
  return $dpi
}

function Get-KiroVisibleControls {
  param(
    [System.Diagnostics.Process]$Process,
    [string]$ClassName,
    [int]$StyleMask = 0,
    [int]$ExpectedStyle = 0
  )
  return @(
    Get-KiroNativeControls $Process $ClassName $StyleMask $ExpectedStyle |
      Where-Object { [KiroInstallerNative]::IsWindowVisible($_) }
  )
}

function Get-KiroOptionsControls {
  param([System.Diagnostics.Process]$Process)

  [void](Wait-KiroControls $Process 'ComboBox' 0 0 1 'Custom options page')
  [void](Wait-KiroControls $Process 'Edit' 0 0 1 'Custom options page')
  $combos = @(Get-KiroVisibleControls $Process 'ComboBox')
  $edits = @(Get-KiroVisibleControls $Process 'Edit')
  $checks = @(
    Get-KiroVisibleControls $Process 'Button' 0xF 3 |
      Sort-Object { (Get-KiroNativeRect $_).Top }
  )
  $buttons = @(Get-KiroVisibleControls $Process 'Button')
  $browse = @()
  if ($edits.Count -eq 1) {
    $locationRect = Get-KiroNativeRect $edits[0]
    $browse = @($buttons | Where-Object {
      $rect = Get-KiroNativeRect $_
      [Math]::Abs($rect.Top - $locationRect.Top) -le 4 -and
        $rect.Left -ge $locationRect.Right
    })
  }
  $install = @($buttons | Where-Object {
    [KiroInstallerNative]::ReadText($_) -eq 'Install Kiro Crew'
  })
  $staticLabels = @(Get-KiroVisibleControls $Process 'Static')
  $desktopLabel = @()
  $startupLabel = @()
  if ($checks.Count -eq 2) {
    $desktopRect = Get-KiroNativeRect $checks[0]
    $startupRect = Get-KiroNativeRect $checks[1]
    $desktopLabel = @($staticLabels | Where-Object {
      $rect = Get-KiroNativeRect $_
      [Math]::Abs($rect.Top - $desktopRect.Top) -le 4 -and
        $rect.Left -gt $desktopRect.Left -and
        [KiroInstallerNative]::ReadText($_)
    })
    $startupLabel = @($staticLabels | Where-Object {
      $rect = Get-KiroNativeRect $_
      [Math]::Abs($rect.Top - $startupRect.Top) -le 4 -and
        $rect.Left -gt $startupRect.Left -and
        [KiroInstallerNative]::ReadText($_)
    })
  }
  $exit = @(
    foreach ($className in @('Button', 'Static')) {
      Get-KiroVisibleControls $Process $className | Where-Object {
        [KiroInstallerNative]::ReadText($_) -like 'Exit setup*'
      }
    }
  )
  if (
    $combos.Count -ne 1 -or $edits.Count -ne 1 -or
    $checks.Count -ne 2 -or $browse.Count -ne 1 -or
    $install.Count -ne 1 -or $desktopLabel.Count -ne 1 -or
    $startupLabel.Count -ne 1 -or $exit.Count -ne 1
  ) {
    throw (
      'Custom options page controls: ' +
      "combo=$($combos.Count), edit=$($edits.Count), checks=$($checks.Count), " +
      "browse=$($browse.Count), install=$($install.Count), " +
      "desktop-label=$($desktopLabel.Count), startup-label=$($startupLabel.Count), " +
      "exit=$($exit.Count)"
    )
  }
  return [pscustomobject]@{
    Scope = $combos[0]
    Location = $edits[0]
    Browse = $browse[0]
    Desktop = $checks[0]
    DesktopLabel = $desktopLabel[0]
    Startup = $checks[1]
    StartupLabel = $startupLabel[0]
    Install = $install[0]
    Exit = $exit[0]
  }
}

function Assert-KiroOptionsLayout {
  param([System.Diagnostics.Process]$Process)

  $controls = Get-KiroOptionsControls $Process
  $window = Get-KiroNativeRect $Process.MainWindowHandle
  $scope = Get-KiroElementRect $controls.Scope
  $location = Get-KiroElementRect $controls.Location
  $browse = Get-KiroElementRect $controls.Browse
  $desktop = Get-KiroElementRect $controls.Desktop
  $desktopLabel = Get-KiroElementRect $controls.DesktopLabel
  $startup = Get-KiroElementRect $controls.Startup
  $startupLabel = Get-KiroElementRect $controls.StartupLabel
  $install = Get-KiroElementRect $controls.Install
  Assert-KiroNoOverlap $location $browse 'Install location overlaps Browse'
  Assert-KiroNoOverlap $desktop $startup 'Installer option checkboxes overlap'
  Assert-KiroNoOverlap $desktopLabel $startupLabel 'Installer option labels overlap'
  Assert-KiroNoOverlap $startupLabel $install 'Startup option overlaps Install action'
  if ([Math]::Abs($location.Top - $browse.Top) -gt 4 -or [Math]::Abs($location.Bottom - $browse.Bottom) -gt 4) {
    throw 'Install location and Browse are not vertically aligned'
  }
  if ([Math]::Abs($desktop.Left - $startup.Left) -gt 4) {
    throw 'Installer option checkboxes are not left-aligned'
  }
  if (
    [Math]::Abs($desktop.Top - $desktopLabel.Top) -gt 4 -or
    [Math]::Abs($startup.Top - $startupLabel.Top) -gt 4
  ) {
    throw 'Installer option glyphs and labels are not vertically aligned'
  }
  foreach ($rect in @(
    $scope, $location, $browse, $desktop, $desktopLabel,
    $startup, $startupLabel, $install
  )) {
    if (
      $rect.Left -lt $window.Left -or $rect.Top -lt $window.Top -or
      $rect.Right -gt $window.Right -or $rect.Bottom -gt $window.Bottom
    ) {
      throw 'A custom option is clipped outside the fitted installer window'
    }
    if ($rect.Top -lt ($window.Top + ($window.Height * 0.64))) {
      throw 'A custom option escaped the lower glass panel'
    }
  }
  $dpi = [KiroInstallerNative]::GetDpiForWindow($Process.MainWindowHandle)
  if ($dpi -eq 0) { $dpi = 96 }
  if (($browse.Width * 96 / $dpi) -lt 70) {
    throw 'Browse button is too narrow for localized labels'
  }
  foreach ($dialog in @(Get-KiroVisibleControls $Process '#32770')) {
    $style = [KiroInstallerNative]::GetWindowLong($dialog, -16)
    if (($style -band 0x00100000) -ne 0 -or ($style -band 0x00200000) -ne 0) {
      throw 'The custom options page unexpectedly requires scrolling'
    }
  }
  return $controls
}

function Invoke-KiroElement {
  param([IntPtr]$Handle)

  if ($Handle -eq [IntPtr]::Zero) {
    throw 'Cannot invoke a control without a native HWND'
  }

  # Post BN_CLICKED to this control's own parent. Unlike BM_CLICK, this remains
  # reliable when the dialog is not active; unlike UIA Invoke(), it cannot
  # block this test thread behind a modal validation dialog.
  $parent = [KiroInstallerNative]::GetParent($Handle)
  $controlId = [KiroInstallerNative]::GetDlgCtrlID($Handle)
  if ($parent -eq [IntPtr]::Zero -or $controlId -le 0) {
    throw "Could not address native control $Handle"
  }
  $command = [IntPtr]($controlId -band 0xFFFF)
  if (-not [KiroInstallerNative]::PostMessage(
    $parent,
    0x0111,
    $command,
    $Handle
  )) {
    throw "Could not invoke native control $Handle"
  }
}

function Set-KiroValue {
  param(
    [IntPtr]$Handle,
    [string]$Value
  )

  $element = [System.Windows.Automation.AutomationElement]::FromHandle($Handle)
  $pattern = $null
  if ($element.TryGetCurrentPattern(
    [System.Windows.Automation.ValuePattern]::Pattern,
    [ref]$pattern
  )) {
    ([System.Windows.Automation.ValuePattern]$pattern).SetValue($Value)
  } elseif (-not [KiroInstallerNative]::SetWindowText($Handle, $Value)) {
    throw 'Could not set the native install-location field'
  }
  # UIA's legacy Win32 proxy and WM_SETTEXT can omit EN_CHANGE. Replace the
  # edit selection through its exact HWND so the control emits the same change
  # notification as typed input, without global keyboard or focus state.
  [void][KiroInstallerNative]::SendMessage(
    $Handle,
    0x00B1,
    [IntPtr]::Zero,
    [IntPtr](-1)
  )
  [void][KiroInstallerNative]::SendMessageString(
    $Handle,
    0x00C2,
    [IntPtr]1,
    $Value
  )
}

function Get-KiroValue {
  param([IntPtr]$Handle)

  # The legacy Win32 UIA proxy can return its pre-disable cache after the
  # all-users callback changes this edit and immediately disables it. Read the
  # exact HWND so assertions observe the value NSIS has just painted.
  return [KiroInstallerNative]::ReadText($Handle)
}

function Set-KiroToggle {
  param(
    [IntPtr]$Handle,
    [bool]$Enabled
  )

  $element = [System.Windows.Automation.AutomationElement]::FromHandle($Handle)
  $pattern = $null
  if ($element.TryGetCurrentPattern(
    [System.Windows.Automation.TogglePattern]::Pattern,
    [ref]$pattern
  )) {
    $isEnabled = (
      ([System.Windows.Automation.TogglePattern]$pattern).Current.ToggleState -eq
      [System.Windows.Automation.ToggleState]::On
    )
    if ($isEnabled -ne $Enabled) {
      ([System.Windows.Automation.TogglePattern]$pattern).Toggle()
    }
    return
  }
  $isEnabled = (
    [KiroInstallerNative]::SendMessage(
      $Handle,
      0x00F0,
      [IntPtr]::Zero,
      [IntPtr]::Zero
    ).ToInt32() -eq 1
  )
  if ($isEnabled -ne $Enabled) {
    [void][KiroInstallerNative]::SendMessage(
      $Handle,
      0x00F1,
      [IntPtr][int]$Enabled,
      [IntPtr]::Zero
    )
    Invoke-KiroElement $Handle
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
      Start-Sleep -Milliseconds 25
      $isEnabled = (
        [KiroInstallerNative]::SendMessage(
          $Handle,
          0x00F0,
          [IntPtr]::Zero,
          [IntPtr]::Zero
        ).ToInt32() -eq 1
      )
      if ($isEnabled -eq $Enabled) { return }
    }
    throw "Native checkbox $Handle did not reach state '$Enabled'"
  }
}

function Get-KiroToggle {
  param([IntPtr]$Handle)

  $element = [System.Windows.Automation.AutomationElement]::FromHandle($Handle)
  $pattern = $null
  if ($element.TryGetCurrentPattern(
    [System.Windows.Automation.TogglePattern]::Pattern,
    [ref]$pattern
  )) {
    return (
      ([System.Windows.Automation.TogglePattern]$pattern).Current.ToggleState -eq
      [System.Windows.Automation.ToggleState]::On
    )
  }
  return [KiroInstallerNative]::SendMessage(
    $Handle,
    0x00F0,
    [IntPtr]::Zero,
    [IntPtr]::Zero
  ).ToInt32() -eq 1
}

function Set-KiroScope {
  param(
    [IntPtr]$Handle,
    [ValidateSet(0, 1)][int]$Index
  )

  # Select the same popup-list item a user does. CB_SETCURSEL plus a synthetic
  # WM_COMMAND reaches the callback, but the legacy ComboBox can retain its old
  # edit text during that re-entrant notification on hosted runners.
  $element = [System.Windows.Automation.AutomationElement]::FromHandle($Handle)
  $expandPattern = $null
  if ($element.TryGetCurrentPattern(
    [System.Windows.Automation.ExpandCollapsePattern]::Pattern,
    [ref]$expandPattern
  )) {
    ([System.Windows.Automation.ExpandCollapsePattern]$expandPattern).Expand()
    $items = $null
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
      $items = $element.FindAll(
        [System.Windows.Automation.TreeScope]::Subtree,
        (New-Object System.Windows.Automation.PropertyCondition(
          [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
          [System.Windows.Automation.ControlType]::ListItem
        ))
      )
      if ($items.Count -gt $Index) { break }
      Start-Sleep -Milliseconds 25
    }
    if ($items.Count -le $Index) {
      throw "Install scope popup did not expose index $Index"
    }
    $itemBounds = $items[$Index].Current.BoundingRectangle
    if ($itemBounds.IsEmpty -or $itemBounds.Width -le 0 -or $itemBounds.Height -le 0) {
      throw "Install scope index $Index has no clickable bounds"
    }
    $root = [KiroInstallerNative]::GetAncestor($Handle, 2)
    if ($root -eq [IntPtr]::Zero) {
      throw "Install scope control $Handle has no root window"
    }
    [void][KiroInstallerNative]::SetForegroundWindow($root)
    [KiroInstallerNative]::ClickPoint(
      [int]($itemBounds.Left + ($itemBounds.Width / 2)),
      [int]($itemBounds.Top + ($itemBounds.Height / 2))
    )
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
      $selected = [KiroInstallerNative]::SendMessage(
        $Handle,
        0x0147,
        [IntPtr]::Zero,
        [IntPtr]::Zero
      ).ToInt32()
      if ($selected -eq $Index) { return }
      Start-Sleep -Milliseconds 25
    }
    throw "Install scope did not select index $Index"
  }

  $selected = [KiroInstallerNative]::SendMessage(
    $Handle,
    0x014E,
    [IntPtr]$Index,
    [IntPtr]::Zero
  ).ToInt32()
  if ($selected -ne $Index) {
    throw "Could not select install scope index $Index"
  }
  $parent = [KiroInstallerNative]::GetParent($Handle)
  $controlId = [KiroInstallerNative]::GetDlgCtrlID($Handle)
  if ($parent -eq [IntPtr]::Zero -or $controlId -le 0) {
    throw "Could not address install scope control $Handle"
  }
  $command = [IntPtr]((1 -shl 16) -bor ($controlId -band 0xFFFF))
  # NSIS dispatches nsDialogs callbacks while handling WM_COMMAND. Send the
  # notification synchronously so the callback has completed before state is
  # inspected, matching a real selection change without relying on focus.
  [void][KiroInstallerNative]::SendMessage(
    $parent,
    0x0111,
    $command,
    $Handle
  )
}

function Wait-KiroScopeState {
  param(
    [IntPtr]$Location,
    [IntPtr]$Browse,
    [bool]$Enabled
  )

  for ($attempt = 0; $attempt -lt 100; $attempt++) {
    $controlsReady = (
      [KiroInstallerNative]::IsWindowEnabled($Location) -eq $Enabled -and
      [KiroInstallerNative]::IsWindowEnabled($Browse) -eq $Enabled
    )
    if ($controlsReady) { return }
    Start-Sleep -Milliseconds 50
  }
  throw (
    "Install scope did not reach enabled='$Enabled'; " +
    "location-enabled=$([KiroInstallerNative]::IsWindowEnabled($Location)), " +
    "browse-enabled=$([KiroInstallerNative]::IsWindowEnabled($Browse))"
  )
}

function Save-KiroWindow {
  param([IntPtr]$Handle, [string]$Path)

  $rect = Get-KiroNativeRect $Handle
  $bitmap = New-Object System.Drawing.Bitmap $rect.Width, $rect.Height
  $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
  try {
    $graphics.CopyFromScreen($rect.Left, $rect.Top, 0, 0, $bitmap.Size)
    $bitmap.Save($Path)
  } finally {
    $graphics.Dispose()
    $bitmap.Dispose()
  }
}

function Assert-KiroInvalidLocation {
  param([System.Diagnostics.Process]$Process, [string]$Screenshot)

  $owner = $Process.MainWindowHandle
  $popup = [IntPtr]::Zero
  for ($attempt = 0; $attempt -lt 100; $attempt++) {
    $popup = [KiroInstallerNative]::GetLastActivePopup($owner)
    if (
      $popup -ne [IntPtr]::Zero -and $popup -ne $owner -and
      [KiroInstallerNative]::IsWindowVisible($popup)
    ) {
      break
    }
    Start-Sleep -Milliseconds 100
  }
  if ($popup -eq [IntPtr]::Zero -or $popup -eq $owner) {
    throw 'Invalid install location did not open an error dialog'
  }

  Save-KiroWindow $popup $Screenshot
  $message = (
    [KiroInstallerNative]::FindControls($popup, 'Static', 0, 0) |
      ForEach-Object { [KiroInstallerNative]::ReadText($_) }
  ) -join ' '
  if ($message -notlike '*Choose a valid folder for Kiro Crew.*') {
    throw "Invalid-location dialog was not localized: '$message'"
  }
  $button = [KiroInstallerNative]::FindControls(
    $popup,
    'Button',
    0,
    0
  ) | Select-Object -First 1
  if (-not $button) { throw 'Invalid-location dialog has no dismiss button' }
  Invoke-KiroElement $button
}

function Wait-KiroFinishPage {
  param(
    [System.Diagnostics.Process]$Process,
    [System.Diagnostics.Stopwatch]$InstallTimer,
    [int]$MaxSeconds = 60
  )

  $sawProgress = $false
  $lastProgressPosition = -1
  $lastProgressChange = [DateTime]::UtcNow
  $maxAttempts = $MaxSeconds * 20
  for ($attempt = 0; $attempt -lt $maxAttempts; $attempt++) {
    if ($Process.HasExited) {
      throw "Installer exited before its finish page with code $($Process.ExitCode)"
    }
    $progress = @(
      Get-KiroVisibleControls $Process 'msctls_progress32'
    )
    $customExit = @(
      Get-KiroVisibleControls $Process 'Static' |
        Where-Object { [KiroInstallerNative]::ReadText($_) -like 'Exit setup*' }
    )
    if ($progress.Count -gt 0) {
      $sawProgress = $true
      $progressPosition = [KiroInstallerNative]::SendMessage(
        $progress[0],
        0x0408,
        [IntPtr]::Zero,
        [IntPtr]::Zero
      ).ToInt32()
      if ($progressPosition -ne $lastProgressPosition) {
        $lastProgressPosition = $progressPosition
        $lastProgressChange = [DateTime]::UtcNow
      } elseif (([DateTime]::UtcNow - $lastProgressChange).TotalSeconds -ge 30) {
        Save-KiroWindow $Process.MainWindowHandle 'runtime-evidence/dark-progress-stalled.png'
        throw "Custom install progress stalled for 30 seconds at position $progressPosition"
      }
      if (-not $script:CapturedProgress -and $customExit.Count -eq 1) {
        Save-KiroWindow $Process.MainWindowHandle 'runtime-evidence/dark-progress.png'
        $script:CapturedProgress = $true
      }
    } elseif ($sawProgress) {
      $checks = @(
        Get-KiroVisibleControls $Process 'Button' 0xF 3
      )
      $finish = @(
        Get-KiroVisibleControls $Process 'Button' |
          Where-Object {
            ([KiroInstallerNative]::ReadText($_) -replace '&', '') -eq 'Finish'
          }
      )
      if ($checks.Count -eq 1 -and $finish.Count -eq 1) {
        return [pscustomobject]@{
          Launch = $checks[0]
          Finish = $finish[0]
          ElapsedSeconds = $InstallTimer.Elapsed.TotalSeconds
        }
      }
    }
    Start-Sleep -Milliseconds 50
  }
  if (-not $sawProgress) { throw 'Custom install progress page did not appear' }
  Save-KiroWindow $Process.MainWindowHandle 'runtime-evidence/dark-finish-timeout.png'
  throw "Custom installer did not reach its finish page in $MaxSeconds seconds; last progress position=$lastProgressPosition"
}

function Start-KiroRecorder {
  param([string]$Path)

  $ffmpeg = (Get-Command ffmpeg -ErrorAction Stop).Source
  $info = New-Object System.Diagnostics.ProcessStartInfo
  $info.FileName = $ffmpeg
  $info.UseShellExecute = $false
  $info.RedirectStandardInput = $true
  foreach ($argument in @(
    '-y', '-loglevel', 'warning', '-f', 'gdigrab', '-framerate', '10',
    '-i', 'desktop', '-c:v', 'libx264', '-preset', 'ultrafast',
    '-pix_fmt', 'yuv420p', $Path
  )) {
    [void]$info.ArgumentList.Add($argument)
  }
  $recorder = New-Object System.Diagnostics.Process
  $recorder.StartInfo = $info
  if (-not $recorder.Start()) { throw 'Could not start the desktop recorder' }
  Start-Sleep -Milliseconds 500
  return $recorder
}

function Stop-KiroRecorder {
  param([System.Diagnostics.Process]$Recorder)

  if (-not $Recorder) { return }
  if ($Recorder.HasExited) {
    if ($Recorder.ExitCode -ne 0) {
      throw "ffmpeg exited early with $($Recorder.ExitCode)"
    }
    return
  }
  $Recorder.StandardInput.WriteLine('q')
  $Recorder.StandardInput.Close()
  if (-not $Recorder.WaitForExit(30000)) {
    $Recorder.Kill($true)
    throw 'ffmpeg did not finish the installer recording'
  }
  if ($Recorder.ExitCode -ne 0) {
    throw "ffmpeg exited with $($Recorder.ExitCode)"
  }
}

function Find-KiroRegistrations {
  param([string]$ProductName)

  $registrations = @()
  foreach ($location in @(
    @{
      Uninstall = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*'
      Install = 'HKCU:\Software'
    },
    @{
      Uninstall = 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*'
      Install = 'HKLM:\Software'
    },
    @{
      Uninstall = 'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
      Install = 'HKLM:\Software\WOW6432Node'
    }
  )) {
    foreach (
      $entry in Get-ItemProperty -Path $location.Uninstall -ErrorAction SilentlyContinue
    ) {
      $displayName = $entry.PSObject.Properties['DisplayName']
      if ($displayName -and $displayName.Value -like "$ProductName*") {
        $registrations += [pscustomobject]@{
          Entry = $entry
          InstallRoot = $location.Install
        }
      }
    }
  }
  return @($registrations)
}

function Assert-KiroRegistration {
  param([string]$ProductName)

  $registrations = @(Find-KiroRegistrations $ProductName)
  if ($registrations.Count -ne 1) {
    throw "Expected one $ProductName uninstall registration, found $($registrations.Count)"
  }
  $entry = $registrations[0].Entry
  $uninstallString = $entry.PSObject.Properties['UninstallString']
  if (-not $uninstallString -or -not $uninstallString.Value) {
    throw "$ProductName uninstall registration has no UninstallString"
  }
  $installKey = Join-Path $registrations[0].InstallRoot $entry.PSChildName
  $installEntry = Get-ItemProperty -LiteralPath $installKey -ErrorAction Stop
  $installLocation = $installEntry.PSObject.Properties['InstallLocation']
  if (-not $installLocation -or -not $installLocation.Value) {
    throw "$ProductName install registration has no InstallLocation"
  }
  $registeredLocation = [IO.Path]::GetFullPath($installLocation.Value.TrimEnd('\'))
  if ($uninstallString.Value -notmatch '^"([^"]+)"') {
    throw "$ProductName UninstallString does not quote its executable"
  }
  $registeredUninstaller = [IO.Path]::GetFullPath($Matches[1])
  if ((Split-Path -Parent $registeredUninstaller) -ne $registeredLocation) {
    throw "$ProductName install and uninstall registrations disagree"
  }
  return [pscustomobject]@{
    InstallLocation = $registeredLocation
    Uninstaller = $registeredUninstaller
  }
}

$themeKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize'
New-Item -ItemType Directory -Force runtime-evidence | Out-Null
New-Item -Path $themeKey -Force | Out-Null
$installer = Get-ChildItem dist -Filter '*Setup*.exe' | Select-Object -First 1
if (-not $installer) { throw 'Compiled installer not found' }
$package = Get-Content package.json -Raw | ConvertFrom-Json
$productName = $package.build.productName
$productFilenameProperty = $package.build.PSObject.Properties['productFilename']
$productFilename = if ($productFilenameProperty -and $productFilenameProperty.Value) {
  $productFilenameProperty.Value
} else {
  $package.build.productName
}
if (-not $productName -or -not $productFilename) {
  throw 'package.json must define an installer product name'
}

# A file can never own an extracted app directory. Exercise the real silent
# entry point so /D validation cannot regress behind source-pattern assertions.
$blockedDestination = Join-Path (Resolve-Path runtime-evidence).Path 'blocked-parent'
Set-Content -Path $blockedDestination -Value 'not a directory'
$blockedProcess = Start-Process -FilePath $installer.FullName -ArgumentList @(
  '/S',
  "/D=$blockedDestination"
) -PassThru -Wait
if ($blockedProcess.ExitCode -ne 2) {
  throw "Existing-file /D destination returned $($blockedProcess.ExitCode), expected 2"
}

$video = Join-Path (Resolve-Path runtime-evidence).Path 'windows-installer-flow.mp4'
$recorder = $null
$lightProcess = $null
$darkProcess = $null
$script:CapturedProgress = $false
try {
  $recorder = Start-KiroRecorder $video

  # The installer intentionally uses one branded light-glass palette so native
  # controls never become detached white rectangles over a dark dock. Exercise
  # that same coherent surface under both Windows application theme settings.
  Set-ItemProperty -Path $themeKey -Name AppsUseLightTheme -Type DWord -Value 1
  $lightProcess = Start-Process -FilePath $installer.FullName -PassThru
  [void](Wait-KiroWindow $lightProcess 'light')
  [void](Assert-KiroWindow $lightProcess 'Light-theme custom options page')
  $lightOptions = Assert-KiroOptionsLayout $lightProcess
  Save-KiroWindow $lightProcess.MainWindowHandle 'runtime-evidence/light-options.png'

  Set-KiroScope $lightOptions.Scope 1
  Wait-KiroScopeState $lightOptions.Location $lightOptions.Browse $false
  Save-KiroWindow $lightProcess.MainWindowHandle 'runtime-evidence/light-all-users.png'
  Set-KiroScope $lightOptions.Scope 0
  Wait-KiroScopeState $lightOptions.Location $lightOptions.Browse $true

  $customParent = Join-Path (Resolve-Path runtime-evidence).Path 'custom-parent'
  Set-KiroValue $lightOptions.Location $customParent
  Set-KiroToggle $lightOptions.Desktop $false
  Set-KiroToggle $lightOptions.Startup $true
  if ((Get-KiroValue $lightOptions.Location) -ne $customParent) {
    throw 'Custom install location did not remain visible on the integrated page'
  }
  if ((Get-KiroToggle $lightOptions.Desktop) -or -not (Get-KiroToggle $lightOptions.Startup)) {
    throw 'Integrated install options did not retain their selected state'
  }

  $invalidLocation = Join-Path (Resolve-Path runtime-evidence).Path 'invalid-location'
  Set-Content -Path $invalidLocation -Value 'not a directory'
  Set-KiroValue $lightOptions.Location $invalidLocation
  Invoke-KiroElement $lightOptions.Install
  Assert-KiroInvalidLocation $lightProcess 'runtime-evidence/light-invalid-location.png'
  if (-not $lightProcess.HasExited) {
    Stop-Process -Id $lightProcess.Id -Force
    $lightProcess.WaitForExit()
  }

  Set-ItemProperty -Path $themeKey -Name AppsUseLightTheme -Type DWord -Value 0
  $darkProcess = Start-Process -FilePath $installer.FullName -PassThru
  [void](Wait-KiroWindow $darkProcess 'dark')
  [void](Assert-KiroWindow $darkProcess 'Dark-theme custom options page')
  $darkOptions = Assert-KiroOptionsLayout $darkProcess
  $darkParent = Join-Path (Resolve-Path runtime-evidence).Path 'installed-parent'
  Set-KiroValue $darkOptions.Location $darkParent
  Set-KiroToggle $darkOptions.Desktop $false
  Set-KiroToggle $darkOptions.Startup $false
  Save-KiroWindow $darkProcess.MainWindowHandle 'runtime-evidence/dark-options.png'

  $installTimer = [Diagnostics.Stopwatch]::StartNew()
  Invoke-KiroElement $darkOptions.Install
  $finishControls = Wait-KiroFinishPage $darkProcess $installTimer
  $installTimer.Stop()
  $installSeconds = [Math]::Round($finishControls.ElapsedSeconds, 2)
  "install-click-to-finish-seconds=$installSeconds" |
    Set-Content -Encoding ascii 'runtime-evidence/install-timing.txt'
  Write-Host "Custom install reached its finish page in $installSeconds seconds"
  if (-not $script:CapturedProgress) {
    throw 'Custom install progress page was not captured'
  }
  [void](Assert-KiroWindow $darkProcess 'Custom finish page')
  Save-KiroWindow $darkProcess.MainWindowHandle 'runtime-evidence/dark-finish.png'
  Set-KiroToggle $finishControls.Launch $false
  Invoke-KiroElement $finishControls.Finish
  if (-not $darkProcess.WaitForExit(30000)) {
    throw 'Installer did not exit after Finish'
  }
  if ($darkProcess.ExitCode -ne 0) {
    throw "Assisted installer exited with $($darkProcess.ExitCode)"
  }

  $registration = Assert-KiroRegistration $productName
  $installedLocation = $registration.InstallLocation
  $installedExecutable = Join-Path $installedLocation "$productFilename.exe"
  if (-not (Test-Path -LiteralPath $installedExecutable -PathType Leaf)) {
    throw "Installed executable is missing: $installedExecutable"
  }

  $reinstall = Start-Process -FilePath $installer.FullName -ArgumentList @(
    '/S',
    "/D=$installedLocation"
  ) -PassThru -Wait
  if ($reinstall.ExitCode -ne 0) {
    throw "Silent reinstall exited with $($reinstall.ExitCode)"
  }
  if (-not (Test-Path -LiteralPath $installedExecutable -PathType Leaf)) {
    throw 'Silent reinstall removed the installed executable'
  }
  $reinstalled = Assert-KiroRegistration $productName
  if ($reinstalled.InstallLocation -ne $installedLocation) {
    throw 'Silent reinstall changed the registered install location'
  }

  $uninstaller = Join-Path $installedLocation "Uninstall $productFilename.exe"
  if ([IO.Path]::GetFullPath($uninstaller) -ne $reinstalled.Uninstaller) {
    throw 'Registered uninstaller does not match the derived product filename'
  }
  if (-not (Test-Path -LiteralPath $uninstaller -PathType Leaf)) {
    throw "Derived uninstaller is missing: $uninstaller"
  }
  $uninstall = Start-Process -FilePath $uninstaller -ArgumentList '/S' -PassThru -Wait
  if ($uninstall.ExitCode -ne 0) {
    throw "Silent uninstall exited with $($uninstall.ExitCode)"
  }
  for ($attempt = 0; $attempt -lt 120; $attempt++) {
    if (
      @(Find-KiroRegistrations $productName).Count -eq 0 -and
      -not (Test-Path -LiteralPath $installedLocation)
    ) {
      break
    }
    Start-Sleep -Milliseconds 250
  }
  if (@(Find-KiroRegistrations $productName).Count -ne 0) {
    throw 'Silent uninstall left an uninstall registration behind'
  }
  if (Test-Path -LiteralPath $installedLocation) {
    throw "Silent uninstall left the install directory behind: $installedLocation"
  }
} finally {
  foreach ($installerProcess in @($lightProcess, $darkProcess)) {
    if ($installerProcess) {
      $installerProcess.Refresh()
      if (-not $installerProcess.HasExited) {
        Stop-Process -Id $installerProcess.Id -Force
        $installerProcess.WaitForExit()
      }
    }
  }
  Stop-KiroRecorder $recorder
}

if (-not (Test-Path -LiteralPath $video -PathType Leaf)) {
  throw 'Windows installer recording was not created'
}
$ffprobe = (Get-Command ffprobe -ErrorAction Stop).Source
$durationText = & $ffprobe -v error -show_entries format=duration -of 'default=noprint_wrappers=1:nokey=1' $video
if ($LASTEXITCODE -ne 0) { throw 'ffprobe could not inspect the installer recording' }
$duration = [double]::Parse(
  $durationText.Trim(),
  [Globalization.CultureInfo]::InvariantCulture
)
if ($duration -lt 3) {
  throw "Installer recording is unexpectedly short: $duration seconds"
}
$ffmpeg = (Get-Command ffmpeg -ErrorAction Stop).Source
& $ffmpeg -v error -i $video -f null NUL
if ($LASTEXITCODE -ne 0) {
  throw 'ffmpeg could not decode the complete installer recording'
}
