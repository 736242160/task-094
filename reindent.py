#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
reindent.py — 嵌套结构缩进重排工具（纯 Python 标准库，单文件）

设计说明（关键选择的理由）
==========================
1. 嵌套界定：用显式括号 { } ( ) [ ] 界定嵌套，而不用“冒号+缩进”式关键字。
   理由：括号是显式的开/闭记号，嵌套状态完全由记号流决定、与缩进无关。
   因此重排（只改每行前导空白）不会改变结构，按同一套规则重新解析输出
   即可精确还原嵌套——这直接保证“可逆”。若用缩进本身界定结构，
   改缩进就等于改结构，可逆性无从谈起。

2. 缩进宽度：4 个空格（可用 --width 调整）。
   理由：4 空格是主流约定（PEP 8 等），层级肉眼易分辨；不用 Tab，
   因为 Tab 的显示宽度依赖编辑器，同级对齐在不同环境下会失真。

3. 重排规则（确定性，保证可逆）：
   R1 普通行：缩进 = 当前嵌套深度 × 宽度（同级对齐、子级递增）。
   R2 闭括号起始行：行首有效记号是闭括号时，先按匹配的前导闭括号数
      回退层级再缩进（`}` 与其开括号同级）。
   R3 空行输出为空行；纯注释行按当前深度缩进（注释不携带结构）。
   R4 多行字符串 / 块注释内部的行原样保留、绝不重排——
      其前导空白属于字符串字面值，改动会破坏数据（也会破坏可逆性）。
   R5 冲突：一行同时存在两种合法缩进时（闭括号起始行既可按 R2 回退、
      又可按 R1 视为当前块内容），且该行不是纯闭括号行（如 `} else {`），
      或它关闭的是一个空块，则记入冲突清单（行号 + 两个候选缩进），
      并确定性地采用 R2。冲突是告警，不影响输出。

4. 错误报告：嵌套未闭合（报告开括号起始行/列）、闭括号与开括号不匹配、
   多余的闭括号、嵌套深度超过 --max-depth（默认 32）。

5. 豁免：字符串（'...' "..." '''...''' 三引号，支持反斜杠转义）与
   注释（#、//、块注释）中的括号不参与嵌套计算。

用法：
    python3 reindent.py [输入文件] [--width N] [--max-depth N]
    python3 reindent.py --selftest
