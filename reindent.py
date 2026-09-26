#!/usr/bin/env python3
"""reindent.py — 缩进重排工具（纯 Python 标准库，单文件）

逐行读入嵌套结构代码行流，按嵌套层级重排缩进，输出重排后的行流与错误清单。

设计决策（自定并说明理由）
-------------------------
1. 嵌套界定：只用花括号 `{` / `}`，不用关键字。
   理由：花括号是 C/Java/JS/Go/Rust 等主流语言共同的块界定符，单字符、
   无歧义、无需关键字表；关键字界定（begin/end、then/fi 等）依赖具体语言
   语法，误判率高。圆括号/方括号属于表达式级结构，跨行时往往只是表达式
   续行而非块层级，计入会把续行误当嵌套，故不纳入。

2. 缩进宽度：4 个空格（--width 可调）。
   理由：4 是 PEP 8 与多数 C/C++/Java 风格指南的通用值；用空格不用 Tab，
   保证任何查看器下对齐一致——可逆性要求缩进是嵌套深度的确定性函数。

3. 行缩进规则：设行首深度为 D，行首（第一个 `{` 之前）有 c 个有效闭合
   括号 `}`，则该行缩进层级 = D - c；否则 = D。同级自然对齐、子级递增。

4. 冲突规则：一行同时含"行首闭合括号"与"开启括号"（如 `} else {`）时，
   存在两种合法缩进：
     A = D - c  闭合对齐：与它所闭合块的开启行对齐（本工具确定采用）
     B = D      体级对齐：Whitesmiths 风格把边界括号放在块体层级
   可逆性要求缩进只由深度决定，故固定选 A，并把 (行, 候选缩进) 报告出来。

5. 豁免：字符串字面量（'...'、"..."，支持反斜杠转义）与注释（// 行注释、
   跨行 /* ... */ 块注释）中的括号不参与嵌套计算。

6. 深度上限：默认 16（--max-depth 可调）。超过即报告；正常代码极少超过
   16 层块嵌套，超过几乎总是失控或病态生成代码。

7. 可逆性：重排只改写行首空白，不增删任何 token；每行缩进是嵌套深度的
   纯函数，因此用同一套规则重新解析输出即可还原完全相同的嵌套结构。
   自测验证 structure(源) == structure(重排结果) 且重排幂等。

用法：
    python3 reindent.py [文件]          # 缺省读标准输入，重排流写 stdout
    python3 reindent.py --demo          # 对内置乱缩进样例做重排演示
    python3 reindent.py --selftest      # 运行内置自测
错误清单写 stderr；存在 error 级问题时退出码为 1。
"""

from __future__ import annotations

import argparse
import sys
import unittest
from dataclasses import dataclass

OPEN, CLOSE = '{', '}'
QUOTES = ('"', "'")
DEFAULT_WIDTH = 4
DEFAULT_MAX_DEPTH = 16


@dataclass
class Issue:
    """一条错误/警告。kind 取值：
    unclosed-block / unmatched-close / depth-exceeded /
    unterminated-string / unterminated-comment / conflict
    """
    line: int
    kind: str
    message: str
    severity: str = 'error'      # conflict 为 warning，其余为 error
    col: int = 0                 # 1 起始列，0 表示不适用
    candidates: tuple = ()       # conflict 的候选缩进层级

    def __str__(self) -> str:
        loc = f'第{self.line}行'
        if self.col:
            loc += f':第{self.col}列'
        text = f'[{self.severity}] {loc} {self.kind}: {self.message}'
        if self.candidates:
            text += f'（候选缩进层级: {list(self.candidates)}）'
        return text


def scan_line(text, in_block_comment):
    """扫描一行，跳过字符串与注释，返回括号事件。

    返回 (events, still_in_block_comment, opened_comment_col, bad_string)：
    - events: [(列号1起始, '{'或'}'), ...]，按列排序
    - opened_comment_col: 本行开启且到行尾未闭合的块注释起始列，否则 None
    - bad_string: 行尾仍处于字符串中（未闭合字符串）
    """
    events = []
    opened_comment_col = None
    bad_string = False
    i, n = 0, len(text)
    while i < n:
        if in_block_comment:
            end = text.find('*/', i)
            if end == -1:
                return events, True, opened_comment_col, bad_string
            i = end + 2
            in_block_comment = False
            continue
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ''
        if ch == '/' and nxt == '/':
            break
        if ch == '/' and nxt == '*':
            in_block_comment = True
            opened_comment_col = i + 1
            i += 2
            continue
        if ch in QUOTES:
            quote = ch
            j = i + 1
            closed = False
            while j < n:
                if text[j] == '\\':
                    j += 2
                    continue
                if text[j] == quote:
                    closed = True
                    break
                j += 1
            if not closed:
                bad_string = True
                return events, in_block_comment, opened_comment_col, bad_string
            i = j + 1
            continue
        if ch == OPEN or ch == CLOSE:
            events.append((i + 1, ch))
        i += 1
    return events, in_block_comment, opened_comment_col, bad_string


