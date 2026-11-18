"""No-network archive safety and replacement checks for M3 project intake."""

import hashlib
import io
import json
import stat
import zipfile
from pathlib import Path

import pytest
import requests

from hms_commander import HmsM3Model
from hms_commander import _m3_intake
from hms_commander._parsing import HmsFileParser


def archive_bytes(members):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, content in members:
            archive.writestr(name, content)
    return stream.getvalue()


class Response:
    def __init__(self, content):
        self.content = content
        self.headers = {"content-length": str(len(content))}

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=8192):
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start : start + chunk_size]

    def close(self):
        pass


@pytest.fixture
def serve(monkeypatch):
    monkeypatch.setattr(
        HmsM3Model,
        "get_project_info",
        lambda *args: {
            "relative_path": "D/HEC-HMS/D_D100-00-00",
            "hms_file": "sample.hms",
        },
    )

    def install(members):
        inner = archive_bytes(members)
        outer = archive_bytes([("HEC-HMS/D_D100-00-00.zip", inner)])
        monkeypatch.setattr(requests, "get", lambda *args, **kwargs: Response(outer))
        return outer

    return install


def test_traversal_refuses_without_external_write(tmp_path, serve):
    serve([("wrapper/../escaped.hms", "Project: escaped\nEnd:\n")])
    with pytest.raises(ValueError, match="path"):
        HmsM3Model.extract_project("D", "D100-00-00", tmp_path)
    assert not (tmp_path / "D/escaped.hms").exists()


def test_failed_replacement_download_preserves_original(tmp_path, serve, monkeypatch):
    serve([("wrapper/sample.hms", "Project: sample\nEnd:\n")])
    destination = tmp_path / "D/D100-00-00"
    destination.mkdir(parents=True)
    (destination / "original.hms").write_bytes(b"retained original")

    def failed(*args, **kwargs):
        raise requests.ConnectionError("injected acquisition failure")

    monkeypatch.setattr(requests, "get", failed)
    with pytest.raises(requests.ConnectionError):
        HmsM3Model.extract_project("D", "D100-00-00", tmp_path, overwrite=True)
    assert (destination / "original.hms").read_bytes() == b"retained original"


@pytest.mark.parametrize("wrapper", ["wrapper/", ""])
def test_valid_projects_preserve_nested_files_and_parse(tmp_path, serve, wrapper):
    serve(
        [
            (wrapper + "sample.hms", "Project: Sample\n     Version: 3.3\nEnd:\n"),
            (wrapper + "data/rain.txt", "retained rainfall"),
            (wrapper + "sample.basin", "Basin: Sample\nEnd:\n"),
        ]
    )
    destination = HmsM3Model.extract_project("D", "D100-00-00", str(tmp_path))
    assert destination == tmp_path / "D/D100-00-00"
    assert (destination / "data/rain.txt").read_text() == "retained rainfall"
    content = HmsFileParser.read_file(destination / "sample.hms")
    assert HmsFileParser.parse_blocks(content, "Project")["Sample"]["Version"] == "3.3"
    receipts = list((tmp_path / "D").glob(".D100-00-00.intake-*/intake.json"))
    receipt = json.loads(receipts[0].read_text())
    assert receipt["status"] == "complete"
    assert receipt["project"]["file_count"] == 3
    assert receipt["previous"] is None


@pytest.mark.parametrize(
    "name",
    [
        "wrapper/../outside.hms",
        "../wrapper/sample.hms",
        "/absolute/sample.hms",
        "C:/drive/sample.hms",
        "wrapper/C:drive.hms",
        "wrapper/sub/file.hms:stream",
        "wrapper\\..\\outside.hms",
        "wrapper/CON.hms",
        "wrapper/NUL",
        "wrapper/file. ",
        "wrapper/./sample.hms",
        "wrapper//sample.hms",
    ],
)
def test_unsafe_names_are_rejected_before_project_writes(tmp_path, serve, name):
    serve([(name, "Project: sample\n")])
    with pytest.raises(ValueError, match="path"):
        HmsM3Model.extract_project("D", "D100-00-00", tmp_path)
    assert not (tmp_path / "D/D100-00-00").exists()
    assert not list(tmp_path.rglob("*.hms"))


@pytest.mark.parametrize(
    "members",
    [
        [("wrapper/sample.hms", "a"), ("wrapper/sample.hms", "b")],
        [("wrapper/sample.hms", "a"), ("wrapper/SAMPLE.hms", "b")],
        [("wrapper/sample.hms", "a"), ("wrapper/sample.hms/file", "b")],
        [("wrapper/A/sample.hms", "a"), ("wrapper/a/other.txt", "b")],
        [("wrapper/caf\u00e9/sample.hms", "a"), ("wrapper/cafe\u0301/other.txt", "b")],
    ],
)
def test_colliding_files_and_directory_aliases_refuse(tmp_path, serve, members):
    serve(members)
    with pytest.raises(ValueError, match="[Cc]olli|[Aa]lias"):
        HmsM3Model.extract_project("D", "D100-00-00", tmp_path)
    assert not (tmp_path / "D/D100-00-00").exists()


