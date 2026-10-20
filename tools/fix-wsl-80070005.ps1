#Requires -RunAsAdministrator
<#
.SYNOPSIS
    修复"启用 WSL 所需组件（虚拟机平台）时报 0x80070005"。

.DESCRIPTION
    2026-10-20 在本机定位到的根因链（证据见 CBS.log / DISM.log / 注册表）：
      1. 这台机器的 Windows Update 被策略整个关死：
           HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate
             DisableWindowsUpdateAccess = 1     # 直接拒绝访问 WU 接口
             NoAutoUpdate              = 1     # AU\NoAutoUpdate
             UseWUServer               = 1     # 指向一个不存在的 WSUS
             WUServer = localserver.localdomain.wsus
          服务 wuauserv = Disabled（StartMode=Disabled）
      2. "虚拟机平台"是按需功能（FOD）。本地只有清单（.mum/.cat），没有 .cab 载荷，
         载荷必须由 Windows Update 取；WU 接口拒绝访问 → CBS 规划阶段
         E_ACCESSDENIED(0x80070005) → DISM / wsl 把它原样报出来。
      3. 次要因素：必须以管理员身份运行（UAC 过滤令牌下 CBS 事务同样会被拒绝）。

    本脚本做三件事：
      · 打印当前事实（功能状态 / WU 服务 / 策略 / 载荷）
      · -Apply 时：备份并临时解除 WU 封锁 → 启动 wuauserv → DISM 启用虚拟机平台
      · 不重启机器、不删用户数据；-Rollback 可把 WU 策略还原

.PARAMETER Apply
    真的执行修复（不加只做检查，不动系统）。

.PARAMETER Rollback
    还原备份的 WU 策略并恢复服务为禁用状态。

.EXAMPLE
    # 只看现状
    powershell -ExecutionPolicy Bypass -File .\fix-wsl-80070005.ps1
    # 执行修复
    powershell -ExecutionPolicy Bypass -File .\fix-wsl-80070005.ps1 -Apply
#>
[CmdletBinding()]
param(
    [switch]$Apply,
    [switch]$Rollback,
    # 清空 Windows Update 下载缓存后重试（对应 0x80070102 WAIT_TIMEOUT 反复重下）
    [switch]$ResetWU
)

$ErrorActionPreference = 'Continue'
$wuKey = 'HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate'
$backup = Join-Path $env:USERPROFILE ('wu-policy-backup-{0}.reg' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))

function Show-Facts {
    Write-Host "`n=== 1. 功能状态 ===" -ForegroundColor Cyan
    foreach ($f in 'VirtualMachinePlatform', 'Microsoft-Windows-Subsystem-Linux') {
        try {
            $r = Get-WindowsOptionalFeature -Online -FeatureName $f -ErrorAction Stop
            Write-Host ("  {0,-42} = {1}" -f $f, $r.State)
        } catch {
            Write-Host ("  {0,-42} = 查询失败: {1}" -f $f, $_.Exception.Message)
        }
    }

    Write-Host "`n=== 2. Windows Update 服务 ===" -ForegroundColor Cyan
    $svc = Get-Service wuauserv -ErrorAction SilentlyContinue
    if ($svc) { Write-Host ("  wuauserv = {0} / StartType={1}" -f $svc.Status, $svc.StartType) }

    Write-Host "`n=== 3. WU 策略 ===" -ForegroundColor Cyan
    if (Test-Path ('HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate')) {
        Get-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate' |
            Select-Object * -Exclude PS* | Format-List | Out-String | Write-Host
    } else {
        Write-Host "  策略键不存在（正常）"
    }

    Write-Host "=== 4. FOD 载荷 ===" -ForegroundColor Cyan
    $fod = Get-ChildItem 'C:\Windows\servicing\Packages' -Filter '*VirtualMachinePlatform*' -ErrorAction SilentlyContinue
    $ext = ($fod | Group-Object Extension | ForEach-Object { "$($_.Name)=$($_.Count)" }) -join ', '
    Write-Host ("  清单/载荷文件 {0} 个：{1}" -f $fod.Count, $ext)
    if (-not ($fod | Where-Object Extension -eq '.cab')) {
        Write-Host "  注意：没有 .cab 载荷 → 必须能连 Windows Update（或改用 ISO 作源）" -ForegroundColor Yellow
    }
}

