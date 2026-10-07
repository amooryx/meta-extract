#!/usr/bin/env python3
"""Extract common metadata from PDF, OpenDocument, OOXML, and JPEG files."""

import argparse
import io
import json
import os
import re
import struct
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path
from xml.etree import ElementTree

MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_XML_BYTES = 1024 * 1024
MAX_ZIP_ENTRIES = 10_000
MAX_METADATA_VALUE_BYTES = 64 * 1024

PDF_KEYS = ("Author", "Creator", "Producer", "Subject", "Title")
EXIF_TAGS = {
    0x010F: "Make",
    0x0110: "Model",
    0x0112: "Orientation",
    0x011A: "XRes",
    0x011B: "YRes",
    0x0132: "DateTime",
    0x013B: "Artist",
    0x8298: "Copyright",
    0x9003: "DateTimeOriginal",
    0x9286: "UserComment",
}
TIFF_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}
OOXML_MAIN_PARTS = {
    ".docx": "word/document.xml",
    ".xlsx": "xl/workbook.xml",
    ".pptx": "ppt/presentation.xml",
}
ODF_MIMETYPES = {
    ".odt": b"application/vnd.oasis.opendocument.text",
    ".ods": b"application/vnd.oasis.opendocument.spreadsheet",
}


def _read_file(path: str) -> bytes:
    file_path = Path(path)
    if not file_path.is_file():
        raise ValueError("not a regular file")
    if file_path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError(f"file exceeds the {MAX_INPUT_BYTES // (1024 * 1024)} MiB input limit")
    with file_path.open("rb") as source:
        data = source.read(MAX_INPUT_BYTES + 1)
    if len(data) > MAX_INPUT_BYTES:
        raise ValueError(f"file exceeds the {MAX_INPUT_BYTES // (1024 * 1024)} MiB input limit")
    return data


def _decode_pdf_string(raw: bytes) -> str:
    if raw.startswith((b"\xfe\xff", b"\xff\xfe")):
        return raw.decode("utf-16", errors="replace").rstrip("\x00")
    return raw.decode("latin-1", errors="replace").rstrip("\x00")


def _pdf_value(data: bytes, start: int):
    while start < len(data) and data[start] in b"\x00\t\n\x0c\r ":
        start += 1
    if start >= len(data):
        return None

    if data[start:start + 1] == b"(":
        result = bytearray()
        depth = 1
        pos = start + 1
        while pos < len(data) and pos - start <= MAX_METADATA_VALUE_BYTES:
            char = data[pos]
            pos += 1
            if char == 0x5C:
                if pos >= len(data):
                    break
                escaped = data[pos]
                pos += 1
                replacements = {
                    ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t",
                    ord("b"): b"\b", ord("f"): b"\f",
                    ord("("): b"(", ord(")"): b")", ord("\\"): b"\\",
                }
                if escaped in replacements:
                    result.extend(replacements[escaped])
                elif escaped in b"01234567":
                    octal = bytes((escaped,))
                    for _ in range(2):
                        if pos < len(data) and data[pos] in b"01234567":
                            octal += bytes((data[pos],))
                            pos += 1
                        else:
                            break
                    result.append(int(octal, 8) & 0xFF)
                elif escaped == 0x0D:
                    if pos < len(data) and data[pos] == 0x0A:
                        pos += 1
                elif escaped != 0x0A:
                    result.append(escaped)
            elif char == 0x28:
                depth += 1
                result.append(char)
            elif char == 0x29:
                depth -= 1
                if depth == 0:
                    return _decode_pdf_string(bytes(result))
                result.append(char)
            else:
                result.append(char)
        return None

    if data[start:start + 1] == b"<" and data[start:start + 2] != b"<<":
        end = data.find(b">", start + 1, start + MAX_METADATA_VALUE_BYTES + 2)
        if end == -1:
            return None
        encoded = re.sub(rb"\s+", b"", data[start + 1:end])
        if len(encoded) % 2:
            encoded += b"0"
        try:
            return _decode_pdf_string(bytes.fromhex(encoded.decode("ascii")))
        except (UnicodeDecodeError, ValueError):
            return None
    return None


def _parse_pdf(path: str, data: bytes) -> dict:
    if b"%PDF-" not in data[:1024]:
        raise ValueError("file does not contain a PDF header in its first 1024 bytes")
    result = {"file": path, "type": "PDF"}
    pattern = re.compile(rb"/(" + b"|".join(key.encode("ascii") for key in PDF_KEYS) + rb")\b")
    for match in pattern.finditer(data):
        key = match.group(1).decode("ascii")
        if key not in result:
            value = _pdf_value(data, match.end())
            if value is not None:
                result[key] = value
    return result


