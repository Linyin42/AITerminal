# 一次性守候脚本（P5 收尾）：等 huawei 嵌入跑完 → 只读验收。
# 不写 D:\ait_llm、不做组装（组装由人在场时执行）。
$ErrorActionPreference = "Continue"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$repo = "C:\Users\linyin\WorkBuddy AI\2026-09-21-18-54-37\aiterminal"
$meta = "D:\ait_llm\index\huawei_emb.meta.json"
$log  = Join-Path $repo "tools\_p5_watch.log"
function Say($m) {
    $line = ("{0}  {1}" -f (Get-Date -Format "HH:mm:ss"), $m)
    Add-Content -LiteralPath $log -Value $line -Encoding UTF8
    Write-Output $line
}
Say "watch start"
$deadline = (Get-Date).AddMinutes(190)
$ok = $false
while ((Get-Date) -lt $deadline) {
    if (Test-Path -LiteralPath $meta) {
        $j = $null
        try { $j = Get-Content -LiteralPath $meta -Raw -Encoding UTF8 | ConvertFrom-Json } catch {}
        if ($j -and $j.n -gt 0 -and $j.done -ge $j.n -and $j.failed -eq 0) {
            Say ("embedding done: done={0}/{1} failed={2}" -f $j.done, $j.n, $j.failed)
            $ok = $true
            break
        }
    }
    Start-Sleep -Seconds 60
}
if (-not $ok) { Say "TIMEOUT after 190min"; exit 2 }
Start-Sleep -Seconds 30
Push-Location $repo
Say "== step1: tools\check_emb_store.py (4 vendors) =="
python tools\check_emb_store.py *>&1 | ForEach-Object { Say $_ }
Say ("step1 rc={0}" -f $LASTEXITCODE)
Say "== step2: tests\test_rag_retrieval.py --vendor huawei =="
python tests\test_rag_retrieval.py --vendor huawei *>&1 | ForEach-Object { Say $_ }
Say ("step2 rc={0}" -f $LASTEXITCODE)
Pop-Location
Say "watch end"
