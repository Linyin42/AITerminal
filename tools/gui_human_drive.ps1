# 像人一样驱动 NetOpsTerminal：真实鼠标 / 键盘（Win32 SendInput）+ 窗口截图取证
#
# 为什么要有这个脚本：控件级用例（QKeyEvent 直喂）证明不了"人真的这样用的时候行不行"——
# 它绕过了窗口、焦点、真实按键映射。这台机器上的用户反馈（点哪光标跳哪、退格删不动、
# 提示符被吃掉）全是"人用鼠标键盘"时才暴露的，所以复验也必须这么来。
#
# 用法：
#   pwsh -File tools\gui_human_drive.ps1 -ExePath D:\ait_delivery\NetOpsTerminal\NetOpsTerminal_fix.exe `
#                                         -ShotDir $env:TEMP\ait_gui
#   pwsh -File tools\gui_human_drive.ps1 -Pid 31988 -ShotDir ... -ShotOnly
#   pwsh -File tools\gui_human_drive.ps1 -Pid 123 -Script $env:TEMP\steps.txt -ShotDir ...
#
# steps.txt 每行一步（# 开头是注释）：
#   focus                把窗口切到前台（会抢焦点，仅这一步需要）
#   text system-view     像人一样敲这一串字符（逐键，带 shift 处理）
#   key BACKSPACE        按一个功能键（ENTER/BACKSPACE/TAB/ESC/UP/DOWN/…）
#   wait 300             等 300 毫秒
#   shot 01-typed.png    截窗口图到 <ShotDir>\01-typed.png
#   click 120 260        在窗口客户区坐标点一下（真实鼠标）
#
# 退出码：0 = 全部步骤执行完；1 = 找不到窗口 / 步骤里有失败。

param(
    [string]$ExePath = "",
    [int]$TargetPid = 0,
    [string]$TitleLike = "NetOpsTerminal",
    [string]$Script = "",
    [string]$ShotDir = "$env:TEMP\ait_gui",
    [switch]$ShotOnly,
    [int]$FocusWaitMs = 400
)

$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win {
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint f, int dx, int dy, uint d, IntPtr e);
    [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint f, IntPtr e);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr h, System.Text.StringBuilder s, int n);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern short VkKeyScan(char ch);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("imm32.dll")] public static extern IntPtr ImmAssociateContext(IntPtr hWnd, IntPtr hIMC);
    public delegate bool EnumProc(IntPtr h, IntPtr l);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }

    // 枚举某进程的全部可见顶层窗口：hwnd|标题|x,y,w,h
    // 只用 MainWindowHandle 会挑到辅助小窗口（实测拿到过 199x34），必须按面积挑。
    public static System.Collections.Generic.List<string> ListWindows(uint pid) {
        var res = new System.Collections.Generic.List<string>();
        EnumWindows((h, l) => {
            uint p; GetWindowThreadProcessId(h, out p);
            if (p == pid && IsWindowVisible(h)) {
                var sb = new System.Text.StringBuilder(512);
                GetWindowTextW(h, sb, 512);
                RECT r; GetWindowRect(h, out r);
                res.Add(h.ToInt64() + "|" + sb.ToString() + "|" + r.Left + "," + r.Top + ","
                        + (r.Right - r.Left) + "," + (r.Bottom - r.Top));
            }
            return true;
        }, IntPtr.Zero);
        return res;
    }
}
"@

$KEYEVENTF_KEYUP = 0x0002
$MOUSEEVENTF_LEFTDOWN = 0x0002
$MOUSEEVENTF_LEFTUP = 0x0004

# 功能键 → 虚拟键码
$VK = @{
    "ENTER" = 0x0D; "RETURN" = 0x0D; "TAB" = 0x09; "ESC" = 0x1B; "ESCAPE" = 0x1B
    "BACKSPACE" = 0x08; "BS" = 0x08; "SPACE" = 0x20; "UP" = 0x26; "DOWN" = 0x28
    "LEFT" = 0x25; "RIGHT" = 0x27; "DELETE" = 0x2E; "DEL" = 0x2E; "HOME" = 0x24
    "END" = 0x23; "CTRLC" = 0x43; "SHIFT" = 0x10; "LSHIFT" = 0xA0; "CTRL" = 0x11
}

function Find-AppWindow {
    param([int]$TargetPid, [string]$TitleLike)
    $pids = @()
    if ($TargetPid -gt 0) { $pids = @($TargetPid) }
    else {
        $pids = @(Get-Process | Where-Object { $_.ProcessName -like "*$TitleLike*" } |
                  Select-Object -ExpandProperty Id)
    }
    $bestH = [IntPtr]::Zero; $bestTitle = ""; $bestArea = -1
    foreach ($one in $pids) {
        foreach ($row in [Win]::ListWindows([uint32]$one)) {
            $f = $row -split '\|'
            if ($f.Count -lt 3) { continue }
            $geo = $f[2] -split ','
            $area = [int]$geo[2] * [int]$geo[3]
            # 主窗口一定是有标题的大窗口；小窗口（提示条/托盘）按面积淘汰
            if ($f[1] -and $area -gt $bestArea) {
                $bestArea = $area; $bestH = [IntPtr][int64]$f[0]; $bestTitle = $f[1]
            }
        }
    }
    return @{ Handle = $bestH; Title = $bestTitle; Area = $bestArea }
}

function Get-WindowRect {
    param([IntPtr]$Handle)
    $r = New-Object Win+RECT
    [void][Win]::GetWindowRect($Handle, [ref]$r)
    return $r
}

