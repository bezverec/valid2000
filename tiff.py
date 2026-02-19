#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Valid2000 (v0.0.1)
Author: Jan Houserek
License: GPLv3
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple, Optional, Iterable
import pathlib
import subprocess
import json
import re

APP_TITLE = "Valid2000 v0.0.1"
SCRIPT_VERSION = "2026-02-19-valid2000-v0.0.1"

# -------------------------
# Helpers
# -------------------------

def win_path_to_wsl(p: str) -> str:
    """
    Convert Windows path like C:\\temp\\Image00001.tif to /mnt/c/temp/Image00001.tif
    Best-effort only.
    """
    p = (p or "").strip()
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", p)
    if not m:
        if p.startswith("/"):
            return p
        return p
    drive = m.group(1).lower()
    rest = m.group(2).replace("\\", "/")
    return f"/mnt/{drive}/{rest}"


def parse_rational_or_float(s: str) -> float | None:
    s = (s or "").strip()
    if not s:
        return None
    if re.fullmatch(r"[+-]?\d+", s):
        return float(int(s))
    if re.fullmatch(r"[+-]?\d+\.\d+", s):
        return float(s)
    m = re.fullmatch(r"([+-]?\d+)\s*/\s*([+-]?\d+)", s)
    if m:
        num = int(m.group(1))
        den = int(m.group(2))
        if den == 0:
            return None
        return float(num) / float(den)
    return None


def split_args_simple(s: str) -> list[str]:
    # intentionally simple; GUI already says "split podle mezer"
    s = (s or "").strip()
    return s.split() if s else []


# -------------------------
# TIFF dump parsing (best-effort)
# -------------------------

TIFF_RE_KV = re.compile(
    r"^(?P<name>[A-Za-z0-9_]+)\s*"
    r"\((?P<tag>\d+)\)\s*"
    r"(?P<type>[A-Z]+)\s*"
    r"\((?P<typeid>\d+)\)\s*"
    r"(?P<count>\d+)<(?P<val>.*)>$"
)


def parse_tiffdump_lines(lines: list[str]) -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {}

    for ln in lines:
        ln = ln.strip()
        m = TIFF_RE_KV.match(ln)
        if not m:
            continue

        name = m.group("name")
        tag = int(m.group("tag"))
        typ = m.group("type")
        typeid = int(m.group("typeid"))
        count = int(m.group("count"))
        val_raw = m.group("val").strip()

        tokens: list[object] = []
        if val_raw:
            parts = val_raw.split()
            for p in parts:
                if re.fullmatch(r"[+-]?\d+", p):
                    tokens.append(int(p))
                else:
                    f = parse_rational_or_float(p)
                    if f is not None:
                        tokens.append(f)
                    else:
                        tokens.append(p)

        out[name] = {
            "name": name,
            "tag": tag,
            "type": typ,
            "typeid": typeid,
            "count": count,
            "value_raw": val_raw,
            "value_tokens": tokens,
        }

        if tag == 34675 and name != "ICCProfile":
            out["ICCProfile"] = out[name]

    return out


def normalize_tiff_map(tags: dict[str, dict[str, object]]) -> dict[str, object]:
    m: dict[str, object] = {}

    def setv(key: str, val: object):
        m[key] = val

    for k in (
        "ImageWidth", "ImageLength",
        "Compression", "Photometric",
        "SamplesPerPixel", "BitsPerSample",
        "XResolution", "YResolution",
        "ResolutionUnit",
        "PlanarConfiguration", "ExtraSamples", "Orientation",
        "ICCProfile",
    ):
        t = tags.get(k)
        if not t:
            setv(f"tiff.{k}", None)
            continue

        raw = t.get("value_raw")
        tok = t.get("value_tokens", [])

        setv(f"tiff.{k}", raw)

        if k in ("ImageWidth", "ImageLength", "Compression", "Photometric", "SamplesPerPixel", "ResolutionUnit"):
            if tok and isinstance(tok[0], int):
                setv(f"tiff.{k}.n", tok[0])

        if k in ("XResolution", "YResolution"):
            if tok and isinstance(tok[0], (int, float)):
                setv(f"tiff.{k}.dpi", float(tok[0]))

        if k == "BitsPerSample":
            if tok and all(isinstance(x, int) for x in tok):
                setv("tiff.BitsPerSample.list", tok)

        if k == "ICCProfile":
            setv("tiff.ICCProfile.present", True)
            setv("tiff.ICCProfile.type", t.get("type"))
            setv("tiff.ICCProfile.typeid", t.get("typeid"))

    icc_t = tags.get("ICCProfile")
    setv("derived.icc_present", icc_t is not None)
    setv("derived.icc_typeid", icc_t.get("typeid") if icc_t else None)

    return m


