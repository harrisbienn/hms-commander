"""Owned native process trees with bounded diagnostics (no engine semantics)."""

import contextlib
import ctypes
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

_NATIVE_ENVIRONMENT = frozenset(
    {
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATH",
        "PATHEXT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "HOME",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "COMMONPROGRAMFILES",
        "JAVA_HOME",
        "LANG",
        "LC_ALL",
        "TZ",
        "NUMBER_OF_PROCESSORS",
    }
)
_MARKERS = (
    "Computation completed",
    "Finished computing",
    "End script",
    "Exit code 0",
    "SystemExit: 0",
    "ERROR",
    "Error",
    "readf fail",
    "DataFormatException",
    "Failed to create TemporalDataReader",
    "NullPointerException",
)
# The child cannot start native code until the parent has installed its ownership
# boundary. This closes the Popen -> AssignProcessToJobObject escape race.
_LAUNCHER = (
    "import json,subprocess,sys; "
    "args=json.loads(sys.stdin.buffer.readline()); "
    "p=subprocess.Popen(args,stdin=subprocess.DEVNULL); "
    "sys.exit(p.wait())"
)


def native_environment(additions=None):
    """Keep required OS/runtime settings; additions are explicit caller policy."""
    result = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _NATIVE_ENVIRONMENT
    }
    if additions:
        for key, value in additions.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise TypeError("Native environment entries must be strings")
            if not key or "=" in key or "\0" in key or "\0" in value:
                raise ValueError("Invalid native environment entry")
            result[key] = value
    return result


def dss_environment():
    """Pass only explicit DSS trust policy in addition to native necessities."""
    return native_environment(
        {
            key: os.environ[key]
            for key in ("HMS_DSS_TRUST_MANIFEST", "HMS_DSS_ALLOW_DOWNLOADS")
            if key in os.environ
        }
    )