@pytest.mark.parametrize("mode", [stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o600])
def test_links_and_special_members_refuse(tmp_path, serve, mode):
    member = zipfile.ZipInfo("wrapper/sample.hms")
    member.create_system = 3
    member.external_attr = mode << 16
    serve([(member, "outside")])
    with pytest.raises(ValueError, match="links or special"):
        HmsM3Model.extract_project("D", "D100-00-00", tmp_path)


@pytest.mark.parametrize(
    "limits,match",
    [
        ({"max_archive_bytes": 10}, "max_archive_bytes"),
        ({"max_uncompressed_bytes": 2}, "max_uncompressed_bytes"),
        ({"max_members": 1}, "max_members"),
        ({"max_path_depth": 1}, "max_path_depth"),
        ({"max_members": True}, "positive integer"),
        ({"max_archive_bytes": 0}, "positive integer"),
    ],
)
def test_configured_budgets_refuse_before_publication(tmp_path, serve, limits, match):
    serve(
        [("wrapper/sample.hms", "Project: sample\n"), ("wrapper/data/rain.txt", "rain")]
    )
    with pytest.raises(ValueError, match=match):
        HmsM3Model.extract_project("D", "D100-00-00", tmp_path, **limits)
    assert not (tmp_path / "D/D100-00-00").exists()


def test_actual_download_bytes_are_bounded_without_content_length(
    tmp_path, serve, monkeypatch
):
    outer = serve([("wrapper/sample.hms", "Project: sample\n")])
    response = Response(outer)
    response.headers = {}
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response)
    with pytest.raises(ValueError, match="max_archive_bytes"):
        HmsM3Model.extract_project("D", "D100-00-00", tmp_path, max_archive_bytes=20)
    assert not (tmp_path / "D/D100-00-00").exists()


def _prior(tmp_path):
    destination = tmp_path / "D/D100-00-00"
    destination.mkdir(parents=True)
    (destination / "original.hms").write_bytes(b"retained original")
    return destination


def test_replacement_inventories_and_retains_prior_copy(tmp_path, serve, monkeypatch):
    serve([("wrapper/sample.hms", "Project: replacement\n")])
    destination = _prior(tmp_path)
    rename = Path.rename

    def checked_rename(source, target):
        if source == destination:
            receipt = json.loads((target.parent / "intake.json").read_text())
            assert receipt["status"] == "prepared"
            assert receipt["previous"] == {
                "file_count": 1,
                "bytes": 17,
                "files": [
                    {
                        "path": "original.hms",
                        "bytes": 17,
                        "sha256": hashlib.sha256(b"retained original").hexdigest(),
                    }
                ],
            }
        return rename(source, target)

    monkeypatch.setattr(Path, "rename", checked_rename)
    assert (
        HmsM3Model.extract_project("D", "D100-00-00", tmp_path, overwrite=True)
        == destination
    )
    previous = list((tmp_path / "D").glob(".D100-00-00.intake-*/previous/original.hms"))
    assert len(previous) == 1 and previous[0].read_bytes() == b"retained original"
    assert (destination / "sample.hms").read_text() == "Project: replacement\n"


@pytest.mark.parametrize("phase", ["corrupt", "extraction", "publication"])
def test_failure_preserves_or_restores_prior_copy(tmp_path, serve, monkeypatch, phase):
    serve([("wrapper/sample.hms", "Project: replacement\n")])
    destination = _prior(tmp_path)
    if phase == "corrupt":
        monkeypatch.setattr(
            requests, "get", lambda *args, **kwargs: Response(b"not a ZIP")
        )
    elif phase == "extraction":
        copy = _m3_intake._copy

        def interrupted(source, target, limit):
            if str(target.name).endswith(".hms"):
                target.write(b"partial")
                raise OSError("injected extraction failure")
            return copy(source, target, limit)

        monkeypatch.setattr(_m3_intake, "_copy", interrupted)
    else:
        rename = Path.rename

        def interrupted(source, target):
            if source.name == "project":
                raise OSError("injected publication failure")
            return rename(source, target)

        monkeypatch.setattr(Path, "rename", interrupted)
    with pytest.raises((zipfile.BadZipFile, OSError)):
        HmsM3Model.extract_project("D", "D100-00-00", tmp_path, overwrite=True)
    assert (destination / "original.hms").read_bytes() == b"retained original"
    assert not (destination / "sample.hms").exists()


