from __future__ import annotations

import argparse
import contextlib
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal
from uuid import uuid4

import av
import ctranslate2
import yt_dlp
from faster_whisper import WhisperModel

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = BASE_DIR / "transcripts"
DEFAULT_DOWNLOAD_DIR = BASE_DIR / "downloads"
DEFAULT_MODEL_CACHE = BASE_DIR / ".cache" / "models"
DEFAULT_LOCK_PATH = BASE_DIR / ".cache" / "transcription.lock"

MODEL_CHOICES = ("large-v3", "turbo", "medium", "small", "base", "tiny")
LANGUAGE_CHOICES = ("auto", "es", "en", "pt", "fr", "de", "it")

StatusCallback = Callable[[str], None]
JobState = Literal[
    "idle",
    "queued",
    "validating",
    "runtime_setup",
    "model_loading",
    "audio_probe",
    "transcribing",
    "writing_outputs",
    "done",
    "error",
    "cancelled",
]

LOCAL_PHASE_RANGES: dict[str, tuple[float, float]] = {
    "validating": (0.0, 5.0),
    "runtime_setup": (5.0, 12.0),
    "model_loading": (12.0, 30.0),
    "audio_probe": (30.0, 40.0),
    "transcribing": (40.0, 90.0),
    "writing_outputs": (90.0, 100.0),
    "done": (100.0, 100.0),
}

YOUTUBE_PHASE_RANGES: dict[str, tuple[float, float]] = {
    "validating": (0.0, 3.0),
    "downloading_audio": (3.0, 28.0),
    "download_done": (28.0, 28.0),
    "runtime_setup": (28.0, 35.0),
    "model_loading": (35.0, 50.0),
    "audio_probe": (50.0, 60.0),
    "transcribing": (60.0, 93.0),
    "writing_outputs": (93.0, 100.0),
    "done": (100.0, 100.0),
}

NVIDIA_BIN_PATHS = (
    BASE_DIR / ".venv" / "Lib" / "site-packages" / "nvidia" / "cublas" / "bin",
    BASE_DIR / ".venv" / "Lib" / "site-packages" / "nvidia" / "cudnn" / "bin",
    BASE_DIR / ".venv" / "Lib" / "site-packages" / "nvidia" / "cuda_nvrtc" / "bin",
)

_MODEL_CACHE: dict[tuple[str, str], WhisperModel] = {}
_MODEL_LOCK = threading.Lock()
_EVENT_PRINT_LAST_AT: dict[str, float] = {}
_CLI_STATUS_LAST_AT: dict[str, float] = {}
_SEGMENT_PRINT_INTERVAL_SECONDS = 10.0
_QUEUE_PRINT_INTERVAL_SECONDS = 30.0


class JobCancelledError(RuntimeError):
    """Raised when a cooperative cancel request is observed."""


@dataclass(slots=True)
class TranscriptionOptions:
    model: str = "large-v3"
    language: str = "es"
    device: str = "auto"
    task: str = "transcribe"
    word_timestamps: bool = False
    vad_filter: bool = True


@dataclass(slots=True)
class DownloadResult:
    url: str
    title: str
    downloaded_path: Path


@dataclass(slots=True)
class TranscriptionResult:
    input_path: Path
    output_dir: Path
    model: str
    device: str
    task: str
    language_requested: str
    detected_language: str | None
    language_probability: float | None
    segment_count: int
    transcript_text: str
    output_files: dict[str, Path]
    elapsed_seconds: float
    job_status: str = "done"
    artifacts: list[Path] = field(default_factory=list)
    events: list["JobEvent"] = field(default_factory=list)


@dataclass(slots=True)
class JobEvent:
    timestamp: str
    state: JobState
    phase: str
    message: str
    progress: float
    detail: str | None = None


@dataclass(slots=True)
class SessionHistoryItem:
    job_id: str
    started_at: str
    source_label: str
    state: str
    output_dir: str
    elapsed_seconds: float


@dataclass(slots=True)
class JobSnapshot:
    job_id: str | None
    state: str
    phase: str
    progress: float
    status_message: str
    source_label: str
    output_dir: str
    events: list[JobEvent]
    can_start: bool
    can_cancel: bool
    is_active: bool
    started_at: str | None
    elapsed_seconds: float
    transcript_text: str
    artifact_paths: list[str]
    summary_markdown: str
    history_rows: list[list[str]]


