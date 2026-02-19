#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Valid2000 (v0.0.1)
Author: Jan Houserek
License: GPLv3
"""

from __future__ import annotations

import sys
import pathlib
import threading
import queue
import subprocess
import json
import tkinter as tk
import tiff
from tkinter import ttk, filedialog, messagebox


APP_TITLE = "Valid2000 v0.0.1"
SCRIPT_VERSION = "2026-02-19-valid2000-v0.0.1"
DEFAULT_TIMEOUT_JP2 = 60
DEFAULT_TIMEOUT_TIFF = 30
CONFIG_NAME = "gui_config.json"


# -------------------------
# Tooltip (Tkinter)
# -------------------------

class ToolTip:
    def __init__(self, widget: tk.Widget, text: str, delay_ms: int = 450):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._after_id: str | None = None
        self._tw: tk.Toplevel | None = None

        widget.bind("<Enter>", self._on_enter, add=True)
        widget.bind("<Leave>", self._on_leave, add=True)
        widget.bind("<ButtonPress>", self._on_leave, add=True)

    def _on_enter(self, _evt=None):
        self._cancel()
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _on_leave(self, _evt=None):
        self._cancel()
        self._hide()

    def _cancel(self):
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _show(self):
        if self._tw is not None:
            return
        if not self.text:
            return

        x = self.widget.winfo_rootx() + 16
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6

        tw = tk.Toplevel(self.widget)
        self._tw = tw
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")

        lbl = tk.Label(
            tw,
            text=self.text,
            justify="left",
            background="#1f1f1f",
            foreground="#e6e6e6",
            relief="solid",
            borderwidth=1,
            padx=8,
            pady=6,
            font=("TkDefaultFont", 9),
            wraplength=560,
        )
        lbl.pack()

    def _hide(self):
        if self._tw is not None:
            try:
                self._tw.destroy()
            except Exception:
                pass
            self._tw = None


# -------------------------
# Helpers
# -------------------------

def try_int(s: str, default: int) -> int:
    s = (s or "").strip()
    if not s:
        return default
    return int(s)


def split_semicolon_globs(s: str, fallback: list[str]) -> list[str]:
    s = (s or "").strip()
    if not s:
        return fallback
    parts = [p.strip() for p in s.split(";")]
    parts = [p for p in parts if p]
    return parts if parts else fallback


# -------------------------
# App
# -------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1180x860")
        self.minsize(980, 660)

        self._q: "queue.Queue[tuple[str, str, str]]" = queue.Queue()
        self._proc: subprocess.Popen | None = None
        self._worker: threading.Thread | None = None

        self._build_ui()
        self._load_config()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.after(50, self._drain_queue)

    # ---------------- UI ----------------

    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        nb = ttk.Notebook(self)
        nb.grid(row=0, column=0, sticky="nsew", padx=10, pady=(10, 6))
        self.nb = nb

        self.tab_jp2 = ttk.Frame(nb)
        self.tab_tiff = ttk.Frame(nb)
        nb.add(self.tab_jp2, text="JP2 (jpylyzer + NDK)")
        nb.add(self.tab_tiff, text="TIFF (WSL tiffdump + NDK)")

        # Output
        out_frame = ttk.Frame(self)
        out_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 6))
        out_frame.columnconfigure(0, weight=1)
        out_frame.rowconfigure(0, weight=1)

        self.txt = tk.Text(out_frame, wrap="word", undo=False)
        self.txt.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(out_frame, orient="vertical", command=self.txt.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        self.txt.configure(yscrollcommand=yscroll.set)

        self.txt.tag_configure("OK", foreground="#1b8f3a")
        self.txt.tag_configure("WARN", foreground="#b36b00")
        self.txt.tag_configure("FAIL", foreground="#c62828")
        self.txt.tag_configure("ERROR", foreground="#c62828", font=("TkDefaultFont", 10, "bold"))
        self.txt.tag_configure("HEADER", foreground="#2a5bd7", font=("TkDefaultFont", 10, "bold"))
        self.txt.tag_configure("DIM", foreground="#777777")

        status = ttk.Frame(self)
        status.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))
        status.columnconfigure(0, weight=1)
        self.var_status = tk.StringVar(value="Připraveno.")
        ttk.Label(status, textvariable=self.var_status).grid(row=0, column=0, sticky="w")

        self._build_tab_jp2(self.tab_jp2)
        self._build_tab_tiff(self.tab_tiff)

        bottom = ttk.Frame(self)
        bottom.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 12))
        bottom.columnconfigure(0, weight=1)
        ttk.Button(bottom, text="Vyčistit výstup", command=self._clear_output).grid(row=0, column=1, sticky="e", padx=(8, 0))
        ttk.Button(bottom, text="Zkopírovat vše", command=self._copy_output).grid(row=0, column=2, sticky="e", padx=(8, 0))
        ttk.Button(bottom, text="Uložit výstup…", command=self._save_output).grid(row=0, column=3, sticky="e", padx=(8, 0))

    def _build_tab_jp2(self, parent: ttk.Frame):
        parent.columnconfigure(1, weight=1)

        ttk.Label(parent, text="Vstup (JP2/XML nebo adresář):").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=(10, 6))
        self.var_jp2_input = tk.StringVar()
        ent_in = ttk.Entry(parent, textvariable=self.var_jp2_input)
        ent_in.grid(row=0, column=1, sticky="ew", pady=(10, 6))
        ToolTip(ent_in, "Vyber soubor JP2/J2K/J2C/JPH/JHC/XML nebo adresář (batch).")

        btns_pick = ttk.Frame(parent)
        btns_pick.grid(row=0, column=2, sticky="e", padx=(8, 0), pady=(10, 6))
        ttk.Button(btns_pick, text="Soubor…", command=self._pick_jp2_input_file).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(btns_pick, text="Adresář…", command=self._pick_jp2_input_dir).grid(row=0, column=1)

        ttk.Label(parent, text="Profil (JSON, volitelné):").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(0, 6))
        self.var_jp2_profile = tk.StringVar()
        ent_prof = ttk.Entry(parent, textvariable=self.var_jp2_profile)
        ent_prof.grid(row=1, column=1, sticky="ew", pady=(0, 6))
        ToolTip(ent_prof, "Volitelný JSON profil s rules/descriptions. Když není, používá se vestavěný profil v jp2.py.")
        ttk.Button(parent, text="Vybrat…", command=self._pick_jp2_profile).grid(row=1, column=2, sticky="e", padx=(8, 0), pady=(0, 6))

        ttk.Label(parent, text="jpylyzer cmd (exe, volitelné):").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(0, 6))
        self.var_jp2_jpy = tk.StringVar()
        ent_jpy = ttk.Entry(parent, textvariable=self.var_jp2_jpy)
        ent_jpy.grid(row=2, column=1, sticky="ew", pady=(0, 6))
        ToolTip(ent_jpy, "Cesta k jpylyzer.exe. Když prázdné, spouští se 'jpylyzer' z PATH.")
        ttk.Button(parent, text="Vybrat…", command=self._pick_jp2_jpylyzer).grid(row=2, column=2, sticky="e", padx=(8, 0), pady=(0, 6))

        batch = ttk.Labelframe(parent, text="Batch (adresář)", padding=10)
        batch.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(6, 6))
        batch.columnconfigure(1, weight=1)

        self.var_jp2_recursive = tk.BooleanVar(value=False)
        cb_rec = ttk.Checkbutton(batch, text="Rekurzivně", variable=self.var_jp2_recursive)
        cb_rec.grid(row=0, column=0, sticky="w")
        ToolTip(cb_rec, "Když je vstup adresář: hledat i v podadresářích.")

        ttk.Label(batch, text="Glob:").grid(row=0, column=1, sticky="e", padx=(12, 6))
        self.var_jp2_glob = tk.StringVar(value="*.jp2;*.j2k;*.j2c;*.jph;*.jhc;*.xml")
        ent_glob = ttk.Entry(batch, textvariable=self.var_jp2_glob)
        ent_glob.grid(row=0, column=2, sticky="ew")
        ToolTip(ent_glob, "Pro adresář: více globů odděl středníkem. Např. *.jp2;*.xml")

        opts = ttk.Labelframe(parent, text="Přepínače JP2", padding=10)
        opts.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 8))
        opts.columnconfigure(0, weight=1)

        row0 = ttk.Frame(opts)
        row0.grid(row=0, column=0, sticky="ew")
        ttk.Label(row0, text="Timeout (s):").grid(row=0, column=0, sticky="w")
        self.var_jp2_timeout = tk.StringVar(value=str(DEFAULT_TIMEOUT_JP2))
        ent_to = ttk.Entry(row0, width=8, textvariable=self.var_jp2_timeout)
        ent_to.grid(row=0, column=1, sticky="w", padx=(6, 18))
        ToolTip(ent_to, "Timeout pro běh jp2.py/jpylyzer (sekundy) na 1 soubor.")

        ttk.Label(row0, text="--jp2-format:").grid(row=0, column=2, sticky="w")
        self.var_jp2_format = tk.StringVar(value="")
        cmb = ttk.Combobox(row0, textvariable=self.var_jp2_format, values=["", "jp2", "jph", "j2c", "jhc"], width=6, state="readonly")
        cmb.grid(row=0, column=3, sticky="w", padx=(6, 18))
        ToolTip(cmb, "Předá jpylyzeru --format (typ codestreamu).")

        ttk.Label(row0, text="--mix:").grid(row=0, column=4, sticky="w")
        self.var_jp2_mix = tk.StringVar(value="")
        cmb2 = ttk.Combobox(row0, textvariable=self.var_jp2_mix, values=["", "1", "2"], width=4, state="readonly")
        cmb2.grid(row=0, column=5, sticky="w", padx=(6, 18))
        ToolTip(cmb2, "Přidá NISO MIX výstup (1.0/2.0).")

        checks = ttk.Frame(opts)
        checks.grid(row=1, column=0, sticky="w", pady=(10, 0))

        self.var_jp2_dump = tk.BooleanVar(value=False)
        self.var_jp2_show_ok = tk.BooleanVar(value=False)
        self.var_jp2_nopretty = tk.BooleanVar(value=False)
        self.var_jp2_nullxml = tk.BooleanVar(value=False)
        self.var_jp2_recurse_jpylyzer = tk.BooleanVar(value=False)
        self.var_jp2_packetmarkers = tk.BooleanVar(value=False)
        self.var_jp2_verbose = tk.BooleanVar(value=False)
        self.var_jp2_scan_markers = tk.BooleanVar(value=True)
        self.var_jp2_dump_scan = tk.BooleanVar(value=False)

        def add_cb(r: int, text: str, var: tk.BooleanVar, tip: str):
            cb = ttk.Checkbutton(checks, text=text, variable=var)
            cb.grid(row=r, column=0, sticky="w")
            ToolTip(cb, tip)

        add_cb(0, "--dump-map", self.var_jp2_dump, "Vypíše mapu klíčů jako JSON (pozor: u batch je to hodně textu).")
        add_cb(1, "--show-ok", self.var_jp2_show_ok, "Zobrazí i pravidla, která prošla (OK).")
        add_cb(2, "--nopretty", self.var_jp2_nopretty, "Předá jpylyzeru --nopretty.")
        add_cb(3, "--nullxml", self.var_jp2_nullxml, "Předá jpylyzeru --nullxml.")
        add_cb(4, "--recurse (jpylyzer)", self.var_jp2_recurse_jpylyzer, "Předá jpylyzeru --recurse (jiné než batch rekurze).")
        add_cb(5, "--packetmarkers", self.var_jp2_packetmarkers, "Předá jpylyzeru --packetmarkers.")
        add_cb(6, "--verbose", self.var_jp2_verbose, "Předá jpylyzeru --verbose.")
        add_cb(7, "--scan-markers (FF55)", self.var_jp2_scan_markers, "Hledá FF55 (TLM) v codestreamu (jp2c payload).")
        add_cb(8, "--dump-jp2scan", self.var_jp2_dump_scan, "Vypíše JP2SCAN shrnutí (offsety/count).")

        btns = ttk.Frame(parent)
        btns.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        btns.columnconfigure(0, weight=1)

        self.btn_jp2_run = ttk.Button(btns, text="▶ Spustit JP2 validaci", command=self._run_jp2)
        self.btn_jp2_run.grid(row=0, column=1, sticky="e", padx=(8, 0))
        ToolTip(self.btn_jp2_run, "Spustí jp2.py přes aktuální Python (venv).")

        self.btn_stop = ttk.Button(btns, text="■ Stop", command=self._stop, state="disabled")
        self.btn_stop.grid(row=0, column=2, sticky="e", padx=(8, 0))
        ToolTip(self.btn_stop, "Ukončí běžící proces (terminate).")

    def _build_tab_tiff(self, parent: ttk.Frame):
        parent.columnconfigure(1, weight=1)

        ttk.Label(parent, text="Vstup (TIFF nebo adresář):").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=(10, 6))
        self.var_tiff_input = tk.StringVar()
        ent_in = ttk.Entry(parent, textvariable=self.var_tiff_input)
        ent_in.grid(row=0, column=1, sticky="ew", pady=(10, 6))
        ToolTip(ent_in, "Vyber .tif/.tiff nebo adresář (batch).")

        btns_pick = ttk.Frame(parent)
        btns_pick.grid(row=0, column=2, sticky="e", padx=(8, 0), pady=(10, 6))
        ttk.Button(btns_pick, text="Soubor…", command=self._pick_tiff_input_file).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(btns_pick, text="Adresář…", command=self._pick_tiff_input_dir).grid(row=0, column=1)

        ttk.Label(parent, text="Profil TIFF (JSON, volitelné):").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(0, 6))
        self.var_tiff_profile = tk.StringVar()
        ent_prof = ttk.Entry(parent, textvariable=self.var_tiff_profile)
        ent_prof.grid(row=1, column=1, sticky="ew", pady=(0, 6))
        ToolTip(ent_prof, "Volitelný JSON profil pro TIFF. Když není, použije se vestavěný NDK Master profil.")
        ttk.Button(parent, text="Vybrat…", command=self._pick_tiff_profile).grid(row=1, column=2, sticky="e", padx=(8, 0), pady=(0, 6))

        batch = ttk.Labelframe(parent, text="Batch (adresář)", padding=10)
        batch.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(6, 6))
        batch.columnconfigure(1, weight=1)

        self.var_tiff_recursive = tk.BooleanVar(value=False)
        cb_rec = ttk.Checkbutton(batch, text="Rekurzivně", variable=self.var_tiff_recursive)
        cb_rec.grid(row=0, column=0, sticky="w")
        ToolTip(cb_rec, "Když je vstup adresář: hledat i v podadresářích.")

        ttk.Label(batch, text="Glob:").grid(row=0, column=1, sticky="e", padx=(12, 6))
        self.var_tiff_glob = tk.StringVar(value="*.tif;*.tiff")
        ent_glob = ttk.Entry(batch, textvariable=self.var_tiff_glob)
        ent_glob.grid(row=0, column=2, sticky="ew")
        ToolTip(ent_glob, "Pro adresář: více globů odděl středníkem. Např. *.tif;*.tiff")

        opts = ttk.Labelframe(parent, text="WSL / tiffdump", padding=10)
        opts.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 8))
        opts.columnconfigure(5, weight=1)

        ttk.Label(opts, text="WSL launcher:").grid(row=0, column=0, sticky="w")
        self.var_wsl = tk.StringVar(value="wsl")
        ent_wsl = ttk.Entry(opts, width=12, textvariable=self.var_wsl)
        ent_wsl.grid(row=0, column=1, sticky="w", padx=(6, 18))
        ToolTip(ent_wsl, "Příkaz pro spuštění WSL (typicky 'wsl').")

        ttk.Label(opts, text="tiffdump cmd:").grid(row=0, column=2, sticky="w")
        self.var_tiffdump = tk.StringVar(value="tiffdump")
        ent_td = ttk.Entry(opts, width=14, textvariable=self.var_tiffdump)
        ent_td.grid(row=0, column=3, sticky="w", padx=(6, 18))
        ToolTip(ent_td, "Název příkazu ve WSL (typicky 'tiffdump').")

        ttk.Label(opts, text="Extra args:").grid(row=0, column=4, sticky="w")
        self.var_tiff_args = tk.StringVar(value="")
        ent_args = ttk.Entry(opts, textvariable=self.var_tiff_args)
        ent_args.grid(row=0, column=5, sticky="ew", padx=(6, 0))
        ToolTip(ent_args, "Extra argumenty pro tiffdump (jednoduchý split podle mezer).")

        self.var_tiff_convert_path = tk.BooleanVar(value=True)
        cb = ttk.Checkbutton(opts, text="Převést Win cestu → /mnt/…", variable=self.var_tiff_convert_path)
        cb.grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ToolTip(cb, "Zapnuto: C:\\… převede na /mnt/c/… aby to WSL umělo otevřít.")

        ttk.Label(opts, text="Timeout (s):").grid(row=1, column=3, sticky="w", pady=(8, 0))
        self.var_tiff_timeout = tk.StringVar(value=str(DEFAULT_TIMEOUT_TIFF))
        ent_to = ttk.Entry(opts, width=8, textvariable=self.var_tiff_timeout)
        ent_to.grid(row=1, column=4, sticky="w", padx=(6, 18), pady=(8, 0))
        ToolTip(ent_to, "Timeout pro běh tiffdump (sekundy) na 1 soubor.")

        self.var_tiff_show_ok = tk.BooleanVar(value=False)
        cb2 = ttk.Checkbutton(opts, text="Zobrazit i OK pravidla", variable=self.var_tiff_show_ok)
        cb2.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ToolTip(cb2, "Když zapnuto, vypíše i [OK] řádky v TIFF reportu.")

        btns = ttk.Frame(parent)
        btns.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        btns.columnconfigure(0, weight=1)

        self.btn_tiff_run = ttk.Button(btns, text="▶ Spustit TIFF validaci (tiff.py)", command=self._run_tiff)
        self.btn_tiff_run.grid(row=0, column=1, sticky="e", padx=(8, 0))
        ToolTip(self.btn_tiff_run, "Spustí tiff.py, který umí file i directory (batch).")

    # ---------------- Dialogs ----------------

    def _pick_jp2_input_file(self):
        p = filedialog.askopenfilename(
            title="Vyber JP2 nebo XML",
            filetypes=[("JP2 / XML", "*.jp2 *.j2k *.j2c *.jph *.jhc *.xml"), ("All files", "*.*")]
        )
        if p:
            self.var_jp2_input.set(p)

    def _pick_jp2_input_dir(self):
        p = filedialog.askdirectory(title="Vyber adresář (JP2 batch)")
        if p:
            self.var_jp2_input.set(p)

    def _pick_jp2_profile(self):
        p = filedialog.askopenfilename(
            title="Vyber profil JSON",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")]
        )
        if p:
            self.var_jp2_profile.set(p)

    def _pick_jp2_jpylyzer(self):
        p = filedialog.askopenfilename(
            title="Vyber jpylyzer executable",
            filetypes=[("Executable", "*.exe"), ("All files", "*.*")]
        )
        if p:
            self.var_jp2_jpy.set(p)

    def _pick_tiff_input_file(self):
        p = filedialog.askopenfilename(
            title="Vyber TIFF",
            filetypes=[("TIFF", "*.tif *.tiff"), ("All files", "*.*")]
        )
        if p:
            self.var_tiff_input.set(p)

    def _pick_tiff_input_dir(self):
        p = filedialog.askdirectory(title="Vyber adresář (TIFF batch)")
        if p:
            self.var_tiff_input.set(p)

    def _pick_tiff_profile(self):
        p = filedialog.askopenfilename(
            title="Vyber TIFF profil JSON",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")]
        )
        if p:
            self.var_tiff_profile.set(p)

    # ---------------- Output helpers ----------------

    def _clear_output(self):
        self.txt.delete("1.0", "end")
        self.var_status.set("Vyčištěno.")

    def _copy_output(self):
        data = self.txt.get("1.0", "end-1c")
        self.clipboard_clear()
        self.clipboard_append(data)
        self.var_status.set("Zkopírováno do schránky.")

    def _save_output(self):
        data = self.txt.get("1.0", "end-1c")
        p = filedialog.asksaveasfilename(
            title="Uložit výstup",
            defaultextension=".txt",
            filetypes=[("Text", "*.txt"), ("All files", "*.*")]
        )
        if not p:
            return
        pathlib.Path(p).write_text(data, encoding="utf-8", errors="replace")
        self.var_status.set(f"Uloženo: {p}")

    def _append(self, tag: str, text: str):
        self.txt.insert("end", text, tag)
        self.txt.see("end")

    def _tag_for_line(self, line: str) -> str:
        s = (line or "").strip()
        if s.startswith("OK:"):
            return "OK"
        if s.startswith("SUMMARY:") or s.startswith("BATCH SUMMARY") or s.startswith("JP2 RUN") or s.startswith("TIFF RUN"):
            return "HEADER"
        if s.startswith("[FAIL]"):
            return "FAIL"
        if s.startswith("[WARN]"):
            return "WARN"
        if s.startswith("[OK]"):
            return "OK"
        if s.startswith("ERROR:") or s.startswith("Traceback"):
            return "ERROR"
        if s.startswith("  - ") or s.startswith("- "):
            return "DIM"
        if s.startswith("=== FILE"):
            return "HEADER"
        return ""

    # ---------------- Process control ----------------

    def _stop(self):
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._append("ERROR", "\n[STOP] Ukončuji proces…\n")

    def _set_running(self, running: bool):
        self.btn_stop.configure(state=("normal" if running else "disabled"))
        self.btn_jp2_run.configure(state=("disabled" if running else "normal"))
        self.btn_tiff_run.configure(state=("disabled" if running else "normal"))
        self.var_status.set("Běží…" if running else "Připraveno.")

    def _run_cmd_streamed(self, cmd: list[str], timeout_sec: int, mode: str):
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
            )
            assert self._proc.stdout is not None

            for line in self._proc.stdout:
                self._q.put(("LINE", mode, line))

            rc = self._proc.wait(timeout=timeout_sec) if timeout_sec > 0 else self._proc.wait()
            self._q.put(("DONE", mode, str(rc)))
        except subprocess.TimeoutExpired:
            try:
                if self._proc and self._proc.poll() is None:
                    self._proc.terminate()
            except Exception:
                pass
            self._q.put(("ERROR", mode, f"Timeout po {timeout_sec}s"))
        except Exception as e:
            self._q.put(("ERROR", mode, str(e)))
        finally:
            self._proc = None

    def _drain_queue(self):
        try:
            while True:
                typ, mode, payload = self._q.get_nowait()

                if typ == "LINE":
                    tag = self._tag_for_line(payload)
                    self._append(tag, payload if payload.endswith("\n") else payload + "\n")

                elif typ == "DONE":
                    rc = int(payload)
                    self.var_status.set(f"Hotovo ({mode} exit {rc}).")
                    self._append("DIM", "\n")
                    self._set_running(False)

                elif typ == "ERROR":
                    self._append("ERROR", f"ERROR: {payload}\n")
                    self._set_running(False)

        except queue.Empty:
            pass
        finally:
            self.after(50, self._drain_queue)

    # ---------------- JP2 run ----------------

    def _validate_jp2_inputs(self) -> tuple[pathlib.Path, pathlib.Path]:
        inp = self.var_jp2_input.get().strip()
        if not inp:
            raise ValueError("Zadej vstupní JP2/XML soubor nebo adresář.")
        in_path = pathlib.Path(inp)
        if not in_path.exists():
            raise ValueError(f"Vstupní cesta neexistuje: {in_path}")

        jp2_py = pathlib.Path(__file__).resolve().parent / "jp2.py"
        if not jp2_py.exists():
            raise ValueError(f"Nenalezen jp2.py vedle gui.py: {jp2_py}")

        _ = try_int(self.var_jp2_timeout.get(), DEFAULT_TIMEOUT_JP2)
        return in_path, jp2_py

    def _build_jp2_cmd(self, in_path: pathlib.Path, jp2_py: pathlib.Path) -> tuple[list[str], int]:
        cmd = [sys.executable, str(jp2_py), str(in_path)]

        prof = self.var_jp2_profile.get().strip()
        if prof:
            cmd += ["--profile", prof]

        jpy = self.var_jp2_jpy.get().strip()
        if jpy:
            cmd += ["--jpylyzer-cmd", jpy]

        timeout = try_int(self.var_jp2_timeout.get(), DEFAULT_TIMEOUT_JP2)
        cmd += ["--timeout", str(timeout)]

        fmt = self.var_jp2_format.get().strip()
        if fmt:
            cmd += ["--jp2-format", fmt]

        mix = self.var_jp2_mix.get().strip()
        if mix:
            cmd += ["--mix", mix]

        if self.var_jp2_dump.get():
            cmd += ["--dump-map"]
        if self.var_jp2_show_ok.get():
            cmd += ["--show-ok"]
        if self.var_jp2_nopretty.get():
            cmd += ["--nopretty"]
        if self.var_jp2_nullxml.get():
            cmd += ["--nullxml"]
        if self.var_jp2_recurse_jpylyzer.get():
            cmd += ["--recurse"]
        if self.var_jp2_packetmarkers.get():
            cmd += ["--packetmarkers"]
        if self.var_jp2_verbose.get():
            cmd += ["--verbose"]
        if self.var_jp2_scan_markers.get():
            cmd += ["--scan-markers"]
        if self.var_jp2_dump_scan.get():
            cmd += ["--dump-jp2scan"]

        # directory mode flags (only meaningful if input is dir, but harmless otherwise)
        if self.var_jp2_recursive.get():
            cmd += ["--recursive"]
        globs = split_semicolon_globs(self.var_jp2_glob.get(), ["*.jp2", "*.j2k", "*.j2c", "*.jph", "*.jhc", "*.xml"])
        for g in globs:
            cmd += ["--glob", g]

        return cmd, max(timeout, 1)

    def _run_jp2(self):
        if self._worker and self._worker.is_alive():
            messagebox.showinfo("Běží", "Nějaký proces už běží.")
            return
        try:
            in_path, jp2_py = self._validate_jp2_inputs()
            cmd, timeout = self._build_jp2_cmd(in_path, jp2_py)
        except Exception as e:
            messagebox.showerror("Chyba", str(e))
            return

        self._clear_output()
        self._append("HEADER", "JP2 RUN\n")
        self._append("DIM", " ".join(cmd) + "\n\n")
        self._set_running(True)

        self._worker = threading.Thread(target=self._run_cmd_streamed, args=(cmd, timeout, "JP2"), daemon=True)
        self._worker.start()

    # ---------------- TIFF run ----------------

    def _validate_tiff_inputs(self) -> tuple[pathlib.Path, pathlib.Path]:
        inp = self.var_tiff_input.get().strip()
        if not inp:
            raise ValueError("Zadej vstupní TIFF soubor nebo adresář.")
        in_path = pathlib.Path(inp)
        if not in_path.exists():
            raise ValueError(f"Vstupní cesta neexistuje: {in_path}")

        tiff_py = pathlib.Path(__file__).resolve().parent / "tiff.py"
        if not tiff_py.exists():
            raise ValueError(f"Nenalezen tiff.py vedle gui.py: {tiff_py}")

        _ = try_int(self.var_tiff_timeout.get(), DEFAULT_TIMEOUT_TIFF)
        return in_path, tiff_py

    def _build_tiff_cmd(self, in_path: pathlib.Path, tiff_py: pathlib.Path) -> tuple[list[str], int]:
        cmd = [sys.executable, str(tiff_py), str(in_path)]

        prof = (self.var_tiff_profile.get() or "").strip()
        if prof:
            cmd += ["--profile", prof]

        if self.var_tiff_show_ok.get():
            cmd += ["--show-ok"]

        wsl = (self.var_wsl.get() or "wsl").strip()
        td = (self.var_tiffdump.get() or "tiffdump").strip()
        extra = (self.var_tiff_args.get() or "").strip()

        cmd += ["--wsl", wsl, "--tiffdump", td, "--timeout", str(try_int(self.var_tiff_timeout.get(), DEFAULT_TIMEOUT_TIFF))]
        if extra:
            cmd += ["--tiffdump-args", extra]
        if not self.var_tiff_convert_path.get():
            cmd += ["--no-convert-path"]

        if self.var_tiff_recursive.get():
            cmd += ["--recursive"]

        globs = split_semicolon_globs(self.var_tiff_glob.get(), ["*.tif", "*.tiff"])
        for g in globs:
            cmd += ["--glob", g]

        return cmd, max(try_int(self.var_tiff_timeout.get(), DEFAULT_TIMEOUT_TIFF), 1)

    def _run_tiff(self):
        if self._worker and self._worker.is_alive():
            messagebox.showinfo("Běží", "Nějaký proces už běží.")
            return
        try:
            in_path, tiff_py = self._validate_tiff_inputs()
            cmd, timeout = self._build_tiff_cmd(in_path, tiff_py)
        except Exception as e:
            messagebox.showerror("Chyba", str(e))
            return

        self._clear_output()
        self._append("HEADER", "TIFF RUN\n")
        self._append("DIM", " ".join(cmd) + "\n\n")
        self._set_running(True)

        self._worker = threading.Thread(target=self._run_cmd_streamed, args=(cmd, timeout, "TIFF"), daemon=True)
        self._worker.start()

    # ---------------- Config persist ----------------

    def _config_path(self) -> pathlib.Path:
        return pathlib.Path(__file__).resolve().parent / CONFIG_NAME

    def _load_config(self):
        p = self._config_path()
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return

        # JP2
        self.var_jp2_input.set(data.get("jp2_input", ""))
        self.var_jp2_profile.set(data.get("jp2_profile", ""))
        self.var_jp2_jpy.set(data.get("jp2_jpy", ""))
        self.var_jp2_timeout.set(str(data.get("jp2_timeout", DEFAULT_TIMEOUT_JP2)))
        self.var_jp2_format.set(data.get("jp2_format", ""))
        self.var_jp2_mix.set(data.get("jp2_mix", ""))
        self.var_jp2_dump.set(bool(data.get("jp2_dump", False)))
        self.var_jp2_show_ok.set(bool(data.get("jp2_show_ok", False)))
        self.var_jp2_nopretty.set(bool(data.get("jp2_nopretty", False)))
        self.var_jp2_nullxml.set(bool(data.get("jp2_nullxml", False)))
        self.var_jp2_recurse_jpylyzer.set(bool(data.get("jp2_recurse_jpylyzer", False)))
        self.var_jp2_packetmarkers.set(bool(data.get("jp2_packetmarkers", False)))
        self.var_jp2_verbose.set(bool(data.get("jp2_verbose", False)))
        self.var_jp2_scan_markers.set(bool(data.get("jp2_scan_markers", True)))
        self.var_jp2_dump_scan.set(bool(data.get("jp2_dump_scan", False)))
        self.var_jp2_recursive.set(bool(data.get("jp2_recursive", False)))
        self.var_jp2_glob.set(data.get("jp2_glob", "*.jp2;*.j2k;*.j2c;*.jph;*.jhc;*.xml"))

        # TIFF
        self.var_tiff_input.set(data.get("tiff_input", ""))
        self.var_tiff_profile.set(data.get("tiff_profile", ""))
        self.var_wsl.set(data.get("wsl", "wsl"))
        self.var_tiffdump.set(data.get("tiffdump", "tiffdump"))
        self.var_tiff_args.set(data.get("tiff_args", ""))
        self.var_tiff_convert_path.set(bool(data.get("tiff_convert_path", True)))
        self.var_tiff_timeout.set(str(data.get("tiff_timeout", DEFAULT_TIMEOUT_TIFF)))
        self.var_tiff_show_ok.set(bool(data.get("tiff_show_ok", False)))
        self.var_tiff_recursive.set(bool(data.get("tiff_recursive", False)))
        self.var_tiff_glob.set(data.get("tiff_glob", "*.tif;*.tiff"))

        try:
            tab = data.get("active_tab", 0)
            self.nb.select(int(tab))
        except Exception:
            pass

    def _save_config(self):
        data = {
            # JP2
            "jp2_input": self.var_jp2_input.get(),
            "jp2_profile": self.var_jp2_profile.get(),
            "jp2_jpy": self.var_jp2_jpy.get(),
            "jp2_timeout": try_int(self.var_jp2_timeout.get(), DEFAULT_TIMEOUT_JP2),
            "jp2_format": self.var_jp2_format.get(),
            "jp2_mix": self.var_jp2_mix.get(),
            "jp2_dump": self.var_jp2_dump.get(),
            "jp2_show_ok": self.var_jp2_show_ok.get(),
            "jp2_nopretty": self.var_jp2_nopretty.get(),
            "jp2_nullxml": self.var_jp2_nullxml.get(),
            "jp2_recurse_jpylyzer": self.var_jp2_recurse_jpylyzer.get(),
            "jp2_packetmarkers": self.var_jp2_packetmarkers.get(),
            "jp2_verbose": self.var_jp2_verbose.get(),
            "jp2_scan_markers": self.var_jp2_scan_markers.get(),
            "jp2_dump_scan": self.var_jp2_dump_scan.get(),
            "jp2_recursive": self.var_jp2_recursive.get(),
            "jp2_glob": self.var_jp2_glob.get(),

            # TIFF
            "tiff_input": self.var_tiff_input.get(),
            "tiff_profile": self.var_tiff_profile.get(),
            "wsl": self.var_wsl.get(),
            "tiffdump": self.var_tiffdump.get(),
            "tiff_args": self.var_tiff_args.get(),
            "tiff_convert_path": self.var_tiff_convert_path.get(),
            "tiff_timeout": try_int(self.var_tiff_timeout.get(), DEFAULT_TIMEOUT_TIFF),
            "tiff_show_ok": self.var_tiff_show_ok.get(),
            "tiff_recursive": self.var_tiff_recursive.get(),
            "tiff_glob": self.var_tiff_glob.get(),

            # UI
            "active_tab": int(self.nb.index(self.nb.select())),
        }
        try:
            self._config_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _on_close(self):
        try:
            self._save_config()
        finally:
            self.destroy()


def main():
    try:
        app = App()
        app.mainloop()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
