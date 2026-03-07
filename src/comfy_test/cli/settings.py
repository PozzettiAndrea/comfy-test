"""Settings TUI command for comfy-test."""

import os


def _read_env_file(path):
    """Read KEY=VALUE file, return set of enabled keys."""
    enabled = set()
    if path.exists():
        try:
            for line in path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    if v.strip().lower() in ("1", "true", "yes"):
                        enabled.add(k.strip())
        except Exception:
            pass
    return enabled


def cmd_settings(args) -> int:
    """Configure comfy-test settings (tabbed TUI)."""
    return _open_settings_tui(initial_tab=0)


def add_settings_parser(subparsers):
    sp = subparsers.add_parser("settings", help="Configure comfy-test settings")
    sp.set_defaults(func=cmd_settings)


def _open_settings_tui(initial_tab=0) -> int:
    from ..settings import (
        GENERAL_SETTINGS, GENERAL_DEFAULTS, SETTINGS_FILE,
        DEBUG_SETTINGS, DEBUG_DEFAULTS,
    )

    # Read current state from file
    file_enabled = _read_env_file(SETTINGS_FILE)

    # Merge with live env vars and defaults
    general_enabled = set(file_enabled)
    for var, _ in GENERAL_SETTINGS:
        val = os.environ.get(var, "")
        if val.lower() in ("1", "true", "yes"):
            general_enabled.add(var)
        elif val == "" and GENERAL_DEFAULTS.get(var, False):
            general_enabled.add(var)

    debug_enabled = set(file_enabled)
    for var, _ in DEBUG_SETTINGS:
        val = os.environ.get(var, "")
        if val.lower() in ("1", "true", "yes"):
            debug_enabled.add(var)
        elif val == "" and DEBUG_DEFAULTS.get(var, False):
            debug_enabled.add(var)

    tabs = [
        ("General", GENERAL_SETTINGS, general_enabled, SETTINGS_FILE),
        ("Debug", DEBUG_SETTINGS, debug_enabled, SETTINGS_FILE),
    ]

    try:
        import curses
        return _settings_tui(curses, tabs, initial_tab)
    except ImportError:
        return _settings_text(tabs, initial_tab)


def _settings_tui(curses, tabs, initial_tab):
    """Curses-based tabbed settings TUI."""

    tab_names = [t[0] for t in tabs]
    tab_items = [t[1] for t in tabs]
    tab_selected = [[var in t[2] for var, _ in t[1]] for t in tabs]
    tab_files = [t[3] for t in tabs]

    active_tab = initial_tab
    cursor = 0
    status_msg = ""

    def cur_items():
        return tab_items[active_tab]

    def cur_sel():
        return tab_selected[active_tab]

    def n_items():
        return len(cur_items())

    def draw(stdscr):
        nonlocal active_tab, cursor, status_msg
        curses.curs_set(0)
        if curses.has_colors():
            curses.start_color()
            curses.init_pair(1, curses.COLOR_GREEN, curses.COLOR_BLACK)
            curses.init_pair(2, curses.COLOR_RED, curses.COLOR_BLACK)
            curses.init_pair(3, curses.COLOR_CYAN, curses.COLOR_BLACK)

        while True:
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            stdscr.addstr(0, 2, "comfy-test settings", curses.A_BOLD)
            stdscr.addstr(1, 2, "\u2501" * min(40, w - 4))

            # Tab bar
            col = 2
            for i, name in enumerate(tab_names):
                if i == active_tab:
                    label = f" \u25b8 {name} "
                    attr = curses.A_BOLD | curses.A_REVERSE
                else:
                    label = f"   {name} "
                    attr = curses.A_DIM
                if col + len(label) < w:
                    stdscr.addstr(2, col, label, attr)
                col += len(label) + 1

            # Checkboxes
            items = cur_items()
            sel = cur_sel()
            ni = n_items()
            for i, (var, label) in enumerate(items):
                y = i + 4
                if y >= h - 4:
                    break
                check = "x" if sel[i] else " "
                attr = curses.A_REVERSE if cursor == i else 0
                line = f"  [{check}] {label:<48s} {var}"
                stdscr.addstr(y, 0, line[:w-1], attr)

            # Button row
            btn_y = ni + 5
            if btn_y < h - 2:
                apply_attr = curses.A_REVERSE | curses.A_BOLD if cursor == ni else curses.A_BOLD
                stdscr.addstr(btn_y, 2, "[ Apply & Exit ]", apply_attr)
                quit_attr = curses.A_REVERSE if cursor == ni + 1 else 0
                stdscr.addstr(btn_y, 22, "[ Quit ]", quit_attr)

            # Help
            help_y = btn_y + 2
            if help_y < h:
                stdscr.addstr(help_y, 2,
                              "Tab/\u2190\u2192 switch tab  \u2191\u2193 navigate  Space toggle  q quit",
                              curses.A_DIM)

            # Status message
            if status_msg:
                sy = help_y + 1
                if sy < h:
                    color = curses.color_pair(1) if curses.has_colors() else curses.A_BOLD
                    stdscr.addstr(sy, 2, status_msg, color)

            stdscr.refresh()
            key = stdscr.getch()
            status_msg = ""

            ni = n_items()

            if key in (ord('q'), ord('Q'), 27):  # q or ESC
                return 0
            elif key == 9 or key == curses.KEY_RIGHT:  # Tab or Right
                active_tab = (active_tab + 1) % len(tabs)
                cursor = 0
            elif key == curses.KEY_LEFT:
                active_tab = (active_tab - 1) % len(tabs)
                cursor = 0
            elif key == curses.KEY_UP and cursor > 0:
                cursor -= 1
            elif key == curses.KEY_DOWN and cursor < n_items() + 1:
                cursor += 1
            elif key in (ord(' '), curses.KEY_ENTER, 10, 13):
                ni = n_items()
                if cursor < ni:
                    cur_sel()[cursor] = not cur_sel()[cursor]
                elif cursor == ni:
                    _save_all_settings(tab_items, tab_selected, tab_files)
                    return 0
                elif cursor == ni + 1:
                    return 0
            elif key in (ord('a'), ord('A')):
                _save_all_settings(tab_items, tab_selected, tab_files)
                return 0

    return curses.wrapper(draw)