@dataclass
class JobRecord:
    job_id: str
    source_label: str
    output_dir: Path
    progress_profile: dict[str, tuple[float, float]]
    started_at_dt: datetime
    started_at_text: str
    cancel_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    state: JobState = "queued"
    phase: str = "queued"
    progress: float = 0.0
    status_message: str = "Trabajo en cola"
    events: list[JobEvent] = field(default_factory=list)
    result: TranscriptionResult | None = None
    error_message: str | None = None
    thread: threading.Thread | None = None
    log_path: Path | None = None
    _run_fn: Callable[[], None] | None = None

    def emit(
        self,
        state: JobState,
        phase: str,
        message: str,
        stage_fraction: float | None = None,
        detail: str | None = None,
    ) -> None:
        progress = self.progress
        if phase in self.progress_profile:
            start, end = self.progress_profile[phase]
            if stage_fraction is None:
                progress = max(self.progress, start)
            else:
                bounded = min(max(stage_fraction, 0.0), 1.0)
                progress = max(self.progress, start + (end - start) * bounded)
        elif state == "done":
            progress = 100.0

        event = JobEvent(
            timestamp=_now_text(),
            state=state,
            phase=phase,
            message=message,
            progress=progress,
            detail=detail,
        )
        with self.lock:
            self.state = state
            self.phase = phase
            self.progress = progress
            self.status_message = message
            self.events.append(event)
            if len(self.events) > 400:
                self.events = self.events[-400:]
            log_path = self.log_path
        _print_event(event)
        if log_path:
            _append_log_event(log_path, event)

    @property
    def is_active(self) -> bool:
        return self.state not in {"done", "error", "cancelled"}

    def elapsed_seconds(self) -> float:
        return max((datetime.now() - self.started_at_dt).total_seconds(), 0.0)

    def wait_for_cancel(self, timeout_seconds: float) -> bool:
        return self.cancel_event.wait(timeout_seconds)


class JobManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, JobRecord] = {}
        self._queue: queue.Queue[str] = queue.Queue()
        self._active_job_id: str | None = None
        self._last_job_id: str | None = None
        self._history: list[SessionHistoryItem] = []
        
        # Iniciar hilo worker
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True, name="JobManager-Worker")
        self._worker_thread.start()

    def _worker_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                with self._lock:
                    record = self._records.get(job_id)
                    if not record:
                        continue
                    self._active_job_id = job_id
                
                # Ejecutar el trabajo guardado en el record (usando el thread original logic)
                # En lugar de guardar el thread, guardamos la funcion a ejecutar
                if hasattr(record, "_run_fn"):
                    record._run_fn()
            except Exception as e:
                print(f"Error critico en worker loop: {e}")
            finally:
                with self._lock:
                    self._active_job_id = None
                self._queue.task_done()

    def start_file_job(self, input_path: str, options: TranscriptionOptions) -> str:
        source_path = Path(input_path)
        output_dir = build_session_dir(DEFAULT_OUTPUT_DIR, source_path.stem)
        record = self._create_record(str(source_path), output_dir, LOCAL_PHASE_RANGES)
        
        def run():
            self._run_file_job(record, source_path, output_dir, options)
        
        record._run_fn = run
        self._queue.put(record.job_id)
        return record.job_id

    def start_youtube_job(self, url: str, options: TranscriptionOptions) -> str:
        output_dir = build_session_dir(DEFAULT_OUTPUT_DIR, "youtube")
        record = self._create_record(url.strip(), output_dir, YOUTUBE_PHASE_RANGES)
        
        def run():
            self._run_youtube_job(record, url.strip(), options)
            
        record._run_fn = run
        self._queue.put(record.job_id)
        return record.job_id

    def cancel_job(self, job_id: str | None = None) -> bool:
        with self._lock:
            target_id = job_id or self._active_job_id
            if not target_id or target_id not in self._records:
                # Buscar en la cola si no es el activo
                return False
            record = self._records[target_id]
        
        if record.state == "queued":
            record.emit("cancelled", "cancelled", "Trabajo cancelado mientras estaba en cola.")
            self._finalize(record)
            return True

        if not record.is_active or record.cancel_event.is_set():
            return False
            
        record.cancel_event.set()
        record.emit(
            record.state,
            record.phase,
            "Cancelacion solicitada. El trabajo se detendra en el siguiente punto seguro.",
            detail="Puede tardar unos segundos si el modelo esta cargando.",
        )
        return True

    def get_snapshot(self, job_id: str | None = None) -> JobSnapshot:
        with self._lock:
            target_id = job_id or self._active_job_id or self._last_job_id
            record = self._records.get(target_id) if target_id else None
            
            # El historial ahora incluye trabajos que estan en cola para visibilidad
            history_rows = []
            
            # Primero los activos/encolados (en orden inverso de creacion para ver lo mas nuevo arriba)
            active_items = []
            for r in self._records.values():
                if r.state in {"queued", "validating", "runtime_setup", "model_loading", "audio_probe", "transcribing", "writing_outputs"}:
                    active_items.append(r)
            
            for r in sorted(active_items, key=lambda x: x.started_at_dt, reverse=True):
                history_rows.append([
                    r.started_at_text,
                    r.source_label,
                    f"[{r.state.upper()}]",
                    str(r.output_dir.name),
                    f"{r.elapsed_seconds():.1f}s"
                ])

            # Luego el historial de completados
            for item in reversed(self._history):
                history_rows.append([
                    item.started_at,
                    item.source_label,
                    item.state,
                    item.output_dir.split(os.sep)[-1] if os.sep in item.output_dir else item.output_dir,
                    f"{item.elapsed_seconds:.1f}s",
                ])

        if record is None:
            return JobSnapshot(
                job_id=None,
                state="idle",
                phase="idle",
                progress=0.0,
                status_message="Listo para iniciar.",
                source_label="",
                output_dir="",
                events=[],
                can_start=True, # Siempre se puede encolar
                can_cancel=False,
                is_active=False,
                started_at=None,
                elapsed_seconds=0.0,
                transcript_text="",
                artifact_paths=[],
                summary_markdown="Sin trabajo activo.",
                history_rows=history_rows,
            )

        with record.lock:
            result = record.result
            error_message = record.error_message
            transcript = result.transcript_text if result else ""
            artifacts = [str(path) for path in (result.artifacts if result else [])]
            summary = _build_summary(record, result, error_message)
            events = list(record.events)
            state = record.state
            phase = record.phase
            progress = record.progress
            status_message = record.status_message

        return JobSnapshot(
            job_id=record.job_id,
            state=state,
            phase=phase,
            progress=progress,
            status_message=status_message,
            source_label=record.source_label,
            output_dir=str(record.output_dir),
            events=events,
            can_start=True,
            can_cancel=record.is_active,
            is_active=record.is_active,
            started_at=record.started_at_text,
            elapsed_seconds=record.elapsed_seconds(),
            transcript_text=transcript,
            artifact_paths=artifacts,
            summary_markdown=summary,
            history_rows=history_rows,
        )

    def _create_record(
        self,
        source_label: str,
        output_dir: Path,
        progress_profile: dict[str, tuple[float, float]],
    ) -> JobRecord:
        with self._lock:
            started_at = datetime.now()
            output_dir.mkdir(parents=True, exist_ok=True)
            record = JobRecord(
                job_id=uuid4().hex[:8],
                source_label=source_label,
                output_dir=output_dir,
                progress_profile=progress_profile,
                started_at_dt=started_at,
                started_at_text=started_at.strftime("%Y-%m-%d %H:%M:%S"),
                log_path=output_dir / "run.log",
            )
            self._records[record.job_id] = record
            self._last_job_id = record.job_id
        record.emit("queued", "queued", "Trabajo en cola. Esperando turno.")
        return record

    def _run_file_job(
        self,
        record: JobRecord,
        input_path: Path,
        output_dir: Path,
        options: TranscriptionOptions,
    ) -> None:
        self._run_pipeline(record, lambda: _transcribe_pipeline(record, input_path, output_dir, options))

    def _run_youtube_job(
        self,
        record: JobRecord,
        url: str,
        options: TranscriptionOptions,
    ) -> None:
        def pipeline() -> TranscriptionResult:
            _raise_if_cancelled(record)
            download_dir = build_session_dir(DEFAULT_DOWNLOAD_DIR, "youtube-audio")
            record.emit("validating", "validating", "Validating YouTube URL.", 0.4)
            download = _download_audio_with_reporter(record, url, download_dir)
            record.source_label = f"{download.title} ({download.downloaded_path.name})"
            output_dir = build_session_dir(DEFAULT_OUTPUT_DIR, download.title)
            output_dir.mkdir(parents=True, exist_ok=True)
            record.output_dir = output_dir
            record.log_path = output_dir / "run.log"
            return _transcribe_pipeline(record, download.downloaded_path, output_dir, options)

        self._run_pipeline(record, pipeline)

    def _run_pipeline(self, record: JobRecord, runner: Callable[[], TranscriptionResult]) -> None:
        try:
            result = runner()
            result.job_status = "done"
            result.events = list(record.events)
            with record.lock:
                record.result = result
            record.emit("done", "done", "Job finished. Results ready.", 1.0)
        except JobCancelledError:
            record.emit("cancelled", "cancelled", "Job cancelled by user.")
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            detail = traceback.format_exc(limit=8)
            with record.lock:
                record.error_message = message
            record.emit("error", "error", "Transcription failed.", detail=detail)
        finally:
            self._finalize(record)

    def _finalize(self, record: JobRecord) -> None:
        with self._lock:
            if self._active_job_id == record.job_id:
                self._active_job_id = None
            if record.state in {"done", "error", "cancelled"}:
                self._history.append(
                    SessionHistoryItem(
                        job_id=record.job_id,
                        started_at=record.started_at_text,
                        source_label=record.source_label,
                        state=record.state,
                        output_dir=str(record.output_dir),
                        elapsed_seconds=record.elapsed_seconds(),
                    )
                )
                self._history = self._history[-25:]


