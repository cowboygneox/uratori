# Third-party notices

uratori itself is licensed under BUSL-1.1 (see `LICENSE`). The `documents`
extra (`as document` / `as page of`, `docs/documents.md`) adds a small
number of third-party components, each under a licence compatible with
distributing this project under BUSL -- none of them AGPL, which is why
PyMuPDF was considered and passed over for page rendering and text
extraction in favour of pypdfium2.

- **pypdfium2** (Apache-2.0 / BSD-3-Clause, dual-licensed) -- the Python
  binding this project uses to render PDF pages and read their text layer.
  It bundles a build of Google's **PDFium**, itself BSD-3-Clause with a
  handful of third-party components under their own permissive licences
  (zlib, libopenjpeg2, FreeType, ICU and others). The full set of licence
  texts for the exact build in use ships inside the installed package at
  `pypdfium2-*.dist-info/licenses/`, and travels with every image this
  project's `Dockerfile` builds.
- **pytesseract** (Apache-2.0) -- a thin wrapper that shells out to the
  `tesseract` binary; it carries no bundled binary of its own.
- **Tesseract OCR** (Apache-2.0) -- installed separately as the
  `tesseract-ocr` apt package in the Docker image (`Dockerfile`), not
  bundled by any Python package. Used only for pages with no text layer
  (scans, faxes); see `docs/setup.md` for what that means for data leaving
  the process.
- **python-multipart** (Apache-2.0) -- parses the upload route's multipart
  request body (the file plus the optional `record` JSON part).
- **reportlab** (BSD, dev-only) -- generates the tiny synthetic PDF
  fixtures this project's own tests and `examples/records/` use. Not
  installed in the runtime image; it lives in the `dev` extra only.
