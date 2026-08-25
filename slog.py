#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""slog — 极简学习记录 CLI

一天一个 Markdown 文件（YYYY-MM-DD.md），五个板块，用你喜欢的编辑器书写。
数据永远是纯文本，属于你自己。
"""

import argparse
import os
import re
import shlex
import subprocess
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

DEFAULT_LOG_DIR = Path.home() / "Desktop/University/Learning log"

BUILTIN_TEMPLATE = """\
# Learning Log — YYYY-MM-DD

## 学了什么
-

## 弄懂了什么
-

## [?] 还没懂
-

## [!] 错误 / 卡点
-

## 明天第一件事
- [ ] """

SEC_LEARNED = "学了什么"
SEC_UNDERSTOOD = "弄懂了什么"
SEC_UNCLEAR = "[?] 还没懂"
SEC_BLOCKED = "[!] 错误 / 卡点"
SEC_TOMORROW = "明天第一件事"

SECTION_STYLE = {
    SEC_LEARNED: ("32",),      # 绿
    SEC_UNDERSTOOD: ("36",),   # 青
    SEC_UNCLEAR: ("33",),      # 黄
    SEC_BLOCKED: ("31",),      # 红
    SEC_TOMORROW: ("35",),     # 紫
}

DATE_FILE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\.md$")
HEADING_RE = re.compile(r"^(#+)\s+(.+?)\s*$")
BULLET_RE = re.compile(r"^[-*+]\s*(?:\[([ xX])\]\s*)?(.*)$")

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


# ---------- 终端着色 ----------

def paint(text, *codes):
    if not COLOR:
        return text
    return "\033[" + ";".join(codes) + "m" + text + "\033[0m"


def bold(t):
    return paint(t, "1")


def dim(t):
    return paint(t, "2")


def red(t):
    return paint(t, "31")


def green(t):
    return paint(t, "32")


def yellow(t):
    return paint(t, "33")


def cyan(t):
    return paint(t, "36")


def disp_width(s):
    """东亚字符按两个宽度计，用于对齐。"""
    return sum(2 if ord(c) > 0x2E80 else 1 for c in s)


def pad(s, width):
    return s + " " * max(0, width - disp_width(s))


# ---------- 基础工具 ----------

def die(msg):
    print(red("✗ " + msg), file=sys.stderr)
    sys.exit(1)


def log_dir():
    env = os.environ.get("SLOG_DIR")
    return Path(env).expanduser() if env else DEFAULT_LOG_DIR


def log_file(d, day):
    return d / f"{day.isoformat()}.md"


def log_dates(d):
    if not d.is_dir():
        return []
    days = []
    for f in d.iterdir():
        m = DATE_FILE_RE.match(f.name)
        if not (m and f.is_file()):
            continue
        try:
            days.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            continue
    return sorted(days)


def resolve_date(spec):
    if not spec:
        return date.today()
    s = spec.strip().lower()
    if s in ("today", "t", "今天"):
        return date.today()
    if s in ("yesterday", "y", "昨天"):
        return date.today() - timedelta(days=1)
    m = re.fullmatch(r"-(\d{1,3})", s)
    if m:
        return date.today() - timedelta(days=int(m.group(1)))
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            die(f"日期不合法：{spec}")
    die(f"无法识别的日期「{spec}」，支持：YYYY-MM-DD / yesterday / -N")


def read_text(f):
    return f.read_text(encoding="utf-8")


# ---------- 日志解析 ----------

def parse_sections(text):
    """按二级标题切分，返回 {板块名: [该板块下的原始行]}。"""
    sections, current = {}, None
    for line in text.splitlines():
        m = HEADING_RE.match(line)
        if m:
            current = m.group(2) if len(m.group(1)) == 2 else None
            if current is not None:
                sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return sections


def bullets(lines):
    """解析条目，返回 [(checkbox 状态, 文字)]；checkbox 为 None 表示无复选框。"""
    out = []
    for ln in lines:
        m = BULLET_RE.match(ln.strip())
        if m:
            out.append((m.group(1), m.group(2).strip()))
    return out


def nonempty_bullets(lines):
    return [(c, t) for c, t in bullets(lines) if t]


def count_items(secs, key, unchecked_only=False):
    items = nonempty_bullets(secs.get(key, []))
    if unchecked_only:
        return sum(1 for c, t in items if c not in ("x", "X"))
    return len(items)


# ---------- 新建日志与待办衔接 ----------

def load_template(d):
    t = d / "template.md"
    if t.is_file():
        try:
            return read_text(t)
        except OSError:
            pass
    return BUILTIN_TEMPLATE


def find_prev_log(d, day):
    """目标日期之前最近的一篇日志，返回 (路径, 日期) 或 (None, None)。"""
    prevs = [x for x in log_dates(d) if x < day]
    if not prevs:
        return None, None
    p = prevs[-1]
    return log_file(d, p), p


def extract_carry(text):
    """从前一篇日志提取未完成内容：还没懂、错误/卡点、未勾选的明天第一件事。"""
    secs = parse_sections(text)
    carry = {}
    for key in (SEC_UNCLEAR, SEC_BLOCKED):
        items = [t for _, t in nonempty_bullets(secs.get(key, []))]
        if items:
            carry[key] = [f"- {t}" for t in items]
    todos = [t for c, t in bullets(secs.get(SEC_TOMORROW, [])) if t and c not in ("x", "X")]
    if todos:
        carry[SEC_TOMORROW] = [f"- [ ] {t}" for t in todos]
    return carry


def build_new_log(d, day, carry):
    """按模板生成新日志，把 carry 中的条目填进对应板块（替换空占位行）。"""
    text = load_template(d).replace("YYYY-MM-DD", day.isoformat())
    out, section, emitted = [], None, set()
    for line in text.splitlines():
        m = HEADING_RE.match(line)
        if m:
            section = m.group(2) if len(m.group(1)) == 2 else None
            out.append(line)
            continue
        bm = BULLET_RE.match(line.strip())
        if (bm and section in carry and section not in emitted
                and not bm.group(2).strip()):
            out.extend(carry[section])
            emitted.add(section)
            continue
        out.append(line)
    return "\n".join(out) + "\n"


def ensure_log(day):
    """返回该天日志路径；不存在则按模板新建并带入前一篇的未完成内容。"""
    d = log_dir()
    d.mkdir(parents=True, exist_ok=True)
    f = log_file(d, day)
    if f.is_file():
        return f
    prev_path, prev_day = find_prev_log(d, day)
    carry = extract_carry(read_text(prev_path)) if prev_path else {}
    f.write_text(build_new_log(d, day, carry), encoding="utf-8")
    if prev_path and carry:
        parts = []
        if carry.get(SEC_UNCLEAR):
            parts.append(f"{len(carry[SEC_UNCLEAR])} 条未懂")
        if carry.get(SEC_BLOCKED):
            parts.append(f"{len(carry[SEC_BLOCKED])} 条卡点")
        if carry.get(SEC_TOMORROW):
            parts.append(f"{len(carry[SEC_TOMORROW])} 件待办")
        print(green(f"↩ 从 {prev_day.isoformat()} 带入：" + "、".join(parts)))
    return f


# ---------- 编辑器 ----------

def open_in_editor(f):
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nvim"
    try:
        subprocess.run(shlex.split(editor) + [str(f)])
    except FileNotFoundError:
        die(f"找不到编辑器「{editor}」，可用 export EDITOR=code\\ -w 指定")


# ---------- 输出 ----------

def print_log(day, text):
    print(bold(cyan(f"◆ {day.isoformat()} {WEEKDAYS[day.weekday()]}")))
    for line in text.splitlines():
        m = HEADING_RE.match(line)
        if m:
            level, head = len(m.group(1)), m.group(2)
            if level == 1:
                continue  # 标题行换成上面的日期头
            style = SECTION_STYLE.get(head)
            print()
            print(paint(bold(head), *style) if style else bold(head))
            continue
        stripped = line.strip()
        bm = BULLET_RE.match(stripped)
        if stripped and bm:
            checked, content = bm.group(1), bm.group(2)
            prefix = ("- [x] " if checked in ("x", "X")
                      else "- [ ] " if checked else "- ")
            if not content:
                print(dim("  " + prefix))
            elif checked in ("x", "X"):
                print("  " + dim(prefix + content))
            else:
                print("  " + dim(prefix) + content)
        elif stripped:
            print("  " + line)
    print()


def summarize(day):
    f = log_file(log_dir(), day)
    if not f.is_file():
        return
    secs = parse_sections(read_text(f))
    counts = [
        ("学", count_items(secs, SEC_LEARNED)),
        ("懂", count_items(secs, SEC_UNDERSTOOD)),
        ("疑", count_items(secs, SEC_UNCLEAR)),
        ("错", count_items(secs, SEC_BLOCKED)),
        ("待", count_items(secs, SEC_TOMORROW, unchecked_only=True)),
    ]
    summary = " ".join(
        paint(f"{k}{v}", "1") if v else dim(f"{k}{v}") for k, v in counts
    )
    print(green(f"✓ {day.isoformat()} 已保存") + f"（{summary}）")


# ---------- 子命令 ----------

def cmd_edit(args):
    day = resolve_date(args.date)
    f = ensure_log(day)
    open_in_editor(f)
    summarize(day)


def cmd_show(args):
    d = log_dir()
    day = resolve_date(args.date)
    f = log_file(d, day)
    if not f.is_file():
        die(f"{day.isoformat()} 没有记录，可用 slog edit {day.isoformat()} 补记")
    print_log(day, read_text(f))


def cmd_recent(args):
    d = log_dir()
    dates = log_dates(d)
    if not dates:
        die(f"日志目录还没有任何记录：{d}")
    if dates[-1] < date.today():
        print(dim("（提示：今天还没记，敲 slog 开始写）"))
    for day in reversed(dates[-args.n:]):
        secs = parse_sections(read_text(log_file(d, day)))
        line = (f"{count_items(secs, SEC_LEARNED)} 学 "
                f"{count_items(secs, SEC_UNDERSTOOD)} 懂 "
                f"{count_items(secs, SEC_UNCLEAR)} 疑 "
                f"{count_items(secs, SEC_BLOCKED)} 错 "
                f"{count_items(secs, SEC_TOMORROW, unchecked_only=True)} 待")
        learned = nonempty_bullets(secs.get(SEC_LEARNED, []))
        first = learned[0][1] if learned else "（未填内容）"
        if disp_width(first) > 36:
            first = first[:17] + "…"
        print(cyan(f"{day.isoformat()} {WEEKDAYS[day.weekday()]}")
              + "  " + pad(line, 16) + "  " + (first if learned else dim(first)))


def cmd_search(args):
    d = log_dir()
    q = args.keyword.lower()
    hits = 0
    for day in reversed(log_dates(d)):
        f = log_file(d, day)
        for line in read_text(f).splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if q in s.lower():
                print(cyan(day.isoformat()) + "  " + s)
                hits += 1
    if hits:
        print(dim(f"共 {hits} 条"))
    else:
        print(f"没有找到与「{args.keyword}」相关的记录")


def cmd_stats(args):
    d = log_dir()
    dates = log_dates(d)
    if not dates:
        die(f"日志目录还没有任何记录：{d}")

    longest = run = 1
    for a, b in zip(dates, dates[1:]):
        run = run + 1 if (b - a).days == 1 else 1
        longest = max(longest, run)

    cur, i = 1, len(dates) - 1
    while i > 0 and (dates[i] - dates[i - 1]).days == 1:
        cur += 1
        i -= 1

    subjects = Counter()
    for day in dates:
        secs = parse_sections(read_text(log_file(d, day)))
        for key in (SEC_LEARNED, SEC_UNDERSTOOD):
            for _, t in nonempty_bullets(secs.get(key, [])):
                subjects[t.split(None, 1)[0]] += 1

    print(bold("统计") + dim(f"  目录 {d}"))
    print(f"总记录天数  {len(dates)}")
    note = "" if dates[-1] == date.today() else dim(f"（最近记录 {dates[-1].isoformat()}，今天还没记）")
    print(f"当前连续    {cur} 天 {note}")
    print(f"最长连续    {longest} 天")

    if subjects:
        print()
        print(bold("科目分布") + dim("（学了什么 + 弄懂了什么，按条目首词）"))
        width = max(disp_width(k) for k in subjects)
        top = subjects.most_common(10)
        bar_max = max(n for _, n in top)
        for name, n in top:
            if disp_width(name) > 16:
                name = name[:8] + "…"
            bar = "▇" * max(1, round(n / bar_max * 24))
            print(f"  {pad(name, width + 2)}{cyan(bar)} {n}")


def cmd_path(args):
    d = log_dir()
    print(d)
    if d.is_dir():
        print(dim(f"{len(log_dates(d))} 篇日志"))
    else:
        print(dim("目录不存在"))


# ---------- 入口 ----------

def main(argv=None):
    p = argparse.ArgumentParser(
        prog="slog",
        description="slog — 极简学习记录：一天一个 Markdown，五个板块，编辑器书写。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  slog                打开今天的日志（不存在则按模板新建）\n"
            "  slog show yesterday 查看昨天的日志\n"
            "  slog edit -3        补记 3 天前的日志\n"
            "  slog recent 14      最近 14 天一览\n"
            "  slog search 光学    全文搜索\n"
            "  slog stats          连续天数与科目分布\n"
            "\n"
            "环境变量:\n"
            "  SLOG_DIR  日志目录（默认 ~/Desktop/University/Learning log）\n"
            "  EDITOR    编辑器（默认 nvim，可改为 code -w、nano 等）"
        ),
    )
    sub = p.add_subparsers(dest="cmd", metavar="命令")

    sp = sub.add_parser("show", help="显示某天的日志（默认今天）")
    sp.add_argument("date", nargs="?", help="YYYY-MM-DD / yesterday / -N")

    sp = sub.add_parser("edit", help="编辑/补记某天（默认今天），不存在则新建")
    sp.add_argument("date", nargs="?", help="YYYY-MM-DD / yesterday / -N")

    sp = sub.add_parser("recent", help="最近 N 天一览（默认 7）")
    sp.add_argument("n", nargs="?", type=int, default=7)

    sp = sub.add_parser("search", help="按关键词搜索所有日志")
    sp.add_argument("keyword", help="关键词")

    sub.add_parser("stats", help="统计：记录天数、连续天数、科目分布")
    sub.add_parser("path", help="显示日志目录")

    args = p.parse_args(argv)

    handlers = {
        "show": cmd_show,
        "edit": cmd_edit,
        "recent": cmd_recent,
        "search": cmd_search,
        "stats": cmd_stats,
        "path": cmd_path,
    }
    handler = handlers.get(args.cmd, cmd_edit)  # 无子命令 = 打开今天
    if args.cmd is None:
        args.date = None
    handler(args)


if __name__ == "__main__":
    main()
