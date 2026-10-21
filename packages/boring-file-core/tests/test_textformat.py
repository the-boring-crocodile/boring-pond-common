"""The portable format layer is independent of Linux filesystem primitives."""
import codecs
import subprocess
import sys
import unittest
from boring_file_core.textformat import TextFormat, TextFormatError, decode, encode, target_format

class TextFormatTest(unittest.TestCase):
    def test_roundtrip_corpus(self):
        for encoding, bom in (("utf-8", b""), ("utf-8", codecs.BOM_UTF8),
                              ("utf-16-le", codecs.BOM_UTF16_LE), ("utf-16-be", codecs.BOM_UTF16_BE)):
            for newline in ("\n", "\r\n"):
                for ending in ("", newline):
                    with self.subTest(encoding=encoding, bom=bom, newline=newline, ending=ending):
                        raw = bom + ("Grüße" + newline + "second" + ending).encode(encoding)
                        result = decode(raw)
                        self.assertEqual(encode(result.text, result.format), raw)
                        self.assertNotIn("\r", result.text)

    def test_ambiguous_and_nontext_inputs(self):
        mixed = decode(b"a\r\nb\n")
        with self.assertRaises(TextFormatError):
            target_format(mixed.format, None, None, None)
        self.assertEqual(encode(mixed.text, target_format(mixed.format, None, None, "lf")), b"a\nb\n")
        for data in (codecs.BOM_UTF32_LE+b"x", codecs.BOM_UTF32_BE+b"x", b"\0", b"\xff", codecs.BOM_UTF16_LE+b"x"):
            with self.subTest(data=data), self.assertRaises(TextFormatError):
                decode(data)
        with self.assertRaises(TextFormatError):
            encode("a\0b", TextFormat("utf-8", False, "lf"))
        with self.assertRaises(TextFormatError):
            encode("a", TextFormat("utf-16-le", False, "lf"))

    def test_caller_owns_new_file_default(self):
        self.assertEqual(encode("a\nb", target_format(None, None, None, None)), b"a\nb")
        self.assertEqual(encode("a\nb", target_format(None, None, None, None, default_newline="crlf")), b"a\r\nb")

    def test_import_does_not_require_linux_flags(self):
        # Models the missing Windows flags; native Windows acceptance is still separate.
        script = "import os; os.__dict__.pop('O_DIRECTORY', None); os.__dict__.pop('O_NOFOLLOW', None); from boring_file_core.textformat import decode; assert decode(b'ok').text == 'ok'"
        subprocess.run([sys.executable, "-c", script], check=True)