function Take-Shot {
    param([IntPtr]$Handle, [string]$Path)
    $r = Get-WindowRect $Handle
    $w = $r.Right - $r.Left; $h = $r.Bottom - $r.Top
    if ($w -le 0 -or $h -le 0) { throw "窗口尺寸异常: ${w}x${h}" }
    $bmp = New-Object System.Drawing.Bitmap $w, $h
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen($r.Left, $r.Top, 0, 0, (New-Object System.Drawing.Size $w, $h))
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path $dir)) { [void](New-Item -ItemType Directory -Path $dir -Force) }
    $bmp.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
    $g.Dispose(); $bmp.Dispose()
    Write-Host ("  截图 {0} ({1}x{2})" -f $Path, $w, $h)
}

function Send-Key {
    param([int]$Vk, [switch]$Shift)
    if ($Shift) { [Win]::keybd_event(0x10, 0, 0, [IntPtr]::Zero) }   # VK_SHIFT
    [Win]::keybd_event([byte]$Vk, 0, 0, [IntPtr]::Zero)
    Start-Sleep -Milliseconds 20
    [Win]::keybd_event([byte]$Vk, 0, $KEYEVENTF_KEYUP, [IntPtr]::Zero)
    if ($Shift) { [Win]::keybd_event(0x10, 0, $KEYEVENTF_KEYUP, [IntPtr]::Zero) }
    Start-Sleep -Milliseconds 30
}

function Send-Text {
    param([string]$Text)
    foreach ($ch in $Text.ToCharArray()) {
        # 交给 Win32 做字符→键位映射（真键盘就是这么走的）
        $scan = [Win]::VkKeyScan($ch)
        if ($scan -eq -1) { Write-Warning "跳过无法映射的字符: $ch"; continue }
        $shift = (($scan -shr 8) -band 1) -eq 1
        Send-Key -Vk ($scan -band 0xFF) -Shift:$shift
    }
}

# ---- 定位窗口 ----
$win = Find-AppWindow -TargetPid $TargetPid -TitleLike $TitleLike
if ($win.Handle -eq [IntPtr]::Zero) {
    Write-Host "找不到窗口（pid=$TargetPid 标题含 '$TitleLike'）" -ForegroundColor Red
    exit 1
}
Write-Host ("窗口 hwnd={0} 标题='{1}'" -f $win.Handle, $win.Title)

if ($ShotOnly -and -not $Script) {
    Take-Shot -Handle $win.Handle -Path (Join-Path $ShotDir "window.png")
    exit 0
}

if (-not $Script) { Write-Host "没有 -Script，也没有 -ShotOnly" -ForegroundColor Red; exit 1 }
if (-not (Test-Path $Script)) { Write-Host "步骤文件不存在: $Script" -ForegroundColor Red; exit 1 }

$stepNo = 0
foreach ($raw in Get-Content $Script -Encoding UTF8) {
    $line = $raw.Trim()
    if (-not $line -or $line.StartsWith("#")) { continue }
    $stepNo++
    $parts = $line -split '\s+', 2
    $cmd = $parts[0].ToLower()
    $arg = if ($parts.Count -gt 1) { $parts[1] } else { "" }
    switch ($cmd) {
        "focus" {
            [void][Win]::ShowWindow($win.Handle, 9)          # SW_RESTORE
            [void][Win]::SetForegroundWindow($win.Handle)
            Start-Sleep -Milliseconds $FocusWaitMs
            Write-Host "  focus -> 前台"
        }
        "text"   { Send-Text -Text $arg;  Write-Host "  text '$arg'" }
        "key"    {
            $name = $arg.ToUpper()
            if (-not $VK.ContainsKey($name)) { Write-Host "  未知按键 $name" -ForegroundColor Red; exit 1 }
            Send-Key -Vk $VK[$name]
            Write-Host "  key  $name"
        }
        "wait"   { Start-Sleep -Milliseconds ([int]$arg); Write-Host "  wait ${arg}ms" }
        "ime"    {
            # 中文输入法会先截走真实按键（组字），实验时经常需要关掉它复现/排除
            #   ime off -> 把这个窗口的输入法上下文摘掉（按键直达控件，纯 ASCII）
            #   ime on  -> 接回去（用户平时就是这个状态：中文输入法激活）
            if ($arg.Trim().ToLower() -eq "off") {
                $script:oldImc = [Win]::ImmAssociateContext($win.Handle, [IntPtr]::Zero)
                Write-Host "  ime -> off（已摘除输入法上下文 0x$($script:oldImc.ToString('X')))"
            } else {
                [void][Win]::ImmAssociateContext($win.Handle, $script:oldImc)
                Write-Host "  ime -> on（已恢复输入法上下文）"
            }
            Start-Sleep -Milliseconds 200
        }
        "shot"   { Take-Shot -Handle $win.Handle -Path (Join-Path $ShotDir $arg) }
        "click"  {
            $xy = $arg -split '\s+'
            $r = Get-WindowRect $win.Handle
            $x = $r.Left + [int]$xy[0]; $y = $r.Top + [int]$xy[1]
            [void][Win]::SetCursorPos($x, $y)
            Start-Sleep -Milliseconds 60
            [Win]::mouse_event($MOUSEEVENTF_LEFTDOWN, 0, 0, 0, [IntPtr]::Zero)
            Start-Sleep -Milliseconds 40
            [Win]::mouse_event($MOUSEEVENTF_LEFTUP, 0, 0, 0, [IntPtr]::Zero)
            Write-Host "  click ($($xy[0]),$($xy[1]))"
        }
        default  { Write-Host "  未知步骤: $line" -ForegroundColor Red; exit 1 }
    }
}
Write-Host "共执行 $stepNo 步；证据目录 $ShotDir"
exit 0

