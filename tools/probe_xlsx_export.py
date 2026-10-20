"""导出可读性检查：造一份"像巡检结果"的表，写出去再用 openpyxl 读回来

为什么单独成脚本：用户报"导出来的数据谁看的懂"，而 xlsx_writer 是手写 XML，
只有"另一个解析器读回来"才能证明单元格真的存在（自解析自己写的东西会一起错）。

跑法（工程根）：
    .venv-train\\Scripts\\python.exe tools\\probe_xlsx_export.py
退出码 0 = 所有字符串单元格都读得回来；1 = 有丢失（并打印哪一格丢了）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.xlsx_writer import write_xlsx                            # noqa: E402

TARGET = os.path.join(os.environ.get("TEMP", "."), "xlsx_probe.xlsx")

# 与 core/patrol.py 三张表同构：表头 + 中文设备名 + 命令 + 状态 + 数字 + 多行输出
SHEETS = [
    ("巡检信息", [
        ["任务名称", "2026-10-19 核心巡检"],
        ["开始时间", "2026-10-19 20:34:38"],
        ["设备数量", 1],
        ["巡检命令清单", ""],
        ["  1", "display version"],
    ]),
    ("巡检汇总", [
        ["序号", "设备名称", "协议", "地址", "状态", "耗时(秒)", "命令数", "正常数", "异常数", "说明"],
        [1, "H3C-S7503X", "Telnet", "10.2.0.1", "成功", 12.5, 3, 3, 0, ""],
    ]),
    ("命令明细", [
        ["设备", "序号", "命令", "状态", "耗时(秒)", "输出"],
        ["H3C-S7503X", 1, "display version", "完成", 1.8,
         "H3C Comware Software, Version 7.1.070\nH3C S7503X uptime is 8 weeks"],
        ["H3C-S7503X", 2, "display cpu-usage", "完成", 1.7, "CPU usage: 12% in last 5 minutes"],
    ]),
]


def main() -> int:
    write_xlsx(TARGET, SHEETS, column_widths={"命令明细": [18, 6, 28, 16, 10, 90]})
    print(f"写出 {TARGET}  {os.path.getsize(TARGET)} 字节")

    import openpyxl
    book = openpyxl.load_workbook(TARGET)
    print(f"工作表: {book.sheetnames}")

    lost = []
    for name, rows in SHEETS:
        sheet = book[name]
        print(f"\n[{name}] {sheet.max_row} 行 × {sheet.max_column} 列")
        for row_index, row in enumerate(rows, start=1):
            got = [sheet.cell(row_index, column).value for column in range(1, len(row) + 1)]
            for column, (want, have) in enumerate(zip(row, got), start=1):
                # 数字比数值相等；字符串必须原样（允许 openpyxl 把换行原样保留）
                same = (abs(float(want) - float(have)) < 1e-9
                        if isinstance(want, (int, float)) and have is not None
                        else str(want or "") == str(have or ""))
                if not same:
                    lost.append((name, row_index, column, want, have))
            preview = [("" if v is None else str(v))[:22].replace("\n", "\\n") for v in got]
            print(f"  {row_index}: {preview}")

    if lost:
        print(f"\n[FAIL] {len(lost)} 个单元格读回来和写出去不一致：")
        for name, row_index, column, want, have in lost[:12]:
            print(f"  {name} 第{row_index}行第{column}列: 期望 {want!r} 实际 {have!r}")
        return 1
    print("\n[PASS] 所有单元格（含中文表头/设备名/命令/状态/多行输出）都读得回来")
    return 0


if __name__ == "__main__":
    sys.exit(main())
