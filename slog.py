#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""slog — 极简学习记录 CLI

一天一个 Markdown 文件（YYYY-MM-DD.md），六个板块，用你喜欢的编辑器书写。
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

## 每日思考
-

## 明天第一件事
- [ ] """

SEC_LEARNED = "学了什么"
SEC_UNDERSTOOD = "弄懂了什么"
SEC_UNCLEAR = "[?] 还没懂"
SEC_BLOCKED = "[!] 错误 / 卡点"
SEC_THOUGHT = "每日思考"
SEC_TOMORROW = "明天第一件事"
SEC_DEBT = "[⚠] Coach 欠账"

# 相对日志目录的欠账清单路径，由每晚 coach 维护；不存在则不启用
DEBT_LEDGER_REL = Path("coach/open-items.md")

SECTION_STYLE = {
    SEC_LEARNED: ("32",),      # 绿
    SEC_UNDERSTOOD: ("36",),   # 青
    SEC_UNCLEAR: ("33",),      # 黄
    SEC_BLOCKED: ("31",),      # 红
    SEC_THOUGHT: ("34",),      # 蓝
    SEC_TOMORROW: ("35",),     # 紫
    SEC_DEBT: ("31",),         # 红（升级欠账）
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


def load_debt_items(d):
    """读 coach 欠账清单，按文件顺序返回未勾选条目（原样文本，含 [标签] 前缀）。"""
    f = d / DEBT_LEDGER_REL
    if not f.is_file():
        return []
    try:
        secs = parse_sections(read_text(f))
    except OSError:
        return []
    items = []
    for lines in secs.values():
        for checked, text in nonempty_bullets(lines):
            if checked not in ("x", "X"):
                items.append(f"- [ ] {text}")
    return items


def build_new_log(d, day, carry, debt=None):
    """按模板生成新日志；欠账条目注入顶部板块，carry 中的条目填进对应板块。"""
    text = load_template(d).replace("YYYY-MM-DD", day.isoformat())
    if debt and f"## {SEC_DEBT}" not in text:
        lines = text.splitlines()
        idx = next((i for i, ln in enumerate(lines) if ln.startswith("## ")), len(lines))
        block = [f"## {SEC_DEBT}"] + list(debt) + [""]
        text = "\n".join(lines[:idx] + block + lines[idx:])
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
    """返回该天日志路径；不存在则按模板新建并带入欠账清单与前一篇的未完成内容。"""
    d = log_dir()
    d.mkdir(parents=True, exist_ok=True)
    f = log_file(d, day)
    if f.is_file():
        return f
    prev_path, prev_day = find_prev_log(d, day)
    carry = extract_carry(read_text(prev_path)) if prev_path else {}
    debt = load_debt_items(d)
    f.write_text(build_new_log(d, day, carry, debt), encoding="utf-8")
    if debt:
        print(red(f"⚠ Coach 欠账 {len(debt)} 条已带入，勾 [x] 销账，否则每天出现"))
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


def snapshot(d, day):
    """日志目录是 git 仓库时，自动把改动提交为本地快照（纯本地，不联网）。"""
    if not (d / ".git").exists():
        return
    try:
        def git(*args):
            return subprocess.run(["git", *args], cwd=d, capture_output=True, text=True)
        if git("status", "--porcelain").stdout.strip():
            git("add", "-A")
            git("commit", "-m", day.isoformat(), "--quiet")
            print(dim(f"已快照 git: {day.isoformat()}"))
    except OSError:
        pass


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
        ("思", count_items(secs, SEC_THOUGHT)),
        ("待", count_items(secs, SEC_TOMORROW, unchecked_only=True)),
        ("欠", count_items(secs, SEC_DEBT, unchecked_only=True)),
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
    snapshot(log_dir(), day)


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
                f"{count_items(secs, SEC_THOUGHT)} 思 "
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


def cmd_review(args):
    d = log_dir()
    dates = log_dates(d)
    if not dates:
        die(f"日志目录还没有任何记录：{d}")
    # 首次出现日期：跨全部日志统计
    first_seen = {}
    for day in dates:
        secs = parse_sections(read_text(log_file(d, day)))
        for key in (SEC_UNCLEAR, SEC_BLOCKED):
            for _, t in nonempty_bullets(secs.get(key, [])):
                first_seen.setdefault((key, t), day)
    # 未解决集合：以最近一篇日志为准（未带到最近一篇的即视为已解决）
    last_day = dates[-1]
    secs = parse_sections(read_text(log_file(d, last_day)))
    print(bold("遗留问题") + dim(f"（以最近一篇 {last_day.isoformat()} 为准）"))
    shown = False
    for key in (SEC_UNCLEAR, SEC_BLOCKED):
        items = [t for _, t in nonempty_bullets(secs.get(key, []))]
        if not items:
            continue
        shown = True
        print()
        print(paint(bold(key), *SECTION_STYLE[key]) + dim(f"（{len(items)}）"))
        for t in items:
            fd = first_seen.get((key, t))
            note = dim(f"  首次 {fd.isoformat()}") if fd and fd < last_day else ""
            print(f"  - {t}{note}")
    if not shown:
        print()
        print(green("没有遗留问题"))
    debt = load_debt_items(d)
    if debt:
        print()
        print(paint(bold("Coach 欠账"), *SECTION_STYLE[SEC_DEBT])
              + dim(f"（{len(debt)} 条未销，见 {DEBT_LEDGER_REL.as_posix()}）"))
        for item in debt:
            print("  " + item)


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


# ---------- TUI ----------

ANSI_TO_CURSES = {"32": "green", "33": "yellow", "31": "red",
                  "35": "magenta", "36": "cyan", "34": "blue"}


def clip_cjk(s, width):
    """按显示宽度截断，避免宽字符越界。"""
    out, w = "", 0
    for ch in s:
        cw = 2 if ord(ch) > 0x2E80 else 1
        if w + cw > width:
            break
        out += ch
        w += cw
    return out


def _log_outline(day, text):
    """把一篇日志整理成 [(文字, 类型, 所属板块)]，供 TUI 右栏渲染。"""
    out = [(f"{day.isoformat()} {WEEKDAYS[day.weekday()]}", "title", None)]
    section = None
    for line in text.splitlines():
        m = HEADING_RE.match(line)
        if m:
            if len(m.group(1)) == 1:
                continue
            section = m.group(2)
            out.append(("", "gap", None))
            out.append((section, "header", section))
            continue
        s = line.strip()
        bm = BULLET_RE.match(s) if s else None
        if bm:
            checked, content = bm.group(1), bm.group(2)
            prefix = ("- [x] " if checked in ("x", "X")
                      else "- [ ] " if checked else "- ")
            if not content:
                out.append(("  " + prefix, "empty", section))
            elif checked in ("x", "X"):
                out.append(("  " + prefix + content, "done", section))
            else:
                out.append(("  " + prefix + content, "item", section))
        elif s:
            out.append(("  " + line, "item", section))
    return out


def _tui_main(stdscr):
    import curses

    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.keypad(True)

    pairs = {}
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        for i, name in enumerate(("green", "yellow", "red", "magenta", "cyan", "blue"), 1):
            curses.init_pair(i, getattr(curses, f"COLOR_{name.upper()}"), -1)
            pairs[name] = curses.color_pair(i)

    def draw(dates, sel, top):
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        if h < 8 or w < 60:
            try:
                stdscr.addstr(0, 0, clip_cjk("终端窗口太小（至少 60x8）", w - 1))
            except curses.error:
                pass
            stdscr.refresh()
            return
        lw, rows = 34, h - 3
        try:
            stdscr.addstr(0, 0, f"slog 学习日志 · {len(dates)} 篇", curses.A_BOLD)
        except curses.error:
            pass
        for i, day in enumerate(dates[top:top + rows]):
            secs = parse_sections(read_text(log_file(log_dir(), day)))
            learned = nonempty_bullets(secs.get(SEC_LEARNED, []))
            first = clip_cjk(learned[0][1], lw - 15) if learned else "—"
            label = f"{day.isoformat()} {WEEKDAYS[day.weekday()]} {first}"
            attr = curses.A_REVERSE if top + i == sel else 0
            try:
                stdscr.addstr(1 + i, 0, clip_cjk(label, lw), attr)
            except curses.error:
                pass
        for y in range(1, h - 1):
            try:
                stdscr.addstr(y, lw, "│", curses.A_DIM)
            except curses.error:
                pass
        if dates:
            day = dates[sel]
            outline = _log_outline(day, read_text(log_file(log_dir(), day)))
            x, pw = lw + 3, w - lw - 4
            y = 1
            for text, kind, sec in outline:
                if y >= h - 1:
                    break
                if kind == "gap":
                    y += 1
                    continue
                attr = 0
                if kind == "title":
                    attr = curses.A_BOLD
                elif kind == "header":
                    code = next(iter(SECTION_STYLE.get(sec, ("0",))))
                    attr = curses.A_BOLD | pairs.get(ANSI_TO_CURSES.get(code, ""), 0)
                elif kind in ("done", "empty"):
                    attr = curses.A_DIM
                try:
                    stdscr.addstr(y, x, clip_cjk(text, pw), attr)
                except curses.error:
                    pass
                y += 1
        else:
            try:
                stdscr.addstr(h // 2, 2, "还没有任何记录，按 t 创建今天")
            except curses.error:
                pass
        try:
            stdscr.addstr(h - 1, 0, "↑↓/jk 选择 · Enter/e 编辑 · t 今天 · q 退出",
                          curses.A_DIM)
        except curses.error:
            pass
        stdscr.refresh()

    sel = top = 0
    dates = list(reversed(log_dates(log_dir())))
    while True:
        h = stdscr.getmaxyx()[0]
        rows = max(1, h - 3)
        sel = min(sel, max(0, len(dates) - 1))
        top = max(0, min(top, sel))
        if sel >= top + rows:
            top = sel - rows + 1
        draw(dates, sel, top)
        key = stdscr.getch()
        if key in (ord("q"), 27):
            return
        if key in (curses.KEY_UP, ord("k")) and dates:
            sel = max(0, sel - 1)
        elif key in (curses.KEY_DOWN, ord("j")) and dates:
            sel = min(len(dates) - 1, sel + 1)
        elif key == ord("t"):
            day = date.today()
            ensure_log(day)
            dates = list(reversed(log_dates(log_dir())))
            sel, top = dates.index(day), 0
        elif key in (curses.KEY_ENTER, 10, 13, ord("e")):
            day = dates[sel] if dates else date.today()
            f = ensure_log(day)
            curses.endwin()
            open_in_editor(f)
            snapshot(log_dir(), day)
            stdscr.refresh()
            dates = list(reversed(log_dates(log_dir())))


def cmd_tui(args):
    try:
        import curses
    except ImportError:
        die("当前 Python 缺少 curses，无法启动 TUI")
    try:
        curses.wrapper(_tui_main)
    except KeyboardInterrupt:
        pass
    except curses.error:
        die("无法初始化终端界面，请在真实终端中运行")


# ---------- 入口 ----------

def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["help"]:
        argv = ["--help"]
    p = argparse.ArgumentParser(
        prog="slog",
        description="slog — 极简学习记录：一天一个 Markdown，六个板块，编辑器书写。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  slog                打开今天的日志（不存在则按模板新建）\n"
            "  slog show yesterday 查看昨天的日志\n"
            "  slog edit -3        补记 3 天前的日志\n"
            "  slog recent 14      最近 14 天一览\n"
            "  slog search 光学    全文搜索\n"
            "  slog review         汇总还没懂/卡点等遗留问题与 Coach 欠账\n"
            "  slog stats          连续天数与科目分布\n"
            "  slog tui            交互式浏览（方向键选择，回车编辑）\n"
            "  slog help           显示本帮助\n"
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

    sub.add_parser("review", help="汇总最近一篇的还没懂/卡点，标注首次出现日期，附 Coach 欠账清单")
    sub.add_parser("tui", help="交互式浏览（curses 界面）")

    sub.add_parser("stats", help="统计：记录天数、连续天数、科目分布")
    sub.add_parser("path", help="显示日志目录")

    args = p.parse_args(argv)

    handlers = {
        "show": cmd_show,
        "edit": cmd_edit,
        "recent": cmd_recent,
        "search": cmd_search,
        "review": cmd_review,
        "tui": cmd_tui,
        "stats": cmd_stats,
        "path": cmd_path,
    }
    handler = handlers.get(args.cmd, cmd_edit)  # 无子命令 = 打开今天
    if args.cmd is None:
        args.date = None
    handler(args)


if __name__ == "__main__":
    main()
