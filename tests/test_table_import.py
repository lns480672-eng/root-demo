"""TEST-010: CSV inspection and safe prefill for a confirmed table."""

import json
import unittest

from accounts import ApiError
from table_import import build_table_tool, inspect_table


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
            ("事项\n" + "事" * (96 * 1024 // 3 + 1), "csv_size"),
            ("事项\n" + "第一条\n" * 201, "csv_rows"),
        ]
        for source, code in cases:
            with self.subTest(code=code), self.assertRaises(ApiError) as caught:
                self.inspect_upload(source)
            self.assertEqual(caught.exception.code, code)
            self.assertTrue(str(caught.exception))
        for filename in ("项目.xlsx", "项目\ud800.csv"):
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


if __name__ == "__main__":
    unittest.main()
