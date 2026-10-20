# -*- coding: utf-8 -*-
"""验证 P2「分档与降级接线」的决策层（`core/local_model.py`）。

判据来自《后续实施安排-本地LLM嵌入与项目收尾-2026-10-16.md》P2：
    "用 `--ngl 0` 模拟纯 CPU 档、用低档模型模拟小显存机，界面标注正确、检索仍可用"

本脚本**不起 llama-server、不联网、不消耗任何额度**：
  · 档位/服务状态用**注入假数据**模拟（小显存机、纯 CPU 档、探测失败…）
  · 只有"检索仍可用"那一项跑的是**真的**本地检索层（core/tech_knowledge.py）

跑法：python tests\\test_local_model.py
"""
import os
import sys
import tempfile

P = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, P)
os.chdir(P)

from core import local_model as lm          # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


# ------------------------------------------------------------------ 模拟数据
S_RUNNING = {"running": True, "port": 8080, "tier": "mid",
             "model": "Qwen3-4B-Q4_K_M.gguf", "device": "Vulkan0",
             "started": "2026-10-16 22:00:00", "reason": "pid 1234 在跑"}
S_FREE = {"running": False, "port": 8080, "tier": "", "model": "",
          "device": "", "started": "", "reason": "没有启动记录"}


def p(tier, model, ngl, reason="测试注入"):
    return {"ok": True, "tier": tier, "model": model, "device": "Vulkan0",
            "n_gpu_layers": ngl, "reason": reason,
            "model_weight_mb": 0, "ram_total_gb": 8.0, "ram_avail_gb": 3.0}


def test_server_running_uses_local_mode():
    d = lm.decide(p_info=p(("mid"), "Qwen3-4B-Q4_K_M.gguf", -1), s_info=S_RUNNING)
    check("服务在跑 → MODE_LOCAL", d["mode"] == lm.MODE_LOCAL, d["mode"])
    check("  地址指向本机 :8080/v1", d["base_url"] == "http://127.0.0.1:8080/v1", d["base_url"])
    check("  标注带出真实模型名", "Qwen3-4B-Q4_K_M.gguf" in d["label"], d["label"])
    # 服务在跑时**不该**因为档位探测失败就降级（pidfile 已足够说明可用）
    d2 = lm.decide(p_info={"ok": False, "error": "boom"}, s_info=S_RUNNING)
    check("  探测失败但不影响已就绪的服务", d2["mode"] == lm.MODE_LOCAL, d2["mode"])


def test_cpu_tier_degrades_to_retrieval():
    """`--ngl 0` 那一档：没独显/显存不够 → 只关 LLM，检索照旧。"""
    d = lm.decide(p_info=p("cpu", "Qwen3-1.7B-Q8_0.gguf", 0,
                           "未发现任何 Vulkan 设备"), s_info=S_FREE)
    check("纯 CPU 档 → MODE_RETRIEVAL", d["mode"] == lm.MODE_RETRIEVAL, d["mode"])
    check("  标注说明检索不受影响", "检索" in d["detail"], d["detail"][:60])
    check("  带出判定理由", "Vulkan" in d["reason"], d["reason"])
    check("  不给出 base_url（不该误以为能用 LLM）", d["base_url"] == "", d["base_url"])


def test_small_vram_tier_is_startable():
    """小显存机（2-3GB）→ low 档 1.7B；档位够就只差"把服务起起来"。"""
    d = lm.decide(p_info=p("low", "Qwen3-1.7B-Q8_0.gguf", -1, "小显存档"), s_info=S_FREE)
    check("low 档 → MODE_STARTABLE", d["mode"] == lm.MODE_STARTABLE, d["mode"])
    check("  标注带出 1.7B", "Qwen3-1.7B-Q8_0.gguf" in d["label"], d["label"])


def test_missing_model_file_degrades():
    """档位判得出来、但模型文件不在 → 必须降级并说明缺哪个文件，不能假装可用。"""
    d = lm.decide(p_info=p("high", "NoSuchModel.gguf", -1), s_info=S_FREE)
    check("缺模型文件 → MODE_RETRIEVAL", d["mode"] == lm.MODE_RETRIEVAL, d["mode"])
    check("  原因里点名缺哪个文件", "NoSuchModel.gguf" in d["reason"], d["reason"])


def test_probe_failure_degrades_gracefully():
    d = lm.decide(p_info={"ok": False, "error": "找不到档位探测脚本 X"}, s_info=S_FREE)
    check("探测失败 → MODE_RETRIEVAL（不抛异常）", d["mode"] == lm.MODE_RETRIEVAL, d["mode"])
    check("  标注写明探测失败", "探测失败" in d["label"], d["label"])
    check("  原因原样带出", "找不到档位探测脚本" in d["reason"], d["reason"])