def ensure_nvidia_bins_in_path() -> None:
    current = os.environ.get("PATH", "")
    parts = current.split(";") if current else []
    for candidate in NVIDIA_BIN_PATHS:
        if candidate.exists() and str(candidate) not in parts:
            parts.insert(0, str(candidate))
    os.environ["PATH"] = ";".join(parts)


def get_runtime_status() -> dict[str, object]:
    ensure_nvidia_bins_in_path()
    cuda_devices = int(ctranslate2.get_cuda_device_count())
    return {
        "cuda_available": cuda_devices > 0,
        "cuda_devices": cuda_devices,
        "default_device": "cuda" if cuda_devices > 0 else "cpu",
    }


def build_session_dir(root: Path, stem: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-") or "session"
    session_dir = root / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{safe[:48]}"
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def download_audio(
    url: str,
    output_dir: Path,
    status_callback: StatusCallback | None = None,
) -> DownloadResult:
    output_dir = _resolve_path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    record = JobRecord(
        job_id="inline-download",
        source_label=url,
        output_dir=output_dir,
        progress_profile=YOUTUBE_PHASE_RANGES,
        started_at_dt=now,
        started_at_text=now.strftime("%Y-%m-%d %H:%M:%S"),
        log_path=output_dir / "run.log",
    )
    return _download_audio_with_reporter(record, url, output_dir, status_callback=status_callback)


def transcribe_file(
    input_path: str | Path,
    output_dir: Path,
    options: TranscriptionOptions,
    status_callback: StatusCallback | None = None,
) -> TranscriptionResult:
    resolved_output = _resolve_path(output_dir)
    resolved_output.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    record = JobRecord(
        job_id="inline-transcribe",
        source_label=str(input_path),
        output_dir=resolved_output,
        progress_profile=LOCAL_PHASE_RANGES,
        started_at_dt=now,
        started_at_text=now.strftime("%Y-%m-%d %H:%M:%S"),
        log_path=resolved_output / "run.log",
    )
    result = _transcribe_pipeline(record, Path(input_path), resolved_output, options, status_callback=status_callback)
    result.job_status = "done"
    record.emit("done", "done", "Job finished. Results ready.", 1.0)
    result.events = list(record.events)
    return result


def _download_audio_with_reporter(
    record: JobRecord,
    url: str,
    output_dir: Path,
    status_callback: StatusCallback | None = None,
) -> DownloadResult:
    _raise_if_cancelled(record)
    output_dir.mkdir(parents=True, exist_ok=True)
    record.emit("validating", "downloading_audio", "Preparando descarga de audio.", 0.0)
    last_percent = -1

    def hook(data: dict[str, object]) -> None:
        nonlocal last_percent
        _raise_if_cancelled(record)
        status = str(data.get("status", ""))
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            downloaded = data.get("downloaded_bytes") or 0
            fraction = (float(downloaded) / float(total)) if total else None
            percent = int((fraction or 0.0) * 100)
            if fraction is not None and percent <= last_percent:
                return
            last_percent = percent
            message = (
                f"Descargando audio {percent}%."
                if fraction is not None
                else "Descargando audio."
            )
            detail = None
            if total:
                detail = f"{downloaded / (1024 * 1024):.1f} MB de {total / (1024 * 1024):.1f} MB"
            record.emit("validating", "downloading_audio", message, fraction, detail)
            if status_callback:
                status_callback(message)
        elif status == "finished":
            record.emit("validating", "download_done", "Audio descargado. Normalizando archivo.", 1.0)
            if status_callback:
                status_callback("Audio descargado.")

    class QuietLogger:
        def debug(self, message: str) -> None:
            return

        def info(self, message: str) -> None:
            return

        def warning(self, message: str) -> None:
            return

        def error(self, message: str) -> None:
            return

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(output_dir / "%(title)s [%(id)s].%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": QuietLogger(),
        "progress_hooks": [hook],
    }
    try:
        with yt_dlp.YoutubeDL(opts) as downloader:
            info = downloader.extract_info(url, download=True)
            downloaded_path = Path(downloader.prepare_filename(info))
    except yt_dlp.utils.DownloadError as exc:
        if record.cancel_event.is_set():
            raise JobCancelledError() from exc
        raise

    return DownloadResult(
        url=url,
        title=str(info.get("title") or downloaded_path.stem),
        downloaded_path=downloaded_path,
    )


def _transcribe_pipeline(
    record: JobRecord,
    input_path: Path,
    output_dir: Path,
    options: TranscriptionOptions,
    status_callback: StatusCallback | None = None,
) -> TranscriptionResult:
    with _exclusive_transcription_slot(record):
        return _transcribe_pipeline_unlocked(record, input_path, output_dir, options, status_callback)


@contextlib.contextmanager
def _exclusive_transcription_slot(record: JobRecord):
    DEFAULT_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    record.emit("queued", "queued", "Esperando turno exclusivo de transcripcion.")
    lock_file = DEFAULT_LOCK_PATH.open("a+b")
    try:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"0")
            lock_file.flush()
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            while True:
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    winerror = getattr(exc, "winerror", None)
                    errno_code = getattr(exc, "errno", None)
                    lock_is_busy = winerror in {33, 36} or errno_code in {13, 33, 36}
                    if not lock_is_busy:
                        raise
                    if record.cancel_event.is_set():
                        raise JobCancelledError() from exc
                    record.emit("queued", "queued", "Sigue en cola. Esperando liberacion del lock.")
                    record.wait_for_cancel(1.0)
            try:
                record.emit("queued", "queued", "Turno adquirido. Iniciando trabajo.")
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                record.emit("queued", "queued", "Turno adquirido. Iniciando trabajo.")
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()


