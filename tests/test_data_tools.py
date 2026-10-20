"""数据工具（随包给别的 AI 用）得是真能跑的，不是几段说明文字。

守三件事：
  ① 手册与样例在位，且手册里给的就是**真实存在的脚本名与命令**（防"文档写了、包里没有"）；
  ② 样例 md 符合《资料导入规范》的硬约束（UTF-8 无 BOM / 首个标题 / 功能分类在前 20 行 / source 注释）；
  ③ 真跑一遍第 1 步：照便携包的目录形状搭临时树，用样例 md 切片，产物字段与行号对得上。

跑法：python tests\\test_data_tools.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(REPO, "docs", "data_tools")
DOC = os.path.join(DOCS, "给AI的说明.md")
SAMPLE = os.path.join(DOCS, "sample", "示例资料.md")
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""), flush=True)


print("=== ① 手册与样例在位，且指向真实脚本 ===")
check("给AI的说明.md 存在且非空", os.path.exists(DOC) and os.path.getsize(DOC) > 2000,
      f"{os.path.getsize(DOC) if os.path.exists(DOC) else 0} B")
check("样例 md 存在且非空", os.path.exists(SAMPLE) and os.path.getsize(SAMPLE) > 200,
      f"{os.path.getsize(SAMPLE) if os.path.exists(SAMPLE) else 0} B")
doc_text = open(DOC, encoding="utf-8").read() if os.path.exists(DOC) else ""
for script in ("build_index.py", "embed_chunks.py", "check_vectors.py", "_embed_chain.py",
               "build_tech_docs.py", "retrieve.py"):
    check(f"手册提到了 {script}", script in doc_text)
check("手册给了 exe --script 通道（交付机不装 Python 也能跑）", "--script" in doc_text)
_script_paths = re.findall(r'--script\s+"([^"]+)"', doc_text)
# 只查"真脚本路径"（占位符形如 "<脚本>" 不算）；实测 exe 的 --script 通道不吃非 ASCII 路径
_real_paths = [p for p in _script_paths if p.endswith(".py")]
check("手册里 --script 的真脚本路径全是 ASCII（实测 exe 通道不吃非 ASCII 路径）",
      len(_real_paths) >= 4 and all(p.isascii() for p in _real_paths),
      f"{len(_real_paths)} 条：" + "; ".join(sorted(set(_real_paths))))
check("手册里有可直接粘贴给别的 AI 的提示词块", doc_text.count("```") >= 2)
for src_dir in ("tools", "local_llm"):
    for name in ("build_tech_docs.py", "build_index.py", "embed_chunks.py", "check_vectors.py"):
        if (name in ("build_tech_docs.py",) and src_dir == "tools") or \
           (name in ("build_index.py", "embed_chunks.py", "check_vectors.py") and src_dir == "local_llm"):
            check(f"手册提到的脚本在仓库里真的存在：{name}",
                  os.path.exists(os.path.join(REPO, src_dir, name)))

print("=== ② 样例 md 符合硬约束 ===")
raw = open(SAMPLE, "rb").read()
check("UTF-8 无 BOM", not raw.startswith(b"\xef\xbb\xbf"), raw[:3].hex())
text = raw.decode("utf-8", "replace")
lines = text.splitlines()
first = next((l for l in lines if l.strip()), "")
check("首个非空行是标题", first.startswith("# "), first[:30])
check("含 `- 功能分类：` 且在前 20 行内",
      any(l.startswith("- 功能分类：") for l in lines[:20]),
      next((l for l in lines[:20] if l.startswith("- 功能分类：")), "没找到"))
check("有小节标题（## ~ ####，且 # 后有空格）",
      any(l.startswith("### ") or l.startswith("## ") or l.startswith("#### ") for l in lines))
src_at = next((i for i, l in enumerate(lines) if l.startswith("<!-- source:")), -1)
body_at = next((i for i, l in enumerate(lines)
                if i > src_at >= 0 and l.strip() and not l.startswith("<!--")), -1)
check("`<!-- source: -->` 在任何正文之前", src_at >= 0 and body_at > src_at,
      f"source 行={src_at + 1} 首个正文行={body_at + 1}")

print("=== ③ 真跑一遍切片（照便携包目录形状：脚本在 data_tools\\chunk_embed\\） ===")
with tempfile.TemporaryDirectory(prefix="ait_datatools_") as tmp:
    root = os.path.join(tmp, "pkg")
    script_dir = os.path.join(root, "data_tools", "chunk_embed")
    os.makedirs(script_dir)
    for name in ("build_index.py", "paths.py"):
        shutil.copy2(os.path.join(REPO, "local_llm", name),
                     os.path.join(script_dir, name))
    vendor = "示例"
    tech = os.path.join(root, "knowledge", vendor, "tech")
    os.makedirs(tech)
    shutil.copy2(SAMPLE, os.path.join(tech, "示例资料.md"))
    llm_home = os.path.join(root, "llm_data")
    os.makedirs(llm_home)
    env = dict(os.environ, AIT_LLM_HOME=llm_home, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    cmd = [sys.executable, os.path.join(script_dir, "build_index.py"),
           "--vendor", vendor, "--chunk"]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=180)
    print(f"      rc={proc.returncode}  {(proc.stdout or '').strip().splitlines()[-1:]}")
    out = os.path.join(llm_home, "index", f"{vendor}_chunks.jsonl")
    check("切片产物落在 <数据根>\\index\\ 下", os.path.exists(out), out)
    if os.path.exists(out):
        rows = [json.loads(l) for l in open(out, encoding="utf-8") if l.strip()]
        check("切出了片段", len(rows) > 0, f"{len(rows)} 个片段")
        want = {"id", "vendor", "file", "section", "part", "source", "text", "chars"}
        check("片段字段齐 8 个", want.issubset(rows[0].keys()),
              f"缺={sorted(want - set(rows[0].keys()))}")
        check("id 就是从 0 起的行序（行号减 1）", all(r["id"] == i for i, r in enumerate(rows)))
        check("sample 的出处行被带进片段 source",
              any("示例厂家_示例产品" in (r.get("source") or "") for r in rows),
              str(rows[0].get("source"))[:60])
        check("片段带上了 `厂商 · 功能分类 · 小节` 前缀",
              any("示例 · 示例功能/基本ACL · " in r["text"] for r in rows),
              str(rows[0]["text"].splitlines()[0])[:70])
    else:
        print((proc.stdout or "")[-600:], (proc.stderr or "")[-600:], flush=True)

print(f"\n结果: {len(PASS)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项: " + "；".join(FAIL))
sys.exit(1 if FAIL else 0)