def test_server_status_reads_pidfile():
    """真读 pid 文件：活 pid → running；死 pid → 不算在跑（不能看谁都像在跑）。"""
    import json
    real = lm.PIDFILE
    tmpdir = tempfile.mkdtemp(prefix="ait_lm_")
    try:
        lm.PIDFILE = os.path.join(tmpdir, "fake.pid")

        with open(lm.PIDFILE, "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "port": 8089, "tier": "low",
                       "model": "Qwen3-1.7B-Q8_0.gguf", "device": "CPU",
                       "started": "2026-10-16 23:00:00"}, fh)
        s = lm.server_status()
        check("活 pid → running=True", s["running"] is True, str(s["running"]))
        check("  端口/档位/模型按记录带出",
              (s["port"], s["tier"], s["model"]) == (8089, "low", "Qwen3-1.7B-Q8_0.gguf"),
              f'{s["port"]}/{s["tier"]}/{s["model"]}')

        with open(lm.PIDFILE, "w", encoding="utf-8") as fh:
            json.dump({"pid": 999999, "port": 8080, "tier": "mid",
                       "model": "Qwen3-4B-Q4_K_M.gguf"}, fh)
        s2 = lm.server_status()
        check("死 pid → running=False", s2["running"] is False, str(s2["running"]))
        check("  说明 pid 已不在", "999999" in s2["reason"], s2["reason"])

        os.remove(lm.PIDFILE)
        s3 = lm.server_status()
        check("无 pid 文件 → running=False", s3["running"] is False, str(s3["running"]))
    finally:
        lm.PIDFILE = real


def test_ram_short_degrades_to_retrieval():
    """**内存**不足也要降级（交付机内存 6-14GB，纯 CPU 档权重全在内存里）。

    探针的 ram_ok 由**实测系数**算出（RSS ≈ 权重×1.28，见 probe_tier.ram_check），
    这里注入结果验证应用侧行为；阈值本身由 ram_check 的单测覆盖。
    """
    d = lm.decide(p_info={**p("mid", "Qwen3-4B-Q4_K_M.gguf", -1), "ram_ok": False,
                          "ram_note": "内存不够：需约 0.6GB + 余量 0.8GB，当前空闲仅 1.2GB"},
                  s_info=S_FREE)
    check("内存不足 → MODE_RETRIEVAL", d["mode"] == lm.MODE_RETRIEVAL, d["mode"])
    check("  原因带出内存说明", "内存" in d["reason"], d["reason"])
    check("  标注写明检索照常可用", "检索" in d["detail"], d["detail"][:60])

    d2 = lm.decide(p_info={**p("mid", "Qwen3-4B-Q4_K_M.gguf", -1), "ram_ok": True},
                   s_info=S_FREE)
    check("内存够 → 不降级，仍可启动", d2["mode"] == lm.MODE_STARTABLE, d2["mode"])

    d3 = lm.decide(p_info=p("mid", "Qwen3-4B-Q4_K_M.gguf", -1), s_info=S_FREE)
    check("老探测结果缺 ram_ok 时不降级（向后兼容）",
          d3["mode"] == lm.MODE_STARTABLE, d3["mode"])


def test_retrieval_still_works():
    """降级的底气：不靠 LLM，本地检索层照样给得出"有出处"的结果。"""
    from core import tech_knowledge as tk
    from core import vendor_knowledge as vk

    root = tk.knowledge_dir()
    vendors = [d for d in sorted(os.listdir(root))
               if os.path.isdir(os.path.join(root, d))] if os.path.isdir(root) else []
    print(f"        （检索根目录 {root}；厂商 {vendors}）")
    avail = [v for v in vendors if tk.available(v)]
    check("至少一个厂商的资料库可用（tech_knowledge.available）", bool(avail), str(avail))
    if not avail:
        return

    v = avail[0]
    st = tk.stats(v)
    check(f"  {v} 有可检索的条目", bool(st), str(st)[:80])

    feats = tk.list_features(v)
    check(f"  {v} 能列出特性列表", bool(feats), f"{len(feats)} 项")
    if not feats:
        return

    # 用真实存在的特性名当查询 —— 检索层必须在**没有任何 LLM** 的情况下命中
    query = feats[0]
    hits = tk.search(v, query, top_k=5)
    check(f"  无 LLM 也能搜到（query={query!r}）", bool(hits), f"{len(hits)} 条命中")

    # 命令层（SQLite）同样是"零额外内存"的降级路径
    if hasattr(vk, "knowledge_dir"):
        vroot = vk.knowledge_dir()
        vvendors = [d for d in sorted(os.listdir(vroot))
                    if os.path.isdir(os.path.join(vroot, d))] if os.path.isdir(vroot) else []
        if v in vvendors:
            res = vk.lookup_sqlite(v, "display")
            check(f"  {v} 命令层 SQLite 可查", isinstance(res, dict), type(res).__name__)


def main():
    print("=== P2 降级决策 判据测试（core/local_model.py）===")
    for fn in (test_server_running_uses_local_mode,
               test_cpu_tier_degrades_to_retrieval,
               test_small_vram_tier_is_startable,
               test_missing_model_file_degrades,
               test_probe_failure_degrades_gracefully,
               test_server_status_reads_pidfile,
               test_ram_short_degrades_to_retrieval,
               test_retrieval_still_works):
        print(f"\n-- {fn.__name__}")
        fn()
    print(f"\n=== 合计 PASS {len(PASS)} / FAIL {len(FAIL)} ===")
    for name in FAIL:
        print(f"   FAIL: {name}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