def _transcribe_pipeline_unlocked(
    record: JobRecord,
    input_path: Path,
    output_dir: Path,
    options: TranscriptionOptions,
    status_callback: StatusCallback | None = None,
) -> TranscriptionResult:
    started = datetime.now()
    input_path = _resolve_path(input_path)
    output_dir = _resolve_path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    record.emit("validating", "validating", f"Validating input: {input_path.name}.", 0.2)
    if status_callback:
        status_callback(f"Validating input: {input_path.name}")
    if not input_path.exists():
        raise FileNotFoundError(f"No existe el archivo: {input_path}")
    _raise_if_cancelled(record)

    record.emit("runtime_setup", "runtime_setup", "Preparando runtime y rutas CUDA.", 0.2)
    if status_callback:
        status_callback("Preparando runtime y rutas CUDA")
    ensure_nvidia_bins_in_path()
    _raise_if_cancelled(record)

    record.emit("model_loading", "model_loading", f"Cargando modelo {options.model}.", 0.0)
    model, resolved_device = _load_model(record, options)
    if status_callback:
        status_callback(f"Modelo listo en {resolved_device}")
    _raise_if_cancelled(record)

    record.emit("audio_probe", "audio_probe", "Inspeccionando duracion y codec del audio.", 0.1)
    duration_seconds = _probe_duration_seconds(input_path)
    if status_callback:
        if duration_seconds:
            status_callback(f"Audio inspeccionado. Duracion: {duration_seconds:.1f}s")
        else:
            status_callback("Audio inspeccionado.")
    _raise_if_cancelled(record)

    transcribe_kwargs = {
        "language": None if options.language == "auto" else options.language,
        "task": options.task,
        "word_timestamps": options.word_timestamps,
        "vad_filter": options.vad_filter,
        "log_progress": False,
    }
    record.emit("transcribing", "transcribing", "Iniciando transcripcion segmentada.", 0.0)
    if status_callback:
        status_callback("Iniciando transcripcion")
    segments_iter, info = model.transcribe(str(input_path), **transcribe_kwargs)

    detected_language = getattr(info, "language", None)
    language_probability = getattr(info, "language_probability", None)
    total_duration = getattr(info, "duration", None) or duration_seconds

    segments = []
    for index, segment in enumerate(segments_iter, start=1):
        _raise_if_cancelled(record)
        segments.append(segment)
        fraction = None
        if total_duration and total_duration > 0:
            fraction = min(max(float(segment.end) / float(total_duration), 0.0), 1.0)
        snippet = " ".join(segment.text.strip().split())[:140]
        message = f"Segmento {index} listo: {segment.start:.1f}s a {segment.end:.1f}s."
        record.emit("transcribing", "transcribing", message, fraction, snippet or None)
        if status_callback:
            status_callback(message)

    _raise_if_cancelled(record)
    record.emit("writing_outputs", "writing_outputs", "Escribiendo TXT, SRT, VTT, JSON y resumen.", 0.0)
    if status_callback:
        status_callback("Escribiendo archivos de salida")
    transcript_text = "\n".join(segment.text.strip() for segment in segments if segment.text.strip())
    output_files = _write_outputs(
        output_dir,
        input_path.stem,
        segments,
        transcript_text,
        detected_language,
        language_probability,
        options,
        total_duration,
    )
    if record.log_path:
        output_files["log"] = record.log_path
    elapsed = (datetime.now() - started).total_seconds()

    return TranscriptionResult(
        input_path=input_path,
        output_dir=output_dir,
        model=options.model,
        device=resolved_device,
        task=options.task,
        language_requested=options.language,
        detected_language=detected_language,
        language_probability=language_probability,
        segment_count=len(segments),
        transcript_text=transcript_text,
        output_files=output_files,
        elapsed_seconds=elapsed,
        artifacts=list(output_files.values()),
    )