function Invoke-Fix {
    Write-Host "`n[1/4] 备份 WU 策略 → $backup" -ForegroundColor Cyan
    if (Test-Path ('HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate')) {
        & reg.exe export $wuKey $backup /y | Out-Null
        if (Test-Path $backup) { Write-Host "  已备份" } else { throw "备份失败，停止（不改策略）" }
    } else {
        Write-Host "  策略键不存在，无需备份"
    }

    Write-Host "[2/4] 临时解除 WU 封锁（DisableWindowsUpdateAccess / NoAutoUpdate / 假 WSUS）" -ForegroundColor Cyan
    if (Test-Path ('HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate')) {
        Remove-Item ('HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate') -Recurse -Force
        Write-Host "  已删除策略键（可用 -Rollback 还原）"
    }

    Write-Host "[3/4] 启动 Windows Update 服务" -ForegroundColor Cyan
    & sc.exe config wuauserv start= demand | Out-Null
    & sc.exe start wuauserv | Out-Null
    Start-Sleep -Seconds 2
    $svc = Get-Service wuauserv
    Write-Host ("  wuauserv = {0} / StartType={1}" -f $svc.Status, $svc.StartType)

    Write-Host "[4/4] 启用虚拟机平台（不改动机器重启）" -ForegroundColor Cyan
    & dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart
    $rc = $LASTEXITCODE
    Write-Host "  DISM 返回码 = $rc（0=成功，3010=成功但需要重启）"

    Write-Host "`n=== 结果 ===" -ForegroundColor Cyan
    try {
        $r = Get-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform -ErrorAction Stop
        Write-Host ("  VirtualMachinePlatform = {0}" -f $r.State)
    } catch { Write-Host "  查询失败: $($_.Exception.Message)" }

    if ($rc -eq 0 -or $rc -eq 3010) {
        Write-Host "`n下一步：重启电脑 → 在普通窗口执行 wsl --status（应显示 默认版本: 2）" -ForegroundColor Green
        Write-Host "      再装发行版：wsl --install -d Ubuntu" -ForegroundColor Green
    } else {
        Write-Host "`n仍然失败。按顺序尝试：" -ForegroundColor Yellow
        Write-Host "  a) 看 C:\Windows\Logs\CBS\CBS.log 最后 40 行里的第一条 Error"
        Write-Host "  b) DISM /Online /Cleanup-Image /RestoreHealth （需 WU 可用，即先做完第 2 步）"
        Write-Host "  c) 用 Windows 11 ISO 离线装载荷："
        Write-Host "     dism /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart /source:E:\sources\sxs /limitaccess"
    }
}

function Invoke-ResetWU {
    <#
      针对 0x80070102（WAIT_TIMEOUT）反复"下载到 52% → 归零"：
      把 SoftwareDistribution 整个挪走，让 WU 重建缓存再下载。
      不删用户数据；旧目录保留成 SoftwareDistribution.old-<时间戳>。
    #>
    Write-Host "`n[1/3] 停止 WU 相关服务" -ForegroundColor Cyan
    foreach ($s in 'wuauserv', 'bits', 'dosvc', 'usosvc') {
        $svc = Get-Service $s -ErrorAction SilentlyContinue
        if ($svc -and $svc.Status -ne 'Stopped') {
            & net.exe stop $s /y 2>&1 | Out-Null
            Write-Host ("  {0} → {1}" -f $s, (Get-Service $s).Status)
        }
    }

    Write-Host "[2/3] 挪走 SoftwareDistribution（保留备份）" -ForegroundColor Cyan
    $src = 'C:\Windows\SoftwareDistribution'
    $dst = 'C:\Windows\SoftwareDistribution.old-{0}' -f (Get-Date -Format 'yyyyMMdd-HHmmss')
    if (Test-Path $src) {
        try {
            Rename-Item -Path $src -NewName (Split-Path $dst -Leaf) -ErrorAction Stop
            Write-Host "  已改名 → $dst"
        } catch {
            Write-Host "  改名失败（可能有文件被占用）：$($_.Exception.Message)" -ForegroundColor Yellow
            Write-Host "  可稍后重启再试，或手动：ren C:\\Windows\\SoftwareDistribution SoftwareDistribution.old" -ForegroundColor Yellow
        }
    }

    Write-Host "[3/3] 重新启动 wuauserv" -ForegroundColor Cyan
    & sc.exe config wuauserv start= demand | Out-Null
    & net.exe start wuauserv 2>&1 | Out-Null
    Write-Host ("  wuauserv = {0}" -f (Get-Service wuauserv).Status)

    Write-Host "`n现在重新跑（管理员，普通窗口）：wsl --install --no-distribution" -ForegroundColor Green
    Write-Host "若仍超时：换网络（手机热点）重试，或改用 ISO 的 sources\\sxs 做离线源。" -ForegroundColor Green
}

function Invoke-Rollback {
    $latest = Get-ChildItem $env:USERPROFILE -Filter 'wu-policy-backup-*.reg' |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $latest) { throw "没找到备份文件 wu-policy-backup-*.reg" }
    Write-Host "从 $($latest.FullName) 还原 WU 策略" -ForegroundColor Cyan
    & reg.exe import $latest.FullName | Out-Null
    & sc.exe config wuauserv start= disabled | Out-Null
    Write-Host "已还原（服务恢复为 Disabled）"
}

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "需要管理员权限运行（右键 PowerShell → 以管理员身份运行）" -ForegroundColor Red
    exit 5
}

Show-Facts
if ($Rollback) { Invoke-Rollback }
elseif ($ResetWU) { Invoke-ResetWU }
elseif ($Apply) { Invoke-Fix }
else {
    Write-Host "`n以上只是检查。要执行修复请加 -Apply：" -ForegroundColor Yellow
    Write-Host "  powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Apply"
    Write-Host "下载卡在 52% 反复归零（0x80070102）时，先清缓存：加 -ResetWU" -ForegroundColor Yellow
}
