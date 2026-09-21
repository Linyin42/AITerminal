"""
轻量 XLSX 写入器 - 只用标准库生成 Excel 文件

为什么不用 openpyxl：
    项目对第三方依赖比较克制（只有 PDF 需要 pypdf），而巡检导出只需要
    "多工作表 + 表头加粗 + 列宽 + 冻结首行" 这点能力，用 zipfile + XML 直接写足够，
    而且和 core/knowledge.py 里用标准库解析 xlsx 的做法保持一致。

用法:
    write_xlsx("巡检结果.xlsx", [
        ("巡检汇总", [["序号", "设备", "状态"], [1, "核心交换机", "成功"]]),
        ("命令明细", [["设备", "命令", "输出"], ["核心交换机", "display version", "..."]]),
    ], column_widths={"巡检汇总": [6, 22, 10]})
"""
import os
import re
import zipfile
from typing import Dict, List, Sequence, Tuple
from xml.sax.saxutils import escape

# Excel 单元格文本上限
MAX_CELL_CHARS = 32000

# XML 中不允许出现的控制字符（Excel 会直接报"文件损坏"）
_ILLEGAL_XML = re.compile(
    r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f\ufdd0-\ufddf\ufffe\uffff]'
)

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
{sheet_overrides}
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>
"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>
"""

STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2">
<font><sz val="11"/><name val="\u5fae\u8f6f\u96c5\u9ed1"/></font>
<font><b/><sz val="11"/><name val="\u5fae\u8f6f\u96c5\u9ed1"/></font>
</fonts>
<fills count="3">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFDCE6F1"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1">
  <alignment vertical="top" wrapText="1"/>
</xf>
<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1">
  <alignment vertical="center" horizontal="center"/>
</xf>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>
"""


def clean_text(value) -> str:
    """把任意值转成合法且可写入 Excel 的文本"""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = _ILLEGAL_XML.sub('', text)
    if len(text) > MAX_CELL_CHARS:
        text = text[:MAX_CELL_CHARS - 30] + "\n……（内容过长已截断）"
    return text


def column_letter(index: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA"""
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def sheet_xml(rows: Sequence[Sequence], column_widths: Sequence[float] = None,
              freeze_header: bool = True) -> str:
    """生成单个工作表的 XML"""
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">']

    if freeze_header:
        parts.append('<sheetViews><sheetView workbookViewId="0">'
                     '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" '
                     'state="frozen"/></sheetView></sheetViews>')

    if column_widths:
        parts.append('<cols>')
        for index, width in enumerate(column_widths, start=1):
            parts.append(f'<col min="{index}" max="{index}" width="{float(width):.1f}" '
                         f'customWidth="1"/>')
        parts.append('</cols>')

    parts.append('<sheetData>')
    for row_index, row in enumerate(rows, start=1):
        style = '1' if row_index == 1 else '0'
        parts.append(f'<row r="{row_index}">')
        for column_index, value in enumerate(row or [], start=1):
            reference = f"{column_letter(column_index)}{row_index}"
            if isinstance(value, bool):
                value = "是" if value else "否"
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                parts.append(f'<c r="{reference}" s="{style}"><v>{value}</v></c>')
            else:
                text = escape(clean_text(value))
                parts.append(
                    f'<c r="{reference}" s="{style}" t="inlineStr">'
                    f'<t xml:space="preserve">{text}</t></c>'
                )
        parts.append('</row>')
    parts.append('</sheetData></worksheet>')
    return "".join(parts)


def write_xlsx(path: str, sheets: List[Tuple[str, Sequence[Sequence]]],
               column_widths: Dict[str, Sequence[float]] = None,
               freeze_header: bool = True) -> bool:
    """
    写出 xlsx 文件

    参数:
        path: 目标文件路径
        sheets: [(工作表名, 行列表)]，行列表里每个元素是一行的单元格序列
        column_widths: {工作表名: [列宽…]}
        freeze_header: 是否冻结首行
    返回:
        成功返回 True
    """
    if not sheets:
        sheets = [("Sheet1", [["（无数据）"]])]

    column_widths = column_widths or {}
    used_names = set()
    normalized = []
    for index, (name, rows) in enumerate(sheets, start=1):
        safe = re.sub(r'[\\/*?:\[\]]', '_', str(name or f"Sheet{index}"))[:31] or f"Sheet{index}"
        base, suffix = safe, 2
        while safe in used_names:
            safe = f"{base[:28]}_{suffix}"
            suffix += 1
        used_names.add(safe)
        normalized.append((safe, rows))

    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)

    overrides = "\n".join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
        f'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index in range(1, len(normalized) + 1)
    )
    sheet_entries = "".join(
        f'<sheet name="{escape(name)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, (name, _rows) in enumerate(normalized, start=1)
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets>{sheet_entries}</sheets></workbook>'
    )
    rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
    for index in range(1, len(normalized) + 1):
        rels.append(f'<Relationship Id="rId{index}" '
                    f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                    f'Target="worksheets/sheet{index}.xml"/>')
    styles_id = len(normalized) + 1
    rels.append(f'<Relationship Id="rId{styles_id}" '
                f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
                f'Target="styles.xml"/>')
    rels.append('</Relationships>')

    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    core_props = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties '
        'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<dc:creator>AITerminal</dc:creator>'
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:created>'
        '</cp:coreProperties>'
    )
    app_props = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
        '<Application>AITerminal</Application></Properties>'
    )

    try:
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('[Content_Types].xml',
                        CONTENT_TYPES.format(sheet_overrides=overrides))
            zf.writestr('_rels/.rels', ROOT_RELS)
            zf.writestr('xl/workbook.xml', workbook)
            zf.writestr('xl/_rels/workbook.xml.rels', "".join(rels))
            zf.writestr('xl/styles.xml', STYLES)
            zf.writestr('docProps/core.xml', core_props)
            zf.writestr('docProps/app.xml', app_props)
            for index, (name, rows) in enumerate(normalized, start=1):
                widths = column_widths.get(name) or column_widths.get(str(index))
                zf.writestr(f'xl/worksheets/sheet{index}.xml',
                            sheet_xml(rows, widths, freeze_header))
        return True
    except Exception:
        return False