def _load_model(record: JobRecord, options: TranscriptionOptions) -> tuple[WhisperModel, str]:
    runtime = get_runtime_status()
    resolved_device = options.device
    if resolved_device == "auto":
        resolved_device = str(runtime["default_device"])
    compute_type = "float16" if resolved_device == "cuda" else "int8"
    cache_key = (options.model, resolved_device)

    with _MODEL_LOCK:
        if cache_key in _MODEL_CACHE:
            record.emit("model_loading", "model_loading", f"Reusing model {options.model} on {resolved_device}.", 0.85)
            return _MODEL_CACHE[cache_key], resolved_device

        record.emit(
            "model_loading",
            "model_loading",
            f"Instantiating model {options.model} on {resolved_device}.",
            0.2,
            f"compute_type={compute_type}",
        )
        model = WhisperModel(
            options.model,
            device=resolved_device,
            compute_type=compute_type,
            download_root=str(DEFAULT_MODEL_CACHE),
        )
        _MODEL_CACHE[cache_key] = model
        record.emit("model_loading", "model_loading", f"Model {options.model} loaded.", 1.0)
        return model, resolved_device


def _probe_duration_seconds(path: Path) -> float | None:
    with av.open(str(path)) as container:
        if container.duration:
            return float(container.duration / av.time_base)
        for stream in container.streams.audio:
            if stream.duration and stream.time_base:
                return float(stream.duration * stream.time_base)
    return None


