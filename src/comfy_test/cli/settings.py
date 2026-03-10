"""Settings TUI command for comfy-test."""

import os


def _read_env_file(path):
    """Read KEY=VALUE file, return dict of all values."""
    values = {}
    if path.exists():
        try:
            for line in path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    values[k.strip()] = v.strip()
        except Exception:
            pass
    return values


def _read_env_file_enabled(path):
    """Read KEY=VALUE file, return set of enabled (truthy) keys."""
    enabled = set()
    for k, v in _read_env_file(path).items():
        if v.lower() in ("1", "true", "yes"):
            enabled.add(k)
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
        PATH_SETTINGS, get_path,
    )

    # Read current state from file
    file_enabled = _read_env_file_enabled(SETTINGS_FILE)

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

    # Read current path values
    path_values = {}
    for var, label, default in PATH_SETTINGS:
        path_values[var] = get_path(var, default)

    tabs = [
        ("General", GENERAL_SETTINGS, general_enabled, SETTINGS_FILE),
        ("Debug", DEBUG_SETTINGS, debug_enabled, SETTINGS_FILE),
        ("Paths", [], set(), SETTINGS_FILE),  # path-only tab, no boolean items
    ]

    try:
        import curses
        return _settings_tui(curses, tabs, initial_tab, path_values, SETTINGS_FILE)
    except ImportError:
        return _settings_text(tabs, initial_tab, path_values, SETTINGS_FILE)


def _settings_tui(curses, tabs, initial_tab, path_values=None, path_file=None):
    """Curses-based tabbed settings TUI."""
    from ..settings import PATH_SETTINGS

    tab_names = [t[0] for t in tabs]
    tab_items = [t[1] for t in tabs]
    tab_selected = [[var in t[2] for var, _ in t[1]] for t in tabs]
    tab_files = [t[3] for t in tabs]
    paths_tab_idx = next(i for i, name in enumerate(tab_names) if name == "Paths")

    if path_values is None:
        path_values = {}

    active_tab = initial_tab
    cursor = 0
    status_msg = ""

    def cur_items():
        return tab_items[active_tab]

    def cur_sel():
        return tab_selected[active_tab]

    def n_items():
        n = len(cur_items())
        if active_tab == paths_tab_idx:
            n += len(PATH_SETTINGS)
        return n

    def _edit_path(stdscr, var, label, h, w):
        """Mini inline editor for a path value. Returns new value or None."""
        curses.curs_set(1)
        prompt = f"  {label}: "
        cur = path_values.get(var, "")
        buf = list(cur)
        while True:
            stdscr.move(h - 1, 0)
            stdscr.clrtoeol()
            display = prompt + "".join(buf) + "_"
            stdscr.addstr(h - 1, 2, display[:w-3], curses.A_BOLD)
            stdscr.refresh()
            k = stdscr.getch()
            if k in (10, 13, curses.KEY_ENTER):
                curses.curs_set(0)
                return "".join(buf)
            elif k == 27:  # ESC
                curses.curs_set(0)
                return None
            elif k in (curses.KEY_BACKSPACE, 127, 8):
                if buf:
                    buf.pop()
            elif 32 <= k < 127:
                buf.append(chr(k))

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

            # Checkboxes (boolean items)
            items = cur_items()
            sel = cur_sel()
            bool_count = len(items)
            for i, (var, label) in enumerate(items):
                y = i + 4
                if y >= h - 4:
                    break
                check = "x" if sel[i] else " "
                attr = curses.A_REVERSE if cursor == i else 0
                line = f"  [{check}] {label:<48s} {var}"
                stdscr.addstr(y, 0, line[:w-1], attr)

            # Path settings (Paths tab only, after boolean items)
            if active_tab == paths_tab_idx and PATH_SETTINGS:
                for pi, (var, label, default) in enumerate(PATH_SETTINGS):
                    row_idx = bool_count + pi
                    y = row_idx + 4
                    if y >= h - 4:
                        break
                    val = path_values.get(var, default)
                    val_display = val if val else "(not set)"
                    attr = curses.A_REVERSE if cursor == row_idx else 0
                    line = f"  {label:<30s} {val_display}"
                    stdscr.addstr(y, 0, line[:w-1], attr)

            ni = n_items()

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
                help_text = "Tab/\u2190\u2192 switch tab  \u2191\u2193 navigate  Space toggle/edit  q quit"
                stdscr.addstr(help_y, 2, help_text, curses.A_DIM)

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
            elif key == curses.KEY_DOWN and cursor < ni + 1:
                cursor += 1
            elif key in (ord(' '), curses.KEY_ENTER, 10, 13):
                if cursor < bool_count:
                    cur_sel()[cursor] = not cur_sel()[cursor]
                elif active_tab == paths_tab_idx and cursor < bool_count + len(PATH_SETTINGS):
                    # Path setting — open inline editor
                    path_idx = cursor - bool_count
                    var, label, default = PATH_SETTINGS[path_idx]
                    new_val = _edit_path(stdscr, var, label, h, w)
                    if new_val is not None:
                        path_values[var] = new_val
                        status_msg = f"Set {var}={new_val}" if new_val else f"Cleared {var}"
                elif cursor == ni:
                    _save_all_settings(tab_items, tab_selected, tab_files,
                                       path_values, path_file)
                    return 0
                elif cursor == ni + 1:
                    return 0
            elif key in (ord('a'), ord('A')):
                _save_all_settings(tab_items, tab_selected, tab_files,
                                   path_values, path_file)
                return 0

    return curses.wrapper(draw)


