"""
Square Transactions CSV -> QuickBooks Online (QBO) CSV converter.

QBO import spec (bank/transaction CSV import), confirmed against Intuit's
own documentation:
  - 3-column format: Date, Description, Amount (single signed amount)
  - or 4-column format: Date, Description, Credit, Debit
  - UTF-8 encoding
  - No currency symbols ($, commas as thousands separators)
  - Dates as MM/DD/YYYY
  - Blank cells (not 0) for empty amount fields
  - No special characters in Description
  - Max ~350KB / ~1,000-1,500 transactions per upload (caller should chunk)

Square's own help docs don't publish an exact column spec for the
Transactions export (Reports > Transactions > Export), but the real-world
format is well documented by third parties who build against it. The
relevant columns are: "Date", "Time", "Time Zone", "Transaction ID",
"Payment ID", "Gross Sales", "Fees", "Net Total", "Total Collected",
"Event Type", "Transaction Status". Important quirk: Square stores Fees
as a NEGATIVE number, so Net Total = Gross Sales + Fees.

We map:
  Date                        -> Date       (reformatted to MM/DD/YYYY)
  Event Type / Transaction ID -> Description (combined, sanitized)
  Net Total                   -> Amount      (signed, no $ or commas)
"""

import csv
import io
import re
from datetime import datetime

MAX_UPLOAD_BYTES = 350_000
MAX_TRANSACTIONS_PER_FILE = 1000

# Characters QBO's CSV import is known to choke on inside Description.
_SPECIAL_CHARS_RE = re.compile(r"[^A-Za-z0-9 ,.\-/#&]")

# Square's "Date" column is YYYY-MM-DD. Also accept a couple of other
# formats in case a different report variant is uploaded.
_SQUARE_DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"]


class ConversionError(Exception):
    """Raised when the input file can't be safely converted."""


def _parse_square_date(raw: str) -> str:
    raw = raw.strip()
    for fmt in _SQUARE_DATE_FORMATS:
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%m/%d/%Y")
        except ValueError:
            continue
    raise ConversionError(f"Unrecognized date format: '{raw}'")


def _sanitize_description(event_type: str, transaction_id: str) -> str:
    combined = f"{event_type.strip()} {transaction_id.strip()}".strip()
    cleaned = _SPECIAL_CHARS_RE.sub("", combined)
    # Collapse repeated whitespace left behind by stripped characters.
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned or "Square Transaction"
    # Defense-in-depth against CSV/formula injection, same rationale as
    # TillMod's Square converter: a leading =, +, -, or @ could be
    # interpreted as a formula if this file is opened directly in a
    # spreadsheet before being imported into QBO. A leading apostrophe is
    # the standard convention those apps use to mean "plain text."
    if cleaned[0] in "=+-@":
        cleaned = "'" + cleaned
    return cleaned


def _parse_amount(raw: str) -> float:
    cleaned = raw.replace("$", "").replace(",", "").strip()
    if not cleaned:
        raise ConversionError("Empty amount field")
    try:
        return float(cleaned)
    except ValueError:
        raise ConversionError(f"Unparseable amount: '{raw}'")


def convert_square_to_qbo(file_bytes: bytes) -> tuple[str, list[str]]:
    """
    Convert a Square Transactions CSV export (as raw bytes) into a
    QBO-ready 3-column CSV (Date, Description, Amount).

    Returns (csv_text, warnings). Raises ConversionError on fatal problems
    (e.g. missing required columns).
    """
    warnings: list[str] = []

    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = file_bytes.decode("latin-1")
            warnings.append(
                "File was not UTF-8; re-encoded from Latin-1. Verify special "
                "characters in transaction details look correct."
            )
        except UnicodeDecodeError as e:
            raise ConversionError(f"Could not decode file as text: {e}")

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ConversionError("File appears to be empty or not a valid CSV.")

    required = {"Date", "Net Total"}
    header_set = set(reader.fieldnames)
    missing = required - header_set
    if missing:
        raise ConversionError(
            f"Missing required Square columns: {', '.join(sorted(missing))}. "
            f"Found columns: {', '.join(reader.fieldnames)}"
        )

    has_event_type = "Event Type" in header_set
    has_txn_id = "Transaction ID" in header_set

    rows_out = []
    row_count = 0
    for i, row in enumerate(reader, start=2):  # start=2: row 1 is the header
        row_count += 1
        try:
            date = _parse_square_date(row["Date"])
        except ConversionError as e:
            warnings.append(f"Row {i}: skipped ({e})")
            continue

        try:
            amount = _parse_amount(row["Net Total"])
        except ConversionError as e:
            warnings.append(f"Row {i}: skipped ({e})")
            continue

        event_type = row.get("Event Type", "") if has_event_type else ""
        transaction_id = row.get("Transaction ID", "") if has_txn_id else ""
        description = _sanitize_description(event_type, transaction_id)

        rows_out.append([date, description, f"{amount:.2f}"])

    if not rows_out:
        raise ConversionError("No valid transactions found after parsing.")

    if row_count > MAX_TRANSACTIONS_PER_FILE:
        warnings.append(
            f"This file has {row_count} transactions, above QBO's recommended "
            f"~{MAX_TRANSACTIONS_PER_FILE}-transaction limit per upload. "
            "Consider splitting it into multiple imports by date range."
        )

    out_buf = io.StringIO()
    writer = csv.writer(out_buf, lineterminator="\r\n")
    writer.writerow(["Date", "Description", "Amount"])
    writer.writerows(rows_out)

    csv_text = out_buf.getvalue()
    size = len(csv_text.encode("utf-8"))
    if size > MAX_UPLOAD_BYTES:
        warnings.append(
            f"Output file is {size:,} bytes, above QBO's ~350KB upload limit. "
            "Split into multiple files by date range before importing."
        )

    return csv_text, warnings