def reindent(lines, width=DEFAULT_WIDTH, max_depth=DEFAULT_MAX_DEPTH):
    """重排行流。返回 (重排后的行列表, Issue 列表)。"""
    out_lines = []
    issues = []
    depth = 0
    stack = []                 # 未闭合 '{' 的 (行, 列)
    in_block_comment = False
    comment_start = None       # 未闭合块注释的 (行, 列)

    for lineno, raw in enumerate(lines, 1):
        text = raw.rstrip('\r\n')
        if not text.strip():
            out_lines.append('')   # 空行保持为空，不加缩进
            continue

        events, in_block_comment, opened_col, bad_string = scan_line(
            text, in_block_comment)
        if in_block_comment and opened_col is not None:
            comment_start = (lineno, opened_col)
        if bad_string:
            issues.append(Issue(
                lineno, 'unterminated-string',
                '行内字符串未闭合，该行其余内容按字符串豁免处理'))

        # 行首（第一个 '{' 之前）的有效闭合括号数
        leading = 0
        for _, ch in events:
            if ch == CLOSE:
                leading += 1
            else:
                break
        effective_leading = min(leading, depth)
        indent_level = depth - effective_leading

        # 冲突：行首闭合 + 行内开启 => 闭合对齐(D-c) 与体级对齐(D) 皆合法
        if effective_leading > 0 and any(ch == OPEN for _, ch in events):
            issues.append(Issue(
                lineno, 'conflict',
                '边界行同时闭合与开启块，存在两种合法缩进；'
                '为保证可逆性，确定采用闭合对齐',
                severity='warning',
                candidates=(indent_level, depth)))

        # 应用本行事件，更新嵌套状态
        for col, ch in events:
            if ch == OPEN:
                depth += 1
                stack.append((lineno, col))
                if depth > max_depth:
                    issues.append(Issue(
                        lineno, 'depth-exceeded',
                        f'嵌套深度 {depth} 超过上限 {max_depth}', col=col))
            else:
                if depth > 0:
                    depth -= 1
                    stack.pop()
                else:
                    issues.append(Issue(
                        lineno, 'unmatched-close',
                        '闭合括号没有对应的开启括号', col=col))

        out_lines.append(' ' * (width * indent_level) + text.strip())

    # 输入结束：报告未闭合的块（含起始位置）与未闭合的块注释
    for lno, col in stack:
        issues.append(Issue(
            lno, 'unclosed-block', '块开启后直至输入结束未闭合', col=col))
    if in_block_comment and comment_start:
        issues.append(Issue(
            comment_start[0], 'unterminated-comment',
            '块注释开启后直至输入结束未闭合', col=comment_start[1]))
    return out_lines, issues


def parse_structure(lines):
    """用重排规则解析行流，返回 (每行括号事件序列, 结束深度)。

    与缩进无关：对重排前后的行流解析结果相同，即嵌套结构可还原。
    """
    depth = 0
    in_block_comment = False
    per_line = []
    for raw in lines:
        events, in_block_comment, _, _ = scan_line(
            raw.rstrip('\r\n'), in_block_comment)
        seq = []
        for _, ch in events:
            seq.append(ch)
            if ch == OPEN:
                depth += 1
            elif depth > 0:
                depth -= 1
        per_line.append(tuple(seq))
    return per_line, depth


def print_report(issues, file):
    errors = [i for i in issues if i.severity == 'error']
    warnings = [i for i in issues if i.severity == 'warning']
    print(f'--- 错误清单：{len(errors)} 个错误，{len(warnings)} 个警告 ---',
          file=file)
    for issue in issues:
        print(str(issue), file=file)


DEMO_SAMPLE = '''\
if (user) {
        name = "a { b }";   // 字符串与注释里的括号不算
        /* 块注释里的 { } 也不算
           跨行注释 */
if (admin) {
            grant();
} else {
        deny();
}
}
'''


