#!/usr/bin/env python3
"""
PySimpleIDE — a lightweight Python IDE with per-project virtual environments.

Features
    * New Project      — creates a folder + a fresh venv + a starter main.py
    * Open Project      — opens a folder that already has a .venv
    * Import Project    — opens a folder that DOESN'T have a venv and creates one for it
                           (offers to `pip install -r requirements.txt` if present)
    * Save / Save As    — saves whichever file is open in the editor
    * Run               — runs the open file using that project's venv interpreter
    * Build Linux EXE   — packages the open file with PyInstaller into dist/
    * Venv Terminal     — opens a native terminal with the venv already activated

No third-party dependencies — stdlib + tkinter only.
"""

import keyword as kwmodule
import os
import platform
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

APP_TITLE = "PYDE"
IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"

# ---------------------------------------------------------------------------
# Colors (VS Code "dark+"-ish palette)
# ---------------------------------------------------------------------------
BG          = "#1e1e1e"
BG_PANEL    = "#252526"
BG_ALT      = "#2d2d30"
FG          = "#d4d4d4"
FG_DIM      = "#858585"
ACCENT      = "#0e639c"
ACCENT_HOV  = "#1177bb"
SELECT_BG   = "#264f78"
BORDER      = "#3c3c3c"
GUTTER_FG   = "#6e7681"

TOK_KEYWORD   = "#569cd6"
TOK_STRING    = "#ce9178"
TOK_COMMENT   = "#6a9955"
TOK_NUMBER    = "#b5cea8"
TOK_DEF       = "#dcdcaa"
TOK_CLASS     = "#4ec9b0"
TOK_SELF      = "#9cdcfe"


# ---------------------------------------------------------------------------
# venv helpers
# ---------------------------------------------------------------------------
def venv_dir_for(project_dir: Path) -> Path:
    return project_dir / ".venv"


def venv_python(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def venv_pip(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts/pip.exe" if IS_WINDOWS else "bin/pip")


def has_venv(project_dir: Path) -> bool:
    return venv_python(venv_dir_for(project_dir)).exists()


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False)) or hasattr(sys, "_MEIPASS")


def is_bundled() -> bool:
    if is_frozen():
        return True
    if os.environ.get("APPIMAGE") or os.environ.get("APPDIR"):
        return True
    prefix = sys.prefix or ""
    return "/.mount_" in prefix or prefix.startswith("/tmp/.mount_")


def _is_bundled_path(path: Path) -> bool:
    text = str(path)
    if "/.mount_" in text or "/tmp/.mount_" in text:
        return True
    appdir = os.environ.get("APPDIR") or ""
    appimage = os.environ.get("APPIMAGE") or ""
    try:
        resolved = str(path.resolve())
    except Exception:
        resolved = text
    if appdir and resolved.startswith(appdir):
        return True
    if appimage and resolved == appimage:
        return True
    try:
        if resolved == str(Path(sys.executable).resolve()):
            return is_bundled()
    except Exception:
        pass
    return False


def clean_subprocess_env() -> dict:
    """Env for project venv / pip / run — strip AppImage/PyInstaller Python vars."""
    env = os.environ.copy()
    for key in (
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONEXECUTABLE",
        "TCL_LIBRARY",
        "TK_LIBRARY",
        "TCLLIBPATH",
        "APPDIR",
        "APPIMAGE",
        "OWD",
    ):
        env.pop(key, None)
    appdir = os.environ.get("APPDIR") or ""
    for key in ("LD_LIBRARY_PATH", "PATH", "GI_TYPELIB_PATH", "GST_PLUGIN_SYSTEM_PATH"):
        val = env.get(key)
        if not val:
            continue
        parts = [p for p in val.split(":") if p and "/.mount_" not in p]
        if appdir:
            parts = [p for p in parts if not p.startswith(appdir)]
        if parts:
            env[key] = ":".join(parts)
        else:
            env.pop(key, None)
    return env


def host_python() -> str | None:
    """System Python used to create project venvs.

    Never use this AppImage/PyInstaller binary — `sys.executable -m venv`
    either relaunches the IDE or builds a venv pointed at /tmp/.mount_*.
    """
    exe_resolved = Path(sys.executable).resolve()
    candidates = [
        "/usr/bin/python3",
        "/usr/local/bin/python3",
        "/bin/python3",
        "/usr/bin/python",
    ]
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    if not is_bundled():
        candidates.insert(0, sys.executable)

    seen = set()
    for cand in candidates:
        path = Path(cand)
        if not path.exists():
            continue
        try:
            key = str(path.resolve())
        except Exception:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if _is_bundled_path(path):
            continue
        try:
            if path.resolve() == exe_resolved and is_bundled():
                continue
        except Exception:
            pass
        return str(path)
    return None


def interpreter_is_this_app(path: Path) -> bool:
    """True if `path` is this frozen IDE (or a venv copied from it).

    Do not execute `path` — if it is the IDE, that would open another window.
    """
    if not path.exists():
        return False
    try:
        resolved = path.resolve()
    except Exception:
        resolved = path
    try:
        self_exe = Path(sys.executable).resolve()
    except Exception:
        self_exe = Path(sys.executable)

    if resolved == self_exe:
        return True
    if is_frozen() and path.name == self_exe.name:
        return True

    try:
        if path.stat().st_size == self_exe.stat().st_size and is_frozen():
            return True
    except Exception:
        pass

    cfg = path.parent.parent / "pyvenv.cfg"  # .venv/bin/python -> .venv/pyvenv.cfg
    if cfg.exists():
        try:
            text = cfg.read_text(encoding="utf-8", errors="replace").lower()
        except Exception:
            text = ""
        markers = [self_exe.name.lower(), str(self_exe).lower()]
        if is_frozen() and any(m and m in text for m in markers):
            return True
        if "meipass" in text or "/.mount_" in text or "/tmp/.mount_" in text:
            return True
    return False