def tiff_summary_text(tags: dict[str, dict[str, object]]) -> str:
    def g(name: str) -> str:
        t = tags.get(name)
        if not t:
            return "—"
        return str(t.get("value_raw") or "—")

    icc = "Ano" if tags.get("ICCProfile") else "Ne"
    icc_type = "—"
    if tags.get("ICCProfile"):
        icc_type = f"{tags['ICCProfile'].get('type')} ({tags['ICCProfile'].get('typeid')})"

    return (
        "TIFF SUMMARY\n"
        f"- ImageWidth: {g('ImageWidth')}\n"
        f"- ImageLength: {g('ImageLength')}\n"
        f"- Compression: {g('Compression')}\n"
        f"- Photometric: {g('Photometric')}\n"
        f"- BitsPerSample: {g('BitsPerSample')}\n"
        f"- SamplesPerPixel: {g('SamplesPerPixel')}\n"
        f"- XResolution: {g('XResolution')}\n"
        f"- YResolution: {g('YResolution')}\n"
        f"- ResolutionUnit: {g('ResolutionUnit')}\n"
        f"- ICCProfile: {icc} (datatype: {icc_type})\n"
    )


# -------------------------
# Rule engine
# -------------------------

@dataclass
class RuleResult:
    rule_id: str
    key: str
    status: str  # OK/FAIL/WARN/SKIP
    expected: object
    found: object
    message: str
    level: str


def _match(assert_type: str, found: object, expected: object) -> tuple[bool, object]:
    if assert_type == "present":
        return (found is not None, True)
    if assert_type == "absent":
        return (found is None, False)
    if assert_type == "equals":
        return (found == expected, expected)
    if assert_type == "in":
        if expected is None:
            return (False, expected)
        return (found in expected, expected)
    if assert_type == "regex":
        if found is None:
            return (False, expected)
        return (re.fullmatch(str(expected), str(found)) is not None, expected)
    if assert_type == "min":
        if found is None:
            return (False, expected)
        try:
            return (float(found) >= float(expected), expected)
        except Exception:
            return (False, expected)
    if assert_type == "list_equals":
        if not isinstance(found, list):
            return (False, expected)
        return (found == expected, expected)
    raise ValueError(f"Unknown assert type: {assert_type}")


def validate_map(m: dict[str, object], profile: dict[str, object]) -> list[RuleResult]:
    rules = profile.get("rules", [])
    results: list[RuleResult] = []

    for r in rules:
        rule_id = r.get("id", "rule")
        key = r["key"]
        assert_type = r["assert"]
        expected = r.get("expected", None)
        level = r.get("level", "error")
        message = r.get("message", "")

        found = m.get(key, None)

        if "when" in r:
            cond = r["when"]
            c_key = cond.get("key")
            c_val = m.get(c_key)
            if "equals" in cond and c_val != cond["equals"]:
                results.append(RuleResult(rule_id, key, "SKIP", expected, found, message, level))
                continue

        ok, exp_norm = _match(assert_type, found, expected)
        status = "OK" if ok else ("FAIL" if level == "error" else "WARN")
        results.append(RuleResult(rule_id, key, status, exp_norm, found, message, level))

    return results


def describe_key(profile: dict[str, object], key: str) -> str:
    descs = profile.get("descriptions", {}) if isinstance(profile.get("descriptions"), dict) else {}
    return descs.get(key, key)


def format_report(results: list[RuleResult], profile: dict[str, object], show_ok: bool = False) -> str:
    okc = sum(1 for r in results if r.status == "OK")
    warnc = sum(1 for r in results if r.status == "WARN")
    failc = sum(1 for r in results if r.status == "FAIL")

    lines: list[str] = []
    for rr in results:
        if rr.status == "OK" and not show_ok:
            continue
        label = describe_key(profile, rr.key)
        lines.append(f"[{rr.status}] {label}")
        if rr.message:
            lines.append(f"  - {rr.message}")
        lines.append(f"  - EXPECTED: {rr.expected}")
        lines.append(f"  - FOUND:    {rr.found}")

    if failc == 0 and warnc == 0 and not show_ok:
        return "OK: vše odpovídá profilu."
    header = f"SUMMARY: OK={okc} WARN={warnc} FAIL={failc}"
    return header + ("\n" + "\n".join(lines) if lines else "")


def summarize_results(results: list[RuleResult]) -> tuple[str, int, int, int]:
    okc = sum(1 for r in results if r.status == "OK")
    warnc = sum(1 for r in results if r.status == "WARN")
    failc = sum(1 for r in results if r.status == "FAIL")
    if failc > 0:
        return ("FAIL", okc, warnc, failc)
    if warnc > 0:
        return ("WARN", okc, warnc, failc)
    return ("OK", okc, warnc, failc)


