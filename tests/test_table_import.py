"""TEST-010: CSV/XLSX inspection and safe prefill for a confirmed table."""

import base64
import io
import json
import unittest
import zipfile
from unittest.mock import patch

from accounts import ApiError
from table_import import TableLimits, build_table_tool, inspect_table, limits_from_env, render_tool


def sample_xlsx(first_sheet=None):
    first_sheet = first_sheet or (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="inlineStr"><is><t>金额</t></is></c>'
        '<c r="C1" t="inlineStr"><is><t>截止日</t></is></c></row>'
        '<row r="2"><c r="A2" t="s"><v>1</v></c><c r="B2"><v>2.50</v></c><c r="C2" s="1"><v>46335</v></c></row>'
        '<row r="3"><c r="A3" t="inlineStr"><is><r><t>带空格</t></r><r><t xml:space="preserve"> 尾</t></r></is></c>'
        '<c r="C3" t="d"><v>2026-11-09</v></c></row></sheetData></worksheet>'
    )
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as book:
        parts = {
            "workbook.xml": "sheet.main+xml",
            "worksheets/input.xml": "worksheet+xml",
            "worksheets/sheet1.xml": "worksheet+xml",
            "sharedStrings.xml": "sharedStrings+xml",
            "styles.xml": "styles+xml",
        }
        book.writestr("[Content_Types].xml",
                      '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                      '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                      '<Default Extension="xml" ContentType="application/xml"/>'
                      + "".join(f'<Override PartName="/xl/{part}" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.{kind}"/>'
                                for part, kind in parts.items()) + '</Types>')
        book.writestr("_rels/.rels",
                      '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                      '<Relationship Id="office" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                      '</Relationships>')
        book.writestr("xl/workbook.xml",
                      '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                      '<sheets><sheet name="输入" sheetId="7" r:id="first"/><sheet name="不读取" sheetId="1" r:id="second"/></sheets></workbook>')
        book.writestr("xl/_rels/workbook.xml.rels",
                      '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                      '<Relationship Id="second" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                      '<Relationship Id="first" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="/xl/worksheets/input.xml"/>'
                      '<Relationship Id="strings" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
                      '<Relationship Id="styles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                      '</Relationships>')
        book.writestr("xl/sharedStrings.xml",
                      '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>编号</t></si>'
                      '<si><t>0007</t></si></sst>')
        book.writestr("xl/styles.xml",
                      '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                      '<cellXfs><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>')
        book.writestr("xl/worksheets/input.xml", first_sheet)
        book.writestr("xl/worksheets/sheet1.xml",
                      '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
                      '<row r="1"><c r="A1" t="inlineStr"><is><t>这是第二张表</t></is></c></row>'
                      '<row r="2"><c r="A2"><v>999</v></c></row></sheetData></worksheet>')
    return base64.b64encode(stream.getvalue()).decode("ascii")


class TableImportTests(unittest.TestCase):
    def inspect_upload(self, source, filename="交付项目.CSV"):
        return inspect_table({"source": "upload", "filename": filename, "csv": source})

    def test_example_identifies_original_columns_and_rows(self):
        result = inspect_table({"source": "example"})
        self.assertIn("示例表格", result["filename"])
        self.assertEqual(result["suggestion"], "交付跟进板")
        self.assertEqual(result["columns"], [
            {"name": "事项", "type": "text"}, {"name": "负责人", "type": "text"},
            {"name": "截止日", "type": "date"}, {"name": "状态", "type": "option"},
        ])
        self.assertEqual(len(result["rows"]), 3)
        self.assertEqual(result["rows"][2], ["复核体验入口", "小陈", "11 月 9 日", "已完成"])

    def test_utf8_bom_quoted_multiline_values_and_types(self):
        result = self.inspect_upload('\ufeff编号,说明,金额,日期,启用\r\n001,"带,逗号和""引号""\r\n下一行",2.50,2026-11-09,是\r\n')
        self.assertEqual(result["filename"], "交付项目.CSV")
        self.assertEqual(result["rows"], [["001", '带,逗号和"引号"\r\n下一行', "2.50", "2026-11-09", "是"]])
        self.assertEqual([column["type"] for column in result["columns"]],
                         ["text", "text", "number", "date", "boolean"])

    def test_invalid_csv_and_size_have_actionable_errors(self):
        cases = [
            ("", "csv_empty"),
            ("事项,\n第一条,张三", "csv_headers"),
            ('"事项\n",负责人\n第一条,张三', "csv_headers"),
            ("事项, 事项\n第一条,张三", "csv_headers"),
            ("事项,负责人\n第一条", "csv_width"),
            ('事项,负责人\n"第一条,张三', "csv_syntax"),
            ('事项,负责人\n第一"条,张三', "csv_syntax"),
            ("事项\n", "csv_no_rows"),
            ("事项\n" + "事" * (1024 * 1024 // 3 + 1), "csv_size"),
            ("事项\n" + "第一条\n" * 501, "csv_rows"),
        ]
        for source, code in cases:
            with self.subTest(code=code), self.assertRaises(ApiError) as caught:
                self.inspect_upload(source)
            self.assertEqual(caught.exception.code, code)
            self.assertTrue(str(caught.exception))
        for filename in ("项目.xls", "项目\ud800.csv"):
            with self.subTest(filename=repr(filename)), self.assertRaises(ApiError) as caught:
                self.inspect_upload("事项\n第一条", filename=filename)
            self.assertEqual(caught.exception.code, "csv_format")

    def test_confirmed_values_are_seeded_without_embedding_upload(self):
        payload = self.inspect_upload('事项,状态\n"</script><img src=x>",已完成\n保留空格, 未完成 ')
        payload["columns"][0] = {"name": "确认后的事项", "type": "option"}
        title, html, entries = build_table_tool(payload)
        self.assertEqual(title, "交付跟进板")
        self.assertTrue(html.lower().startswith("<!doctype html>"))
        self.assertTrue(html.lower().endswith("</html>"))
        self.assertIn("#table-filter", html)
        self.assertIn("localStorage.getItem", html)
        self.assertIn("localStorage.setItem", html)
        self.assertIn("textContent", html)
        self.assertNotIn(payload["filename"], html)
        self.assertNotIn("确认后的事项", html)
        self.assertNotIn("<img src=x>", html)
        self.assertEqual([entry[0] for entry in entries], ["root.table.v1"])
        seed = json.loads(entries[0][1])
        self.assertEqual(seed["columns"], payload["columns"])
        self.assertEqual(seed["rows"], payload["rows"])
        self.assertEqual(seed["done"], [True, False])

    def test_renderer_accepts_only_safe_title_and_distinct_column_indexes(self):
        html = render_tool('</title><img src=x> & 交付', (1, 3))
        self.assertIn('<title>&lt;/title&gt;&lt;img src=x&gt; &amp; 交付</title>', html)
        self.assertNotIn('<img src=x>', html)
        self.assertIn('const columnFilters = [1, 3];', html)
        self.assertIn('const columnFilters = [];', render_tool())
        for filters in ((0, 0), (-1,), (True,), ('1',), (200,), '1'):
            with self.subTest(filters=filters), self.assertRaises(ValueError):
                render_tool(filters=filters)

    def test_confirmation_revalidates_client_data(self):
        result = self.inspect_upload("事项,负责人\n第一条,张三")
        cases = [
            {"columns": [{"name": "事项", "type": "unknown"}]},
            {"columns": [{"name": "事项", "type": "text"}, {"name": " 事项 ", "type": "text"}]},
            {"rows": [["第一条"]]},
            {"rows": [["第一条", None]]},
            {"filename": "../上传.csv"},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides), self.assertRaises(ApiError) as caught:
                build_table_tool({**result, **overrides})
            self.assertEqual(caught.exception.status, 400)

    def test_xlsx_reads_workbook_order_and_preserves_values(self):
        result = inspect_table({"source": "upload", "filename": "交付.XLSX", "xlsx": sample_xlsx()})
        self.assertEqual(result["sheet"], "输入")
        self.assertEqual(result["rows"], [["0007", "2.50", "2026-11-09"], ["带空格 尾", "", "2026-11-09"]])
        self.assertEqual([column["type"] for column in result["columns"]], ["text", "number", "date"])

    def test_excluded_columns_are_not_saved(self):
        result = self.inspect_upload("事项,客户电话,状态\n第一条,0007,已完成\n第二条,0003,待完成")
        result["columns"][1] = {"include": False}
        _, html, entries = build_table_tool(result)
        seed = json.loads(entries[0][1])
        self.assertEqual([column["name"] for column in seed["columns"]], ["事项", "状态"])
        self.assertEqual(seed["rows"], [["第一条", "已完成"], ["第二条", "待完成"]])
        self.assertNotIn("0007", entries[0][1])
        self.assertNotIn("客户电话", html)

    def test_xlsx_rejects_unsafe_xml_and_invalid_file(self):
        for content in ("not base64", sample_xlsx('<!DOCTYPE worksheet [<!ENTITY x "expanded">]><worksheet>&x;</worksheet>')):
            with self.subTest(content=content[:20]), self.assertRaises(ApiError) as caught:
                inspect_table({"source": "upload", "filename": "导入.xlsx", "xlsx": content})
            self.assertEqual(caught.exception.status, 400)

    def test_default_and_configured_limits_apply_to_inspection_and_confirmation(self):
        self.assertEqual(TableLimits(), TableLimits(1024 * 1024, 500, 20))
        header = ",".join(f"字段{i}" for i in range(20))
        row = ",".join(["甲"] * 20)
        result = self.inspect_upload(header + "\n" + (row + "\n") * 500)
        self.assertEqual(len(result["rows"]), 500)
        self.assertEqual(len(result["columns"]), 20)
        with patch.dict("os.environ", {"ROOT_TABLE_MAX_BYTES": "100000", "ROOT_TABLE_MAX_ROWS": "1",
                                       "ROOT_TABLE_MAX_COLUMNS": "3"}):
            limits = limits_from_env()
        self.assertEqual(limits, TableLimits(100000, 1, 3))
        with self.assertRaises(ApiError) as caught:
            inspect_table({"source": "upload", "filename": "交付.xlsx", "xlsx": sample_xlsx()}, limits)
        self.assertEqual(caught.exception.code, "csv_rows")
        with self.assertRaises(ApiError) as caught:
            build_table_tool(self.inspect_upload("事项\n第一条\n第二条"), limits)
        self.assertEqual(caught.exception.code, "csv_rows")
        with self.assertRaises(ApiError) as caught:
            inspect_table({"source": "upload", "filename": "交付.xlsx", "xlsx": sample_xlsx()}, TableLimits(bytes=64))
        self.assertEqual(caught.exception.code, "csv_size")


if __name__ == "__main__":
    unittest.main()
