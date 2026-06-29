from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence


class FieldFixError(RuntimeError):
    """Base exception shown to CLI and GUI users."""


class DependencyError(FieldFixError):
    pass


class AnalysisError(FieldFixError):
    pass


class ProcessingError(FieldFixError):
    pass


class AmbiguousSourceError(ProcessingError):
    pass


class OutputCollisionError(ProcessingError):
    pass


class CancelledError(ProcessingError):
    pass


@dataclass(slots=True)
class ProgressDetails:
    current_frame: int | None = None
    total_frames: int | None = None
    fps: float | None = None
    elapsed_seconds: float | None = None
    eta_seconds: float | None = None
    speed: float | None = None


@dataclass(slots=True)
class FFmpegProgressState:
    frame: int | None = None
    fps: float | None = None
    speed: float | None = None

    def feed(self, line: str) -> None:
        key, separator, raw = line.strip().partition("=")
        if not separator:
            return
        try:
            if key == "frame":
                self.frame = int(raw)
            elif key == "fps":
                self.fps = float(raw)
            elif key == "speed":
                self.speed = float(raw.rstrip("x"))
        except ValueError:
            return

    def details(
        self,
        total_frames: int | None,
        elapsed_seconds: float,
    ) -> ProgressDetails:
        measured_fps = self.fps
        if (not measured_fps or measured_fps <= 0) and self.frame and elapsed_seconds > 0:
            measured_fps = self.frame / elapsed_seconds
        eta = None
        if total_frames and self.frame is not None and measured_fps and measured_fps > 0:
            eta = max(0.0, total_frames - self.frame) / measured_fps
        return ProgressDetails(
            current_frame=self.frame,
            total_frames=total_frames,
            fps=measured_fps,
            elapsed_seconds=elapsed_seconds,
            eta_seconds=eta,
            speed=self.speed,
        )


