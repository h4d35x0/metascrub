# tests/fixtures/ole2

The fixture set that discharged the OLE2 deferral on 2026-09-04.

`build_ole2.py` generates a genuine legacy compound file for `.doc`, `.xls` and
`.ppt` at test time. It does not ship binaries: an opaque committed `.doc` is a
fixture nobody can review, and a generated one cannot drift away from the seed
it claims to carry.

## How

A seed OOXML document is built with `python-docx` / `openpyxl` / `python-pptx`
carrying a sentinel in every core property, then converted:

```
soffice --headless --convert-to doc --outdir <dir> <seed.docx>
```

Measured 2026-09-04, LibreOffice on Windows, on the resulting `.doc`:

| Check | Result |
|---|---|
| First eight bytes | `d0cf11e0a1b11ae1`, a real OLE2 compound file |
| Streams present | `\005SummaryInformation`, `\005DocumentSummaryInformation`, `\001CompObj`, `\001Ole`, `WordDocument`, `1Table` |
| Sentinel occurrences | 7, all of them inside the two property streams |
| exiftool 13.59 | reads Author, Title, Subject, Keywords, Comments, Last Modified By |

The builder verifies the magic number itself rather than trusting the exit
status, because a conversion that quietly produced an OOXML package with a
`.doc` name would still satisfy a "the file exists" check, and every test after
it would then be exercising the wrong container.

## The caveat this fixture carries

**This is LibreOffice's "MS Word 97" export filter, not Microsoft Word.** It
writes both property streams, `\001CompObj`, and the application streams, so it
exercises every structure `metascrub/engines/ole2_engine.py` touches. It does
**not** write `SttbfAssoc` or `SttbSavedBy`, the Word structures that duplicate
the author and title and record the last ten save paths. Measured: zero sentinel
hits in `1Table`.

That is the reason the engine deliberately leaves those two alone and reports
`.doc` as `PARTIAL`. A fixture from Microsoft Word would let that change; this
one proves what it proves and nothing beyond it.

## When LibreOffice is absent

`build_ole2()` returns `None` for every failure mode, and `tests/test_ole2.py`
turns `None` into a skip. A machine without LibreOffice is not evidence that the
engine is broken, and a test that failed there would be saying it was.
