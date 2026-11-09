"""Portable process fixtures; these tests never launch Java or HMS."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from hms_commander._process import native_environment, run_owned


def run_fixture(tmp_path, source, **kwargs):
    return run_owned(
        [sys.executable, "-I", "-c", source],
        cwd=tmp_path,
        env=native_environment(),
        timeout=10,
        output_dir=tmp_path,
        **kwargs,
    )


def test_environment_excludes_credentials_and_java_injection(monkeypatch, tmp_path):
    monkeypatch.setenv("HMS_SYNTHETIC_SECRET", "sentinel-not-a-real-secret")
    monkeypatch.setenv("JAVA_TOOL_OPTIONS", "-Dunsafe=true")
    outcome = run_fixture(
        tmp_path,
        "import os,json; print(json.dumps({"
        "'sentinel_present':'HMS_SYNTHETIC_SECRET' in os.environ,"
        "'injection_present':'JAVA_TOOL_OPTIONS' in os.environ,"
        "'systemroot_present':any(k.upper()=='SYSTEMROOT' for k in os.environ)}))",
    )
    env = json.loads(outcome.stdout)
    assert not env["sentinel_present"]
    assert not env["injection_present"]
    if os.name == "nt":
        assert env["systemroot_present"]
    assert outcome.quiescent and outcome.returncode == 0


def test_dss_children_receive_explicit_trust_policy_without_ambient_secrets(
    monkeypatch, tmp_path
):
    from hms_commander._process import dss_environment

    monkeypatch.setenv("HMS_DSS_TRUST_MANIFEST", "fixture-approved-trust.json")
    monkeypatch.setenv("HMS_DSS_ALLOW_DOWNLOADS", "0")
    monkeypatch.setenv("HMS_SYNTHETIC_SECRET", "fixture-only-secret")
    outcome = run_owned(
        [
            sys.executable,
            "-I",
            "-c",
            "import os,json; print(json.dumps({"
            "'trust':os.environ.get('HMS_DSS_TRUST_MANIFEST')=='fixture-approved-trust.json',"
            "'download_policy':os.environ.get('HMS_DSS_ALLOW_DOWNLOADS')=='0',"
            "'secret_absent':'HMS_SYNTHETIC_SECRET' not in os.environ}))",
        ],
        cwd=tmp_path,
        env=dss_environment(),
        timeout=10,
        output_dir=tmp_path,
    )
    assert json.loads(outcome.stdout) == {
        "trust": True,
        "download_policy": True,
        "secret_absent": True,
    }


def test_output_limits_preserve_full_stream_markers(tmp_path):
    outcome = run_fixture(
        tmp_path,
        "import sys; print('readf fail'); sys.stdout.write('x'*2000000); "
        "sys.stderr.write('y'*2000000); print('Computation completed')",
        log_limit=4096,
        tail_limit=2048,
    )
    assert len(outcome.stdout) < 2200 and len(outcome.stderr) < 2200
    assert "readf fail" not in outcome.stdout
    assert {"readf fail", "Computation completed"} <= outcome.markers["stdout"]
    evidence = json.loads(outcome.evidence_path.read_text())
    assert evidence["streams"]["stdout"]["total_bytes"] > 2000000
    assert all(stream["truncated"] for stream in evidence["streams"].values())
    assert all(
        path.stat().st_size == 4096
        for path in outcome.evidence_path.parent.glob("*.log")
    )


def test_timeout_owns_grandchild_but_preserves_unrelated_process(tmp_path):
    heartbeat = tmp_path / "heartbeat"
    grandchild = (
        "import pathlib,time; p=pathlib.Path(" + repr(str(heartbeat)) + "); "
        "\nwhile True:\n p.write_text(str(time.time())); time.sleep(.03)"
    )
    child = (
        "import subprocess,sys,time; subprocess.Popen([sys.executable,'-I','-c',"
        + repr(grandchild)
        + "]); time.sleep(60)"
    )
    unrelated = subprocess.Popen(
        [sys.executable, "-I", "-c", "import time; time.sleep(60)"]
    )
    try:
        result = run_owned(
            [sys.executable, "-I", "-c", child],
            cwd=tmp_path,
            env=native_environment(),
            timeout=1.5,
            output_dir=tmp_path,
        )
        assert result.timed_out and result.quiescent
        before = heartbeat.read_text()
        time.sleep(0.15)
        assert heartbeat.read_text() == before
        assert unrelated.poll() is None
        evidence = json.loads(result.evidence_path.read_text())
        assert evidence["quiescent"] and evidence["timed_out"]
    finally:
        unrelated.kill()
        unrelated.wait(timeout=10)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job lifetime guarantee")
def test_supervisor_death_kills_owned_job(tmp_path):
    heartbeat = tmp_path / "heartbeat"
    started = tmp_path / "started"
    package_root = Path(__file__).resolve().parents[1]
    child = (
        "import pathlib,time; p=pathlib.Path(" + repr(str(heartbeat)) + "); "
        "\nwhile True:\n p.write_text(str(time.time())); time.sleep(.03)"
    )
    source = (
        "import sys,pathlib; sys.path.insert(0," + repr(str(package_root)) + "); "
        "from hms_commander._process import run_owned,native_environment; "
        "pathlib.Path(" + repr(str(started)) + ").touch(); "
        "run_owned([sys.executable,'-I','-c',"
        + repr(child)
        + "],cwd="
        + repr(str(tmp_path))
        + ",env=native_environment(),timeout=60,output_dir="
        + repr(str(tmp_path))
        + ")"
    )
    supervisor = subprocess.Popen(
        [sys.executable, "-I", "-c", source],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while (
            not heartbeat.exists()
            and supervisor.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.03)
        assert heartbeat.exists()
        supervisor.kill()
        supervisor.wait(timeout=10)
        time.sleep(0.15)
        before = heartbeat.read_text()
        time.sleep(0.15)
        assert heartbeat.read_text() == before
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.wait(timeout=10)


def test_native_errors_in_either_stream_reject_success():
    from hms_commander import HmsJython

    for marker in (
        "readf fail",
        "DataFormatException",
        "Failed to create TemporalDataReader",
        "NullPointerException",
    ):
        for stream in ("stdout", "stderr"):
            markers = {"stdout": {"Computation completed"}, "stderr": {"SystemExit: 0"}}
            markers[stream].add(marker)
            assert HmsJython._native_failure(markers)


def test_interruption_records_cleanup_before_propagating(tmp_path, monkeypatch):
    original_wait = subprocess.Popen.wait
    interrupted = []

    def wait(process, *args, **kwargs):
        if not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt()
        return original_wait(process, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "wait", wait)
    with pytest.raises(KeyboardInterrupt):
        run_fixture(tmp_path, "import time; time.sleep(60)")
    evidence = json.loads(next(tmp_path.glob("hms-process-*/process.json")).read_text())
    assert evidence["interrupted"] and evidence["quiescent"]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job assignment boundary")
def test_failed_job_assignment_never_launches_native_command(tmp_path, monkeypatch):
    from hms_commander import _process

    def refuse(process):
        raise OSError("fixture assignment refusal")

    monkeypatch.setattr(_process, "_WindowsJob", refuse)
    sentinel = tmp_path / "should-not-exist"
    with pytest.raises(OSError, match="assignment refusal"):
        run_fixture(
            tmp_path,
            "import pathlib; pathlib.Path(" + repr(str(sentinel)) + ").touch()",
        )
    assert not sentinel.exists()
    evidence = json.loads(next(tmp_path.glob("hms-process-*/process.json")).read_text())
    assert evidence["interrupted"] and evidence["quiescent"]


def test_stdin_close_failure_still_terminates_and_records_evidence(
    tmp_path, monkeypatch
):
    from hms_commander import _process

    launch = subprocess.Popen
    processes = []
    pipes = []

    class FailingClose:
        def __init__(self, pipe):
            self.pipe = pipe

        @property
        def closed(self):
            return self.pipe.closed

        def write(self, data):
            return self.pipe.write(data)

        def close(self):
            raise BrokenPipeError("injected close failure")

    def failing_launch(*args, **kwargs):
        process = launch(*args, **kwargs)
        pipes.append(process.stdin)
        process.stdin = FailingClose(process.stdin)
        processes.append(process)
        return process

    monkeypatch.setattr(_process.subprocess, "Popen", failing_launch)
    try:
        with pytest.raises(BrokenPipeError, match="injected"):
            run_fixture(tmp_path, "import time; time.sleep(60)")
        assert len(processes) == 1 and processes[0].poll() is not None
        evidence = json.loads(
            next(tmp_path.glob("hms-process-*/process.json")).read_bytes()
        )
        assert evidence["quiescent"] and evidence["interrupted"]
        assert "injected" in evidence["cleanup_error"]
    finally:
        for pipe in pipes:
            try:
                pipe.close()
            except OSError:
                pass


@pytest.mark.skipif(os.name != "nt", reason="Windows Job accounting boundary")
def test_failed_quiescence_check_is_not_masked_by_interruption(tmp_path, monkeypatch):
    from hms_commander import _process

    original_wait = subprocess.Popen.wait
    interrupted = []

    def wait(process, *args, **kwargs):
        if not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt()
        return original_wait(process, *args, **kwargs)

    def refuse_accounting(job):
        raise OSError("injected accounting failure")

    monkeypatch.setattr(subprocess.Popen, "wait", wait)
    monkeypatch.setattr(_process._WindowsJob, "active", refuse_accounting)
    with pytest.raises(RuntimeError, match="retry is unsafe") as caught:
        run_fixture(tmp_path, "import time; time.sleep(60)")
    assert isinstance(caught.value.__cause__, KeyboardInterrupt)
    evidence = json.loads(
        next(tmp_path.glob("hms-process-*/process.json")).read_bytes()
    )
    assert not evidence["quiescent"] and evidence["interrupted"]
    assert "accounting failure" in evidence["cleanup_error"]
