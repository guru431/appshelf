"""Real JDK verification, separate from synthetic metadata tests; no network or production data."""
import hashlib
import io
import json
import shutil
import subprocess
import zipfile

import pytest

from appshelf.android import fdroid_index as index, net

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


def run(*args):
    return subprocess.run(args, capture_output=True, check=True, timeout=30)


@pytest.fixture(scope="module")
def signed_catalog(tmp_path_factory):
    if not all(shutil.which(x) for x in ("java", "keytool", "jarsigner")):
        pytest.skip("JDK required")
    folder = tmp_path_factory.mktemp("real-fdroid-signature")
    key, jar = folder / "test.p12", folder / "index.jar"
    run("keytool", "-genkeypair", "-alias", "index", "-keystore", str(key), "-storepass", "test-password",
        "-keypass", "test-password", "-keyalg", "RSA", "-keysize", "2048", "-validity", "3650", "-dname", "CN=Test")
    data = {"timestamp": 1800000000000,
            "index": {"name": "/index-v2.json", "sha256": "0" * 64, "size": 2}}
    with zipfile.ZipFile(jar, "w") as out:
        out.writestr("entry.json", json.dumps(data))
    run("jarsigner", "-keystore", str(key), "-storepass", "test-password", str(jar), "index")
    cert = run("keytool", "-exportcert", "-alias", "index", "-keystore", str(key), "-storepass", "test-password").stdout
    return jar.read_bytes(), hashlib.sha256(cert).hexdigest()


def test_signed_json_and_pinned_key_are_both_required(signed_catalog, monkeypatch):
    raw, signer = signed_catalog
    monkeypatch.setattr(index, "REPO_SIGNER", signer)
    index.verify(raw)
    monkeypatch.setattr(index, "REPO_SIGNER", "0" * 64)
    with pytest.raises(net.SourceError, match="подпись"):
        index.verify(raw)


def test_each_repository_uses_its_own_pinned_key(signed_catalog):
    raw, signer = signed_catalog
    index.verify(raw, signer)
    with pytest.raises(net.SourceError, match="подпись"):
        index.verify(raw, index.IZZY_SIGNER)


def test_modified_or_unsigned_json_is_rejected(signed_catalog, monkeypatch):
    raw, signer = signed_catalog
    monkeypatch.setattr(index, "REPO_SIGNER", signer)
    for unsigned in (False, True):
        out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(raw)) as original, zipfile.ZipFile(out, "w") as changed:
            for entry in original.infolist():
                if unsigned and entry.filename.startswith("META-INF/"):
                    continue
                body = original.read(entry)
                if not unsigned and entry.filename == "entry.json":
                    body = body.replace(b"1800000000000", b"1800000000001")
                changed.writestr(entry, body)
        with pytest.raises(net.SourceError, match="подпись"):
            index.verify(out.getvalue())
