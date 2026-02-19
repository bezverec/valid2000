#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Valid2000 (v0.0.1)
Author: Jan Houserek
License: GPLv3
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union, Iterable
import re
import xml.etree.ElementTree as ET
import json
import pathlib
import subprocess
import shutil
import struct

APP_TITLE = "Valid2000 v0.0.1"
SCRIPT_VERSION = "2026-02-19-valid2000-v0.0.1"

# -------------------------
# Normalized model
# -------------------------

@dataclass
class Finding:
    key: str
    value: Any
    raw_path: str


@dataclass
class RuleResult:
    rule_id: str
    key: str
    status: str          # OK / FAIL / WARN / SKIP
    expected: Any
    found: Any
    message: str
    level: str           # info / warn / error


# -------------------------
# Helpers
# -------------------------

def _coerce_scalar(s: Optional[str]) -> Any:
    if s is None:
        return None
    s = s.strip()
    if s == "":
        return None
    sl = s.lower()
    if sl in ("true", "false"):
        return sl == "true"
    if re.fullmatch(r"[+-]?\d+", s):
        try:
            return int(s)
        except Exception:
            return s
    if re.fullmatch(r"[+-]?\d+\.\d+", s):
        try:
            return float(s)
        except Exception:
            return s
    return s


def strip_ns(tag: str) -> str:
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def findings_to_map(findings: List[Finding]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for f in findings:
        if f.key in out:
            if isinstance(out[f.key], list):
                out[f.key].append(f.value)
            else:
                out[f.key] = [out[f.key], f.value]
        else:
            out[f.key] = f.value
    return out


# -------------------------
# Jpylyzer XML parser (namespace-clean + presence flags)
# -------------------------

def parse_jpylyzer_xml_string(xml_text: str) -> List[Finding]:
    root = ET.fromstring(xml_text)
    findings: List[Finding] = []

    def _text(el: Optional[ET.Element]) -> Optional[str]:
        if el is None or el.text is None:
            return None
        t = el.text.strip()
        return t if t != "" else None

    def walk(el: ET.Element, path: str):
        # Presence marker (exists in XML, even if empty element)
        findings.append(Finding(key=f"{path}._present", value=True, raw_path=path))

        # attributes
        for k, v in el.attrib.items():
            k2 = strip_ns(k)
            findings.append(Finding(
                key=f"{path}.@{k2}",
                value=_coerce_scalar(v),
                raw_path=f"{path}[@{k2}]",
            ))

        children = list(el)
        if not children:
            val = _coerce_scalar(_text(el))
            findings.append(Finding(key=path, value=val, raw_path=path))
            return

        for ch in children:
            walk(ch, f"{path}.{strip_ns(ch.tag)}")

    walk(root, strip_ns(root.tag))
    return findings


def parse_jpylyzer_xml(xml_path: Union[str, pathlib.Path]) -> List[Finding]:
    xml_path = pathlib.Path(xml_path)
    xml_text = xml_path.read_text(encoding="utf-8", errors="replace")
    return parse_jpylyzer_xml_string(xml_text)


# -------------------------
# Run jpylyzer
# -------------------------

class JpylyzerError(RuntimeError):
    pass


def run_jpylyzer_xml(
    input_path: Union[str, pathlib.Path],
    jpylyzer_cmd: Optional[str] = None,
    timeout_sec: int = 60,
    stream_format: Optional[str] = None,   # jp2|jph|j2c|jhc
    mix: Optional[int] = None,             # 1|2
    nopretty: bool = False,
    nullxml: bool = False,
    recurse: bool = False,
    packetmarkers: bool = False,
    verbose: bool = False,
) -> str:
    input_path = pathlib.Path(input_path)
    if not input_path.exists():
        raise JpylyzerError(f"Input file not found: {input_path}")

    cmd = jpylyzer_cmd or "jpylyzer"
    if jpylyzer_cmd is None:
        resolved = shutil.which(cmd)
        if not resolved:
            raise JpylyzerError(
                "jpylyzer was not found in PATH. "
                "Install it or pass --jpylyzer-cmd (e.g. full path to jpylyzer.exe)."
            )
        cmd = resolved

    args: List[str] = [cmd]
    if stream_format:
        args += ["--format", stream_format]
    if mix is not None:
        args += ["--mix", str(mix)]
    if nopretty:
        args += ["--nopretty"]
    if nullxml:
        args += ["--nullxml"]
    if recurse:
        args += ["--recurse"]
    if packetmarkers:
        args += ["--packetmarkers"]
    if verbose:
        args += ["--verbose"]
    args += [str(input_path)]

    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        raise JpylyzerError(f"jpylyzer timed out after {timeout_sec}s: {input_path}")

    stdout = (p.stdout or "").strip()
    stderr = (p.stderr or "").strip()

    if p.returncode != 0:
        msg = stderr if stderr else stdout
        raise JpylyzerError(f"jpylyzer failed (exit {p.returncode})\nCommand: {args}\n{msg}")

    candidate = stdout if stdout.startswith("<") else (stderr if stderr.startswith("<") else "")
    if not candidate:
        s_out = stdout[:400].replace("\r", "\\r").replace("\n", "\\n")
        s_err = stderr[:400].replace("\r", "\\r").replace("\n", "\\n")
        raise JpylyzerError(
            "jpylyzer did not return XML.\n"
            f"Command: {args}\n"
            f"stdout(0..400)='{s_out}'\n"
            f"stderr(0..400)='{s_err}'"
        )
    return candidate


# -------------------------
# JP2 container / codestream scan (TLM marker FF55)
# -------------------------

class Jp2ParseError(RuntimeError):
    pass


def _read_u32_be(b: bytes, off: int) -> int:
    return struct.unpack_from(">I", b, off)[0]


def _box_type(b: bytes, off: int) -> str:
    return b[off:off+4].decode("ascii", errors="replace")


def jp2_list_boxes(data: bytes, max_boxes: int = 10_000) -> List[Tuple[int, int, str]]:
    out: List[Tuple[int, int, str]] = []
    off = 0
    n = len(data)
    count = 0
    while off + 8 <= n and count < max_boxes:
        lbox = _read_u32_be(data, off)
        tbox = _box_type(data, off + 4)
        header = 8
        if lbox == 1:
            if off + 16 > n:
                raise Jp2ParseError("Truncated XLBox")
            xlbox = struct.unpack_from(">Q", data, off + 8)[0]
            lbox = int(xlbox)
            header = 16
        elif lbox == 0:
            lbox = n - off

        if lbox < header or off + lbox > n:
            raise Jp2ParseError(f"Invalid box length for {tbox} at {off}: {lbox}")

        out.append((off, lbox, tbox))
        off += lbox
        count += 1
    return out


def jp2_find_jp2c_payload(data: bytes) -> Optional[Tuple[int, int]]:
    boxes = jp2_list_boxes(data)
    for off, lbox, tbox in boxes:
        if tbox == "jp2c":
            lbox0 = _read_u32_be(data, off)
            header = 8
            if lbox0 == 1:
                header = 16
            payload_off = off + header
            payload_len = lbox - header
            if payload_len < 0:
                return None
            return (payload_off, payload_len)
    return None


def scan_codestream_markers(codestream: bytes) -> Dict[str, Any]:
    first_sot = codestream.find(b"\xFF\x90")

    offsets: List[int] = []
    start = 0
    while True:
        i = codestream.find(b"\xFF\x55", start)
        if i < 0:
            break
        offsets.append(i)
        start = i + 2

    before_first_sot = False
    if offsets and first_sot >= 0:
        before_first_sot = any(o < first_sot for o in offsets)

    return {
        "tlm_marker_count": len(offsets),
        "tlm_marker_offsets": offsets,
        "first_sot_offset": first_sot if first_sot >= 0 else None,
        "tlm_before_first_sot": before_first_sot if first_sot >= 0 else None,
    }


def derive_tlm_from_file(path: pathlib.Path, max_read_mb: int = 256) -> Dict[str, Any]:
    size = path.stat().st_size
    if size > max_read_mb * 1024 * 1024:
        return {
            "jp2scan.skipped": True,
            "jp2scan.reason": f"file too large for full read ({size} bytes > {max_read_mb} MB)",
        }

    data = path.read_bytes()
    suf = path.suffix.lower()

    if suf in (".j2c", ".jhc", ".jph", ".j2k"):
        m = scan_codestream_markers(data)
        return {
            "jp2scan.skipped": False,
            "jp2scan.container": "raw",
            "jp2scan.codestream_offset_in_file": 0,
            **{f"jp2scan.{k}": v for k, v in m.items()},
        }

    jp2c = jp2_find_jp2c_payload(data)
    if not jp2c:
        return {
            "jp2scan.skipped": False,
            "jp2scan.container": "jp2",
            "jp2scan.jp2c_found": False,
        }

    payload_off, payload_len = jp2c
    codestream = data[payload_off:payload_off + payload_len]
    m = scan_codestream_markers(codestream)
    return {
        "jp2scan.skipped": False,
        "jp2scan.container": "jp2",
        "jp2scan.jp2c_found": True,
        "jp2scan.codestream_offset_in_file": payload_off,
        "jp2scan.codestream_length": payload_len,
        **{f"jp2scan.{k}": v for k, v in m.items()},
    }


def format_jp2_dump(fmap: Dict[str, Any]) -> str:
    if fmap.get("jp2scan.skipped") is True:
        return f"JP2SCAN: SKIPPED ({fmap.get('jp2scan.reason')})"

    cont = fmap.get("jp2scan.container")
    if cont == "raw":
        lines = ["JP2SCAN: raw codestream scan"]
    elif cont == "jp2":
        found = fmap.get("jp2scan.jp2c_found")
        if found is False:
            return "JP2SCAN: JP2 container, jp2c box NOT found"
        lines = ["JP2SCAN: JP2 container scan (jp2c payload)"]
        lines.append(f"  - jp2c payload offset: {fmap.get('jp2scan.codestream_offset_in_file')}")
        lines.append(f"  - jp2c payload length: {fmap.get('jp2scan.codestream_length')}")
    else:
        return "JP2SCAN: (no data)"

    lines.append(f"  - first SOT (FF90) offset in codestream: {fmap.get('jp2scan.first_sot_offset')}")
    lines.append(f"  - TLM (FF55) count: {fmap.get('jp2scan.tlm_marker_count')}")
    offs = fmap.get("jp2scan.tlm_marker_offsets")
    if isinstance(offs, list):
        show = offs[:20]
        lines.append(f"  - TLM offsets (first {len(show)}): {show}" + (" ..." if len(offs) > len(show) else ""))
    lines.append(f"  - TLM before first SOT: {fmap.get('jp2scan.tlm_before_first_sot')}")
    return "\n".join(lines)


# -------------------------
# Tile-parts heuristics
# -------------------------

def _safe_int_list(x: Any) -> Optional[List[int]]:
    if not isinstance(x, list):
        return None
    if not all(isinstance(i, int) for i in x):
        return None
    return x


def _infer_orgtparts_R(num_tiles: Optional[int], isot: Any, tpsot: Any, tnsot: Any) -> Tuple[bool, str]:
    if not (isinstance(num_tiles, int) and num_tiles > 0):
        return (False, "unknown")

    isot_l = _safe_int_list(isot)
    tpsot_l = _safe_int_list(tpsot)
    tnsot_l = _safe_int_list(tnsot)
    if isot_l is None or tpsot_l is None or tnsot_l is None:
        return (False, "unknown")
    if not (len(isot_l) == len(tpsot_l) == len(tnsot_l) and len(isot_l) > 0):
        return (False, "unknown")

    tnsot_set = set(tnsot_l)
    if len(tnsot_set) != 1:
        return (False, "unknown")
    parts_per_tile = next(iter(tnsot_set))
    if parts_per_tile < 1:
        return (False, "unknown")

    if len(isot_l) != num_tiles * parts_per_tile:
        return (False, "unknown")

    idx = 0
    for part_idx in range(parts_per_tile):
        block_isot: List[int] = []
        for _ in range(num_tiles):
            if idx >= len(isot_l):
                return (False, "unknown")
            if tpsot_l[idx] != part_idx:
                return (False, "unknown")
            block_isot.append(isot_l[idx])
            idx += 1
        if sorted(block_isot) != list(range(num_tiles)):
            return (False, "unknown")

    if idx != len(isot_l):
        return (False, "unknown")

    return (True, "R")


def _infer_orgtparts_T(num_tiles: Optional[int], isot: Any, tpsot: Any, tnsot: Any) -> Tuple[bool, str]:
    if not (isinstance(num_tiles, int) and num_tiles > 0):
        return (False, "unknown")

    isot_l = _safe_int_list(isot)
    tpsot_l = _safe_int_list(tpsot)
    tnsot_l = _safe_int_list(tnsot)
    if isot_l is None or tpsot_l is None or tnsot_l is None:
        return (False, "unknown")
    if not (len(isot_l) == len(tpsot_l) == len(tnsot_l) and len(isot_l) > 0):
        return (False, "unknown")

    tnsot_set = set(tnsot_l)
    if len(tnsot_set) != 1:
        return (False, "unknown")
    parts_per_tile = next(iter(tnsot_set))
    if parts_per_tile < 1:
        return (False, "unknown")

    if len(isot_l) != num_tiles * parts_per_tile:
        return (False, "unknown")

    idx = 0
    for tile in range(num_tiles):
        block_tps: List[int] = []
        block_isot: List[int] = []
        for _ in range(parts_per_tile):
            if idx >= len(isot_l):
                return (False, "unknown")
            block_isot.append(isot_l[idx])
            block_tps.append(tpsot_l[idx])
            idx += 1
        if not all(x == tile for x in block_isot):
            return (False, "unknown")
        if sorted(block_tps) != list(range(parts_per_tile)):
            return (False, "unknown")

    if idx != len(isot_l):
        return (False, "unknown")

    return (True, "T")


def _tileparts_cover_all_tiles(num_tiles: Optional[int], isot: Any) -> bool:
    if not (isinstance(num_tiles, int) and num_tiles > 0):
        return False
    isot_l = _safe_int_list(isot)
    if isot_l is None:
        return False
    return set(isot_l) == set(range(num_tiles))


def _tileparts_per_tile_tpsot_complete(num_tiles: Optional[int], isot: Any, tpsot: Any, tnsot: Any) -> bool:
    if not (isinstance(num_tiles, int) and num_tiles > 0):
        return False
    isot_l = _safe_int_list(isot)
    tpsot_l = _safe_int_list(tpsot)
    tnsot_l = _safe_int_list(tnsot)
    if isot_l is None or tpsot_l is None or tnsot_l is None:
        return False
    if not (len(isot_l) == len(tpsot_l) == len(tnsot_l) and len(isot_l) > 0):
        return False

    tnsot_set = set(tnsot_l)
    if len(tnsot_set) != 1:
        return False
    parts_per_tile = next(iter(tnsot_set))
    if parts_per_tile < 1:
        return False

    by_tile: Dict[int, set] = {t: set() for t in range(num_tiles)}
    for ti, pi in zip(isot_l, tpsot_l):
        if not isinstance(ti, int) or not isinstance(pi, int):
            return False
        if ti not in by_tile:
            return False
        by_tile[ti].add(pi)

    expect = set(range(parts_per_tile))
    return all(by_tile[t] == expect for t in range(num_tiles))


# -------------------------
# Derived keys (interpretation helpers)
# -------------------------

def add_derived(findings_map: Dict[str, Any], jp2_path: Optional[pathlib.Path] = None, scan_markers: bool = False) -> None:
    # ICC present
    meth = findings_map.get("jpylyzer.file.properties.jp2HeaderBox.colourSpecificationBox.meth")
    enum_cs = findings_map.get("jpylyzer.file.properties.jp2HeaderBox.colourSpecificationBox.enumCS")

    icc_present = False
    if isinstance(meth, str) and meth.lower() != "enumerated":
        icc_present = True
    if not icc_present:
        for k, v in findings_map.items():
            if k.startswith("jpylyzer.file.properties.jp2HeaderBox.colourSpecificationBox.") and "icc" in k.lower():
                if v is not None:
                    icc_present = True
                    break

    findings_map["derived.icc_present"] = icc_present
    findings_map["derived.colour_meth"] = meth
    findings_map["derived.enum_cs"] = enum_cs

    # Embedded metadata present (conservative heuristic)
    embedded = False
    for k, v in findings_map.items():
        kl = k.lower()
        if ("xmlbox" in kl or "uuidbox" in kl or "asoc" in kl or "xmp" in kl) and v is not None:
            embedded = True
            break
        if (kl.endswith("._present") and ("xmlbox" in kl or "uuidbox" in kl or "asoc" in kl or "xmp" in kl)):
            embedded = True
            break
    findings_map["derived.embedded_metadata_present"] = embedded

    # ROI present heuristic
    roi_present = False
    for k, v in findings_map.items():
        kl = k.lower()
        if ".rgn" in kl or "roi" in kl:
            if v is not None:
                roi_present = True
                break
            if kl.endswith("._present"):
                roi_present = True
                break
    findings_map["derived.roi_present"] = roi_present

    # Tile-parts interpretation
    num_tiles = findings_map.get("jpylyzer.file.properties.contiguousCodestreamBox.siz.numberOfTiles")
    isot = findings_map.get("jpylyzer.file.properties.contiguousCodestreamBox.tileParts.tilePart.sot.isot")
    tpsot = findings_map.get("jpylyzer.file.properties.contiguousCodestreamBox.tileParts.tilePart.sot.tpsot")
    tnsot = findings_map.get("jpylyzer.file.properties.contiguousCodestreamBox.tileParts.tilePart.sot.tnsot")
    pltc = findings_map.get("jpylyzer.file.properties.contiguousCodestreamBox.tileParts.tilePart.pltCount")
    pptc = findings_map.get("jpylyzer.file.properties.contiguousCodestreamBox.tileParts.tilePart.pptCount")

    tileparts_present = isinstance(isot, list) and len(isot) > 0
    findings_map["derived.tileparts_present"] = tileparts_present
    findings_map["derived.tileparts_count"] = len(isot) if isinstance(isot, list) else 0

    findings_map["derived.tileparts_cover_all_tiles"] = _tileparts_cover_all_tiles(num_tiles, isot)
    findings_map["derived.tileparts_per_tile_tpsot_complete"] = _tileparts_per_tile_tpsot_complete(num_tiles, isot, tpsot, tnsot)

    plt_all_zero = isinstance(pltc, list) and all(x == 0 for x in pltc) if isinstance(pltc, list) else True
    ppt_all_zero = isinstance(pptc, list) and all(x == 0 for x in pptc) if isinstance(pptc, list) else True
    findings_map["derived.tileparts_plt_all_zero"] = plt_all_zero
    findings_map["derived.tileparts_ppt_all_zero"] = ppt_all_zero

    ok_r, _ = _infer_orgtparts_R(num_tiles, isot, tpsot, tnsot)
    ok_t, _ = _infer_orgtparts_T(num_tiles, isot, tpsot, tnsot)

    findings_map["derived.tparts_r_pattern_ok"] = ok_r
    findings_map["derived.tparts_t_pattern_ok"] = ok_t

    if ok_r and not ok_t:
        org = "R"
    elif ok_t and not ok_r:
        org = "T"
    elif ok_r and ok_t:
        org = "R"
    else:
        org = "unknown"
    findings_map["derived.tparts_org_inferred"] = org

    # TLM from bytes (preferred), fallback to XML presence only for XML-only runs
    tlm_present: Optional[bool] = None

    if scan_markers and jp2_path is not None and jp2_path.exists() and jp2_path.suffix.lower() != ".xml":
        scan = derive_tlm_from_file(jp2_path)
        for k, v in scan.items():
            findings_map[k] = v

        cnt = findings_map.get("jp2scan.tlm_marker_count")
        if isinstance(cnt, int):
            tlm_present = cnt > 0
        else:
            tlm_present = None

        findings_map["derived.tlm_source"] = "jp2scan"
    else:
        tlm_present = bool(findings_map.get("jpylyzer.file.properties.contiguousCodestreamBox.tlm._present"))
        findings_map["derived.tlm_source"] = "xml_presence"

    findings_map["derived.tlm_present"] = tlm_present


# -------------------------
# Rule engine
# -------------------------

def _get_value(m: Dict[str, Any], key: str) -> Any:
    return m.get(key, None)


def _is_pow2(n: int) -> bool:
    return n > 0 and (n & (n - 1) == 0)


def _match(assert_type: str, found: Any, expected: Any) -> Tuple[bool, Any]:
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
    if assert_type == "range":
        if found is None or not isinstance(found, (int, float)):
            return (False, expected)
        mn = expected.get("min", None)
        mx = expected.get("max", None)
        ok = True
        if mn is not None:
            ok = ok and (found >= mn)
        if mx is not None:
            ok = ok and (found <= mx)
        return (ok, expected)
    if assert_type == "precinct_ndk_hint":
        if not isinstance(found, list) or not all(isinstance(x, int) for x in found):
            return (False, expected)
        levels = expected.get("levels")
        if isinstance(levels, int) and len(found) != levels + 1:
            return (False, expected)
        if not all(_is_pow2(x) and x <= 256 for x in found):
            return (False, expected)
        return (True, expected)
    raise ValueError(f"Unknown assert type: {assert_type}")


def validate(findings_map: Dict[str, Any], profile: Dict[str, Any]) -> List[RuleResult]:
    rules = profile.get("rules", [])
    results: List[RuleResult] = []

    for r in rules:
        rule_id = r.get("id", "rule")
        key = r["key"]
        assert_type = r["assert"]
        expected = r.get("expected", None)
        level = r.get("level", "error")
        message = r.get("message", "")

        found = _get_value(findings_map, key)

        if "when" in r:
            cond = r["when"]
            c_key = cond.get("key")
            c_val = findings_map.get(c_key)
            if "equals" in cond and c_val != cond["equals"]:
                results.append(RuleResult(rule_id, key, "SKIP", expected, found, message, level))
                continue

        ok, exp_norm = _match(assert_type, found, expected)

        if ok:
            status = "OK"
        else:
            status = "FAIL" if level == "error" else "WARN"

        results.append(RuleResult(rule_id, key, status, exp_norm, found, message, level))

    return results


# -------------------------
# Reporting (with summary)
# -------------------------

def describe_key(profile: Dict[str, Any], key: str) -> str:
    desc = profile.get("descriptions", {}).get(key)
    return desc if desc else key


def format_report(results: List[RuleResult], profile: Dict[str, Any], show_ok: bool = False) -> str:
    okc = sum(1 for r in results if r.status == "OK")
    warnc = sum(1 for r in results if r.status == "WARN")
    failc = sum(1 for r in results if r.status == "FAIL")

    lines: List[str] = []
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


def summarize_results(results: List[RuleResult]) -> Tuple[str, int, int, int]:
    okc = sum(1 for r in results if r.status == "OK")
    warnc = sum(1 for r in results if r.status == "WARN")
    failc = sum(1 for r in results if r.status == "FAIL")
    if failc > 0:
        return ("FAIL", okc, warnc, failc)
    if warnc > 0:
        return ("WARN", okc, warnc, failc)
    return ("OK", okc, warnc, failc)


# -------------------------
# NDK profile (JP2 Master/Archival)
# -------------------------

NDK_PROFILE_NDK_MASTER = {
    "name": "NDK Master/Archival JP2 (core rules)",
    "descriptions": {
        "jpylyzer.file.properties.contiguousCodestreamBox.cod.transformation": "Transformace (COD)",
        "jpylyzer.file.properties.contiguousCodestreamBox.qcd.qStyle": "Kvantizace (QCD)",
        "jpylyzer.file.properties.contiguousCodestreamBox.cod.layers": "Počet vrstev kvality (COD)",
        "jpylyzer.file.properties.contiguousCodestreamBox.cod.levels": "Počet dekompozičních úrovní (COD)",
        "jpylyzer.file.properties.contiguousCodestreamBox.cod.order": "Progression order (COD)",
        "jpylyzer.file.properties.contiguousCodestreamBox.cod.codeBlockWidth": "Velikost bloků – šířka (COD)",
        "jpylyzer.file.properties.contiguousCodestreamBox.cod.codeBlockHeight": "Velikost bloků – výška (COD)",
        "jpylyzer.file.properties.contiguousCodestreamBox.cod.codingBypass": "Bypass (COD)",
        "jpylyzer.file.properties.contiguousCodestreamBox.cod.sop": "SOP (COD)",
        "jpylyzer.file.properties.contiguousCodestreamBox.cod.eph": "EPH (COD)",

        "jpylyzer.file.properties.contiguousCodestreamBox.siz.xTsiz": "Dlaždice – tile width (SIZ)",
        "jpylyzer.file.properties.contiguousCodestreamBox.siz.yTsiz": "Dlaždice – tile height (SIZ)",

        "jpylyzer.file.properties.contiguousCodestreamBox.cod.precinctSizeX": "Precinct size X (COD)",
        "jpylyzer.file.properties.contiguousCodestreamBox.cod.precinctSizeY": "Precinct size Y (COD)",

        "derived.tlm_present": "TLM (Tile Length Markers) přítomno",
        "derived.tlm_source": "TLM detekce – zdroj",
        "jp2scan.tlm_marker_count": "TLM marker count (FF55) – jp2scan",
        "jp2scan.tlm_before_first_sot": "TLM před prvním SOT – jp2scan",

        "derived.icc_present": "ICC profil přítomen",
        "derived.roi_present": "ROI (Regions of Interest) přítomno",
        "derived.embedded_metadata_present": "Vložená metadata přítomna",

        "derived.tileparts_present": "Tile-parts přítomné (SOT existují)",
        "derived.tileparts_count": "Počet nalezených tile-parts (SOT)",
        "derived.tileparts_cover_all_tiles": "Tile-parts pokrývají všechny tiles (set isot == 0..N-1)",
        "derived.tileparts_per_tile_tpsot_complete": "Pro každý tile je tpsot kompletní 0..tnsot-1",

        "derived.tparts_org_inferred": "Tile-part organization (inferováno)",

        "derived.tileparts_plt_all_zero": "PLT count = 0 (všechny tile-parts)",
        "derived.tileparts_ppt_all_zero": "PPT count = 0 (všechny tile-parts)",

        "jpylyzer.file.properties.compressionRatio": "Kompresní poměr (informativní)",
        "jpylyzer.file.properties.contiguousCodestreamBox.siz.numberOfTiles": "Počet tiles (SIZ)",
    },

    "rules": [
        {
            "id": "lossless_transformation",
            "key": "jpylyzer.file.properties.contiguousCodestreamBox.cod.transformation",
            "assert": "equals",
            "expected": "5-3 reversible",
            "level": "error",
            "message": "NDK Master: vyžaduje 5-3 reversible filter (bezeztrátově).",
        },
        {
            "id": "lossless_no_quant",
            "key": "jpylyzer.file.properties.contiguousCodestreamBox.qcd.qStyle",
            "assert": "equals",
            "expected": "no quantization",
            "level": "error",
            "message": "NDK Master: bez kvantizace (no quantization).",
        },
        {
            "id": "progression_order_rpcl",
            "key": "jpylyzer.file.properties.contiguousCodestreamBox.cod.order",
            "assert": "equals",
            "expected": "RPCL",
            "level": "error",
            "message": "NDK Master: progression order RPCL.",
        },
        {
            "id": "decomposition_levels",
            "key": "jpylyzer.file.properties.contiguousCodestreamBox.cod.levels",
            "assert": "in",
            "expected": [5, 6],
            "level": "error",
            "message": "NDK Master: 5 nebo 6 dekompozičních úrovní.",
        },
        {
            "id": "quality_layers_1",
            "key": "jpylyzer.file.properties.contiguousCodestreamBox.cod.layers",
            "assert": "equals",
            "expected": 1,
            "level": "error",
            "message": "NDK Master: počet vrstev kvality = 1.",
        },
        {
            "id": "codeblock_w",
            "key": "jpylyzer.file.properties.contiguousCodestreamBox.cod.codeBlockWidth",
            "assert": "equals",
            "expected": 64,
            "level": "error",
            "message": "NDK Master: code-block size 64x64.",
        },
        {
            "id": "codeblock_h",
            "key": "jpylyzer.file.properties.contiguousCodestreamBox.cod.codeBlockHeight",
            "assert": "equals",
            "expected": 64,
            "level": "error",
            "message": "NDK Master: code-block size 64x64.",
        },
        {
            "id": "bypass_yes",
            "key": "jpylyzer.file.properties.contiguousCodestreamBox.cod.codingBypass",
            "assert": "equals",
            "expected": "yes",
            "level": "error",
            "message": "NDK Master: Bypass (codingBypass) = yes.",
        },
        {
            "id": "sop_yes",
            "key": "jpylyzer.file.properties.contiguousCodestreamBox.cod.sop",
            "assert": "equals",
            "expected": "yes",
            "level": "error",
            "message": "NDK Master: SOP = yes (Cuse_sop=yes).",
        },
        {
            "id": "eph_yes",
            "key": "jpylyzer.file.properties.contiguousCodestreamBox.cod.eph",
            "assert": "equals",
            "expected": "yes",
            "level": "error",
            "message": "NDK Master: EPH = yes (Cuse_eph=yes).",
        },

        {
            "id": "tiling_x_4096",
            "key": "jpylyzer.file.properties.contiguousCodestreamBox.siz.xTsiz",
            "assert": "equals",
            "expected": 4096,
            "level": "error",
            "message": "NDK Master: tiling 4096x4096 (tile width).",
        },
        {
            "id": "tiling_y_4096",
            "key": "jpylyzer.file.properties.contiguousCodestreamBox.siz.yTsiz",
            "assert": "equals",
            "expected": 4096,
            "level": "error",
            "message": "NDK Master: tiling 4096x4096 (tile height).",
        },

        {
            "id": "precinct_sanity_x",
            "key": "jpylyzer.file.properties.contiguousCodestreamBox.cod.precinctSizeX",
            "assert": "precinct_ndk_hint",
            "expected": {"levels": 5, "max": 256, "pow2": True},
            "level": "warn",
            "message": "Sanity check precinctů (pow2, max 256, délka ~ levels+1).",
        },
        {
            "id": "precinct_sanity_y",
            "key": "jpylyzer.file.properties.contiguousCodestreamBox.cod.precinctSizeY",
            "assert": "precinct_ndk_hint",
            "expected": {"levels": 5, "max": 256, "pow2": True},
            "level": "warn",
            "message": "Sanity check precinctů (pow2, max 256, délka ~ levels+1).",
        },

        {
            "id": "tlm_present",
            "key": "derived.tlm_present",
            "assert": "equals",
            "expected": True,
            "level": "error",
            "message": "NDK Master: TLM (Tile Length Markers) = Ano. (Primárně z jp2scan: hledáme FF55 v codestreamu.)",
        },
        {
            "id": "tlm_before_first_sot_warn",
            "key": "jp2scan.tlm_before_first_sot",
            "assert": "in",
            "expected": [True, None],
            "level": "warn",
            "message": "Heuristika: TLM by měl být v main headeru (před prvním SOT).",
        },

        {
            "id": "icc_present",
            "key": "derived.icc_present",
            "assert": "equals",
            "expected": True,
            "level": "error",
            "message": "NDK Master: ICC profil = Ano.",
        },

        {
            "id": "roi_no",
            "key": "derived.roi_present",
            "assert": "equals",
            "expected": False,
            "level": "warn",
            "message": "NDK Master: Regions of Interest = Ne.",
        },
        {
            "id": "embedded_metadata_no",
            "key": "derived.embedded_metadata_present",
            "assert": "equals",
            "expected": False,
            "level": "warn",
            "message": "NDK Master: vložená metadata = Ne.",
        },

        {
            "id": "tileparts_present",
            "key": "derived.tileparts_present",
            "assert": "equals",
            "expected": True,
            "level": "error",
            "message": "NDK: očekáváme tile-parts/SOT informace (jpylyzer tileParts).",
        },
        {
            "id": "tileparts_cover_all_tiles",
            "key": "derived.tileparts_cover_all_tiles",
            "assert": "equals",
            "expected": True,
            "level": "error",
            "message": "Tile-parts musí pokrýt všechny tiles: množina isot == 0..N-1.",
        },
        {
            "id": "tileparts_per_tile_tpsot_complete",
            "key": "derived.tileparts_per_tile_tpsot_complete",
            "assert": "equals",
            "expected": True,
            "level": "error",
            "message": "Pro každý tile musí být přítomné všechny tile-parts: tpsot = 0..tnsot-1.",
        },

        # ORGtparts inference: WARN only (not strict)
        {
            "id": "tileparts_org_warn_if_T",
            "key": "derived.tparts_org_inferred",
            "assert": "in",
            "expected": ["R", "unknown"],
            "level": "warn",
            "message": "ORGtparts=R není v profilu striktně, ale pokud inferujeme per-tile (T), dáme WARN.",
        },

        {
            "id": "plt_all_zero",
            "key": "derived.tileparts_plt_all_zero",
            "assert": "equals",
            "expected": True,
            "level": "warn",
            "message": "PLT count = 0 (pokud používáte PLT, zvažte zda je to žádoucí).",
        },
        {
            "id": "ppt_all_zero",
            "key": "derived.tileparts_ppt_all_zero",
            "assert": "equals",
            "expected": True,
            "level": "warn",
            "message": "PPT count = 0 (pokud používáte PPT, zvažte zda je to žádoucí).",
        },

        {
            "id": "compression_ratio_info",
            "key": "jpylyzer.file.properties.compressionRatio",
            "assert": "present",
            "expected": None,
            "level": "warn",
            "message": "Kompresní poměr je informativní.",
        },
    ],
}


# -------------------------
# Batch helpers
# -------------------------

DEFAULT_GLOBS = ["*.jp2", "*.j2k", "*.j2c", "*.jph", "*.jhc", "*.xml"]


def iter_inputs(base: pathlib.Path, globs: List[str], recursive: bool) -> Iterable[pathlib.Path]:
    if base.is_file():
        yield base
        return

    if not base.is_dir():
        return

    iters: List[Iterable[pathlib.Path]] = []
    for g in globs:
        if recursive:
            iters.append(base.rglob(g))
        else:
            iters.append(base.glob(g))

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
# Single-file run
# -------------------------

def run_one(
    in_path: pathlib.Path,
    profile: Dict[str, Any],
    *,
    dump_map: bool,
    show_ok: bool,
    jpylyzer_cmd: Optional[str],
    timeout: int,
    stream_format: Optional[str],
    mix: Optional[int],
    nopretty: bool,
    nullxml: bool,
    recurse_jpylyzer: bool,
    packetmarkers: bool,
    verbose: bool,
    scan_markers: bool,
    dump_jp2scan: bool,
) -> Tuple[str, str]:
    """
    Returns: (overall_status: OK/WARN/FAIL/ERROR, output_text)
    """
    try:
        if in_path.suffix.lower() == ".xml":
            findings = parse_jpylyzer_xml(in_path)
            fmap = findings_to_map(findings)
        else:
            xml_text = run_jpylyzer_xml(
                in_path,
                jpylyzer_cmd=jpylyzer_cmd,
                timeout_sec=timeout,
                stream_format=stream_format,
                mix=mix,
                nopretty=nopretty,
                nullxml=nullxml,
                recurse=recurse_jpylyzer,
                packetmarkers=packetmarkers,
                verbose=verbose,
            )
            findings = parse_jpylyzer_xml_string(xml_text)
            fmap = findings_to_map(findings)

        add_derived(fmap, jp2_path=in_path, scan_markers=scan_markers)

        parts: List[str] = []

        if dump_jp2scan:
            parts.append(format_jp2_dump(fmap) + "\n")

        if dump_map:
            parts.append(json.dumps(fmap, ensure_ascii=False, indent=2) + "\n")

        results = validate(fmap, profile)
        rep = format_report(results, profile, show_ok=show_ok)
        parts.append(rep + "\n")

        st, _, _, _ = summarize_results(results)
        return (st, "".join(parts))

    except (JpylyzerError, ET.ParseError, Jp2ParseError, OSError) as e:
        return ("ERROR", f"ERROR: {e}\n")


# -------------------------
# Main
# -------------------------

def main():
    import argparse

    ap = argparse.ArgumentParser(description="Interpret jpylyzer output and validate against NDK JP2 profile.")
    ap.add_argument("input", help="Path to JP2/XML file OR directory")
    ap.add_argument("--profile", help="Path to profile JSON (optional). If not provided, uses built-in NDK profile.")
    ap.add_argument("--dump-map", action="store_true", help="Dump flattened finding map as JSON")
    ap.add_argument("--show-ok", action="store_true", help="Show OK rule results too")

    ap.add_argument("--jpylyzer-cmd", help="jpylyzer executable path/name (optional)")
    ap.add_argument("--timeout", type=int, default=60, help="jpylyzer timeout in seconds (default: 60)")

    ap.add_argument("--jp2-format", dest="stream_format",
                    choices=["jp2", "jph", "j2c", "jhc"],
                    help="jpylyzer --format: jp2|jph|j2c|jhc")
    ap.add_argument("--mix", type=int, choices=[1, 2], help="Add NISO MIX output (1 or 2)")
    ap.add_argument("--nopretty", action="store_true", help="Pass --nopretty to jpylyzer")
    ap.add_argument("--nullxml", action="store_true", help="Pass --nullxml to jpylyzer")
    ap.add_argument("--recurse", action="store_true", help="Pass --recurse to jpylyzer (NOTE: different from directory recursion)")
    ap.add_argument("--packetmarkers", action="store_true", help="Pass --packetmarkers to jpylyzer")
    ap.add_argument("--verbose", action="store_true", help="Pass --verbose to jpylyzer")

    ap.add_argument("--scan-markers", action="store_true",
                    help="Scan JP2/j2c bytes and look for marker FF55 (TLM) in codestream (jp2c payload).")
    ap.add_argument("--dump-jp2scan", action="store_true",
                    help="Print a short JP2SCAN dump (marker counts/offsets).")

    # Directory/batch options
    ap.add_argument("--glob", action="append", help="Glob(s) for directory mode. Can be repeated. Default: JP2/XML extensions.")
    ap.add_argument("--recursive", action="store_true", help="When input is a directory: search recursively.")

    args = ap.parse_args()
    in_path = pathlib.Path(args.input)

    profile = NDK_PROFILE_NDK_MASTER
    if args.profile:
        profile = json.loads(pathlib.Path(args.profile).read_text(encoding="utf-8"))

    globs = args.glob[:] if args.glob else DEFAULT_GLOBS

    files = list(iter_inputs(in_path, globs=globs, recursive=args.recursive))
    if not files:
        print(f"ERROR: No input files found for: {in_path}")
        raise SystemExit(2)

    # For deterministic output
    files.sort(key=lambda p: str(p).lower())

    # Batch counters
    total = len(files)
    ok_files = warn_files = fail_files = err_files = 0

    for i, f in enumerate(files, start=1):
        print(file_banner(f, i, total), end="")

        st, out = run_one(
            f,
            profile,
            dump_map=args.dump_map,
            show_ok=args.show_ok,
            jpylyzer_cmd=args.jpylyzer_cmd,
            timeout=int(args.timeout),
            stream_format=args.stream_format,
            mix=args.mix,
            nopretty=args.nopretty,
            nullxml=args.nullxml,
            recurse_jpylyzer=args.recurse,
            packetmarkers=args.packetmarkers,
            verbose=args.verbose,
            scan_markers=args.scan_markers,
            dump_jp2scan=args.dump_jp2scan,
        )
        print(out, end="")

        if st == "OK":
            ok_files += 1
        elif st == "WARN":
            warn_files += 1
        elif st == "FAIL":
            fail_files += 1
        else:
            err_files += 1

    print(f"\nBATCH SUMMARY: files={total} OK={ok_files} WARN={warn_files} FAIL={fail_files} ERROR={err_files}")

    # exit code: 0 ok, 1 warn, 2 fail/error
    if fail_files > 0 or err_files > 0:
        raise SystemExit(2)
    if warn_files > 0:
        raise SystemExit(1)
    raise SystemExit(0)


if __name__ == "__main__":
    main()