def test_failed_rollback_retains_both_copies(tmp_path, serve, monkeypatch):
    serve([("wrapper/sample.hms", "Project: replacement\n")])
    _prior(tmp_path)
    rename = Path.rename

    def interrupted(source, target):
        if source.name in {"project", "previous"}:
            raise OSError("injected rename failure")
        return rename(source, target)

    monkeypatch.setattr(Path, "rename", interrupted)
    with pytest.raises(RuntimeError, match="retained prior project"):
        HmsM3Model.extract_project("D", "D100-00-00", tmp_path, overwrite=True)
    attempts = list((tmp_path / "D").glob(".D100-00-00.intake-*"))
    assert (attempts[0] / "previous/original.hms").read_bytes() == b"retained original"
    assert (attempts[0] / "project/sample.hms").read_text() == "Project: replacement\n"


def test_missing_project_does_not_replace_prior(tmp_path, serve):
    serve([("wrapper/file.txt", "no project")])
    destination = _prior(tmp_path)
    with pytest.raises(ValueError, match="no nonempty .hms"):
        HmsM3Model.extract_project("D", "D100-00-00", tmp_path, overwrite=True)
    assert (destination / "original.hms").read_bytes() == b"retained original"


def test_existing_project_reuse_does_not_download(tmp_path, monkeypatch, serve):
    serve([("wrapper/sample.hms", "Project: replacement\n")])
    destination = _prior(tmp_path)
    monkeypatch.setattr(
        requests, "get", lambda *args, **kwargs: pytest.fail("unexpected network")
    )
    assert HmsM3Model.extract_project("D", "D100-00-00", tmp_path) == destination


def test_existing_lock_refuses_without_stealing_or_downloading(
    tmp_path, monkeypatch, serve
):
    serve([("wrapper/sample.hms", "Project: replacement\n")])
    destination = _prior(tmp_path)
    lock = destination.parent / ".D100-00-00.intake.lock"
    lock.write_text("retained owner")
    monkeypatch.setattr(
        requests, "get", lambda *args, **kwargs: pytest.fail("unexpected network")
    )
    with pytest.raises(RuntimeError, match="intake lock exists"):
        HmsM3Model.extract_project("D", "D100-00-00", tmp_path, overwrite=True)
    assert lock.read_text() == "retained owner"
    assert (destination / "original.hms").read_bytes() == b"retained original"


def test_inner_crc_failure_does_not_publish(tmp_path, serve, monkeypatch):
    inner = archive_bytes([("wrapper/sample.hms", b"Project: CRC fixture\n")])
    inner = inner.replace(b"Project: CRC fixture\n", b"Project: BAD fixture\n")
    outer = archive_bytes([("HEC-HMS/D_D100-00-00.zip", inner)])
    serve([])
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: Response(outer))
    with pytest.raises(zipfile.BadZipFile, match="CRC"):
        HmsM3Model.extract_project("D", "D100-00-00", tmp_path)
    assert not (tmp_path / "D/D100-00-00").exists()


def test_nul_original_member_name_refuses(tmp_path, serve, monkeypatch):
    # Patch both name fields, then recompute the outer CRC when wrapping it.
    inner = archive_bytes([("wrapper/sample!.hms", b"Project: sample\n")])
    inner = inner.replace(b"sample!.hms", b"sample\0.hms")
    outer = archive_bytes([("HEC-HMS/D_D100-00-00.zip", inner)])
    serve([])
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: Response(outer))
    with pytest.raises(ValueError, match="path"):
        HmsM3Model.extract_project("D", "D100-00-00", tmp_path)
    assert not (tmp_path / "D/D100-00-00").exists()


def test_ambiguous_inner_archives_refuse(tmp_path, serve, monkeypatch):
    inner = archive_bytes([("wrapper/sample.hms", "Project: sample\n")])
    outer = archive_bytes(
        [("HEC-HMS/D_D100-00-00.zip", inner), ("HEC-HMS/D100-00-00.zip", inner)]
    )
    serve([])
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: Response(outer))
    with pytest.raises(ValueError, match="Ambiguous"):
        HmsM3Model.extract_project("D", "D100-00-00", tmp_path)


def test_outer_zip_link_refuses(tmp_path, serve, monkeypatch):
    inner = archive_bytes([("wrapper/sample.hms", "Project: sample\n")])
    link = zipfile.ZipInfo("HEC-HMS/D_D100-00-00.zip")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    outer = archive_bytes([(link, inner)])
    serve([])
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: Response(outer))
    with pytest.raises(ValueError, match="links or special"):
        HmsM3Model.extract_project("D", "D100-00-00", tmp_path)