def _safe_xml(data: bytes):
    if len(data) > MAX_XML_BYTES:
        raise ValueError("metadata XML exceeds the 1 MiB limit")
    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", data, re.IGNORECASE):
        raise ValueError("DTD and entity declarations are not accepted in metadata XML")
    return ElementTree.fromstring(data)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1]


def _metadata_text(element) -> str:
    value = (element.text or "").strip()
    if len(value.encode("utf-8")) > MAX_METADATA_VALUE_BYTES:
        raise ValueError("metadata value exceeds the 64 KiB limit")
    return value


def _read_zip_member(archive: zipfile.ZipFile, names, member: str):
    matches = [name for name in names if name == member]
    if len(matches) > 1:
        raise ValueError(f"archive contains duplicate {member} entries")
    if not matches:
        return None
    info = archive.getinfo(member)
    if info.file_size > MAX_XML_BYTES:
        raise ValueError(f"{member} exceeds the 1 MiB metadata limit")
    with archive.open(info) as source:
        data = source.read(MAX_XML_BYTES + 1)
    if len(data) > MAX_XML_BYTES:
        raise ValueError(f"{member} exceeds the 1 MiB metadata limit")
    return data


def _parse_ooxml(path: str, data: bytes, extension: str) -> dict:
    if not data.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        raise ValueError("file is not a ZIP-based office document")
    result = {"file": path, "type": extension[1:].upper()}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            if len(names) > MAX_ZIP_ENTRIES:
                raise ValueError(f"archive exceeds the {MAX_ZIP_ENTRIES} entry limit")
            if extension in OOXML_MAIN_PARTS:
                main_part = OOXML_MAIN_PARTS[extension]
                if main_part not in names:
                    raise ValueError(f"archive is missing its {main_part} document part")
                core = _read_zip_member(archive, names, "docProps/core.xml")
                if core is not None:
                    root = _safe_xml(core)
                    values = {
                        "creator": "creator",
                        "lastModifiedBy": "lastModifiedBy",
                        "created": "created",
                        "modified": "modified",
                        "revision": "revision",
                    }
                    for element in root.iter():
                        key = values.get(_local_name(element.tag))
                        value = _metadata_text(element)
                        if key and value:
                            result[key] = value
                app = _read_zip_member(archive, names, "docProps/app.xml")
                if app is not None:
                    root = _safe_xml(app)
                    for element in root.iter():
                        name = _local_name(element.tag)
                        if name in ("Application", "AppVersion", "Company"):
                            value = _metadata_text(element)
                            if value:
                                result[name.lower()] = value
            else:
                mimetype = _read_zip_member(archive, names, "mimetype")
                if mimetype != ODF_MIMETYPES[extension]:
                    raise ValueError("ZIP archive is not the expected OpenDocument format")
                if "content.xml" not in names:
                    raise ValueError("archive is missing its content.xml document part")
                meta = _read_zip_member(archive, names, "meta.xml")
                if meta is not None:
                    root = _safe_xml(meta)
                    values = {
                        "creator": "creator",
                        "initial-creator": "initialCreator",
                        "creation-date": "created",
                        "date": "modified",
                        "generator": "generator",
                    }
                    for element in root.iter():
                        key = values.get(_local_name(element.tag))
                        value = _metadata_text(element)
                        if key and value:
                            result[key] = value
    except zipfile.BadZipFile as exc:
        raise ValueError(f"invalid office ZIP archive: {exc}") from exc
    except (zipfile.LargeZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error) as exc:
        raise ValueError(f"could not read office archive: {exc}") from exc
    return result


def _tiff_bytes(data: bytes, offset: int, length: int) -> bytes:
    if offset < 0 or length < 0 or offset + length > len(data):
        raise ValueError("EXIF data contains an out-of-bounds TIFF offset")
    return data[offset:offset + length]