class _WindowsJob:
    """A kill-on-close job assigned before the launcher receives its command."""

    def __init__(self, process):
        from ctypes import wintypes

        class BasicLimit(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_longlong),
                ("job_time", ctypes.c_longlong),
                ("flags", wintypes.DWORD),
                ("min_working", ctypes.c_size_t),
                ("max_working", ctypes.c_size_t),
                ("active_limit", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class Limits(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimit),
                ("io", ctypes.c_ulonglong * 6),
                ("memory", ctypes.c_size_t * 4),
            ]

        class Accounting(ctypes.Structure):
            _fields_ = [
                ("times", ctypes.c_longlong * 4),
                ("faults", wintypes.DWORD),
                ("total", wintypes.DWORD),
                ("active", wintypes.DWORD),
                ("terminated", wintypes.DWORD),
            ]

        self.accounting = Accounting
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": (
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
                wintypes.BOOL,
            ),
            "AssignProcessToJobObject": (
                [wintypes.HANDLE, wintypes.HANDLE],
                wintypes.BOOL,
            ),
            "QueryInformationJobObject": (
                [
                    wintypes.HANDLE,
                    ctypes.c_int,
                    ctypes.c_void_p,
                    wintypes.DWORD,
                    ctypes.c_void_p,
                ],
                wintypes.BOOL,
            ),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            method = getattr(self.api, name)
            method.argtypes, method.restype = args, result
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            limits = Limits()
            limits.basic.flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.api.SetInformationJobObject(
                self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise

    def terminate(self):
        if not self.api.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def active(self):
        info = self.accounting()
        if not self.api.QueryInformationJobObject(
            self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.active

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


@dataclass
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    quiescent: bool
    markers: dict
    evidence_path: Path


class _Capture:
    def __init__(self, path, log_limit, tail_limit):
        self.path, self.log_limit, self.tail_limit = path, log_limit, tail_limit
        self.total = 0
        self.retained = 0
        self.tail = b""
        self.markers = set()
        self.error = None

    def drain(self, pipe):
        overlap = b""
        try:
            with self.path.open("xb") as log:
                while chunk := pipe.read1(65536):
                    sample = overlap + chunk
                    for marker in _MARKERS:
                        if marker.encode() in sample:
                            self.markers.add(marker)
                    overlap = sample[-128:]
                    self.total += len(chunk)
                    keep = chunk[: max(0, self.log_limit - self.retained)]
                    log.write(keep)
                    log.flush()
                    self.retained += len(keep)
                    self.tail = (self.tail + chunk)[-self.tail_limit :]
        except (OSError, ValueError) as error:
            self.error = str(error)
        finally:
            pipe.close()

    def text(self):
        prefix = (
            "[output truncated; see process evidence]\n"
            if self.total > len(self.tail)
            else ""
        )
        return prefix + self.tail.decode("utf-8", errors="replace")


def run_owned(
    command,
    *,
    cwd,
    env,
    timeout,
    output_dir,
    log_limit=8 * 1024 * 1024,
    tail_limit=1024 * 1024,
):
    """Run a gated launcher; return only after owned descendants are quiescent.

    Windows jobs also terminate descendants if this supervisor dies. POSIX uses
    a dedicated session; detached descendants are outside that platform policy.
    """
    if timeout <= 0 or log_limit <= 0 or tail_limit <= 0:
        raise ValueError("Timeout and diagnostic byte limits must be positive")
    run_dir = Path(output_dir) / ("hms-process-" + uuid.uuid4().hex)
    run_dir.mkdir(parents=True)
    captures = [
        _Capture(run_dir / (name + ".log"), log_limit, tail_limit)
        for name in ("stdout", "stderr")
    ]
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", _LAUNCHER],
        cwd=str(cwd),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        start_new_session=os.name != "nt",
    )
    job = None
    threads = []
    timed_out = False
    quiescent = False
    interruption = None
    cleanup_error = None
    try:
        if os.name == "nt":
            job = _WindowsJob(process)  # Failure refuses launch, never falls back.
        for capture, pipe in zip(captures, (process.stdout, process.stderr)):
            thread = threading.Thread(target=capture.drain, args=(pipe,), daemon=True)
            thread.start()
            threads.append(thread)
        process.stdin.write(json.dumps(list(command)).encode("utf-8") + b"\n")
        process.stdin.close()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
    except BaseException as error:
        interruption = error
    finally:
        try:
            if not process.stdin.closed:
                process.stdin.close()
        except OSError as error:
            # Closing a buffered pipe can flush and fail if the launcher exited.
            # Cleanup and its evidence must still run after that failure.
            cleanup_error = str(error)
        try:
            if job is not None:
                job.terminate()
                deadline = time.monotonic() + 10
                while job.active() and time.monotonic() < deadline:
                    time.sleep(0.01)
                quiescent = job.active() == 0
            elif os.name != "nt":
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                quiescent = True  # Owned non-detached process group only.
            else:
                process.kill()  # Gated launcher never received native command.
                quiescent = True
        except (OSError, subprocess.TimeoutExpired) as error:
            cleanup_error = str(error)
            quiescent = False
        finally:
            if job is not None:
                job.close()
        # Reap the launcher even if job termination/accounting failed. Closing
        # the job above is the final kill-on-close fallback, not proof of a
        # successful accounting check.
        try:
            process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired) as error:
            cleanup_error = str(error)
            quiescent = False
        for thread in threads:
            thread.join(timeout=10)
        if not threads:
            for pipe in (process.stdout, process.stderr):
                try:
                    pipe.close()
                except OSError as error:
                    cleanup_error = str(error)
        if any(thread.is_alive() for thread in threads):
            quiescent = False
        evidence_path = run_dir / "process.json"
        evidence = {
            "format_version": 1,
            "supervisor_pid": os.getpid(),
            "launcher_pid": process.pid,
            "returncode": process.returncode,
            "timed_out": timed_out,
            "interrupted": interruption is not None,
            "quiescent": quiescent,
            "ownership": "windows-job" if os.name == "nt" else "posix-process-group",
            "cleanup_error": cleanup_error,
            "streams": {
                name: {
                    "total_bytes": capture.total,
                    "retained_bytes": capture.retained,
                    "truncated": capture.total > capture.retained,
                    "markers": sorted(capture.markers),
                    "read_error": capture.error,
                }
                for name, capture in zip(("stdout", "stderr"), captures)
            },
        }
        evidence_path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    if not quiescent or any(capture.error for capture in captures):
        raise RuntimeError(
            f"Native process cleanup or diagnostics failed; retry is unsafe: {evidence_path}"
        ) from interruption
    if interruption is not None:
        raise interruption
    if cleanup_error:
        raise RuntimeError(f"Native process stream cleanup failed: {evidence_path}")
    return ProcessResult(
        process.returncode,
        captures[0].text(),
        captures[1].text(),
        timed_out,
        quiescent,
        {
            name: capture.markers
            for name, capture in zip(("stdout", "stderr"), captures)
        },
        evidence_path,
    )