def _settings_text(tabs, initial_tab, path_values=None, path_file=None):
    """Simple text fallback for systems without curses."""
    from ..settings import PATH_SETTINGS

    tab_items = [t[1] for t in tabs]
    tab_selected = [[var in t[2] for var, _ in t[1]] for t in tabs]
    tab_files = [t[3] for t in tabs]

    if path_values is None:
        path_values = {}

    def _display():
        nonlocal offset
        offset = 0
        offsets.clear()
        for ti, (name, items, _, _) in enumerate(tabs):
            print(f"\n  --- {name} ---")
            offsets.append(offset)
            for i, (var, label) in enumerate(items):
                check = "x" if tab_selected[ti][i] else " "
                print(f"  {offset + i}. [{check}] {label:<48s} {var}")
            offset += len(items)
            # Path settings on Paths tab
            if name == "Paths" and PATH_SETTINGS:
                for var, label, default in PATH_SETTINGS:
                    val = path_values.get(var, default)
                    val_display = val if val else "(not set)"
                    print(f"  {offset}. {label:<48s} = {val_display}")
                    offset += 1

    offset = 0
    offsets = []
    print("comfy-test settings")
    print("=" * 40)
    _display()
    print()
    print("Enter numbers to toggle, 'set VAR=VALUE' for paths, 'save' to save, 'quit' to exit:")

    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if line.lower() in ("q", "quit", "exit"):
            return 0
        if line.lower() in ("s", "save"):
            _save_all_settings(tab_items, tab_selected, tab_files,
                               path_values, path_file)
            print("Saved.")
            return 0
        # Handle 'set VAR=VALUE'
        if line.lower().startswith("set ") and "=" in line:
            _, rest = line.split(None, 1)
            k, v = rest.split("=", 1)
            k, v = k.strip(), v.strip()
            path_values[k] = v
            print(f"  Set {k}={v}")
            continue
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
        _display()


def _save_all_settings(tab_items, tab_selected, tab_files,
                       path_values=None, path_file=None):
    """Write settings to their respective files."""
    from collections import defaultdict
    file_entries = defaultdict(list)
    for items, selected, filepath in zip(tab_items, tab_selected, tab_files):
        for i, (var, _) in enumerate(items):
            val_str = "1" if selected[i] else "0"
            file_entries[filepath].append((var, val_str))
            os.environ[var] = val_str

    # Append path settings to their file
    if path_values and path_file:
        for var, val in path_values.items():
            file_entries[path_file].append((var, val))
            if val:
                os.environ[var] = val
            elif var in os.environ:
                del os.environ[var]

    for filepath, entries in file_entries.items():
        filepath.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# comfy-test settings (managed by `comfy-test settings`)\n"]
        for var, val_str in entries:
            lines.append(f"{var}={val_str}\n")
        filepath.write_text("".join(lines))

    unique_files = list(dict.fromkeys(str(f) for f in tab_files))
    print(f"Saved to {', '.join(unique_files)}")