# -------------------------
# Built-in TIFF profile (NDK Master)
# -------------------------

TIFF_PROFILE_NDK_MASTER = {
    "name": "NDK TIFF Master (RGB, 8bit/kanál, >=300dpi, no compression, ICC yes)",
    "descriptions": {
        "tiff.Compression.n": "Komprese (Compression) = 1 (none)",
        "tiff.Photometric.n": "Fotometrie (Photometric) = 2 (RGB)",
        "tiff.SamplesPerPixel.n": "SamplesPerPixel = 3 (RGB)",
        "tiff.BitsPerSample.list": "BitsPerSample = [8,8,8]",
        "tiff.ResolutionUnit.n": "ResolutionUnit = 2 (inch)",
        "tiff.XResolution.dpi": "XResolution (dpi) >= 300",
        "tiff.YResolution.dpi": "YResolution (dpi) >= 300",
        "derived.icc_present": "ICCProfile tag (34675) přítomen",
        "derived.icc_typeid": "ICCProfile datatype = UNDEFINED (7)",
    },
    "rules": [
        {
            "id": "tiff_compression_none",
            "key": "tiff.Compression.n",
            "assert": "equals",
            "expected": 1,
            "level": "error",
            "message": "NDK Master: bez komprese (Compression=1).",
        },
        {
            "id": "tiff_photometric_rgb",
            "key": "tiff.Photometric.n",
            "assert": "equals",
            "expected": 2,
            "level": "error",
            "message": "NDK Master: RGB (Photometric=2).",
        },
        {
            "id": "tiff_samples_rgb",
            "key": "tiff.SamplesPerPixel.n",
            "assert": "equals",
            "expected": 3,
            "level": "error",
            "message": "NDK Master: SamplesPerPixel=3 (RGB).",
        },
        {
            "id": "tiff_bps_8_8_8",
            "key": "tiff.BitsPerSample.list",
            "assert": "list_equals",
            "expected": [8, 8, 8],
            "level": "error",
            "message": "NDK Master: 8 bitů na kanál (BitsPerSample 8,8,8).",
        },
        {
            "id": "tiff_resunit_inch",
            "key": "tiff.ResolutionUnit.n",
            "assert": "equals",
            "expected": 2,
            "level": "error",
            "message": "NDK Master: ResolutionUnit musí být inch (2) pro dpi interpretaci.",
        },
        {
            "id": "tiff_xdpi_min_300",
            "key": "tiff.XResolution.dpi",
            "assert": "min",
            "expected": 300,
            "level": "error",
            "message": "NDK Master: XResolution >= 300 dpi.",
        },
        {
            "id": "tiff_ydpi_min_300",
            "key": "tiff.YResolution.dpi",
            "assert": "min",
            "expected": 300,
            "level": "error",
            "message": "NDK Master: YResolution >= 300 dpi.",
        },
        {
            "id": "tiff_icc_present",
            "key": "derived.icc_present",
            "assert": "equals",
            "expected": True,
            "level": "error",
            "message": "NDK Master: ICC profil musí být přítomen (tag 34675).",
        },
        {
            "id": "tiff_icc_datatype",
            "key": "derived.icc_typeid",
            "assert": "equals",
            "expected": 7,
            "level": "error",
            "message": "NDK Master: ICCProfile musí mít datatype UNDEFINED (7).",
        },
    ],
}


# -------------------------
# Run + validate helpers
# -------------------------

class TiffDumpError(RuntimeError):
    pass


def run_tiffdump_wsl(
    tiff_path: str | pathlib.Path,
    wsl_cmd: str = "wsl",
    tiffdump_cmd: str = "tiffdump",
    extra_args: list[str] | None = None,
    timeout_sec: int = 30,
    convert_win_path_to_wsl: bool = True,
) -> str:
    p = pathlib.Path(tiff_path)
    if not p.exists():
        raise TiffDumpError(f"Input file not found: {p}")

    path_arg = str(p)
    if convert_win_path_to_wsl:
        path_arg = win_path_to_wsl(path_arg)

    cmd = [wsl_cmd, tiffdump_cmd]
    if extra_args:
        cmd += extra_args
    cmd += [path_arg]

    try:
        pr = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        raise TiffDumpError(f"tiffdump timeout after {timeout_sec}s\nCommand: {cmd}")

    out = (pr.stdout or "")
    err = (pr.stderr or "")
    if pr.returncode != 0:
        raise TiffDumpError(f"tiffdump failed (exit {pr.returncode})\nCommand: {cmd}\n{err or out}")

    return out + (("\n" + err) if err.strip() else "")


