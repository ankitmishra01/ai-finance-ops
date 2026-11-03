"""Validates a generated T4 XML file against the CRA's published XML schema package.

The schema is not committed here. Download it from the CRA page "Filing information
returns electronically - what you should know before" (xmlschm1-27-1.zip for returns
filed in 2027, i.e. the 2026 tax year), unzip it, then:

  uv run --with lxml python 03_payroll/validate_t4_xml.py <unzipped schema folder> \
      out/payroll_yearend/payroll_qc/SAMPLE_NOT_FOR_FILING_t4.xml

Exits 0 if the file is valid against T619_T4.xsd. Passing the schema says the structure
and field formats are right; it does not mean the CRA would accept a filing.
"""

import sys
from pathlib import Path

from lxml import etree


def main() -> int:
    xsd_dir, xml_path = Path(sys.argv[1]), Path(sys.argv[2])
    schema = etree.XMLSchema(etree.parse(str(xsd_dir / "T619_T4.xsd")))
    doc = etree.parse(str(xml_path))
    if schema.validate(doc):
        print(f"VALID against {xsd_dir / 'T619_T4.xsd'}: {xml_path}")
        return 0
    for err in schema.error_log:
        print(f"line {err.line}: {err.message}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
