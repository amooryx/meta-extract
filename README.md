<div align="center">
  <img src="./banner.svg" alt="meta-extract" width="800">
</div>

# meta-extract

A dependency-free, local document metadata extractor for PDF, DOCX, XLSX, PPTX,
ODT, ODS, and JPEG files. It reads metadata only: embedded documents, macros,
scripts, and other file content are never executed, and files are never uploaded.

Use it only on files you own or are authorized to inspect.

## Requirements

- Python 3.9 or newer
- No third-party Python packages

## Usage

```console
python meta_extract.py report.pdf notes.docx photo.jpg
python meta_extract.py --format json report.pdf photo.jpg
python meta_extract.py --out metadata.json report.pdf
python meta_extract.py --format text --out metadata.txt report.pdf
```

Text is the default for standard output; `--out` defaults to JSON for backwards
compatibility. `--format` selects the format for either destination. JSON output
is an array with one result per input. A file that cannot be read or parsed gets
an `error` field; the tool continues with the remaining files and exits with
status 1. Output write failures exit with status 2. Unsupported extensions and
files whose content does not match their extension are reported as errors.
Output paths are not allowed to overwrite an input file.

## Extracted fields and limits

- **PDF:** Author, Creator, Producer, Subject, and Title from uncompressed PDF
  literal or hexadecimal strings. This lightweight parser does not resolve PDF
  cross-reference tables, compressed objects, or XMP metadata, so some PDFs
  will contain no extractable fields.
- **DOCX, XLSX, PPTX:** common core properties and application properties.
- **ODT, ODS:** creator, initial creator, created/modified dates, and generator.
- **JPEG:** common camera/date fields and GPS latitude, longitude, and altitude
  from EXIF. Coordinates are decimal degrees.

Inputs are limited to 32 MiB, metadata XML to 1 MiB, ZIP archives to 10,000
entries, EXIF IFDs to 1,024 entries, and individual metadata values to 64 KiB.
Office archive metadata is read without extracting archive members; DTD and
entity declarations in metadata XML are rejected. These bounds are intended to
keep resource use predictable, not to make the parser a validator for every
format variant.

Metadata may contain personal information such as names, dates, and GPS
coordinates. Review results before sharing them. Results are written only to
the terminal or the output path you specify.

## Project

**Omar Khalid** — [omareldemery.com](https://omareldemery.com) |
[@amooryx](https://github.com/amooryx)
