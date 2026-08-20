import base64
import hashlib

from tools.terminal_tool import _omit_inline_binary_base64


def test_large_pdf_base64_is_replaced_without_a_preview():
    raw = b"%PDF-1.7\n" + (b"private document bytes\n" * 400)
    encoded = base64.b64encode(raw).decode("ascii")

    result = _omit_inline_binary_base64(encoded)

    assert encoded[:80] not in result
    assert "binary base64 output omitted" in result
    assert "media_type: application/pdf" in result
    assert f"decoded_bytes: {len(raw)}" in result
    assert hashlib.sha256(raw).hexdigest() in result


def test_line_wrapped_binary_base64_is_replaced():
    raw = b"PK\x03\x04" + (b"archive bytes" * 500)
    encoded = base64.b64encode(raw).decode("ascii")
    wrapped = "\n".join(encoded[i : i + 76] for i in range(0, len(encoded), 76))

    result = _omit_inline_binary_base64(wrapped)

    assert "media_type: application/zip" in result
    assert encoded[:80] not in result


def test_short_base64_and_normal_output_are_unchanged():
    short = base64.b64encode(b"small value").decode("ascii")
    prose = "build completed; artifact is in /tmp/report.pdf"

    assert _omit_inline_binary_base64(short) == short
    assert _omit_inline_binary_base64(prose) == prose


def test_long_base64_alphabet_text_that_is_not_valid_payload_is_unchanged():
    invalid = "A" * 4097

    assert _omit_inline_binary_base64(invalid) == invalid
