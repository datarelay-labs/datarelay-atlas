from __future__ import annotations

import hashlib
import io
import json
import subprocess
import uuid
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

from atlas.cli import main
from atlas.operations_readiness import _dependency_inventory
from atlas.provenance import ValidationError
from atlas.sbom import (
    BOM_FILENAME,
    PROVENANCE_FILENAME,
    build_sbom_documents_from_facts,
    publish_sbom_bundle,
    validate_cyclonedx_sbom,
    validate_sbom_bundle,
    validate_sbom_provenance,
)

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs" / "contracts"
FIXTURES = CONTRACTS / "fixtures"
HEAD_A = "a" * 40
HEAD_B = "b" * 40
SOURCE = {
    "repository": "datarelay-labs/datarelay-atlas",
    "source_revision": HEAD_A,
    "clean": True,
}
PYTHON = {"implementation": "cpython", "version": "3.12.3"}
INVENTORY = [
    {"name": "jsonschema", "version": "4.26.0", "license_expression": "MIT"},
    {"name": "mcp", "version": "2.2.0", "license": "MIT"},
    {
        "name": "cryptography",
        "version": "50.0.1",
        "license_expression": "Apache-2.0",
    },
    {"name": "PyYAML", "version": "6.0.3", "license": "MIT"},
    {"name": "anyio", "version": "4.15.1", "license": "MIT"},
]


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _artifact_digest(payload: object) -> str:
    raw = (
        json.dumps(
            payload,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    return hashlib.sha256(raw).hexdigest()


def _git(argv: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *argv],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout.strip()


def _make_repo(base: Path) -> Path:
    repo = base / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "atlas-test@example.invalid"], repo)
    _git(["config", "user.name", "Atlas Test"], repo)
    (repo / "requirements.txt").write_text(
        "jsonschema>=4.0\n"
        "mcp==2.2.0\n"
        "cryptography>=46.0.0,<52\n"
        "PyYAML>=6.0,<7\n",
        encoding="utf-8",
    )
    (repo / "THIRD_PARTY.md").write_text(
        "# Third-Party Components\n\nTest fixture.\n",
        encoding="utf-8",
    )
    _git(["add", "requirements.txt", "THIRD_PARTY.md"], repo)
    _git(["commit", "-m", "fixture"], repo)
    _git(
        [
            "remote",
            "add",
            "origin",
            "https://github.com/datarelay-labs/datarelay-atlas.git",
        ],
        repo,
    )
    return repo