class ReindentTests(unittest.TestCase):
    def test_basic_nesting(self):
        src = ['if (a) {', 'x = 1;', 'if (b) {', 'y = 2;', '}', '}']
        out, issues = reindent(src)
        self.assertEqual(out, [
            'if (a) {',
            '    x = 1;',
            '    if (b) {',
            '        y = 2;',
            '    }',
            '}',
        ])
        self.assertEqual(issues, [])

    def test_messy_input_normalized(self):
        src = ['if (a) {', '            x = 1;', ' }']
        out, _ = reindent(src)
        self.assertEqual(out, ['if (a) {', '    x = 1;', '}'])

    def test_empty_lines_and_width(self):
        out, _ = reindent(['a {', '', 'b;', '}'], width=2)
        self.assertEqual(out, ['a {', '', '  b;', '}'])

    def test_conflict_boundary_line(self):
        src = ['if (a) {', 'x();', '} else {', 'y();', '}']
        out, issues = reindent(src)
        conflicts = [i for i in issues if i.kind == 'conflict']
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].line, 3)
        self.assertEqual(conflicts[0].candidates, (0, 1))
        self.assertEqual(conflicts[0].severity, 'warning')
        self.assertEqual(out[2], '} else {')   # 确定采用闭合对齐

    def test_string_and_line_comment_exempt(self):
        src = [
            'if (a) {',
            's = "}{{{"; // } {',
            "t = '}\\'';",
            'x = 1;',
            '}',
        ]
        out, issues = reindent(src)
        self.assertEqual(out[1], '    s = "}{{{"; // } {')
        self.assertEqual(out[3], '    x = 1;')
        self.assertEqual([i for i in issues if i.severity == 'error'], [])

    def test_block_comment_spanning_lines_exempt(self):
        src = ['if (a) {', '/* {', 'still comment } */', 'x();', '}']
        out, issues = reindent(src)
        self.assertEqual(out[2], '    still comment } */')
        self.assertEqual(out[3], '    x();')
        self.assertEqual([i for i in issues if i.severity == 'error'], [])

    def test_unclosed_block_reports_start_position(self):
        src = ['if (a) {', 'x();', 'if (b) {', 'y();', '}']
        _, issues = reindent(src)
        unclosed = [i for i in issues if i.kind == 'unclosed-block']
        self.assertEqual(len(unclosed), 1)
        self.assertEqual((unclosed[0].line, unclosed[0].col), (1, 8))

    def test_unmatched_close(self):
        out, issues = reindent(['x();', '}'])
        self.assertEqual(issues[0].kind, 'unmatched-close')
        self.assertEqual((issues[0].line, issues[0].col), (2, 1))
        self.assertEqual(out[1], '}')

    def test_depth_limit(self):
        _, issues = reindent(['{' * 5], max_depth=3)
        exceeded = [i for i in issues if i.kind == 'depth-exceeded']
        self.assertEqual(len(exceeded), 2)   # 深度 4、5 各报一次

    def test_unterminated_comment_reports_start(self):
        _, issues = reindent(['if (a) {', '/* never ends', 'x();'])
        kinds = [i.kind for i in issues]
        self.assertIn('unterminated-comment', kinds)
        c = [i for i in issues if i.kind == 'unterminated-comment'][0]
        self.assertEqual((c.line, c.col), (2, 1))

    def test_reversible_and_idempotent(self):
        src = [
            'if (a) {',
            '    x = "}";',
            '        if (b) {',
            'y();',
            '} else {',
            '  z();',
            '}',
            '}',
        ]
        out1, _ = reindent(src)
        out2, _ = reindent(out1)
        self.assertEqual(out1, out2)  # 幂等：重排结果是不动点
        # 可逆：按重排规则解析，源与重排结果的嵌套结构完全一致
        self.assertEqual(parse_structure(src), parse_structure(out1))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='缩进重排工具：按 {} 嵌套层级规范化缩进，'
                    'stdout 输出重排行流，stderr 输出错误清单。')
    parser.add_argument('file', nargs='?', help='输入文件；缺省读标准输入')
    parser.add_argument('--width', type=int, default=DEFAULT_WIDTH,
                        help=f'缩进宽度（空格数），默认 {DEFAULT_WIDTH}')
    parser.add_argument('--max-depth', type=int, default=DEFAULT_MAX_DEPTH,
                        help=f'嵌套深度上限，默认 {DEFAULT_MAX_DEPTH}')
    parser.add_argument('--selftest', action='store_true', help='运行内置自测')
    parser.add_argument('--demo', action='store_true', help='运行内置演示样例')
    args = parser.parse_args(argv)

    if args.selftest:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(ReindentTests)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1

    if args.demo:
        lines = DEMO_SAMPLE.splitlines()
    elif args.file:
        with open(args.file, 'r', encoding='utf-8') as fh:
            lines = fh.read().splitlines()
    else:
        lines = sys.stdin.read().splitlines()

    out_lines, issues = reindent(lines, width=args.width,
                                 max_depth=args.max_depth)
    for line in out_lines:
        print(line)
    print_report(issues, file=sys.stderr)
    return 1 if any(i.severity == 'error' for i in issues) else 0


if __name__ == '__main__':
    sys.exit(main())
