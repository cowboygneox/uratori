# Documents

A bundle of uploaded files -- a patient's scanned records, years of them --
is awkward to model as plain facts: the bytes are large, mostly opaque, and
what a definition can read off them (a page's text, a word's position) is
not something a provider hands over as JSON. Documents close that gap
without opening a second kind of record: a document is a `fact`, of a shape
the language itself knows, and the bytes live beside it in a server-owned
blob store, never inside it.

This page covers the shape: what `fact <kind> as document:` and `fact <kind>
as page of <kind>` declare, and why. See [the language guide](language.md#as-document--as-page-of----a-fact-shape-the-language-knows)
for how the shape sits inside `fact` as a whole, and [the HTTP
API](http-api.md) for the routes that fill it: uploading a file, reading its
pages, rendering a page as an image, and reading the word layer a page's
text was found at.

## The shape

```
# A patient's uploaded records, one file at a time.
fact medical_record as document:
    name title
    source as text            # host fields beside the shape's own

# One page of one.
fact medical_record_page as page of medical_record
```

`as document` declares a *document kind*: a record of it stands for one
uploaded file. The language already knows what that record carries --
`title, mime, sha256, pages, uploaded_at` -- and merges those fields in
ahead of whatever the host writes beside them (`source`, above, is a host
field; a real deployment might add `patient_id`, a case number, anything the
upload's own metadata carries). `name title` works because `title` is
already there once the shape has merged.

`as page of medical_record` declares the *page kind*: one record per page of
an uploaded document. It takes no block -- a page fact is written on a
single line -- because nothing may write a host field onto a page; a page's
only fields are the ones the shape brings (`document_id, number,
text_source, words_sha`), because the server computes every one of them
from the bytes, never from a provider's claim about them. `text_source`
says how the page's text was found (`"pdf"`, `"ocr"`, or `"none"` for a page
with neither), and `words_sha` is the build hash of the page's word layer --
the server table of words and their positions that [provenance](http-api.md)
and search read, kept outside the fact body (see [the language
guide](language.md#structural-only-on-purpose) on why a fact's body holds
only what a definition can read structurally, never a list of boxes).
`words_sha` moving is a fact change like any other field, so a page
re-OCR'd under a better pass is a page every extract re-reads.

Every document kind takes **exactly one** page kind. There is no ambiguity
to resolve about "which pages does this upload fill" because there is never
more than one answer: a document declared with no page kind, or with two, is
refused at compile.

## Who writes these facts

Nothing does, by hand. A document and its pages are written by the server's
own documents routes (`POST /tenants/{tenant}/documents/{kind}` and the
routes beside it) when a file is uploaded -- the same verified write every
other fact goes through, just authored by the server instead of a host
provider. The facts route refuses a direct write or delete against a
document or page kind, naming the documents routes instead: a document's
bytes, its page images, and its facts move together, or not at all.

## Provenance

A page record is what the facts route's `provenance` map cites: a write of
any *other* fact kind can name `{"page": "<page key>", "words": [id, …]}`
beside one of its fields, and the server derives the box and the printed
text from that page's own word layer. The citation lives beside the fact,
never inside it -- the same rule that keeps a page's own text out of its
body -- and it is replaced wholesale alongside the write it attests, never
patched. See [the HTTP API](http-api.md#provenance-in-the-writes) for the
request shape and the rules it is checked against, and
[`Source`/`Box`](http-api.md#source-and-box) for what comes back on
`GET /evidence` and the built-in UI.

## Versions

A document kind's version hashes its host fields and the shape, exactly as
any fact's does. A page kind's version hashes its (empty) host fields, the
shape, and the *name* of the document kind it belongs to -- never that
kind's own version, so a page fact stays downstream of nothing, like every
fact in this language. Changing `medical_record`'s host fields moves
`medical_record`'s version; it does not move `medical_record_page`'s.
