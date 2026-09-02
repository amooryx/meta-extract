#!/usr/bin/env python3
"""
Meta Extract — Document Metadata Extractor for OSINT
Extracts author, software, GPS, timestamps from PDF, DOCX, XLSX, PPTX, images.
Author: Omar Khalid (amooryx) | github.com/amooryx/meta-extract
AUTHORIZED USE ONLY.
"""

import argparse
import json
import os
import struct
import sys
import zipfile
from datetime import datetime
from pathlib import Path

# ─── PDF metadata parser (no external deps) ───────────────────────────────────
def read_pdf_meta(path: str) -> dict:
    meta = {"file": path, "type": "PDF"}
    try:
        with open(path, "rb") as f:
            data = f.read(65536)  # first 64KB usually has metadata
        text = data.decode(errors="ignore")
        for key, pattern in [
            ("Author",   "/Author"),
            ("Creator",  "/Creator"),
            ("Producer", "/Producer"),
            ("Subject",  "/Subject"),
            ("Title",    "/Title"),
        ]:
            idx = text.find(pattern)
            if idx != -1:
                chunk = text[idx+len(pattern):idx+len(pattern)+200]
                # try to extract string between parens or angle brackets
                if chunk.startswith("("):
                    end = chunk.find(")")
                    meta[key] = chunk[1:end] if end != -1 else chunk[1:50]
                elif "(" in chunk[:10]:
                    s = chunk.index("(")
                    e = chunk.find(")", s)
                    meta[key] = chunk[s+1:e] if e != -1 else chunk[s+1:s+50]
    except Exception as e:
        meta["error"] = str(e)
    return meta

# ─── Office Open XML (DOCX, XLSX, PPTX) ──────────────────────────────────────
def read_ooxml_meta(path: str) -> dict:
    meta = {"file": path, "type": Path(path).suffix.upper().strip(".")}
    try:
        with zipfile.ZipFile(path) as z:
            if "docProps/core.xml" in z.namelist():
                xml = z.read("docProps/core.xml").decode(errors="ignore")
                tags = {
                    "creator":          "dc:creator",
                    "lastModifiedBy":   "cp:lastModifiedBy",
                    "created":          "dcterms:created",
                    "modified":         "dcterms:modified",
                    "revision":         "cp:revision",
                }
                for key, tag in tags.items():
                    start = xml.find(f"<{tag}")
                    if start != -1:
                        end = xml.find(">", start) + 1
                        close = xml.find(f"</{tag}>", end)
                        meta[key] = xml[end:close] if close != -1 else ""
            if "docProps/app.xml" in z.namelist():
                xml = z.read("docProps/app.xml").decode(errors="ignore")
                for tag in ["Application", "AppVersion", "Company"]:
                    start = xml.find(f"<{tag}>")
                    if start != -1:
                        end = start + len(tag) + 2
                        close = xml.find(f"</{tag}>", end)
                        meta[tag.lower()] = xml[end:close] if close != -1 else ""
    except Exception as e:
        meta["error"] = str(e)
    return meta

# ─── JPEG EXIF (GPS, camera, date) ───────────────────────────────────────────
EXIF_TAGS = {
    0x010F: "Make",      0x0110: "Model",     0x0112: "Orientation",
    0x011A: "XRes",      0x011B: "YRes",      0x0132: "DateTime",
    0x013B: "Artist",    0x8298: "Copyright", 0x9003: "DateTimeOriginal",
    0x9286: "UserComment", 0x0002: "GPSLat",  0x0004: "GPSLon",
    0x0006: "GPSAlt",
}

def read_jpeg_exif(path: str) -> dict:
    meta = {"file": path, "type": "JPEG"}
    try:
        with open(path, "rb") as f:
            data = f.read()
        # Find EXIF marker (FF E1)
        idx = data.find(b"\xFF\xE1")
        if idx == -1:
            return meta
        exif_start = idx + 10  # skip APP1 header + "Exif\x00\x00"
        # Determine byte order
        if data[exif_start:exif_start+2] == b"II":
            bo = "<"
        else:
            bo = ">"
        ifd_offset = struct.unpack_from(f"{bo}I", data, exif_start + 4)[0]
        ifd_pos    = exif_start + ifd_offset
        num_entries = struct.unpack_from(f"{bo}H", data, ifd_pos)[0]
        for i in range(min(num_entries, 100)):
            entry_pos = ifd_pos + 2 + i * 12
            tag_id    = struct.unpack_from(f"{bo}H", data, entry_pos)[0]
            dtype     = struct.unpack_from(f"{bo}H", data, entry_pos + 2)[0]
            count     = struct.unpack_from(f"{bo}I", data, entry_pos + 4)[0]
            val_off   = entry_pos + 8
            if dtype == 2 and count <= 4:  # ASCII short
                val = data[val_off:val_off+count].decode(errors="ignore").strip("\x00")
            elif dtype == 2:  # ASCII long
                offset = struct.unpack_from(f"{bo}I", data, val_off)[0]
                val = data[exif_start+offset:exif_start+offset+count].decode(errors="ignore").strip("\x00")
            else:
                val = str(struct.unpack_from(f"{bo}I", data, val_off)[0])
            if tag_id in EXIF_TAGS:
                meta[EXIF_TAGS[tag_id]] = val
    except Exception as e:
        meta["error"] = str(e)
    return meta

# ─── Dispatcher ───────────────────────────────────────────────────────────────
def extract(path: str) -> dict:
    ext = Path(path).suffix.lower()
    if ext == ".pdf":
        return read_pdf_meta(path)
    elif ext in (".docx", ".xlsx", ".pptx", ".odt", ".ods"):
        return read_ooxml_meta(path)
    elif ext in (".jpg", ".jpeg"):
        return read_jpeg_exif(path)
    else:
        return {"file": path, "type": ext, "note": "Unsupported type — try .pdf/.docx/.jpg"}

# ─── Entry ────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Meta Extract — Document Metadata Extractor (Authorized use only)",
    )
    parser.add_argument("files", nargs="+", help="Files to extract metadata from")
    parser.add_argument("--out",    help="Output JSON file")
    parser.add_argument("--format", choices=["json", "text"], default="text")
    args = parser.parse_args()

    results = [extract(f) for f in args.files]

    if args.format == "json" or args.out:
        text = json.dumps(results, indent=2)
        if args.out:
            with open(args.out, "w") as f:
                f.write(text)
            print(f"[*] Results → {args.out}")
        else:
            print(text)
    else:
        for r in results:
            print(f"\n{'='*60}")
            for k, v in r.items():
                if v:
                    print(f"  {k:20s}: {v}")

if __name__ == "__main__":
    main()