@dataclass(slots=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str
    required_for_analysis: bool = False
    required_for_processing: bool = False


@dataclass(slots=True)
class DoctorReport:
    checks: list[DoctorCheck] = field(default_factory=list)

    @property
    def analysis_ready(self) -> bool:
        return all(check.ok for check in self.checks if check.required_for_analysis)

    @property
    def processing_ready(self) -> bool:
        return all(check.ok for check in self.checks if check.required_for_processing)


def project_root() -> Path:
    if getattr(__import__("sys"), "frozen", False):
        return Path(__import__("sys").executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _which_with_local(name: str, candidates: Iterable[Path]) -> str | None:
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    return shutil.which(name)


@dataclass(slots=True)
class Toolchain:
    ffmpeg: str | None
    ffprobe: str | None
    vspipe: str | None

    @classmethod
    def discover(cls) -> "Toolchain":
        root = project_root()
        runtime_roots = []
        configured_runtime = os.environ.get("DVD_FIELDFIX_RUNTIME")
        if configured_runtime:
            runtime_roots.append(Path(configured_runtime).expanduser())
        runtime_roots.append(root / ".runtime")
        # A development build lives in dist/DVD-FieldFix while its validated
        # runtime lives at the project root. A moved package still prefers its
        # own adjacent .runtime first.
        runtime_roots.extend(parent / ".runtime" for parent in list(root.parents)[:2])
        runtime_roots = list(dict.fromkeys(runtime_roots))
        ffmpeg = os.environ.get("DVD_FIELDFIX_FFMPEG") or _which_with_local(
            "ffmpeg",
            tuple(runtime / "ffmpeg" / "bin" / "ffmpeg.exe" for runtime in runtime_roots)
            + (root / "ffmpeg.exe",),
        )
        ffprobe = os.environ.get("DVD_FIELDFIX_FFPROBE") or _which_with_local(
            "ffprobe",
            tuple(runtime / "ffmpeg" / "bin" / "ffprobe.exe" for runtime in runtime_roots)
            + (root / "ffprobe.exe",),
        )
        vspipe = os.environ.get("DVD_FIELDFIX_VSPIPE") or _which_with_local(
            "vspipe",
            tuple(
                candidate
                for runtime in runtime_roots
                for candidate in (
                    runtime / "vapoursynth-portable" / "vspipe.bat",
                    runtime / "vapoursynth-portable" / "vspipe.exe",
                    runtime / "vapoursynth" / "vspipe.bat",
                    runtime / "vapoursynth" / "vspipe.exe",
                )
            ),
        )
        return cls(ffmpeg=ffmpeg, ffprobe=ffprobe, vspipe=vspipe)

    def require_analysis(self) -> None:
        missing = [name for name, value in (("ffmpeg", self.ffmpeg), ("ffprobe", self.ffprobe)) if not value]
        if missing:
            raise DependencyError(f"Missing dependencies: {', '.join(missing)}")

    def require_qtgmc(self) -> None:
        if not self.vspipe:
            raise DependencyError(
                "VapourSynth/QTGMC is not installed. Run setup_qtgmc.ps1, then run doctor again."
            )

    def require_dotkill(self) -> None:
        self.require_qtgmc()
        ok, detail = self._test_dotkill()
        if not ok:
            raise DependencyError(
                "Dot-crawl cleanup is unavailable: " + detail + ". Run setup_qtgmc.ps1 again."
            )

    def doctor(self, deep_qtgmc: bool = True) -> DoctorReport:
        report = DoctorReport()
        report.checks.append(
            DoctorCheck("ffmpeg", bool(self.ffmpeg), self.ffmpeg or "not found", True, True)
        )
        report.checks.append(
            DoctorCheck("ffprobe", bool(self.ffprobe), self.ffprobe or "not found", True, True)
        )
        if self.ffmpeg:
            filters = run_capture([self.ffmpeg, "-hide_banner", "-filters"], check=False).stdout
            required_filters = ("idet", "fieldmatch", "setfield", "hqdn3d", "crop", "signalstats")
            absent = [item for item in required_filters if item not in filters]
            report.checks.append(
                DoctorCheck(
                    "FFmpeg filters",
                    not absent,
                    "available" if not absent else f"missing: {', '.join(absent)}",
                    True,
                    True,
                )
            )
            encoders = run_capture([self.ffmpeg, "-hide_banner", "-encoders"], check=False).stdout
            required_encoders = ("libx264", "libx265", "ffv1")
            absent = [item for item in required_encoders if item not in encoders]
            report.checks.append(
                DoctorCheck(
                    "encoders",
                    not absent,
                    "libx264, libx265 and FFV1" if not absent else f"missing: {', '.join(absent)}",
                    False,
                    True,
                )
            )
        report.checks.append(
            DoctorCheck(
                "vspipe",
                bool(self.vspipe),
                self.vspipe or "not found; run setup_qtgmc.ps1",
                False,
                True,
            )
        )
        if self.vspipe and deep_qtgmc:
            ok, detail = self._test_qtgmc()
            report.checks.append(DoctorCheck("QTGMC", ok, detail, False, True))
            dotkill_ok, dotkill_detail = self._test_dotkill()
            report.checks.append(
                DoctorCheck("DotKill (optional)", dotkill_ok, dotkill_detail, False, False)
            )
        return report

    def _test_qtgmc(self) -> tuple[bool, str]:
        assert self.vspipe
        script = """\
import vapoursynth as vs
from vapoursynth import core
from vsdeinterlace import QTempGaussMC
clip = core.std.BlankClip(width=720, height=576, format=vs.YUV420P8, length=8, fpsnum=25, fpsden=1)
clip = core.std.SetFrameProps(clip, _FieldBased=2)
matched = core.vivtc.VFM(clip, order=1, field=2, mode=1, micmatch=1)
matched = core.std.SetFrameProps(matched, _FieldBased=0)
matched50 = core.std.Interleave([matched, matched])
qtgmc = QTempGaussMC().source_match(
    mode=QTempGaussMC.SourceMatchMode.BASIC
).sharpen(strength=0)
bobbed50 = qtgmc.bob(clip, tff=True)
bobbed50 = core.std.SetFrameProps(bobbed50, _FieldBased=0)
fallback25 = core.std.SelectEvery(bobbed50, cycle=2, offsets=0)
fallback25 = core.std.SetFrameProps(fallback25, _FieldBased=0)
def choose(n, f):
    return fallback25 if int(f.props.get('_Combed', 0)) else matched
fieldmatched = core.std.FrameEval(
    matched, choose, prop_src=matched, clip_src=[matched, fallback25]
)
fieldmatched.set_output(0)
def choose_hybrid(n, f):
    return bobbed50 if int(f.props.get('_Combed', 0)) else matched50
hybrid = core.std.FrameEval(
    matched50, choose_hybrid, prop_src=matched50, clip_src=[matched50, bobbed50]
)
hybrid.set_output(1)
core.vivtc.VDecimate(fieldmatched).set_output(2)
"""
        with tempfile.TemporaryDirectory(prefix="dvd-fieldfix-doctor-") as directory:
            path = Path(directory) / "doctor.vpy"
            path.write_text(script, encoding="utf-8")
            # Request frames, rather than only evaluating the graph, so callback
            # signatures and lazy QTGMC/VFM execution are genuinely tested.
            results = [
                run_capture(
                    [self.vspipe, "--outputindex", str(index), str(path), "--"],
                    check=False,
                    timeout=120,
                )
                for index in range(3)
            ]
        combined = "\n".join(result.stdout + "\n" + result.stderr for result in results).strip()
        if all(result.returncode == 0 for result in results):
            return True, "QTempGaussMC, VFM, VDecimate and conditional/hybrid pipelines loaded"
        tail = "\n".join(combined.splitlines()[-5:])
        codes = ", ".join(str(result.returncode) for result in results)
        return False, tail or f"vspipe exited with codes {codes}"

    def _test_dotkill(self) -> tuple[bool, str]:
        assert self.vspipe
        script = """\
import vapoursynth as vs
from vapoursynth import core
clip = core.std.BlankClip(width=720, height=576, format=vs.YUV420P8, length=2)
core.dotkill.DotKillS(clip, iterations=1).set_output()
"""
        with tempfile.TemporaryDirectory(prefix="dvd-fieldfix-dotkill-") as directory:
            path = Path(directory) / "dotkill.vpy"
            path.write_text(script, encoding="utf-8")
            result = run_capture([self.vspipe, str(path), "--"], check=False, timeout=60)
        if result.returncode == 0:
            return True, "DotKillS spatial cleanup loaded"
        tail = "\n".join((result.stdout + "\n" + result.stderr).strip().splitlines()[-5:])
        return False, tail or f"vspipe exited with code {result.returncode}"


@dataclass(slots=True)
class CaptureResult:
    args: Sequence[str]
    returncode: int
    stdout: str
    stderr: str


def _startup_kwargs() -> dict[str, object]:
    if os.name != "nt":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {"startupinfo": startup, "creationflags": subprocess.CREATE_NO_WINDOW}


def run_capture(
    args: Sequence[str | os.PathLike[str]],
    *,
    check: bool = True,
    timeout: float | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> CaptureResult:
    command = [str(arg) for arg in args]
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=cwd,
        **_startup_kwargs(),
    )
    result = CaptureResult(command, completed.returncode, completed.stdout, completed.stderr)
    if check and completed.returncode:
        tail = "\n".join(completed.stderr.splitlines()[-20:])
        raise FieldFixError(f"Command failed ({completed.returncode}): {' '.join(command)}\n{tail}")
    return result


def terminate_process_tree(process: subprocess.Popen[object]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_startup_kwargs(),
        )
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass


def popen_kwargs() -> dict[str, object]:
    kwargs = _startup_kwargs()
    if os.name != "nt":
        kwargs["start_new_session"] = True
    return kwargs


def sha256_file(path: str | os.PathLike[str], chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def json_dump_atomic(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.partial")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


ProgressCallback = Callable[[float, str, ProgressDetails | None], None]


def parse_ffmpeg_progress_line(line: str, duration: float) -> float | None:
    key, separator, value = line.strip().partition("=")
    if not separator or duration <= 0:
        return None
    if key in {"out_time_us", "out_time_ms"}:
        try:
            # Modern FFmpeg reports both keys in microseconds despite the legacy out_time_ms name.
            seconds = int(value) / 1_000_000
        except ValueError:
            return None
        return min(1.0, max(0.0, seconds / duration))
    if key == "progress" and value == "end":
        return 1.0
    return None


def drain_text_stream(stream: object, collector: list[str]) -> None:
    if stream is None:
        return
    for line in stream:  # type: ignore[union-attr]
        collector.append(str(line).rstrip())


def wait_with_cancel(
    process: subprocess.Popen[object],
    cancel_event: threading.Event | None,
    poll_interval: float = 0.1,
) -> int:
    while process.poll() is None:
        if cancel_event and cancel_event.wait(poll_interval):
            terminate_process_tree(process)
            raise CancelledError("Operation cancelled")
        if not cancel_event:
            threading.Event().wait(poll_interval)
    return int(process.returncode or 0)