def _settings_text(tabs, initial_tab):
    """Simple text fallback for systems without curses."""
    tab_items = [t[1] for t in tabs]
    tab_selected = [[var in t[2] for var, _ in t[1]] for t in tabs]
    tab_files = [t[3] for t in tabs]

    print("comfy-test settings")
    print("=" * 40)
    offset = 0
    offsets = []
    for ti, (name, items, _, _) in enumerate(tabs):
        print(f"\n  --- {name} ---")
        offsets.append(offset)
        for i, (var, label) in enumerate(items):
            check = "x" if tab_selected[ti][i] else " "
            print(f"  {offset + i}. [{check}] {label:<48s} {var}")
        offset += len(items)
    print()
    print("Enter numbers to toggle (space-separated), 'save' to save, 'quit' to exit:")

    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if line.lower() in ("q", "quit", "exit"):
            return 0
        if line.lower() in ("s", "save"):
            _save_all_settings(tab_items, tab_selected, tab_files)
            print("Saved.")
            return 0
        for part in line.split():
            try:
                idx = int(part)
                for ti in range(len(tabs)):
                    if idx < offsets[ti] + len(tab_items[ti]):
                        local = idx - offsets[ti]
                        if 0 <= local < len(tab_items[ti]):
                            tab_selected[ti][local] = not tab_selected[ti][local]
                        break
            except ValueError:
                pass
        # Redisplay
        for ti, (name, items, _, _) in enumerate(tabs):
            print(f"\n  --- {name} ---")
            for i, (var, label) in enumerate(items):
                check = "x" if tab_selected[ti][i] else " "
                print(f"  {offsets[ti] + i}. [{check}] {label:<48s} {var}")


def _save_all_settings(tab_items, tab_selected, tab_files):
    """Write settings to their respective files."""
    from collections import defaultdict
    file_entries = defaultdict(list)
    for items, selected, filepath in zip(tab_items, tab_selected, tab_files):
        for i, (var, _) in enumerate(items):
            file_entries[filepath].append((var, selected[i]))
            os.environ[var] = "1" if selected[i] else "0"

    for filepath, entries in file_entries.items():
        filepath.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# comfy-test settings (managed by `comfy-test settings`)\n"]
        for var, enabled in entries:
            lines.append(f"{var}={'1' if enabled else '0'}\n")
        filepath.write_text("".join(lines))

    unique_files = list(dict.fromkeys(str(f) for f in tab_files))
    print(f"Saved to {', '.join(unique_files)}")