class SbomTests(unittest.TestCase):
    def test_public_schemas_fixtures_and_runtime_parity(self) -> None:
        sbom_schema = json.loads(
            (CONTRACTS / "atlas-cyclonedx-sbom.schema.json").read_text()
        )
        provenance_schema = json.loads(
            (CONTRACTS / "atlas-sbom-provenance.schema.json").read_text()
        )
        sbom_fixture = json.loads(
            (FIXTURES / "atlas-cyclonedx-sbom.example.json").read_text()
        )
        provenance_fixture = json.loads(
            (FIXTURES / "atlas-sbom-provenance.example.json").read_text()
        )
        Draft202012Validator.check_schema(sbom_schema)
        Draft202012Validator.check_schema(provenance_schema)
        Draft202012Validator(sbom_schema).validate(sbom_fixture)
        Draft202012Validator(provenance_schema).validate(provenance_fixture)
        self.assertEqual(validate_cyclonedx_sbom(sbom_fixture), sbom_fixture)
        self.assertEqual(
            validate_sbom_provenance(provenance_fixture),
            provenance_fixture,
        )

    def test_build_is_deterministic_and_marks_direct_dependencies(self) -> None:
        first = build_sbom_documents_from_facts(
            ROOT,
            inventory=deepcopy(INVENTORY),
            source_facts=deepcopy(SOURCE),
            python_facts=deepcopy(PYTHON),
        )
        second = build_sbom_documents_from_facts(
            ROOT,
            inventory=list(reversed(deepcopy(INVENTORY))),
            source_facts=deepcopy(SOURCE),
            python_facts=deepcopy(PYTHON),
        )
        self.assertEqual(first, second)
        sbom, provenance = first
        names = [item["name"] for item in sbom["components"]]
        self.assertEqual(names, sorted(names))
        self.assertEqual(provenance["component_count"], len(INVENTORY))
        direct = {
            item["name"]: item["properties"][0]["value"]
            for item in sbom["components"]
        }
        self.assertEqual(direct["jsonschema"], "true")
        self.assertEqual(direct["mcp"], "true")
        self.assertEqual(direct["cryptography"], "true")
        self.assertEqual(direct["pyyaml"], "true")
        self.assertEqual(direct["anyio"], "false")
        self.assertEqual(provenance["authority"], "EVIDENCE_ONLY")

    def test_source_revision_changes_serial_and_provenance_digest(self) -> None:
        sbom_a, provenance_a = build_sbom_documents_from_facts(
            ROOT,
            inventory=deepcopy(INVENTORY),
            source_facts=deepcopy(SOURCE),
            python_facts=deepcopy(PYTHON),
        )
        source_b = deepcopy(SOURCE)
        source_b["source_revision"] = HEAD_B
        sbom_b, provenance_b = build_sbom_documents_from_facts(
            ROOT,
            inventory=deepcopy(INVENTORY),
            source_facts=source_b,
            python_facts=deepcopy(PYTHON),
        )
        self.assertNotEqual(sbom_a["serialNumber"], sbom_b["serialNumber"])
        self.assertNotEqual(
            provenance_a["provenance_digest"],
            provenance_b["provenance_digest"],
        )
        self.assertNotEqual(
            provenance_a["sbom_sha256"],
            provenance_b["sbom_sha256"],
        )


    def test_missing_declared_dependency_and_bad_identity_fail_closed(self) -> None:
        missing = [
            item for item in deepcopy(INVENTORY)
            if item["name"] != "mcp"
        ]
        with self.assertRaisesRegex(ValidationError, "missing declared"):
            build_sbom_documents_from_facts(
                ROOT,
                inventory=missing,
                source_facts=deepcopy(SOURCE),
                python_facts=deepcopy(PYTHON),
            )

        malformed = deepcopy(INVENTORY)
        malformed[0]["name"] = "bad/name"
        with self.assertRaisesRegex(ValidationError, "package name"):
            build_sbom_documents_from_facts(
                ROOT,
                inventory=malformed,
                source_facts=deepcopy(SOURCE),
                python_facts=deepcopy(PYTHON),
            )

        conflicting = deepcopy(INVENTORY)
        conflicting.append(
            {"name": "jsonschema", "version": "99.0.0"}
        )
        with self.assertRaisesRegex(ValidationError, "conflicting"):
            build_sbom_documents_from_facts(
                ROOT,
                inventory=conflicting,
                source_facts=deepcopy(SOURCE),
                python_facts=deepcopy(PYTHON),
            )

    def test_free_form_license_is_omitted_not_invented(self) -> None:
        inventory = deepcopy(INVENTORY)
        inventory[0]["license_expression"] = None
        inventory[0]["license"] = (
            "This is a long free-form license text and not a strict SPDX id"
        )
        sbom, _ = build_sbom_documents_from_facts(
            ROOT,
            inventory=inventory,
            source_facts=deepcopy(SOURCE),
            python_facts=deepcopy(PYTHON),
        )
        component = next(
            row for row in sbom["components"]
            if row["name"] == "jsonschema"
        )
        self.assertNotIn("licenses", component)

    def test_tampered_sbom_and_provenance_fail_closed(self) -> None:
        sbom, provenance = build_sbom_documents_from_facts(
            ROOT,
            inventory=deepcopy(INVENTORY),
            source_facts=deepcopy(SOURCE),
            python_facts=deepcopy(PYTHON),
        )
        bad_sbom = deepcopy(sbom)
        bad_sbom["components"][0]["version"] = "9.9.9"
        with self.assertRaises(ValidationError):
            validate_cyclonedx_sbom(bad_sbom)

        bad_provenance = deepcopy(provenance)
        bad_provenance["component_count"] += 1
        with self.assertRaisesRegex(ValidationError, "digest mismatch"):
            validate_sbom_provenance(bad_provenance)

    def test_fixture_bundle_validates_source_file_digests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp)
            (bundle / BOM_FILENAME).write_bytes(
                (FIXTURES / "atlas-cyclonedx-sbom.example.json").read_bytes()
            )
            (bundle / PROVENANCE_FILENAME).write_bytes(
                (FIXTURES / "atlas-sbom-provenance.example.json").read_bytes()
            )
            summary = validate_sbom_bundle(
                bundle,
                repo_root=ROOT,
                require_current_source=False,
            )
            self.assertEqual(summary["state"], "VALIDATED_EVIDENCE")
            self.assertEqual(summary["authority"], "EVIDENCE_ONLY")


    def test_publish_bundle_refuses_dirty_source_and_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = _make_repo(base)
            out_parent = base / "out"
            out_parent.mkdir()
            bundle = out_parent / "bundle"
            with patch(
                "atlas.sbom._runtime_inventory",
                return_value=deepcopy(INVENTORY),
            ):
                summary = publish_sbom_bundle(repo, bundle)
                readiness = _dependency_inventory(
                    repo,
                    sbom_bundle=bundle,
                )
            self.assertEqual(summary["state"], "VALIDATED_EVIDENCE")
            self.assertEqual(
                readiness["sbom_state"],
                "VALIDATED_EVIDENCE",
            )
            self.assertEqual(
                readiness["sbom_evidence"]["provenance_digest"],
                summary["provenance_digest"],
            )
            self.assertTrue((bundle / BOM_FILENAME).is_file())
            self.assertTrue((bundle / PROVENANCE_FILENAME).is_file())
            with patch(
                "atlas.sbom._runtime_inventory",
                return_value=deepcopy(INVENTORY),
            ):
                with self.assertRaisesRegex(
                    ValidationError,
                    "destination already exists",
                ):
                    publish_sbom_bundle(repo, bundle)

            with patch(
                "atlas.sbom._runtime_inventory",
                return_value=deepcopy(INVENTORY),
            ):
                with self.assertRaisesRegex(
                    ValidationError,
                    "outside the repository",
                ):
                    publish_sbom_bundle(
                        repo,
                        repo / "release-sbom",
                    )

            dirty_bundle = out_parent / "dirty-bundle"
            (repo / "untracked.txt").write_text("dirty\n", encoding="utf-8")
            with patch(
                "atlas.sbom._runtime_inventory",
                return_value=deepcopy(INVENTORY),
            ):
                with self.assertRaisesRegex(
                    ValidationError,
                    "clean repository",
                ):
                    publish_sbom_bundle(repo, dirty_bundle)
            self.assertFalse(dirty_bundle.exists())

    def test_hidden_index_state_cannot_mint_clean_sbom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = _make_repo(base)
            _git(
                ["update-index", "--assume-unchanged", "requirements.txt"],
                repo,
            )
            (repo / "requirements.txt").write_text(
                "jsonschema>=999\n"
                "mcp==2.2.0\n"
                "cryptography>=46.0.0,<52\n"
                "PyYAML>=6.0,<7\n",
                encoding="utf-8",
            )
            out_parent = base / "out"
            out_parent.mkdir()
            with patch(
                "atlas.sbom._runtime_inventory",
                return_value=deepcopy(INVENTORY),
            ):
                with self.assertRaisesRegex(
                    ValidationError,
                    "hidden file state",
                ):
                    publish_sbom_bundle(
                        repo,
                        out_parent / "bundle",
                    )

    def test_bundle_tamper_and_source_staleness_are_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = _make_repo(base)
            out_parent = base / "out"
            out_parent.mkdir()
            bundle = out_parent / "bundle"
            with patch(
                "atlas.sbom._runtime_inventory",
                return_value=deepcopy(INVENTORY),
            ):
                publish_sbom_bundle(repo, bundle)

            sbom_path = bundle / BOM_FILENAME
            sbom = json.loads(sbom_path.read_text())
            sbom["components"][0]["version"] = "99.0.0"
            sbom_path.write_text(json.dumps(sbom), encoding="utf-8")
            with self.assertRaises(ValidationError):
                validate_sbom_bundle(
                    bundle,
                    repo_root=repo,
                    require_current_source=True,
                )

            # Rebuild a clean bundle, then make the source stale.
            stale_bundle = out_parent / "stale-bundle"
            # The first bundle is intentionally tampered, so regenerate after
            # removing it rather than using it as source evidence.
            import shutil
            shutil.rmtree(bundle)
            with patch(
                "atlas.sbom._runtime_inventory",
                return_value=deepcopy(INVENTORY),
            ):
                publish_sbom_bundle(repo, stale_bundle)
            (repo / "requirements.txt").write_text(
                (repo / "requirements.txt").read_text()
                + "anyio>=4\n",
                encoding="utf-8",
            )
            state = _dependency_inventory(
                repo,
                sbom_bundle=stale_bundle,
            )
            self.assertEqual(state["sbom_state"], "INVALID_EVIDENCE")
            self.assertIsNone(state["sbom_evidence"])

    def test_resigned_tamper_fails_current_runtime_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = _make_repo(base)
            out_parent = base / "out"
            out_parent.mkdir()
            bundle = out_parent / "bundle"
            with patch(
                "atlas.sbom._runtime_inventory",
                return_value=deepcopy(INVENTORY),
            ):
                publish_sbom_bundle(repo, bundle)

            sbom_path = bundle / BOM_FILENAME
            provenance_path = bundle / PROVENANCE_FILENAME
            sbom = json.loads(sbom_path.read_text())
            provenance = json.loads(provenance_path.read_text())
            component = next(
                row for row in sbom["components"]
                if row["name"] == "anyio"
            )
            component["version"] = "9.9.9"
            purl = "pkg:pypi/anyio@9.9.9"
            component["purl"] = purl
            component["bom-ref"] = purl
            head = sbom["metadata"]["component"]["version"]
            sbom["serialNumber"] = "urn:uuid:" + str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    (
                        "https://github.com/datarelay-labs/"
                        f"datarelay-atlas@{head}:"
                        f"{_digest(sbom['components'])}"
                    ),
                )
            )
            provenance["sbom_sha256"] = _artifact_digest(sbom)
            provenance["sbom_serial_number"] = sbom["serialNumber"]
            basis = {
                key: value
                for key, value in provenance.items()
                if key != "provenance_digest"
            }
            provenance["provenance_digest"] = _digest(basis)
            sbom_path.write_text(
                json.dumps(sbom, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            provenance_path.write_text(
                json.dumps(provenance, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            validate_sbom_bundle(
                bundle,
                repo_root=repo,
                require_current_source=True,
                require_current_runtime=False,
            )
            with patch(
                "atlas.sbom._runtime_inventory",
                return_value=deepcopy(INVENTORY),
            ):
                with self.assertRaisesRegex(
                    ValidationError,
                    "current runtime",
                ):
                    validate_sbom_bundle(
                        bundle,
                        repo_root=repo,
                        require_current_source=True,
                        require_current_runtime=True,
                    )

    def test_dependency_inventory_never_promotes_declarations_alone(self) -> None:
        inventory = _dependency_inventory(ROOT)
        self.assertEqual(inventory["state"], "DECLARED")
        self.assertEqual(inventory["sbom_state"], "NOT_GENERATED")
        self.assertIsNone(inventory["sbom_evidence"])
        self.assertIn("not an SBOM", inventory["detail"])


    def test_cli_generates_bundle_and_relative_destination_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = _make_repo(base)
            out_parent = base / "out"
            out_parent.mkdir()
            bundle = out_parent / "cli-bundle"
            output = io.StringIO()
            with patch(
                "atlas.sbom._runtime_inventory",
                return_value=deepcopy(INVENTORY),
            ), redirect_stdout(output):
                rc = main(
                    [
                        "ops",
                        "sbom",
                        "--repo-root",
                        str(repo),
                        "--dest",
                        str(bundle),
                    ]
                )
            self.assertEqual(rc, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["state"], "VALIDATED_EVIDENCE")
            self.assertNotIn(str(repo), output.getvalue())

            with patch(
                "atlas.sbom._runtime_inventory",
                return_value=deepcopy(INVENTORY),
            ):
                with self.assertRaisesRegex(
                    ValidationError,
                    "absolute path",
                ):
                    publish_sbom_bundle(repo, Path("relative-output"))


if __name__ == "__main__":
    unittest.main()