def validate_tiffdump_text(
    tiffdump_text: str,
    profile: dict[str, object] | None = None,
    show_ok: bool = False,
) -> tuple[str, str, dict[str, object], list[RuleResult]]:
    lines = tiffdump_text.splitlines()
    tags = parse_tiffdump_lines(lines)
    m = normalize_tiff_map(tags)
    prof = profile or TIFF_PROFILE_NDK_MASTER
    results = validate_map(m, prof)
    return (tiff_summary_text(tags), format_report(results, prof, show_ok=show_ok), m, results)


def load_profile_json(path: str | pathlib.Path) -> dict[str, object]:
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


# -------------------------
# Batch helpers
# -------------------------

DEFAULT_GLOBS = ["*.tif", "*.tiff"]


def iter_inputs(base: pathlib.Path, globs: List[str], recursive: bool) -> Iterable[pathlib.Path]:
    if base.is_file():
        yield base
        return
    if not base.is_dir():
        return

    iters: List[Iterable[pathlib.Path]] = []
    for g in globs:
        iters.append(base.rglob(g) if recursive else base.glob(g))

    seen: set[pathlib.Path] = set()
    for it in iters:
        for p in it:
            if p.is_file():
                rp = p.resolve()
                if rp in seen:
                    continue
                seen.add(rp)
                yield p


def file_banner(path: pathlib.Path, idx: int, total: int) -> str:
    return f"\n=== FILE {idx}/{total}: {path} ===\n"


# -------------------------
# CLI
# -------------------------

def main():
    import argparse

    ap = argparse.ArgumentParser(description="Run WSL tiffdump and validate TIFF against NDK profile (file or directory).")
    ap.add_argument("input", help="Path to TIFF file OR directory")
    ap.add_argument("--profile", help="Path to TIFF profile JSON (optional). If not provided, uses built-in NDK profile.")
    ap.add_argument("--show-ok", action="store_true", help="Show OK rule results too")

    ap.add_argument("--wsl", default="wsl", help="WSL launcher (default: wsl)")
    ap.add_argument("--tiffdump", default="tiffdump", help="tiffdump command inside WSL (default: tiffdump)")
    ap.add_argument("--tiffdump-args", default="", help="Extra args for tiffdump (simple split by spaces)")
    ap.add_argument("--timeout", type=int, default=30, help="Timeout seconds for each tiffdump run (default: 30)")
    ap.add_argument("--no-convert-path", action="store_true", help="Do not convert Windows path to /mnt/... for WSL")

    ap.add_argument("--glob", action="append", help="Glob(s) for directory mode. Can be repeated. Default: *.tif, *.tiff")
    ap.add_argument("--recursive", action="store_true", help="When input is a directory: search recursively.")

    args = ap.parse_args()
    in_path = pathlib.Path(args.input)

    profile = None
    if args.profile:
        profile = load_profile_json(args.profile)

    globs = args.glob[:] if args.glob else DEFAULT_GLOBS
    files = list(iter_inputs(in_path, globs=globs, recursive=args.recursive))
    if not files:
        print(f"ERROR: No input files found for: {in_path}")
        raise SystemExit(2)

    files.sort(key=lambda p: str(p).lower())

    total = len(files)
    ok_files = warn_files = fail_files = err_files = 0

    extra = split_args_simple(args.tiffdump_args)

    for i, f in enumerate(files, start=1):
        print(file_banner(f, i, total), end="")
        try:
            dumped = run_tiffdump_wsl(
                f,
                wsl_cmd=args.wsl,
                tiffdump_cmd=args.tiffdump,
                extra_args=extra,
                timeout_sec=int(args.timeout),
                convert_win_path_to_wsl=(not args.no_convert_path),
            )

            summary, report, _map, results = validate_tiffdump_text(
                dumped,
                profile=profile,
                show_ok=bool(args.show_ok),
            )

            print(summary, end="" if summary.endswith("\n") else "\n")
            print("TIFF REPORT")
            print(report)

            st, _, w, fl = summarize_results(results)
            if st == "OK":
                ok_files += 1
            elif st == "WARN":
                warn_files += 1
            else:
                fail_files += 1

        except Exception as e:
            err_files += 1
            print(f"ERROR: {e}")

    print(f"\nBATCH SUMMARY: files={total} OK={ok_files} WARN={warn_files} FAIL={fail_files} ERROR={err_files}")

    if fail_files > 0 or err_files > 0:
        raise SystemExit(2)
    if warn_files > 0:
        raise SystemExit(1)
    raise SystemExit(0)


if __name__ == "__main__":
    main()
