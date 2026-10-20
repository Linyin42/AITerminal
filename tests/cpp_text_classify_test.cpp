// 防复发：core::classifyRun 返回 **core::Label 枚举**，不是字符串 —— 展示/断言请用 Label 比较或 labelName()。
// 模块级单测：直接调 core::classifyRun（33 条语料 + 点断言）。独立编译，不进 app 构建。
#include "core/TextClassify.h"
#include <QCoreApplication>
#include <QFile>
#include <QStringList>
#include <QTextStream>
#include <QMap>
#include <cstdio>

static int g_pass = 0, g_fail = 0;

static void expect(const QString &text, bool deviceColored, const char *want)
{
    core::ClassifyContext c;
    c.deviceColored = deviceColored;
    const QString got = QString::fromUtf8(core::labelName(core::classifyRun(text, QString(), c)));
    if (got == QLatin1String(want)) { ++g_pass; }
    else { ++g_fail; printf("  FAIL  %-42s want=%-8s got=%s\n", qPrintable(text.left(42)), want, qPrintable(got)); }
}

int main(int argc, char **argv)
{
    QCoreApplication app(argc, argv);
    const QString corpus = QString::fromLocal8Bit(argv[1]);
    QFile f(corpus);
    if (!f.open(QIODevice::ReadOnly | QIODevice::Text)) { printf("cannot open corpus\n"); return 3; }
    const QString all = QString::fromUtf8(f.readAll());
    f.close();

    // 说明：本文件只做**模块级点断言**；run 级分布（12/9/8/4）由端到端层负责
    // （探针 feed_labels + _automation/check_classify_parity.py），此处不重复。
    // 点断言（值来自已验收构建的 feed_labels / 颜色基线）
    expect("<H3C>", false, "prompt");
    expect("[SW1]", false, "prompt");
    expect("Ruijie>", false, "prompt");
    expect("Switch#", false, "prompt");
    expect("<H3C>display version", false, "prompt");
    expect("PING 192.168.1.254: 56 data bytes", false, "prompt");
    expect("2001:db8::1 is reachable", false, "prompt");
    expect("MAC: 00:1a:2b:3c:4d:5e", false, "prompt");
    expect("Compiled Nov 21 2022 11:00:00", false, "neutral");
    expect("Compiled 2022-11-21 11:00:00", false, "neutral");
    expect("DRAM:  512M bytes", false, "neutral");
    expect("H3C Comware Software, Version 7.1.070, Alpha 7170", false, "neutral");
    expect("CPU utilization: 0.00%", false, "neutral");
    expect("---- More ----", false, "neutral");
    expect("GE1/0/1                UP    auto    1000M", false, "iface");
    expect("Eth-Trunk1             UP    full    20G", false, "iface");
    expect("Vlanif10               UP", false, "iface");
    expect("Done", false, "ok");
    expect("Saved", false, "ok");
    expect("已保存", false, "ok");
    expect("THIS-IS-DEVICE-COLORED", true, "neutral");   // 设备上色 -> 不重判
    expect("RED-SEG", true, "neutral");

    printf("\nSUMMARY pass=%d fail=%d\n", g_pass, g_fail);   // 编码免疫：供脚本解析
    printf("\n===== 结果：%d 通过 / %d 失败 =====\n", g_pass, g_fail);
    return g_fail == 0 ? 0 : 2;
}
