"""An undeclared direct grant must not expose stored document content."""

import hashlib

import yaml
from click.testing import CliRunner

from phantomdocs.cli import main
from phantomdocs.identity import doc_version_mac_from_hash, root_mac
from phantomdocs.manifest import empty_manifest


def test_get_cat_denies_undeclared_direct_category(tmp_path):
    category = "category-77"
    org = {
        "version": 1,
        "policies": {
            "security_categories": {},
            "access_levels": {"reader": {"categories": [77]}},
        },
        "roles": [{"id": "reader", "access_level": "reader"}],
        "actors": [{"id": "sample-reader", "role": "reader"}],
    }
    org_path = tmp_path / "org.yaml"
    org_path.write_text(yaml.safe_dump(org), encoding="utf-8")
    content = b"Fictional catalog-protected document"
    digest = hashlib.sha256(content).hexdigest()
    blob = tmp_path / "blobs" / digest[:2] / digest
    blob.parent.mkdir(parents=True)
    blob.write_bytes(content)
    data = empty_manifest("sample-org", "docs", root_mac("sample-org", "", "docs"))
    parent = data["manifest"]["rootMac"]
    mac = doc_version_mac_from_hash(parent, None, "record.txt", digest)
    urn = "urn:sample-org:doc:record.txt"
    data["nodes"] = [
        {
            "urn": urn,
            "mac": mac,
            "parentMac": parent,
            "kind": "doc",
            "slug": "record.txt",
            "category": category,
            "contentHash": digest,
            "size": len(content),
            "owners": ["reader"],
            "locations": [{"backend": "local", "path": str(blob)}],
            "meta": {},
            "relations": {},
            "previous": None,
        }
    ]
    data["currentVersions"][urn] = mac
    data["manifest"]["headMac"] = mac
    (tmp_path / "manifest.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    args = [
        "get",
        "record.txt",
        "--cat",
        "--org-yaml",
        str(org_path),
        "--actor",
        "sample-reader",
        "--root",
        str(tmp_path),
    ]
    refused = CliRunner().invoke(main, args)
    assert refused.exit_code != 0
    assert "denied" in refused.output
    assert content not in refused.stdout_bytes
    # Declaring the same category restores this otherwise valid direct grant.
    org["policies"]["security_categories"][category] = {"label": "Sample"}
    org_path.write_text(yaml.safe_dump(org), encoding="utf-8")
    allowed = CliRunner().invoke(main, args)
    assert allowed.exit_code == 0, allowed.output
    assert content in allowed.stdout_bytes
