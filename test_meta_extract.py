import contextlib
import io
import json
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

import meta_extract


def _tiff_entry(endian, tag, kind, count, value):
    return struct.pack(endian + "HHI", tag, kind, count) + value


def _exif_tiff(endian):
    byte_order = b"II" if endian == "<" else b"MM"
    gps_offset = 38
    gps_ifd_size = 2 + 6 * 12 + 4
    latitude_offset = gps_offset + gps_ifd_size
    longitude_offset = latitude_offset + 24
    altitude_offset = longitude_offset + 24
    root = [
        _tiff_entry(endian, 0x010F, 2, 4, b"ACME"),
        _tiff_entry(endian, 0x8825, 4, 1, struct.pack(endian + "I", gps_offset)),
    ]
    gps = [
        _tiff_entry(endian, 1, 2, 2, b"S\x00\x00\x00"),
        _tiff_entry(endian, 2, 5, 3, struct.pack(endian + "I", latitude_offset)),
        _tiff_entry(endian, 3, 2, 2, b"E\x00\x00\x00"),
        _tiff_entry(endian, 4, 5, 3, struct.pack(endian + "I", longitude_offset)),
        _tiff_entry(endian, 5, 1, 1, b"\x01\x00\x00\x00"),
        _tiff_entry(endian, 6, 5, 1, struct.pack(endian + "I", altitude_offset)),
    ]
    rationals = [
        struct.pack(endian + "II", 12, 1),
        struct.pack(endian + "II", 30, 1),
        struct.pack(endian + "II", 0, 1),
        struct.pack(endian + "II", 20, 1),
        struct.pack(endian + "II", 15, 1),
        struct.pack(endian + "II", 0, 1),
        struct.pack(endian + "II", 30, 1),
    ]
    return (
        byte_order
        + struct.pack(endian + "HI", 42, 8)
        + struct.pack(endian + "H", len(root))
        + b"".join(root)
        + struct.pack(endian + "I", 0)
        + struct.pack(endian + "H", len(gps))
        + b"".join(gps)
        + struct.pack(endian + "I", 0)
        + b"".join(rationals)
    )


def _jpeg_with_exif(endian="<"):
    payload = b"Exif\x00\x00" + _exif_tiff(endian)
    segment = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    return b"\xff\xd8" + segment + b"\xff\xd9"


class MetaExtractTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def write(self, name, data):
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_pdf_literal_escapes_and_utf16_hex_strings(self):
        path = self.write(
            "sample.pdf",
            b"%PDF-1.7\n1 0 obj << /Title (A \\(nested\\) title) "
            b"/Author <FEFF004F006D0061> >> endobj",
        )
        result = meta_extract.extract(str(path))
        self.assertEqual(result["Title"], "A (nested) title")
        self.assertEqual(result["Author"], "Oma")

    def test_ooxml_properties_are_parsed_without_extracting_members(self):
        path = self.root / "sample.docx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", "<document/>")
            archive.writestr(
                "docProps/core.xml",
                '<cp:coreProperties xmlns:cp="urn:cp" xmlns:dc="urn:dc">'
                "<dc:creator>Sam</dc:creator><cp:lastModifiedBy>Lee</cp:lastModifiedBy>"
                "</cp:coreProperties>",
            )
            archive.writestr(
                "docProps/app.xml",
                "<Properties><Application>Writer</Application></Properties>",
            )
        result = meta_extract.extract(str(path))
        self.assertEqual(result["creator"], "Sam")
        self.assertEqual(result["lastModifiedBy"], "Lee")
        self.assertEqual(result["application"], "Writer")

    def test_odf_metadata_is_supported_and_type_is_checked(self):
        path = self.root / "notes.odt"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("mimetype", meta_extract.ODF_MIMETYPES[".odt"])
            archive.writestr("content.xml", "<document/>")
            archive.writestr(
                "meta.xml",
                '<office:document-meta xmlns:office="urn:office" xmlns:dc="urn:dc">'
                "<dc:creator>Alex</dc:creator></office:document-meta>",
            )
        self.assertEqual(meta_extract.extract(str(path))["creator"], "Alex")

        wrong_type = self.write("wrong.odt", b"PK\x03\x04not an ODT")
        self.assertIn("error", meta_extract.extract(str(wrong_type)))

    def test_xml_entity_declarations_are_rejected(self):
        path = self.root / "entity.docx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", "<document/>")
            archive.writestr(
                "docProps/core.xml",
                '<!DOCTYPE root [<!ENTITY x "expanded">]><root>&x;</root>',
            )
        result = meta_extract.extract(str(path))
        self.assertIn("DTD and entity declarations", result["error"])

    def test_oversized_metadata_value_is_rejected(self):
        path = self.root / "large-metadata.docx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", "<document/>")
            archive.writestr(
                "docProps/core.xml",
                "<properties><creator>" + ("x" * (64 * 1024 + 1)) + "</creator></properties>",
            )
        self.assertIn("64 KiB", meta_extract.extract(str(path))["error"])

    def test_jpeg_exif_reads_camera_and_signed_gps_in_both_byte_orders(self):
        for endian in ("<", ">"):
            path = self.write(f"sample-{endian == '>'}.jpg", _jpeg_with_exif(endian))
            result = meta_extract.extract(str(path))
            self.assertEqual(result["Make"], "ACME")
            self.assertAlmostEqual(result["GPSLat"], -12.5)
            self.assertAlmostEqual(result["GPSLon"], 20.25)
            self.assertEqual(result["GPSAlt"], -30)

    def test_malformed_jpeg_exif_reports_error(self):
        payload = b"Exif\x00\x00" + b"II*\x00\xff\xff\xff\x7f"
        path = self.write(
            "broken.jpg",
            b"\xff\xd8\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload,
        )
        self.assertIn("error", meta_extract.extract(str(path)))

    def test_missing_unsupported_and_mismatched_inputs_are_errors(self):
        missing = meta_extract.extract(str(self.root / "missing.pdf"))
        self.assertIn("error", missing)
        unsupported = self.write("program.exe", b"MZ")
        self.assertIn("unsupported file type", meta_extract.extract(str(unsupported))["error"])
        mismatch = self.write("photo.jpg", b"not a jpeg")
        self.assertIn("JPEG start-of-image", meta_extract.extract(str(mismatch))["error"])

    def test_input_size_limit_is_enforced(self):
        oversized = self.write("large.pdf", b"%PDF-" + b"x" * (meta_extract.MAX_INPUT_BYTES))
        self.assertIn("input limit", meta_extract.extract(str(oversized))["error"])

    def test_cli_json_reports_per_file_errors_and_nonzero_status(self):
        valid = self.write("valid.pdf", b"%PDF-1.4\n<< /Title (ok) >>")
        invalid = self.write("invalid.jpg", b"broken")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = meta_extract.main(["--format", "json", str(valid), str(invalid)])
        results = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(results[0]["Title"], "ok")
        self.assertIn("error", results[1])

    def test_cli_never_overwrites_an_input_file(self):
        source = self.write("source.pdf", b"%PDF-1.4")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = meta_extract.main(["--out", str(source), str(source)])
        self.assertEqual(code, 2)
        self.assertIn("must not overwrite", stderr.getvalue())
        self.assertEqual(source.read_bytes(), b"%PDF-1.4")

    def test_text_output_escapes_terminal_control_characters(self):
        rendered = meta_extract._format_text([{"Title": "line\n\x1b[31m"}])
        self.assertNotIn("\x1b", rendered)
        self.assertIn(r"\n", rendered)


if __name__ == "__main__":
    unittest.main()