def _tiff_ifd(data: bytes, offset: int, endian: str):
    count = struct.unpack(endian + "H", _tiff_bytes(data, offset, 2))[0]
    if count > 1024:
        raise ValueError("EXIF IFD exceeds the 1024-entry limit")
    _tiff_bytes(data, offset + 2, count * 12 + 4)
    entries = []
    for index in range(count):
        entry = _tiff_bytes(data, offset + 2 + index * 12, 12)
        tag, kind, item_count = struct.unpack(endian + "HHI", entry[:8])
        size = TIFF_TYPE_SIZES.get(kind)
        if size is None:
            continue
        total = size * item_count
        if total > MAX_METADATA_VALUE_BYTES:
            raise ValueError("EXIF field exceeds the 64 KiB value limit")
        if total <= 4:
            raw = entry[8:8 + total]
        else:
            value_offset = struct.unpack(endian + "I", entry[8:12])[0]
            raw = _tiff_bytes(data, value_offset, total)
        entries.append((tag, kind, item_count, raw))
    return entries


def _tiff_values(kind: int, count: int, raw: bytes, endian: str):
    if kind == 2:
        return raw.decode("latin-1", errors="replace").strip("\x00 ")
    if kind == 7:
        return raw
    formats = {1: "B", 3: "H", 4: "I", 5: "II", 9: "i", 10: "ii"}
    if kind not in formats:
        return None
    code = formats[kind]
    width = struct.calcsize(code)
    values = []
    for index in range(count):
        numbers = struct.unpack(endian + code, raw[index * width:(index + 1) * width])
        if kind in (5, 10):
            numerator, denominator = numbers
            if denominator == 0:
                raise ValueError("EXIF rational contains a zero denominator")
            values.append(numerator / denominator)
        else:
            values.append(numbers[0])
    return values[0] if count == 1 else values


def _exif_user_comment(raw: bytes) -> str:
    if raw.startswith(b"ASCII\x00\x00\x00"):
        raw = raw[8:]
        return raw.decode("ascii", errors="replace").strip("\x00 ")
    if raw.startswith(b"UNICODE\x00"):
        return raw[8:].decode("utf-16", errors="replace").strip("\x00 ")
    if raw.startswith(b"JIS\x00\x00\x00\x00\x00"):
        raw = raw[8:]
    return raw.decode("latin-1", errors="replace").strip("\x00 ")


def _parse_gps(entries, endian: str, result: dict):
    fields = {}
    for tag, kind, count, raw in entries:
        if tag in (1, 3, 5):
            fields[tag] = _tiff_values(kind, count, raw, endian)
        elif tag in (2, 4, 6):
            fields[tag] = _tiff_values(kind, count, raw, endian)
    for tag, ref_tag, name in ((2, 1, "GPSLat"), (4, 3, "GPSLon")):
        coordinates = fields.get(tag)
        if isinstance(coordinates, list) and len(coordinates) == 3:
            decimal = coordinates[0] + coordinates[1] / 60 + coordinates[2] / 3600
            reference = fields.get(ref_tag)
            if isinstance(reference, str) and reference.upper() in ("S", "W"):
                decimal = -decimal
            result[name] = decimal
    altitude = fields.get(6)
    if isinstance(altitude, (int, float)):
        result["GPSAlt"] = -altitude if fields.get(5) == 1 else altitude


def _parse_exif(path: str, tiff: bytes) -> dict:
    result = {"file": path, "type": "JPEG"}
    if len(tiff) < 8:
        raise ValueError("truncated EXIF TIFF header")
    if tiff[:2] == b"II":
        endian = "<"
    elif tiff[:2] == b"MM":
        endian = ">"
    else:
        raise ValueError("invalid EXIF byte-order marker")
    magic, first_ifd = struct.unpack(endian + "HI", _tiff_bytes(tiff, 2, 6))
    if magic != 42:
        raise ValueError("invalid EXIF TIFF signature")

    root_entries = _tiff_ifd(tiff, first_ifd, endian)
    exif_offset = gps_offset = None
    for tag, kind, count, raw in root_entries:
        if tag in EXIF_TAGS:
            value = _tiff_values(kind, count, raw, endian)
            if tag == 0x9286 and isinstance(value, bytes):
                value = _exif_user_comment(value)
            if value not in (None, "", []):
                result[EXIF_TAGS[tag]] = value
        elif tag in (0x8769, 0x8825):
            value = _tiff_values(kind, count, raw, endian)
            if isinstance(value, int):
                if tag == 0x8769:
                    exif_offset = value
                else:
                    gps_offset = value

    if exif_offset is not None:
        for tag, kind, count, raw in _tiff_ifd(tiff, exif_offset, endian):
            if tag in EXIF_TAGS:
                value = _tiff_values(kind, count, raw, endian)
                if tag == 0x9286 and isinstance(value, bytes):
                    value = _exif_user_comment(value)
                if value not in (None, "", []):
                    result[EXIF_TAGS[tag]] = value
    if gps_offset is not None:
        _parse_gps(_tiff_ifd(tiff, gps_offset, endian), endian, result)
    return result


