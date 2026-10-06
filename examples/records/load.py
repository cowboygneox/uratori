"""Load the records worked example into a running uratori (documents-plan-v3,
D4/D5): a synthetic chart bundle in, the BMI trace printed out.

This script is a *host*, the same role `examples/nfl/load.py` plays for
the NFL showcase: it teaches the engine the world (`schema.json`, empty;
`definitions.fig`, where every fact and extract lives), pushes the
`patient` roster as an ordinary host write (D5 -- nothing patient-specific
lives in the engine, so this roster is the host's own job), uploads the
synthetic bundle `generate.py` builds, then reads back the trace the
README walks: the BMI series for one patient, and one value's evidence
walked down to a page and its boxes.

Stdlib only -- `generate.py` needs reportlab (the dev extra), but
`urllib.request` is plenty to talk to the server.

    python examples/records/load.py --base http://localhost:8080
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import generate  # type: ignore[import-not-found]

HERE = Path(__file__).parent
Record = dict[str, Any]


class Client:
    def __init__(self, base: str, token: str | None):
        self.base = base.rstrip("/")
        self.token = token

    def call(self, method: str, path: str, body: Record | None = None) -> Record:
        request = urllib.request.Request(self.base + path, method=method)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        payload = json.dumps(body).encode() if body is not None else None
        try:
            with urllib.request.urlopen(request, data=payload) as response:
                answer: Record = json.loads(response.read())
                return answer
        except urllib.error.HTTPError as refusal:
            detail = refusal.read().decode(errors="replace")
            sys.exit(f"{method} {path} -> {refusal.code}: {detail}")

    def upload(self, path: str, filename: str, data: bytes) -> Record:
        """Multipart upload of one file -- stdlib has no form-data encoder,
        so this builds the one field the documents route needs by hand."""
        boundary = uuid.uuid4().hex
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: application/pdf\r\n\r\n"
        ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
        request = urllib.request.Request(self.base + path, method="POST", data=body)
        request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request) as response:
                answer: Record = json.loads(response.read())
                return answer
        except urllib.error.HTTPError as refusal:
            detail = refusal.read().decode(errors="replace")
            sys.exit(f"POST {path} -> {refusal.code}: {detail}")


def teach(client: Client) -> Record:
    schema_document = json.loads((HERE / "schema.json").read_text())
    source = {"source": (HERE / "definitions.fig").read_text()}
    client.call("PUT", "/schema", schema_document)
    library: Record = client.call("PUT", "/definitions", source)
    return library


def print_bmi_trace(client: Client, tenant: str, patient: str, day: str) -> None:
    """The output this example's README reproduces: the BMI series for one
    patient, then one value's evidence walked down to a page and its
    boxes -- weight's own measurement, and the height carried forward
    from the one day it was actually measured."""
    bmi = client.call("GET", f"/tenants/{tenant}/results/patient.bmi")
    series = sorted(
        (
            s
            for s in bmi["subjects"]
            if s["id"].startswith(f"{patient}@") and s["value"] is not None
        ),
        key=lambda s: s["id"],
    )
    # `patient.height`'s `carried forward` stores a row for every day
    # between the one height measurement and today -- D5's own stated
    # cost. Only the days that actually carry a weight (and so a BMI)
    # are worth printing; the thousands of in-between absent days are
    # the figure doing its job quietly, not something to show here.
    print(f"\npatient.bmi for {patient} (days with a weight on record):")
    for s in series:
        print(f"  {s['id']}: {s['display']}")

    subject = f"{patient}@{day}"
    print(f"\nevidence for patient.bmi?subject={subject}:")
    bmi_evidence = client.call("GET", f"/tenants/{tenant}/evidence/patient.bmi?subject={subject}")
    for member in bmi_evidence["members"]:
        print(f"  part: {member['figure']}")

    for figure in ("patient.weight", "patient.height"):
        evidence = client.call(
            "GET", f"/tenants/{tenant}/evidence/{figure}?subject={subject}"
        )
        print(f"\nevidence for {figure}?subject={subject}:")
        for member in evidence["members"]:
            for source in member["sources"]:
                print(
                    f"  field {source['field']}: page {source.get('page_key')}, "
                    f"printed {source.get('printed')!r}, "
                    f"{len(source.get('boxes') or [])} box(es)"
                )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://localhost:8080")
    parser.add_argument("--tenant", default="records")
    parser.add_argument("--token", default=os.environ.get("URATORI_TOKEN"))
    arguments = parser.parse_args()

    client = Client(arguments.base, arguments.token)

    health = client.call("GET", "/health")
    print(f"engine {health['version']} at {arguments.base}")

    library = teach(client)
    print(
        f"library loaded: {len(library['extracts'])} extracts, "
        f"{len(library['figures'])} figures"
    )

    bundle = generate.build_bundle()

    print(f"\npushing the patient roster ({len(bundle.patients)} patients)")
    client.call(
        "POST",
        f"/tenants/{arguments.tenant}/facts",
        {"writes": {"patient": bundle.patients}},
    )

    for filename, data in bundle.documents.items():
        print(f"uploading {filename} ({len(data)} bytes) ...")
        upload = client.upload(
            f"/tenants/{arguments.tenant}/documents/medical_record", filename, data
        )
        print(f"  -> id {upload['id']}, {upload['pages']} page(s), written={upload['written']}")

    print_bmi_trace(client, arguments.tenant, generate.PATIENT_A, generate.A_VISIT2_DATE)

    print("\nfailures for measurement:")
    failures = client.call(
        "GET", f"/tenants/{arguments.tenant}/extracts/measurement/failures"
    )
    for failure in failures["failures"]:
        print(f"  {failure['subject']} field={failure['field']}: {failure['reason']}")

    print(f"\nloaded. Try:\n  {arguments.base}/ui/  (tenant \"{arguments.tenant}\")")


if __name__ == "__main__":
    main()