def open_terminal(project_dir: Path, venv_dir: Path) -> bool:
    """Best-effort: open a native terminal window with the venv activated."""
    system = platform.system()
    try:
        if system == "Windows":
            activate = venv_dir / "Scripts" / "activate.bat"
            subprocess.Popen(
                ["cmd.exe", "/K", str(activate)],
                cwd=str(project_dir),
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            return True

        if system == "Darwin":
            activate = venv_dir / "bin" / "activate"
            script = f"cd {shlex.quote(str(project_dir))} && source {shlex.quote(str(activate))}"
            osa = f'tell application "Terminal" to do script "{script}"'
            subprocess.Popen(["osascript", "-e", osa])
            return True

        # Linux / other POSIX — try common terminal emulators
        activate = venv_dir / "bin" / "activate"
        inner = (
            f"cd {shlex.quote(str(project_dir))} && "
            f"source {shlex.quote(str(activate))} && exec $SHELL"
        )
        candidates = [
            ["gnome-terminal", "--"],
            ["konsole", "-e"],
            ["xfce4-terminal", "-e"],
            ["x-terminal-emulator", "-e"],
            ["xterm", "-e"],
        ]
        for base in candidates:
            exe = base[0]
            if shutil.which(exe):
                if exe in ("konsole", "xfce4-terminal", "x-terminal-emulator", "xterm"):
                    cmd = base + [f"bash -c {shlex.quote(inner)}"]
                else:
                    cmd = base + ["bash", "-c", inner]
                subprocess.Popen(cmd, cwd=str(project_dir))
                return True
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Line numbers gutter
# ---------------------------------------------------------------------------
class LineNumbers(tk.Canvas):
    def __init__(self, master, text_widget, **kwargs):
        super().__init__(master, width=46, highlightthickness=0, bg=BG_PANEL, **kwargs)
        self.text_widget = text_widget

    def redraw(self, *_args):
        self.delete("all")
        i = self.text_widget.index("@0,0")
        while True:
            dline = self.text_widget.dlineinfo(i)
            if dline is None:
                break
            y = dline[1]
            linenum = str(i).split(".")[0]
            self.create_text(40, y, anchor="ne", text=linenum, fill=GUTTER_FG,
                              font=("Consolas", 10))
            i = self.text_widget.index(f"{i}+1line")


# ---------------------------------------------------------------------------
# Syntax highlighting
# ---------------------------------------------------------------------------
PY_KEYWORDS = sorted(set(kwmodule.kwlist), key=len, reverse=True)

TOKEN_SPECS = [
    ("comment", r"#[^\n]*", 0),
    ("string", r'("""(?:.|\n)*?"""|\'\'\'(?:.|\n)*?\'\'\'|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\')', 0),
    ("class_name", r"(?<=class )\w+", 0),
    ("def_name", r"(?<=def )\w+", 0),
    ("self_kw", r"\bself\b|\bcls\b", 0),
    ("keyword", r"\b(?:%s)\b" % "|".join(PY_KEYWORDS), 0),
    ("number", r"\b\d+\.?\d*\b", 0),
]

TAG_COLORS = {
    "comment": TOK_COMMENT,
    "string": TOK_STRING,
    "class_name": TOK_CLASS,
    "def_name": TOK_DEF,
    "self_kw": TOK_SELF,
    "keyword": TOK_KEYWORD,
    "number": TOK_NUMBER,
}


# ---------------------------------------------------------------------------
# Background subprocess streaming helper
# ---------------------------------------------------------------------------
def stream_process(cmd, cwd, out_queue):
    """Run cmd in the background, push ('proc', Popen) then ('line', text)* then ('done', code)."""
    try:
        kwargs = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen(
            [str(c) for c in cmd], cwd=str(cwd),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=clean_subprocess_env(), **kwargs,
        )
    except Exception as exc:
        out_queue.put(("error", str(exc)))
        out_queue.put(("done", -1))
        return
    out_queue.put(("proc", proc))
    try:
        for line in proc.stdout:
            out_queue.put(("line", line))
    except Exception:
        pass
    proc.wait()
    out_queue.put(("done", proc.returncode))


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------
class PySimpleIDE(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1280x800")
        self.configure(bg=BG)

        self.project_dir: Path | None = None
        self.venv_dir: Path | None = None
        self.current_file: Path | None = None
        self.current_proc = None
        self.proc_running = False
        self._highlight_job = None

        self._build_style()
        self._build_menu()
        self._build_toolbar()
        self._build_body()
        self._build_status_bar()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind_all("<Control-s>", lambda e: self.save_file())
        self.bind_all("<Control-Shift-S>", lambda e: self.save_file_as())
        self.bind_all("<Control-r>", lambda e: self.reload_folder())
        self.bind_all("<F5>", lambda e: self.run_file())
        self.editor.bind("<Control-a>", lambda e: self._editor_select_all())

        self._console_write("Welcome to PySimpleIDE.\n"
                             "Create or open a project to get started.\n")
        self._update_title()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", background=BG_PANEL, fieldbackground=BG_PANEL,
                         foreground=FG, borderwidth=0, rowheight=22)
        style.map("Treeview", background=[("selected", SELECT_BG)])
        style.configure("Vertical.TScrollbar", background=BG_ALT, troughcolor=BG,
                         bordercolor=BG, arrowcolor=FG)
        style.configure("Horizontal.TScrollbar", background=BG_ALT, troughcolor=BG,
                         bordercolor=BG, arrowcolor=FG)
        style.configure("TPanedwindow", background=BG)

    def _build_menu(self):
        menubar = tk.Menu(self)

        filemenu = tk.Menu(menubar, tearoff=0)
        filemenu.add_command(label="New Project...", command=self.new_project)
        filemenu.add_command(label="Open Project...", command=self.open_project)
        filemenu.add_command(label="Import Project...", command=self.import_project)
        filemenu.add_command(label="Reload Folder", command=self.reload_folder, accelerator="Ctrl+R")
        filemenu.add_separator()
        filemenu.add_command(label="Save", command=self.save_file, accelerator="Ctrl+S")
        filemenu.add_command(label="Save As...", command=self.save_file_as, accelerator="Ctrl+Shift+S")
        filemenu.add_separator()
        filemenu.add_command(label="Exit", command=self._on_close)
        menubar.add_cascade(label="File", menu=filemenu)

        runmenu = tk.Menu(menubar, tearoff=0)
        runmenu.add_command(label="Run Current File", command=self.run_file, accelerator="F5")
        runmenu.add_command(label="Stop", command=self.stop_run)
        runmenu.add_separator()
        runmenu.add_command(label="Build Linux Executable", command=self.build_executable)
        runmenu.add_separator()
        runmenu.add_command(label="Recreate Venv", command=self.recreate_venv)
        runmenu.add_command(label="Open Venv Terminal", command=self.open_terminal_btn)
        menubar.add_cascade(label="Run", menu=runmenu)

        self.config(menu=menubar)

    def _toolbar_button(self, parent, text, command):
        btn = tk.Button(parent, text=text, command=command, bg=BG_ALT, fg=FG,
                         activebackground=ACCENT_HOV, activeforeground="white",
                         relief=tk.FLAT, padx=10, pady=4, font=("Helvetica", 9))
        btn.pack(side=tk.LEFT, padx=(0, 6))
        return btn

    def _build_toolbar(self):
        bar = tk.Frame(self, bg=BG, pady=6, padx=8)
        bar.pack(fill=tk.X, side=tk.TOP)

        self._toolbar_button(bar, "New Project", self.new_project)
        self._toolbar_button(bar, "Open Project", self.open_project)
        self._toolbar_button(bar, "Import Project", self.import_project)
        self._toolbar_button(bar, "Reload Folder", self.reload_folder)
        tk.Frame(bar, bg=BORDER, width=1).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        self._toolbar_button(bar, "Save", self.save_file)
        self._toolbar_button(bar, "Save As", self.save_file_as)
        tk.Frame(bar, bg=BORDER, width=1).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        self.btn_run = self._toolbar_button(bar, "▶ Run", self.run_file)
        self.btn_stop = self._toolbar_button(bar, "■ Stop", self.stop_run)
        self.btn_stop.config(state="disabled")
        self.btn_build = self._toolbar_button(bar, "📦 Build Linux EXE", self.build_executable)
        tk.Frame(bar, bg=BORDER, width=1).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        self._toolbar_button(bar, "🖳 Venv Terminal (pip install)", self.open_terminal_btn)

    def _build_body(self):
        body = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True)

        # --- left: project explorer ---
        left = tk.Frame(body, bg=BG_PANEL)
        tk.Label(left, text="PROJECT", bg=BG_PANEL, fg=FG_DIM,
                 font=("Helvetica", 9, "bold"), anchor="w").pack(fill=tk.X, padx=8, pady=(6, 2))
        self.tree = ttk.Treeview(left, show="tree")
        self.tree.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))
        self.tree.bind("<Double-1>", self._on_tree_double_click)
        self.tree.bind("<Button-3>", self._on_tree_right_click)
        if IS_MAC:
            self.tree.bind("<Button-2>", self._on_tree_right_click)
        body.add(left, weight=1)

        # --- right: editor (top) + console (bottom) ---
        right = ttk.PanedWindow(body, orient=tk.VERTICAL)
        body.add(right, weight=4)

        editor_frame = tk.Frame(right, bg=BG)
        self.editor = tk.Text(editor_frame, bg=BG, fg=FG, insertbackground=FG,
                               undo=True, wrap="none", font=("Consolas", 11),
                               relief=tk.FLAT, padx=6, pady=4,
                               selectbackground=SELECT_BG, borderwidth=0,
                               yscrollcommand=self._on_editor_yscroll)
        self.linenumbers = LineNumbers(editor_frame, self.editor)
        self.editor_vscroll = ttk.Scrollbar(editor_frame, command=self._on_vscroll)
        hscroll = ttk.Scrollbar(editor_frame, orient=tk.HORIZONTAL, command=self.editor.xview)
        self.editor.config(xscrollcommand=hscroll.set)

        self.linenumbers.pack(side=tk.LEFT, fill=tk.Y)
        self.editor_vscroll.pack(side=tk.RIGHT, fill=tk.Y)
        hscroll.pack(side=tk.BOTTOM, fill=tk.X)
        self.editor.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        right.add(editor_frame, weight=3)

        for tag, color in TAG_COLORS.items():
            self.editor.tag_configure(tag, foreground=color)

        self.editor.bind("<KeyRelease>", self._on_editor_keyrelease)
        self.editor.bind("<Configure>", lambda e: self.linenumbers.redraw())
        self.editor.bind("<<Modified>>", self._on_modified)
        self.editor.bind("<Tab>", self._on_tab)
        self.editor.bind("<Return>", self._on_return)
        self.editor.bind("<Button-3>", self._on_editor_right_click)
        if IS_MAC:
            self.editor.bind("<Button-2>", self._on_editor_right_click)

        console_frame = tk.Frame(right, bg=BG)
        tk.Label(console_frame, text="OUTPUT", bg=BG_ALT, fg=FG_DIM,
                 font=("Helvetica", 9, "bold"), anchor="w").pack(fill=tk.X, ipady=2)
        self.console = scrolledtext.ScrolledText(
            console_frame, bg="#0c0c0c", fg="#d4d4d4", insertbackground=FG,
            font=("Consolas", 10), relief=tk.FLAT, state="disabled", height=10,
        )
        self.console.pack(fill=tk.BOTH, expand=True)
        right.add(console_frame, weight=1)

    def _build_status_bar(self):
        bar = tk.Frame(self, bg=BG_ALT)
        bar.pack(fill=tk.X, side=tk.BOTTOM)
        self.status_var = tk.StringVar(value="No project open")
        tk.Label(bar, textvariable=self.status_var, bg=BG_ALT, fg=FG_DIM,
                 font=("Helvetica", 9), anchor="w").pack(side=tk.LEFT, padx=8, pady=2)

    # ------------------------------------------------------------------
    # Editor scroll / gutter sync
    # ------------------------------------------------------------------
    def _on_editor_yscroll(self, *args):
        self.editor_vscroll.set(*args)
        self.linenumbers.redraw()

    def _on_vscroll(self, *args):
        self.editor.yview(*args)
        self.linenumbers.redraw()

    def _on_editor_keyrelease(self, event=None):
        self.linenumbers.redraw()
        if self._highlight_job:
            self.after_cancel(self._highlight_job)
        self._highlight_job = self.after(150, self._highlight)

    def _on_modified(self, event=None):
        self._update_title()

    def _on_tab(self, event):
        self.editor.insert("insert", " " * 4)
        return "break"

    def _on_return(self, event):
        line_start = self.editor.index("insert linestart")
        line_text = self.editor.get(line_start, "insert")
        indent = re.match(r"[ \t]*", line_text).group()
        extra = "    " if line_text.rstrip().endswith(":") else ""
        self.editor.insert("insert", "\n" + indent + extra)
        return "break"

    def _on_editor_right_click(self, event):
        # Move the cursor to the click point (without clobbering an existing
        # selection, so right-clicking inside a selection still lets you
        # Cut/Copy it).
        click_index = self.editor.index(f"@{event.x},{event.y}")
        try:
            sel_start = self.editor.index("sel.first")
            sel_end = self.editor.index("sel.last")
            has_selection = self.editor.compare(sel_start, "<", sel_end)
        except tk.TclError:
            has_selection = False
        if not has_selection or not (
            self.editor.compare(click_index, ">=", sel_start)
            and self.editor.compare(click_index, "<=", sel_end)
        ):
            self.editor.mark_set("insert", click_index)
            has_selection = False

        try:
            clipboard_has_text = bool(self.clipboard_get())
        except tk.TclError:
            clipboard_has_text = False

        menu = tk.Menu(self, tearoff=0, bg=BG_ALT, fg=FG,
                        activebackground=ACCENT_HOV, activeforeground="white")
        menu.add_command(label="Cut", command=self._editor_cut,
                          state="normal" if has_selection else "disabled")
        menu.add_command(label="Copy", command=self._editor_copy,
                          state="normal" if has_selection else "disabled")
        menu.add_command(label="Paste", command=self._editor_paste,
                          state="normal" if clipboard_has_text else "disabled")
        menu.add_separator()
        menu.add_command(label="Select All", command=self._editor_select_all)
        menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def _editor_cut(self):
        self.editor.event_generate("<<Cut>>")

    def _editor_copy(self):
        self.editor.event_generate("<<Copy>>")

    def _editor_paste(self):
        self.editor.event_generate("<<Paste>>")

    def _editor_select_all(self):
        self.editor.tag_add("sel", "1.0", "end-1c")
        self.editor.mark_set("insert", "end-1c")
        return "break"

    def _highlight(self):
        content = self.editor.get("1.0", "end-1c")
        for tag in TAG_COLORS:
            self.editor.tag_remove(tag, "1.0", "end")
        for tag, pattern, flags in TOKEN_SPECS:
            for m in re.finditer(pattern, content, flags):
                start = f"1.0+{m.start()}c"
                end = f"1.0+{m.end()}c"
                self.editor.tag_add(tag, start, end)
        self.editor.tag_raise("string")
        self.editor.tag_raise("comment")

    # ------------------------------------------------------------------
    # Console helpers
    # ------------------------------------------------------------------
    def _console_write(self, text):
        self.console.config(state="normal")
        self.console.insert("end", text)
        self.console.see("end")
        self.console.config(state="disabled")

    def _console_clear(self):
        self.console.config(state="normal")
        self.console.delete("1.0", "end")
        self.console.config(state="disabled")

    # ------------------------------------------------------------------
    # Title / status
    # ------------------------------------------------------------------
    def _update_title(self):
        parts = [APP_TITLE]
        if self.project_dir:
            parts.append(self.project_dir.name)
        if self.current_file:
            name = self.current_file.name
            if self.editor.edit_modified():
                name += " *"
            parts.append(name)
        self.title(" — ".join(parts))

    def _update_status(self):
        if not self.project_dir:
            self.status_var.set("No project open")
            return
        vpy = venv_python(self.venv_dir)
        venv_state = "venv ready" if vpy.exists() else "no venv"
        self.status_var.set(f"Project: {self.project_dir}   |   {venv_state}")

    # ------------------------------------------------------------------
    # Project explorer
    # ------------------------------------------------------------------
    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        if not self.project_dir:
            return
        root_id = self.tree.insert("", "end", text=self.project_dir.name,
                                    open=True, values=[str(self.project_dir)])
        self._populate_tree(root_id, self.project_dir)

    def _populate_tree(self, parent_id, dir_path: Path):
        try:
            entries = sorted(dir_path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except Exception:
            return
        for entry in entries:
            if entry.name in (".venv", "__pycache__") or entry.name.startswith("."):
                continue
            node = self.tree.insert(parent_id, "end", text=entry.name, values=[str(entry)])
            if entry.is_dir():
                self._populate_tree(node, entry)

    def _on_tree_double_click(self, event):
        item = self.tree.focus()
        if not item:
            return
        values = self.tree.item(item, "values")
        if not values:
            return
        path = Path(values[0])
        if path.is_file():
            self.open_file(path)

    def _on_tree_right_click(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
        menu = tk.Menu(self, tearoff=0, bg=BG_ALT, fg=FG)
        menu.add_command(label="New File...", command=lambda: self._tree_new(item, is_dir=False))
        menu.add_command(label="New Folder...", command=lambda: self._tree_new(item, is_dir=True))
        if item:
            menu.add_separator()
            menu.add_command(label="Delete", command=lambda: self._tree_delete(item))
        menu.tk_popup(event.x_root, event.y_root)

    def _tree_target_dir(self, item) -> Path | None:
        if not self.project_dir:
            return None
        if not item:
            return self.project_dir
        path = Path(self.tree.item(item, "values")[0])
        return path if path.is_dir() else path.parent

    def _tree_new(self, item, is_dir: bool):
        target = self._tree_target_dir(item)
        if target is None:
            messagebox.showwarning(APP_TITLE, "Open or create a project first.")
            return
        kind = "folder" if is_dir else "file"
        name = simpledialog.askstring(APP_TITLE, f"New {kind} name:")
        if not name:
            return
        new_path = target / name
        try:
            if is_dir:
                new_path.mkdir(parents=True, exist_ok=False)
            else:
                if not new_path.suffix:
                    new_path = new_path.with_suffix(".py")
                new_path.touch(exist_ok=False)
        except FileExistsError:
            messagebox.showerror(APP_TITLE, "That already exists.")
            return
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Couldn't create it:\n{exc}")
            return
        self._refresh_tree()
        if not is_dir:
            self.open_file(new_path)

    def _tree_delete(self, item):
        values = self.tree.item(item, "values")
        if not values:
            return
        path = Path(values[0])
        if not messagebox.askyesno(APP_TITLE, f"Delete '{path.name}' permanently?"):
            return
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Couldn't delete it:\n{exc}")
            return
        if self.current_file and self.current_file == path:
            self.current_file = None
            self.editor.delete("1.0", "end")
            self.editor.edit_modified(False)
            self._update_title()
        self._refresh_tree()

    # ------------------------------------------------------------------
    # File open / save
    # ------------------------------------------------------------------
    def _maybe_save_prompt(self) -> bool:
        """Returns True if it's OK to proceed (discarded, saved, or nothing to save)."""
        if not self.editor.edit_modified():
            return True
        resp = messagebox.askyesnocancel(APP_TITLE, "Save changes before continuing?")
        if resp is None:
            return False
        if resp:
            return self.save_file() if self.current_file else self.save_file_as()
        return True

    def open_file(self, path: Path):
        if not self._maybe_save_prompt():
            return
        try:
            content = path.read_text(encoding="utf-8")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Could not open file:\n{exc}")
            return
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", content)
        self.editor.edit_reset()
        self.editor.edit_modified(False)
        self.current_file = path
        self._highlight()
        self.linenumbers.redraw()
        self._update_title()

    def save_file(self) -> bool:
        if not self.current_file:
            return self.save_file_as()
        try:
            content = self.editor.get("1.0", "end-1c")
            self.current_file.write_text(content, encoding="utf-8")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Could not save file:\n{exc}")
            return False
        self.editor.edit_modified(False)
        self._update_title()
        return True

    def save_file_as(self) -> bool:
        if not self.project_dir:
            messagebox.showwarning(APP_TITLE, "Open or create a project first.")
            return False
        initial = str(self.current_file.parent) if self.current_file else str(self.project_dir)
        path = filedialog.asksaveasfilename(
            initialdir=initial, defaultextension=".py",
            filetypes=[("Python files", "*.py"), ("All files", "*.*")],
        )
        if not path:
            return False
        self.current_file = Path(path)
        try:
            content = self.editor.get("1.0", "end-1c")
            self.current_file.write_text(content, encoding="utf-8")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Could not save file:\n{exc}")
            return False
        self.editor.edit_modified(False)
        self._update_title()
        self._refresh_tree()
        return True

    # ------------------------------------------------------------------
    # Venv creation (async, streamed into console)
    # ------------------------------------------------------------------
    def _create_venv_async(self, project_dir: Path, on_done):
        vdir = venv_dir_for(project_dir)
        py = host_python()
        if not py:
            self._console_write(
                "Cannot create a virtual environment: no system Python found.\n"
                "Install Python 3 (e.g. python3) and try again.\n"
                "The packaged IDE is not itself a Python interpreter.\n"
            )
            on_done(False)
            return
        self._console_write(f"Creating virtual environment at {vdir}\nUsing interpreter: {py}\n")
        q = queue.Queue()

        def worker():
            try:
                kwargs = {}
                if IS_WINDOWS:
                    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                proc = subprocess.Popen(
                    [py, "-m", "venv", str(vdir)],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, env=clean_subprocess_env(), **kwargs,
                )
                for line in proc.stdout:
                    q.put(("line", line))
                proc.wait()
                q.put(("done", proc.returncode))
            except Exception as exc:
                q.put(("error", str(exc)))
                q.put(("done", -1))

        threading.Thread(target=worker, daemon=True).start()

        def poll():
            try:
                while True:
                    kind, payload = q.get_nowait()
                    if kind == "line":
                        self._console_write(payload)
                    elif kind == "error":
                        self._console_write(f"[error] {payload}\n")
                    elif kind == "done":
                        ok = (payload == 0)
                        self._console_write(
                            "Virtual environment ready.\n" if ok
                            else f"venv creation failed (exit {payload}).\n"
                        )
                        self._update_status()
                        on_done(ok)
                        return
            except queue.Empty:
                pass
            self.after(80, poll)

        poll()

    def _pip_install_requirements(self, project_dir: Path):
        pip = venv_pip(venv_dir_for(project_dir))
        self._console_write(f"Installing requirements.txt ...\n")
        q = queue.Queue()
        threading.Thread(
            target=stream_process,
            args=([str(pip), "install", "-r", "requirements.txt"], project_dir, q),
            daemon=True,
        ).start()

        def poll():
            try:
                while True:
                    kind, payload = q.get_nowait()
                    if kind == "line":
                        self._console_write(payload)
                    elif kind == "error":
                        self._console_write(f"[error] {payload}\n")
                    elif kind == "done":
                        self._console_write(f"\n[pip install exited with code {payload}]\n")
                        return
            except queue.Empty:
                pass
            self.after(80, poll)

        poll()

    # ------------------------------------------------------------------
    # Project actions
    # ------------------------------------------------------------------
    def _load_project(self, project_dir: Path):
        self.project_dir = project_dir
        self.venv_dir = venv_dir_for(project_dir)
        self._refresh_tree()
        self._update_status()
        self._update_title()

    def reload_folder(self):
        if not self.project_dir:
            messagebox.showwarning(APP_TITLE, "Open or create a project first.")
            return
        if not self.project_dir.exists():
            messagebox.showerror(APP_TITLE, "The project folder is no longer on disk.")
            return
        self.venv_dir = venv_dir_for(self.project_dir)
        self._refresh_tree()
        self._update_status()
        self._update_title()

        if self.current_file:
            if not self.current_file.exists():
                self.current_file = None
                self.editor.delete("1.0", "end")
                self.editor.edit_modified(False)
                self._update_title()
                self._console_write("Reload: open file is gone from disk.\n")
            elif self.editor.edit_modified():
                if messagebox.askyesno(
                    APP_TITLE,
                    "The editor has unsaved changes.\n"
                    "Reload the file from disk and discard them?",
                ):
                    path = self.current_file
                    self.editor.edit_modified(False)
                    self.open_file(path)
            else:
                path = self.current_file
                self.open_file(path)

        self.status_var.set(
            f"Reloaded {self.project_dir}   |   "
            + ("venv ready" if has_venv(self.project_dir) else "no venv")
        )

    def new_project(self):
        parent = filedialog.askdirectory(title="Choose a location for the new project")
        if not parent:
            return
        name = simpledialog.askstring(APP_TITLE, "Project name:")
        if not name:
            return
        project_dir = Path(parent) / name
        if project_dir.exists():
            messagebox.showerror(APP_TITLE, "That folder already exists.")
            return
        try:
            project_dir.mkdir(parents=True)
            (project_dir / "main.py").write_text(f'print("Hello from {name}!")\n', encoding="utf-8")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Couldn't create the project:\n{exc}")
            return

        self._load_project(project_dir)
        self._console_clear()
        self._console_write(f"Created project '{name}' at {project_dir}\n")
        self.open_file(project_dir / "main.py")
        self._create_venv_async(project_dir, on_done=lambda ok: self._refresh_tree())

    def open_project(self):
        d = filedialog.askdirectory(title="Open project folder")
        if not d:
            return
        project_dir = Path(d)
        if not has_venv(project_dir):
            messagebox.showwarning(
                APP_TITLE,
                "This folder doesn't have a virtual environment.\n"
                "Use File → Import Project instead to create one for it."
            )
            return
        self._load_project(project_dir)
        self._console_clear()
        self._console_write(f"Opened project: {project_dir}\nVirtual environment: {self.venv_dir}\n")
        main_py = project_dir / "main.py"
        if main_py.exists():
            self.open_file(main_py)

    def import_project(self):
        d = filedialog.askdirectory(title="Import an existing project folder")
        if not d:
            return
        project_dir = Path(d)
        self._load_project(project_dir)
        self._console_clear()

        if has_venv(project_dir):
            self._console_write(f"'{project_dir.name}' already has a virtual environment — opening it.\n")
            main_py = project_dir / "main.py"
            if main_py.exists():
                self.open_file(main_py)
            return

        def after_venv(ok):
            self._refresh_tree()
            if not ok:
                return
            req = project_dir / "requirements.txt"
            if req.exists() and messagebox.askyesno(
                APP_TITLE, "Found requirements.txt in this project — install it into the new venv now?"
            ):
                self._pip_install_requirements(project_dir)

        self._create_venv_async(project_dir, on_done=after_venv)

    def recreate_venv(self):
        if not self.project_dir:
            messagebox.showwarning(APP_TITLE, "Open or create a project first.")
            return
        if self.proc_running:
            messagebox.showinfo(APP_TITLE, "Stop the running program first.")
            return
        if not messagebox.askyesno(
            APP_TITLE,
            "Delete this project's .venv and create a new one with system Python?",
        ):
            return
        vdir = venv_dir_for(self.project_dir)
        if vdir.exists():
            try:
                shutil.rmtree(vdir)
            except Exception as exc:
                messagebox.showerror(APP_TITLE, f"Could not remove .venv:\n{exc}")
                return
        self._console_clear()
        self._console_write("Recreating virtual environment with system Python...\n")

        def after(ok):
            self._refresh_tree()
            self._update_status()
            if ok:
                req = self.project_dir / "requirements.txt"
                if req.exists() and messagebox.askyesno(
                    APP_TITLE, "Reinstall requirements.txt into the new venv?"
                ):
                    self._pip_install_requirements(self.project_dir)

        self._create_venv_async(self.project_dir, on_done=after)

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------
    def run_file(self):
        if not self.project_dir:
            messagebox.showwarning(APP_TITLE, "Open or create a project first.")
            return
        if not self.current_file:
            messagebox.showwarning(APP_TITLE, "Open or create a .py file to run.")
            return
        if self.proc_running:
            messagebox.showinfo(APP_TITLE, "A program is already running. Stop it first.")
            return
        if not self.save_file():
            return

        vpy = venv_python(self.venv_dir)
        if not vpy.exists():
            messagebox.showerror(APP_TITLE, "This project has no virtual environment yet.")
            return
        if interpreter_is_this_app(vpy):
            messagebox.showerror(
                APP_TITLE,
                "This project's .venv points at the packaged IDE/AppImage "
                "(PYTHONHOME under /tmp/.mount_...), so it cannot run scripts.\n\n"
                "In a terminal:\n\n"
                "  cd /path/to/project\n"
                "  rm -rf .venv && /usr/bin/python3 -m venv .venv\n\n"
                "Use /usr/bin/python3 specifically — not plain python3, which "
                "may be the AppImage. Then reinstall packages if needed.",
            )
            return

        self._console_clear()
        self._console_write(f"$ {self.current_file.name}\n\n")
        self.btn_run.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.proc_running = True
        self.run_queue = queue.Queue()
        threading.Thread(
            target=stream_process,
            args=([str(vpy), "-u", str(self.current_file)], self.project_dir, self.run_queue),
            daemon=True,
        ).start()
        self.after(50, self._poll_run_queue)

    def _poll_run_queue(self):
        try:
            while True:
                kind, payload = self.run_queue.get_nowait()
                if kind == "proc":
                    self.current_proc = payload
                elif kind == "line":
                    self._console_write(payload)
                elif kind == "error":
                    self._console_write(f"[error] {payload}\n")
                elif kind == "done":
                    self._console_write(f"\n[process exited with code {payload}]\n")
                    self.proc_running = False
                    self.current_proc = None
                    self.btn_run.config(state="normal")
                    self.btn_stop.config(state="disabled")
                    return
        except queue.Empty:
            pass
        if self.proc_running:
            self.after(50, self._poll_run_queue)

    def stop_run(self):
        if self.current_proc and self.proc_running:
            try:
                self.current_proc.terminate()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Build Linux executable (PyInstaller, project's venv)
    # ------------------------------------------------------------------
    def build_executable(self):
        if not self.project_dir:
            messagebox.showwarning(APP_TITLE, "Open or create a project first.")
            return
        if not self.current_file:
            messagebox.showwarning(APP_TITLE, "Open the .py file you want to build.")
            return
        if self.proc_running:
            messagebox.showinfo(APP_TITLE, "A process is already running. Stop it first.")
            return
        if not self.save_file():
            return

        vpy = venv_python(self.venv_dir)
        if not vpy.exists():
            messagebox.showerror(APP_TITLE, "This project has no virtual environment yet.")
            return

        if platform.system() != "Linux":
            if not messagebox.askyesno(
                APP_TITLE,
                "PyInstaller produces an executable for the OS you build on.\n"
                "This machine is not Linux, so the result will not be a Linux binary.\n\n"
                "Build anyway?",
            ):
                return

        # --- Build options dialog ---
        dlg = tk.Toplevel(self)
        dlg.title("Build Executable")
        dlg.configure(bg=BG)
        dlg.transient(self)
        dlg.grab_set()
        dlg.resizable(False, False)

        result = {"ok": False}

        # Checkboxes
        opts = tk.Frame(dlg, bg=BG)
        opts.pack(fill=tk.X, padx=16, pady=(16, 8))

        var_onefile = tk.BooleanVar(value=True)
        var_windowed = tk.BooleanVar(value=False)
        var_tkinter = tk.BooleanVar(value=False)

        tk.Checkbutton(
            opts, text="One File", variable=var_onefile,
            bg=BG, fg=FG, selectcolor=BG_ALT, activebackground=BG, activeforeground=FG,
            font=("Helvetica", 10)
        ).pack(anchor="w", pady=2)

        tk.Checkbutton(
            opts, text="Windowed (GUI)  — hide console window", variable=var_windowed,
            bg=BG, fg=FG, selectcolor=BG_ALT, activebackground=BG, activeforeground=FG,
            font=("Helvetica", 10)
        ).pack(anchor="w", pady=2)

        tk.Checkbutton(
            opts, text="Include Tkinter support  (adds --hidden-import=tkinter --collect-all tkinter)",
            variable=var_tkinter,
            bg=BG, fg=FG, selectcolor=BG_ALT, activebackground=BG, activeforeground=FG,
            font=("Helvetica", 10)
        ).pack(anchor="w", pady=2)

        # Entry file
        tk.Label(dlg, text="Entry File", bg=BG, fg=FG_DIM, font=("Helvetica", 9)).pack(
            anchor="w", padx=16, pady=(8, 2))
        entry_frame = tk.Frame(dlg, bg=BG)
        entry_frame.pack(fill=tk.X, padx=16)
        var_entry = tk.StringVar(value=str(self.current_file))
        tk.Entry(entry_frame, textvariable=var_entry, bg=BG_ALT, fg=FG,
                 insertbackground=FG, relief=tk.FLAT, font=("Consolas", 9),
                 width=50).pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
        def browse_entry():
            p = filedialog.askopenfilename(
                title="Select entry script",
                initialdir=str(self.project_dir),
                filetypes=[("Python files", "*.py"), ("All files", "*.*")],
            )
            if p:
                var_entry.set(p)
        tk.Button(entry_frame, text="Browse…", command=browse_entry,
                  bg=BG_ALT, fg=FG, relief=tk.FLAT, padx=8).pack(side=tk.LEFT, padx=(6, 0))

        # Icon file
        tk.Label(dlg, text="Icon File (optional)", bg=BG, fg=FG_DIM, font=("Helvetica", 9)).pack(
            anchor="w", padx=16, pady=(10, 2))
        icon_frame = tk.Frame(dlg, bg=BG)
        icon_frame.pack(fill=tk.X, padx=16)

        # Pre-fill icon if one exists in the project
        suggested_icon = ""
        for candidate in (
            self.project_dir / "icon.ico",
            self.project_dir / "icon.png",
            self.project_dir / f"{self.project_dir.name}.ico",
            self.project_dir / f"{self.project_dir.name}.png",
        ):
            if candidate.exists():
                suggested_icon = str(candidate)
                break

        var_icon = tk.StringVar(value=suggested_icon)
        tk.Entry(icon_frame, textvariable=var_icon, bg=BG_ALT, fg=FG,
                 insertbackground=FG, relief=tk.FLAT, font=("Consolas", 9),
                 width=50).pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
        def browse_icon():
            p = filedialog.askopenfilename(
                title="Select icon",
                initialdir=str(self.project_dir),
                filetypes=[
                    ("Icon files", "*.ico *.png *.icns"),
                    ("ICO", "*.ico"),
                    ("PNG", "*.png"),
                    ("All files", "*.*"),
                ],
            )
            if p:
                var_icon.set(p)
        tk.Button(icon_frame, text="Browse…", command=browse_icon,
                  bg=BG_ALT, fg=FG, relief=tk.FLAT, padx=8).pack(side=tk.LEFT, padx=(6, 0))

        # Buttons
        btn_frame = tk.Frame(dlg, bg=BG)
        btn_frame.pack(fill=tk.X, padx=16, pady=16)

        def on_ok():
            entry = var_entry.get().strip()
            if not entry or not Path(entry).exists():
                messagebox.showwarning("Build", "Please choose a valid entry .py file.", parent=dlg)
                return
            result["ok"] = True
            result["onefile"] = var_onefile.get()
            result["windowed"] = var_windowed.get()
            result["tkinter"] = var_tkinter.get()
            result["entry"] = Path(entry)
            result["icon"] = Path(var_icon.get().strip()) if var_icon.get().strip() else None
            dlg.destroy()

        def on_cancel():
            dlg.destroy()

        tk.Button(btn_frame, text="Build", command=on_ok,
                  bg=ACCENT, fg="white", relief=tk.FLAT, padx=16, pady=4,
                  font=("Helvetica", 10, "bold")).pack(side=tk.RIGHT, padx=(6, 0))
        tk.Button(btn_frame, text="Cancel", command=on_cancel,
                  bg=BG_ALT, fg=FG, relief=tk.FLAT, padx=12, pady=4).pack(side=tk.RIGHT)

        dlg.wait_window()

        if not result["ok"]:
            return

        onefile = result["onefile"]
        windowed = result["windowed"]
        include_tkinter = result["tkinter"]
        entry_file = result["entry"]
        icon_path = result["icon"]

        exe_name = self.project_dir.name.replace(" ", "_")

        self._console_clear()
        self._console_write(
            f"Building executable from {entry_file.name}\n"
            f"Output name: {exe_name}\n"
            f"Mode: {'onefile' if onefile else 'onedir'}\n"
            f"Windowed: {'yes' if windowed else 'no'}\n"
            f"Tkinter support: {'yes' if include_tkinter else 'no'}\n"
            f"Icon: {icon_path if icon_path else '(none)'}\n\n"
        )
        self.btn_run.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.btn_build.config(state="disabled")
        self.proc_running = True

        pip = venv_pip(self.venv_dir)
        dist_dir = self.project_dir / "dist"
        work_dir = self.project_dir / "build"
        spec_dir = self.project_dir

        pyinstaller_cmd = [
            str(vpy), "-m", "PyInstaller",
            "--noconfirm",
            "--clean",
            "--name", exe_name,
            "--distpath", str(dist_dir),
            "--workpath", str(work_dir),
            "--specpath", str(spec_dir),
        ]
        if onefile:
            pyinstaller_cmd.append("--onefile")
        if windowed:
            pyinstaller_cmd.append("--windowed")
        if include_tkinter:
            pyinstaller_cmd.extend(["--hidden-import=tkinter", "--collect-all", "tkinter"])
        if icon_path and icon_path.exists():
            pyinstaller_cmd.extend(["--icon", str(icon_path)])
        pyinstaller_cmd.append(str(entry_file))

        self.build_queue = queue.Queue()

        def worker():
            check = subprocess.run(
                [str(vpy), "-c", "import PyInstaller"],
                cwd=str(self.project_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if check.returncode != 0:
                self.build_queue.put(("line", "PyInstaller not found in venv — installing...\n"))
                try:
                    kwargs = {}
                    if IS_WINDOWS:
                        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                    inst = subprocess.Popen(
                        [str(pip), "install", "pyinstaller"],
                        cwd=str(self.project_dir),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                        env=clean_subprocess_env(),
                        **kwargs,
                    )
                    self.build_queue.put(("proc", inst))
                    for line in inst.stdout:
                        self.build_queue.put(("line", line))
                    inst.wait()
                    if inst.returncode != 0:
                        self.build_queue.put(("error", f"pip install pyinstaller failed (exit {inst.returncode})"))
                        self.build_queue.put(("done", inst.returncode))
                        return
                    self.build_queue.put(("line", "\nPyInstaller installed. Starting build...\n\n"))
                except Exception as exc:
                    self.build_queue.put(("error", str(exc)))
                    self.build_queue.put(("done", -1))
                    return

            stream_process(pyinstaller_cmd, self.project_dir, self.build_queue)

        threading.Thread(target=worker, daemon=True).start()
        self.after(50, self._poll_build_queue)

    def _poll_build_queue(self):
        try:
            while True:
                kind, payload = self.build_queue.get_nowait()
                if kind == "proc":
                    self.current_proc = payload
                elif kind == "line":
                    self._console_write(payload)
                elif kind == "error":
                    self._console_write(f"[error] {payload}\n")
                elif kind == "done":
                    if self.project_dir:
                        work_dir = self.project_dir / "build"
                        if work_dir.exists():
                            try:
                                shutil.rmtree(work_dir)
                                self._console_write("Removed temporary build/ directory.\n")
                            except Exception as exc:
                                self._console_write(f"Could not remove build/: {exc}\n")
                        spec_path = self.project_dir / f"{self.project_dir.name.replace(' ', '_')}.spec"
                        if spec_path.exists():
                            try:
                                spec_path.unlink()
                            except Exception:
                                pass
                    exe_name = self.project_dir.name.replace(" ", "_") if self.project_dir else "app"
                    dist_onefile = self.project_dir / "dist" / exe_name if self.project_dir else None
                    if payload == 0 and dist_onefile and dist_onefile.exists():
                        self._console_write(f"\nBuild finished. Executable:\n  {dist_onefile}\n")
                        self._console_write("Run it with:\n  chmod +x \"{0}\" && \"{0}\"\n".format(dist_onefile))
                    elif payload == 0 and self.project_dir:
                        self._console_write(f"\nBuild finished. See:\n  {self.project_dir / 'dist'}\n")
                    else:
                        self._console_write(f"\n[build exited with code {payload}]\n")
                    self.proc_running = False
                    self.current_proc = None
                    self.btn_run.config(state="normal")
                    self.btn_stop.config(state="disabled")
                    self.btn_build.config(state="normal")
                    self._refresh_tree()
                    return
        except queue.Empty:
            pass
        if self.proc_running:
            self.after(50, self._poll_build_queue)

    # ------------------------------------------------------------------
    # Venv terminal
    # ------------------------------------------------------------------
    def open_terminal_btn(self):
        if not self.project_dir:
            messagebox.showwarning(APP_TITLE, "Open or create a project first.")
            return
        if not has_venv(self.project_dir):
            messagebox.showwarning(APP_TITLE, "This project has no virtual environment yet.")
            return
        ok = open_terminal(self.project_dir, self.venv_dir)
        if not ok:
            messagebox.showerror(
                APP_TITLE,
                "Couldn't find a terminal to launch.\n\nActivate the venv manually:\n"
                f'  cd "{self.project_dir}"\n'
                "  source .venv/bin/activate      (Linux/Mac)\n"
                "  .venv\\Scripts\\activate         (Windows)"
            )

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------
    def _on_close(self):
        if not self._maybe_save_prompt():
            return
        if self.current_proc and self.proc_running:
            try:
                self.current_proc.terminate()
            except Exception:
                pass
        self.destroy()


if __name__ == "__main__":
    app = PySimpleIDE()
    app.mainloop()