def _write_outputs(
    output_dir: Path,
    stem: str,
    segments: list[object],
    transcript_text: str,
    detected_language: str | None,
    language_probability: float | None,
    options: TranscriptionOptions,
    total_duration: float | None,
) -> dict[str, Path]:
    txt_path = output_dir / f"{stem}.txt"
    srt_path = output_dir / f"{stem}.srt"
    vtt_path = output_dir / f"{stem}.vtt"
    json_path = output_dir / f"{stem}.json"
    summary_path = output_dir / f"{stem}-summary.md"

    txt_path.write_text(_to_txt(segments), encoding="utf-8-sig")
    srt_path.write_text(_to_srt(segments), encoding="utf-8-sig")
    vtt_path.write_text(_to_vtt(segments), encoding="utf-8-sig")
    json_path.write_text(
        json.dumps(
            {
                "detected_language": detected_language,
                "language_probability": language_probability,
                "model": options.model,
                "task": options.task,
                "duration_seconds": total_duration,
                "segments": [_segment_to_dict(segment) for segment in segments],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    summary_path.write_text(
        _to_summary_markdown(stem, transcript_text, segments, detected_language, options, total_duration),
        encoding="utf-8-sig",
    )
    return {"txt": txt_path, "srt": srt_path, "vtt": vtt_path, "json": json_path, "summary": summary_path}


def _to_txt(segments: list[object]) -> str:
    body = "\n".join(segment.text.strip() for segment in segments if segment.text.strip()).strip()
    return f"{body}\n" if body else ""


def _to_srt(segments: list[object]) -> str:
    lines: list[str] = []
    for index, segment in enumerate(segments, start=1):
        lines.extend(
            [
                str(index),
                f"{_format_srt_timestamp(segment.start)} --> {_format_srt_timestamp(segment.end)}",
                segment.text.strip(),
                "",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def _to_vtt(segments: list[object]) -> str:
    lines = ["WEBVTT", ""]
    for segment in segments:
        lines.extend(
            [
                f"{_format_vtt_timestamp(segment.start)} --> {_format_vtt_timestamp(segment.end)}",
                segment.text.strip(),
                "",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def _to_summary_markdown(
    stem: str,
    transcript_text: str,
    segments: list[object],
    detected_language: str | None,
    options: TranscriptionOptions,
    total_duration: float | None,
) -> str:
    clean_text = _collapse_spaces(transcript_text)
    key_points = _extract_key_points(clean_text)
    technical_terms = _extract_technical_terms(clean_text)
    timeline = _extract_timeline(segments)
    duration = _format_duration(total_duration) if total_duration else "desconocida"

    lines = [
        f"# Resumen - {stem}",
        "",
        "## Metadata",
        f"- Modelo: `{options.model}`",
        f"- Tarea: `{options.task}`",
        f"- Idioma pedido: `{options.language}`",
        f"- Idioma detectado: `{detected_language or 'desconocido'}`",
        f"- Duracion: `{duration}`",
        f"- Segmentos: `{len(segments)}`",
        "",
        "## Resumen Corto",
        _build_short_summary(clean_text),
        "",
        "## Puntos Principales",
    ]
    lines.extend(f"- {item}" for item in key_points)
    lines.extend(["", "## Linea De Tiempo"])
    lines.extend(f"- {item}" for item in timeline)
    lines.extend(["", "## Terminos Detectados"])
    if technical_terms:
        lines.extend(f"- `{term}`" for term in technical_terms)
    else:
        lines.append("- No se detectaron terminos tecnicos frecuentes.")
    lines.extend(
        [
            "",
            "## Nota",
            "Este resumen es extractivo y automatico. Para entrega formal conviene revisar nombres propios, fechas y terminos tecnicos.",
            "",
        ]
    )
    return "\n".join(lines)


def _build_short_summary(text: str) -> str:
    sentences = _split_sentences(text)
    if not sentences:
        return "No se detecto texto suficiente para resumir."
    selected = sentences[:2]
    if len(sentences) > 6:
        selected.append(sentences[len(sentences) // 2])
    if len(sentences) > 3:
        selected.append(sentences[-1])
    return " ".join(_dedupe_preserve_order(selected)[:4])


def _extract_key_points(text: str) -> list[str]:
    sentences = _split_sentences(text)
    if not sentences:
        return ["No se detecto texto suficiente."]

    keywords = (
        "vamos a",
        "tienen que",
        "hay que",
        "recuerden",
        "importante",
        "entrega",
        "sprint",
        "login",
        "registro",
        "jwt",
        "mongo",
        "mongoose",
        "vercel",
        "angular",
        "nestjs",
        "base de datos",
    )
    ranked: list[tuple[int, int, str]] = []
    for index, sentence in enumerate(sentences):
        lower = sentence.lower()
        score = sum(2 for keyword in keywords if keyword in lower)
        score += min(len(sentence) // 80, 3)
        if score > 0:
            ranked.append((score, -index, sentence))
    ranked.sort(reverse=True)
    selected = [sentence for _, _, sentence in ranked[:8]] or sentences[:6]
    return _dedupe_preserve_order(selected)[:8]


def _extract_timeline(segments: list[object]) -> list[str]:
    if not segments:
        return ["No hay segmentos para armar linea de tiempo."]
    desired = 8
    if len(segments) <= desired:
        selected = segments
    else:
        step = max(len(segments) // desired, 1)
        selected = [segments[index] for index in range(0, len(segments), step)][:desired]
    items = []
    for segment in selected:
        text = _collapse_spaces(getattr(segment, "text", "").strip())
        if text:
            items.append(f"`{_format_vtt_timestamp(float(getattr(segment, 'start', 0.0)))}` {text[:180]}")
    return items or ["No hay texto suficiente para armar linea de tiempo."]


def _extract_technical_terms(text: str) -> list[str]:
    known = [
        "Angular",
        "API",
        "Atlas",
        "DTO",
        "GitHub",
        "JSON",
        "JWT",
        "MongoDB",
        "Mongoose",
        "NestJS",
        "Vercel",
    ]
    lower = text.lower()
    found = [term for term in known if term.lower() in lower]
    return sorted(set(found), key=str.lower)


def _split_sentences(text: str) -> list[str]:
    clean = _collapse_spaces(text)
    if not clean:
        return []
    sentences = re.split(r"(?<=[.!?])\s+", clean)
    if len(sentences) < 3:
        sentences = re.split(r"\s{2,}|\n+", text)
    return [sentence.strip() for sentence in sentences if len(sentence.strip()) >= 20]


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        normalized = item.lower()
        if normalized not in seen:
            seen.add(normalized)
            result.append(item)
    return result


def _collapse_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "desconocida"
    total = int(round(max(seconds, 0.0)))
    hours, rem = divmod(total, 3600)
    minutes, sec = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{sec:02d}"


def _segment_to_dict(segment: object) -> dict[str, object]:
    words = []
    for word in getattr(segment, "words", []) or []:
        words.append(
            {
                "start": getattr(word, "start", None),
                "end": getattr(word, "end", None),
                "word": getattr(word, "word", ""),
                "probability": getattr(word, "probability", None),
            }
        )
    return {
        "id": getattr(segment, "id", None),
        "seek": getattr(segment, "seek", None),
        "start": getattr(segment, "start", None),
        "end": getattr(segment, "end", None),
        "text": getattr(segment, "text", "").strip(),
        "avg_logprob": getattr(segment, "avg_logprob", None),
        "compression_ratio": getattr(segment, "compression_ratio", None),
        "no_speech_prob": getattr(segment, "no_speech_prob", None),
        "words": words,
    }


def _format_srt_timestamp(seconds: float) -> str:
    seconds = max(seconds, 0.0)
    hours, rem = divmod(seconds, 3600)
    minutes, rem = divmod(rem, 60)
    whole = int(rem)
    millis = int(round((rem - whole) * 1000))
    return f"{int(hours):02d}:{int(minutes):02d}:{whole:02d},{millis:03d}"


def _format_vtt_timestamp(seconds: float) -> str:
    seconds = max(seconds, 0.0)
    hours, rem = divmod(seconds, 3600)
    minutes, rem = divmod(rem, 60)
    whole = int(rem)
    millis = int(round((rem - whole) * 1000))
    return f"{int(hours):02d}:{int(minutes):02d}:{whole:02d}.{millis:03d}"


def _resolve_path(path: str | Path) -> Path:
    raw = Path(path)
    return raw if raw.is_absolute() else (BASE_DIR / raw).resolve()


def _raise_if_cancelled(record: JobRecord) -> None:
    if record.cancel_event.is_set():
        raise JobCancelledError()


def _build_summary(
    record: JobRecord,
    result: TranscriptionResult | None,
    error_message: str | None,
) -> str:
    lines = [
        f"Fuente: `{record.source_label or '-'}`",
        f"Estado: `{record.state}`",
        f"Fase: `{record.phase}`",
        f"Inicio: `{record.started_at_text}`",
        f"Duracion corrida: `{record.elapsed_seconds():.1f}s`",
        f"Salida: `{record.output_dir}`",
        f"Log: `{record.log_path or '-'}`",
    ]
    if result:
        lines.extend(
            [
                f"Modelo: `{result.model}`",
                f"Dispositivo: `{result.device}`",
                f"Idioma pedido: `{result.language_requested}`",
                f"Idioma detectado: `{result.detected_language or 'desconocido'}`",
                f"Segmentos: `{result.segment_count}`",
            ]
        )
    if error_message:
        lines.append(f"Error: `{error_message}`")
    if record.cancel_event.is_set() and record.state == "cancelled":
        lines.append("Cancelado por el usuario.")
    return "  \n".join(lines)


def _print_event(event: JobEvent) -> None:
    if not _should_print_event(event):
        return
    suffix = f" | {event.detail}" if event.detail else ""
    text = f"[{event.timestamp}] {event.phase} [{event.progress:5.1f}%] {event.message}{suffix}"
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("utf-8", errors="replace").decode("utf-8"), flush=True)


def _should_print_event(event: JobEvent) -> bool:
    now = time.monotonic()
    if event.phase == "transcribing" and event.message.startswith("Segmento "):
        last_at = _EVENT_PRINT_LAST_AT.get("transcribing_segments", 0.0)
        if now - last_at < _SEGMENT_PRINT_INTERVAL_SECONDS:
            return False
        _EVENT_PRINT_LAST_AT["transcribing_segments"] = now
        return True
    if event.phase == "queued" and event.message.startswith("Sigue en cola"):
        last_at = _EVENT_PRINT_LAST_AT.get("queued_wait", 0.0)
        if now - last_at < _QUEUE_PRINT_INTERVAL_SECONDS:
            return False
        _EVENT_PRINT_LAST_AT["queued_wait"] = now
        return True
    return True


def _append_log_event(log_path: Path, event: JobEvent) -> None:
    suffix = f" | {event.detail}" if event.detail else ""
    line = f"[{event.timestamp}] {event.phase} [{event.progress:5.1f}%] {event.message}{suffix}\n"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8-sig") as log_file:
            log_file.write(line)
    except OSError:
        print(f"No se pudo escribir el log: {log_path}", file=sys.stderr, flush=True)


def _now_text() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _cli_status(message: str) -> None:
    if message.startswith("Segmento "):
        now = time.monotonic()
        last_at = _CLI_STATUS_LAST_AT.get("segments", 0.0)
        if now - last_at < _SEGMENT_PRINT_INTERVAL_SECONDS:
            return
        _CLI_STATUS_LAST_AT["segments"] = now
    print(f"[{_now_text()}] {message}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Transcribe un archivo local con faster-whisper.")
    parser.add_argument("--input", required=True, help="Archivo de entrada.")
    parser.add_argument("--output-dir", default="transcripts", help="Carpeta de salida.")
    parser.add_argument("--model", default="large-v3", choices=MODEL_CHOICES)
    parser.add_argument("--language", default="es", choices=LANGUAGE_CHOICES)
    parser.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"))
    parser.add_argument("--task", default="transcribe", choices=("transcribe", "translate"))
    parser.add_argument("--word-timestamps", action="store_true")
    parser.add_argument("--no-vad", action="store_true")
    args = parser.parse_args()

    options = TranscriptionOptions(
        model=args.model,
        language=args.language,
        device=args.device,
        task=args.task,
        word_timestamps=args.word_timestamps,
        vad_filter=not args.no_vad,
    )
    input_path = Path(args.input)
    output_dir = build_session_dir(_resolve_path(args.output_dir), input_path.stem)
    result = transcribe_file(
        input_path=input_path,
        output_dir=output_dir,
        options=options,
        status_callback=_cli_status,
    )
    print(
        json.dumps(
            {
                "output_dir": str(result.output_dir),
                "files": {key: str(value) for key, value in result.output_files.items()},
            },
            ensure_ascii=False,
            indent=2,
        )
    )


JOB_MANAGER = JobManager()


if __name__ == "__main__":
    main()