重排结果写 stdout，错误/冲突清单写 stderr；存在错误时退出码为 1。
"""

import argparse
import sys
from dataclasses import dataclass

OPENERS = {'{': '}', '(': ')', '[': ']'}
CLOSERS = {v: k for k, v in OPENERS.items()}
STRING_QUOTES = ('"', "'")


@dataclass
class Issue:
    kind: str            # unclosed / mismatch / stray-closer / depth / conflict
    line: int
    col: int = 0
    message: str = ''
    candidates: tuple = ()   # conflict 时的候选缩进（列数）

    def __str__(self):
        loc = '行 %d' % self.line + (', 列 %d' % self.col if self.col else '')
        extra = ''
        if self.candidates:
            extra = '；候选缩进: %s 列' % ' 或 '.join(map(str, self.candidates))
        return '[%s] %s: %s%s' % (self.kind, loc, self.message, extra)


@dataclass
class LineInfo:
    events: list           # [(col, bracket_char)] 按出现顺序
    tokens: list           # 有效记号流：'code' / 'str' / 括号字符
    leading_closers: int   # 行首连续闭括号数
    starts_inside: bool    # 行首处于多行字符串/块注释内部
    has_content: bool
    is_blank: bool
    ends_with_opener: bool


class Scanner:
    """跨行词法扫描：识别字符串与注释（豁免），抽取括号事件。"""

    def __init__(self):
        self.string_delim = None    # None / "'" / '"' / 三引号
        self.in_block_comment = False

    def scan(self, line):
        events, tokens = [], []
        starts_inside = self.string_delim is not None or self.in_block_comment
        i, n = 0, len(line)
        while i < n:
            ch = line[i]
            if self.string_delim is not None:
                delim = self.string_delim
                if len(delim) == 1 and ch == '\\':
                    i += 2
                    continue
                if line.startswith(delim, i):
                    self.string_delim = None
                    i += len(delim)
                else:
                    i += 1
                continue
            if self.in_block_comment:
                end = line.find('*/', i)
                if end < 0:
                    i = n
                else:
                    self.in_block_comment = False
                    i = end + 2
                continue
            if ch in ' \t':
                i += 1
                continue
            if ch == '#' or line.startswith('//', i):
                break
            if line.startswith('/*', i):
                self.in_block_comment = True
                i += 2
                continue
            if ch in STRING_QUOTES:
                if line.startswith(ch * 3, i):
                    self.string_delim = ch * 3
                    i += 3
                else:
                    self.string_delim = ch
                    i += 1
                tokens.append('str')
                continue
            if ch in OPENERS or ch in CLOSERS:
                events.append((i + 1, ch))
                tokens.append(ch)
                i += 1
                continue
            tokens.append('code')
            i += 1
        leading = 0
        for tok in tokens:
            if tok in CLOSERS:
                leading += 1
            else:
                break
        return LineInfo(
            events=events,
            tokens=tokens,
            leading_closers=leading,
            starts_inside=starts_inside,
            has_content=bool(tokens),
            is_blank=not tokens and line.strip() == '',
            ends_with_opener=bool(tokens) and tokens[-1] in OPENERS,
        )


def _apply_events(info, stack, issues, lineno, max_depth):
    for col, ch in info.events:
        if ch in OPENERS:
            stack.append((ch, lineno, col))
            if len(stack) == max_depth + 1:
                issues.append(Issue(
                    'depth', lineno, col,
                    '嵌套深度 %d 超过上限 %d' % (len(stack), max_depth)))
        else:
            if not stack:
                issues.append(Issue(
                    'stray-closer', lineno, col,
                    "多余的闭括号 '%s'：没有对应的开括号" % ch))
            elif stack[-1][0] == CLOSERS[ch]:
                stack.pop()
            else:
                open_ch, l0, c0 = stack[-1]
                issues.append(Issue(
                    'mismatch', lineno, col,
                    "闭括号 '%s' 与 行 %d 列 %d 的开括号 '%s' 不匹配"
                    % (ch, l0, c0, open_ch)))


def reindent(lines, width=4, max_depth=32):
    """逐行重排。返回 (输出行列表, 问题清单)。"""
    scanner = Scanner()
    out_lines, issues, stack = [], [], []
    prev_opened = False   # 上一行是否以开括号结尾（空块冲突判定用）

    for lineno, raw in enumerate(lines, 1):
        info = scanner.scan(raw)

        # R4：多行字符串/块注释内部的行原样保留（仍处理行尾括号事件以维持状态）
        if info.starts_inside:
            out_lines.append(raw)
            _apply_events(info, stack, issues, lineno, max_depth)
            prev_opened = False
            continue

        # R3：空行 / 纯注释行
        if not info.has_content:
            if info.is_blank:
                out_lines.append('')
            else:
                out_lines.append(' ' * (len(stack) * width) + raw.strip())
            prev_opened = False
            continue

        # R2：前导闭括号中真正匹配栈顶的个数决定回退层级
        matched = 0
        for tok in info.tokens[:info.leading_closers]:
            idx = len(stack) - 1 - matched
            if idx >= 0 and stack[idx][0] == CLOSERS[tok]:
                matched += 1
            else:
                break
        level = len(stack) - matched

        # R5：冲突检测——同一行存在两种合法缩进
        if info.leading_closers > 0:
            has_trailing = len(info.tokens) > info.leading_closers
            content_level = len(stack)
            if (has_trailing or prev_opened) and content_level != level:
                issues.append(Issue(
                    'conflict', lineno,
                    message=('重排规则冲突：按闭括号回退规则应缩进 %d 列，'
                             '按当前块内容规则应缩进 %d 列；已确定性地采用回退规则'
                             % (level * width, content_level * width)),
                    candidates=(level * width, content_level * width)))

        # R1：按层级输出
        out_lines.append(' ' * (level * width) + raw.strip())
        _apply_events(info, stack, issues, lineno, max_depth)
        prev_opened = info.ends_with_opener

    for ch, l0, c0 in stack:
        issues.append(Issue(
            'unclosed', l0, c0,
            "嵌套未闭合：'%s' 从此处开始，直到输入结束都未关闭" % ch))
    return out_lines, issues


def structure_signature(lines):
    """嵌套结构签名：括号事件序列（忽略缩进、字符串、注释）。
    输入与输出的签名一致即证明重排可逆（结构可还原）。"""
    scanner = Scanner()
    sig = []
    for line in lines:
        sig.extend(ch for _, ch in scanner.scan(line).events)
    return sig


# ---------------------------------------------------------------- 自测

def selftest():
    failures = []

    def check(name, cond, extra=''):
        if cond:
            print('  PASS %s' % name)
        else:
            failures.append(name)
            print('  FAIL %s %s' % (name, extra))

    print('== 1. 基本重排：同级对齐、子级递增 ==')
    src = ['def f() {',
           '        x = 1',
           '        if (x > 0) {',
           '        print(x)',
           '}',
           '}']
    out, issues = reindent(src)
    expect = ['def f() {',
              '    x = 1',
              '    if (x > 0) {',
              '        print(x)',
              '    }',
              '}']
    check('输出符合预期', out == expect, repr(out))
    check('无错误', not issues, repr(issues))

    print('== 2. 字符串与注释豁免 ==')
    src = ['x = "}{[( not brackets"   # 注释里的 { ( [ 也不算',
           '# 整行注释 { [ (',
           'y = 2  // 行尾注释 }',
           '/* 块注释 { [ */',
           'z = 3']
    out, issues = reindent(src)
    check('括号不计入嵌套', not issues and out[4] == 'z = 3', repr((out, issues)))

    print('== 3. 多行字符串内部原样保留 ==')
    src = ['s = """',
           '   keep { this   ',
           '"""',
           'after = 1']
    out, issues = reindent(src)
    check('字符串内部行未改动', out[1] == '   keep { this   ', repr(out))
    check('字符串内的 { 不影响嵌套', not issues, repr(issues))

    print('== 4. 嵌套未闭合：报告起始位置 ==')
    out, issues = reindent(['foo (', '  bar {', 'x = 1'])
    unclosed = [i for i in issues if i.kind == 'unclosed']
    check('报告两个未闭合', len(unclosed) == 2, repr(issues))
    check('报告起始行/列',
          any(i.line == 1 and i.col == 5 for i in unclosed) and
          any(i.line == 2 and i.col == 7 for i in unclosed), repr(unclosed))

    print('== 5. 闭括号不匹配 / 多余闭括号 ==')
    _, issues = reindent(['a ( }'])
    check('不匹配被报告', any(i.kind == 'mismatch' for i in issues), repr(issues))
    _, issues = reindent(['}'])
    check('多余闭括号被报告', any(i.kind == 'stray-closer' for i in issues))

    print('== 6. 重排规则冲突：报告行号与候选缩进 ==')
    out, issues = reindent(['foo {', '} else {', '}'])
    conflicts = [i for i in issues if i.kind == 'conflict']
    check('`} else {` 触发冲突', any(c.line == 2 for c in conflicts), repr(issues))
    check('候选缩进为 (0, 4)',
          any(c.candidates == (0, 4) for c in conflicts), repr(conflicts))
    check('空块闭合也触发冲突（第 3 行）',
          any(c.line == 3 for c in conflicts), repr(conflicts))
    check('冲突下仍确定性输出', out == ['foo {', '} else {', '}'], repr(out))

    print('== 7. 嵌套深度上限 ==')
    _, issues = reindent(['a {', 'b {', 'c {'], max_depth=2)
    check('超限被报告（行 3）',
          any(i.kind == 'depth' and i.line == 3 for i in issues), repr(issues))

    print('== 8. 可逆性：结构签名一致 + 幂等 ==')
    messy = ['def main() {',
             '    if (ok) {',
             '            go()',
             '        } else {',
             '    stop()',
             '        }',
             '}']
    out, issues = reindent(messy)
    errors = [i for i in issues if i.kind != 'conflict']
    check('无错误', not errors, repr(errors))
    check('结构签名一致（可还原嵌套）',
          structure_signature(messy) == structure_signature(out))
    out2, _ = reindent(out)
    check('幂等（重排输出再重排不变）', out2 == out, repr(out2))

    print('== 9. 同级对齐 ==')
    out, _ = reindent(['a {', 'x=1', '  y=2', 'z=3', '}'])
    check('同级行缩进相同',
          out[1] == '    x=1' and out[2] == '    y=2' and out[3] == '    z=3',
          repr(out))

    print()
    if failures:
        print('自测失败: %s' % ', '.join(failures))
        return 1
    print('全部自测通过。')
    return 0


# ---------------------------------------------------------------- 入口

def main(argv=None):
    parser = argparse.ArgumentParser(
        description='嵌套结构缩进重排工具（括号界定，可逆，含错误报告）')
    parser.add_argument('path', nargs='?', default='-',
                        help='输入文件（缺省或 - 表示标准输入）')
    parser.add_argument('--width', type=int, default=4, help='缩进宽度（默认 4）')
    parser.add_argument('--max-depth', type=int, default=32, help='嵌套深度上限（默认 32）')
    parser.add_argument('--selftest', action='store_true', help='运行内置自测')
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    if args.path == '-':
        lines = sys.stdin.read().splitlines()
    else:
        with open(args.path, 'r', encoding='utf-8') as f:
            lines = f.read().splitlines()

    out_lines, issues = reindent(lines, width=args.width, max_depth=args.max_depth)

    for line in out_lines:
        print(line)

    errors = [i for i in issues if i.kind != 'conflict']
    if issues:
        print('\n----- 报告（%d 错误, %d 冲突告警）-----'
              % (len(errors), len(issues) - len(errors)), file=sys.stderr)
        for issue in issues:
            print(str(issue), file=sys.stderr)
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