def _parse_jpeg(path: str, data: bytes) -> dict:
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("file does not have a JPEG start-of-image marker")
    position = 2
    while position < len(data):
        if data[position] != 0xFF:
            raise ValueError("malformed JPEG marker sequence")
        while position < len(data) and data[position] == 0xFF:
            position += 1
        if position >= len(data):
            raise ValueError("truncated JPEG marker")
        marker = data[position]
        position += 1
        if marker in (0xD9, 0xDA):
            break
        if marker in (0x01, *range(0xD0, 0xD8)):
            continue
        if position + 2 > len(data):
            raise ValueError("truncated JPEG segment length")
        segment_length = struct.unpack(">H", data[position:position + 2])[0]
        if segment_length < 2 or position + segment_length > len(data):
            raise ValueError("invalid or truncated JPEG segment")
        payload_start = position + 2
        payload_end = position + segment_length
        payload = data[payload_start:payload_end]
        if marker == 0xE1 and payload.startswith(b"Exif\x00\x00"):
            return _parse_exif(path, payload[6:])
        position = payload_end
    return {"file": path, "type": "JPEG"}


def read_pdf_meta(path: str) -> dict:
    return _parse_pdf(path, _read_file(path))


def read_ooxml_meta(path: str) -> dict:
    extension = Path(path).suffix.lower()
    if extension not in (*OOXML_MAIN_PARTS, *ODF_MIMETYPES):
        raise ValueError(f"unsupported office extension: {extension or '(none)'}")
    return _parse_ooxml(path, _read_file(path), extension)


def read_jpeg_exif(path: str) -> dict:
    return _parse_jpeg(path, _read_file(path))


def extract(path: str) -> dict:
    extension = Path(path).suffix.lower()
    try:
        if extension == ".pdf":
            return read_pdf_meta(path)
        if extension in (*OOXML_MAIN_PARTS, *ODF_MIMETYPES):
            return read_ooxml_meta(path)
        if extension in (".jpg", ".jpeg"):
            return read_jpeg_exif(path)
        raise ValueError(f"unsupported file type: {extension or '(no extension)'}")
    except (OSError, ValueError, struct.error, ElementTree.ParseError) as exc:
        return {"file": path, "type": extension.lstrip(".").upper(), "error": str(exc)}


def _text_value(value) -> str:
    return json.dumps(value)


def _format_text(results) -> str:
    lines = []
    for result in results:
        lines.append("=" * 60)
        for key, value in result.items():
            lines.append(f"  {key:20s}: {_text_value(value)}")
    return "\n".join(lines) + "\n"


def _write_output(path: str, content: str, inputs) -> None:
    output = Path(os.path.abspath(path))
    input_paths = {Path(item).resolve() for item in inputs}
    if output.resolve() in input_paths:
        raise ValueError("output path must not overwrite an input file")
    parent = output.parent
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=parent,
            prefix=f".{output.name}.", suffix=".tmp", delete=False,
        ) as target:
            temporary_path = Path(target.name)
            target.write(content)
        os.replace(temporary_path, output)
    except OSError:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract common metadata from PDF, OpenDocument, OOXML, and JPEG files.",
        epilog=(
            "Files are read locally and never uploaded. Input files are limited to 32 MiB; "
            "metadata XML is limited to 1 MiB."
        ),
    )
    parser.add_argument("files", nargs="+", help="Files to inspect")
    parser.add_argument("--out", help="Write results to this file (JSON unless --format is specified)")
    parser.add_argument("--format", choices=("json", "text"), help="Output format (default: text on stdout, JSON with --out)")
    args = parser.parse_args(argv)

    output_format = args.format or ("json" if args.out else "text")
    results = [extract(file) for file in args.files]
    if output_format == "json":
        content = json.dumps(results, indent=2) + "\n"
    else:
        content = _format_text(results)

    if args.out:
        try:
            _write_output(args.out, content, args.files)
        except (OSError, ValueError) as exc:
            print(f"meta-extract: cannot write output: {exc}", file=sys.stderr)
            return 2
        print(f"[*] Results -> {args.out}")
    else:
        sys.stdout.write(content)
    return 1 if any("error" in result for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
