"""Test trust and transfer policy with fixture bytes, never executable downloads."""

import hashlib
import io
import json
import platform
import zipfile

import pytest

from hms_commander.dss._hec_monolith import HecMonolithDownloader


def identity(content):
    return {"sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(
        HecMonolithDownloader,
        "COMMON_JARS",
        [{"artifactId": "fixture", "group": "fixture.group", "version": "1"}],
    )
    native_name = {
        "Windows": "javaHeclib.dll",
        "Linux": "libjavaHeclib.so",
        "Darwin": "libjavaHeclib.dylib",
    }[platform.system()]
    native = b"fixture native bytes, not executable"
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(native_name, native)
    jar_name = "jar/fixture-1.jar"
    jar = b"fixture jar bytes, not executable"
    archive_name = "lib/" + HecMonolithDownloader._filename(
        HecMonolithDownloader.NATIVE_LIBS[platform.system()]
    )
    trust = {
        "format_version": 1,
        "platform": platform.system(),
        "artifacts": {jar_name: identity(jar), "lib/" + native_name: identity(native)},
        "downloads": {
            jar_name: identity(jar),
            archive_name: identity(archive.getvalue()),
        },
    }
    manifest = tmp_path / "approved.json"
    manifest.write_text(json.dumps(trust))
    downloader = HecMonolithDownloader(tmp_path / "cache", trust_manifest=manifest)
    return (
        downloader,
        trust,
        {jar_name: jar, archive_name: archive.getvalue(), "lib/" + native_name: native},
    )


def seed(downloader, payloads, names):
    for name in names:
        path = downloader.cache_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payloads[name])


class Response:
    status_code = 200
    headers = {}

    def __init__(self, content):
        self.raw = io.BytesIO(content)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.raw.close()

    def raise_for_status(self):
        pass


def test_missing_or_cache_local_trust_is_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("HMS_DSS_TRUST_MANIFEST", raising=False)
    with pytest.raises(RuntimeError, match="HMS_DSS_TRUST_MANIFEST"):
        HecMonolithDownloader(tmp_path).is_installed()
    with pytest.raises(ValueError, match="outside"):
        HecMonolithDownloader(
            tmp_path, trust_manifest=tmp_path / "trust.json"
        ).is_installed()


def test_cached_artifacts_verified_on_each_use(runtime):
    downloader, trust, payloads = runtime
    seed(downloader, payloads, trust["artifacts"])
    assert downloader.is_installed()
    assert downloader.get_classpath() == [str(downloader.jar_dir / "fixture-1.jar")]
    (downloader.jar_dir / "fixture-1.jar").write_bytes(
        b"x" * len(payloads["jar/fixture-1.jar"])
    )
    with pytest.raises(ValueError, match="checksum mismatch"):
        downloader.get_classpath()


def test_unexpected_jar_rejected(runtime):
    downloader, trust, payloads = runtime
    seed(downloader, payloads, trust["artifacts"])
    (downloader.jar_dir / "unapproved.jar").write_bytes(b"not allowed")
    with pytest.raises(ValueError, match="Unexpected"):
        downloader.get_classpath()


@pytest.mark.parametrize("filename", ["rogue.so.1", "libgfortran.so.5", "OTHER.SO.12"])
@pytest.mark.parametrize(
    "operation", ["is_installed", "get_library_path", "get_classpath"]
)
def test_unexpected_versioned_shared_library_rejected(runtime, filename, operation):
    downloader, trust, payloads = runtime
    seed(downloader, payloads, trust["artifacts"])
    (downloader.lib_dir / filename).write_bytes(b"unapproved fixture bytes")
    with pytest.raises(ValueError, match="Unexpected executable cache artifact"):
        getattr(downloader, operation)()


def test_sealed_cache_never_fetches(runtime, monkeypatch):
    downloader, _, _ = runtime
    monkeypatch.setattr(
        "requests.get", lambda *a, **k: pytest.fail("Network attempted")
    )
    with pytest.raises(RuntimeError, match="downloads are disabled"):
        downloader.install()


def test_verified_download_and_native_extraction(runtime, monkeypatch):
    downloader, trust, payloads = runtime
    sources = downloader._download_sources()
    by_url = {url: payloads[name] for name, url in sources.items()}

    def get(url, **kwargs):
        assert kwargs["allow_redirects"] is False
        assert kwargs["timeout"] == (5, 1)
        return Response(by_url[url])

    monkeypatch.setattr("requests.get", get)
    downloader.allow_downloads = True
    downloader.install()
    assert downloader.is_installed()
    assert all(
        (downloader.cache_dir / name).read_bytes() == payloads[name]
        for name in trust["artifacts"]
    )
    assert not list(downloader.cache_dir.rglob(".download-*"))
    assert not list(downloader.cache_dir.rglob(".extract-*"))


@pytest.mark.parametrize(
    "failure", ["oversize", "corrupt", "interrupted", "redirect", "deadline"]
)
def test_failed_transfer_never_publishes_and_retry_succeeds(
    runtime, monkeypatch, failure
):
    downloader, _, payloads = runtime
    downloader.allow_downloads = True
    content = payloads["jar/fixture-1.jar"]
    response = Response(content)
    if failure == "oversize":
        response.raw = io.BytesIO(content + b"extra")
    elif failure == "corrupt":
        response.raw = io.BytesIO(b"x" * len(content))
    elif failure == "interrupted":

        class Broken:
            def read1(self, size):
                raise OSError("fixture interruption")

            def close(self):
                pass

        response.raw = Broken()
    elif failure == "redirect":
        response.status_code = 302
    else:
        downloader.DOWNLOAD_SECONDS = 0
    monkeypatch.setattr("requests.get", lambda *a, **k: response)
    name, url = next(iter(downloader._download_sources().items()))
    with pytest.raises((ValueError, OSError, TimeoutError)):
        downloader.download_file(url, downloader.cache_dir / name)
    assert not (downloader.cache_dir / name).exists()
    assert not list(downloader.jar_dir.glob(".download-*"))
    downloader.DOWNLOAD_SECONDS = 300
    monkeypatch.setattr("requests.get", lambda *a, **k: Response(content))
    assert (
        downloader.download_file(url, downloader.cache_dir / name).read_bytes()
        == content
    )


@pytest.mark.parametrize("member", ["../escape.dll", "JAVAHECLIB.DLL", "extra.dll"])
def test_native_archive_allowlist_refuses_unexpected_members(
    runtime, monkeypatch, member
):
    downloader, trust, payloads = runtime
    archive_name = next(name for name in trust["downloads"] if name.endswith(".zip"))
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(member, b"fixture")
    payloads[archive_name] = archive.getvalue()
    trust["downloads"][archive_name] = identity(archive.getvalue())
    downloader.trust_manifest.write_text(json.dumps(trust))
    seed(downloader, payloads, [archive_name])
    with pytest.raises(ValueError, match="allowlist"):
        downloader.download_native_library()
    assert not (downloader.cache_dir / "escape.dll").exists()


def test_conflicting_cached_transfer_is_not_overwritten(runtime, monkeypatch):
    downloader, _, payloads = runtime
    name, url = next(iter(downloader._download_sources().items()))
    seed(downloader, payloads, [name])
    destination = downloader.cache_dir / name
    destination.write_bytes(b"corrupt")
    monkeypatch.setattr(
        "requests.get", lambda *a, **k: pytest.fail("Network attempted")
    )
    with pytest.raises(ValueError):
        downloader.download_file(url, destination)
    assert destination.read_bytes() == b"corrupt"
