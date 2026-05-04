from __future__ import annotations

from typing import Iterable

import gradio as gr

from scripts.transcribe import (
    DEFAULT_DOWNLOAD_DIR,
    DEFAULT_OUTPUT_DIR,
    JOB_MANAGER,
    LANGUAGE_CHOICES,
    MODEL_CHOICES,
    JobSnapshot,
    TranscriptionOptions,
    get_runtime_status,
)

APP_TITLE = "Transcription Cabin"
APP_THEME = gr.themes.Base(
    primary_hue="green",
    secondary_hue="stone",
    neutral_hue="stone",
    radius_size="sm",
)
APP_CSS = """
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&display=swap');

:root {
  --bg: #f4f3ee;
  --surface: #ffffff;
  --ink: #171717;
  --muted: #646464;
  --line: #d6d2c8;
  --accent: #1f4a3d;
  --accent-soft: #e6eee9;
  --danger: #7a2f2f;
}

html, body, .gradio-container {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: 'IBM Plex Sans', sans-serif;
}

.gradio-container {
  max-width: none !important;
  padding: 18px 22px 24px 22px !important;
}

#header {
  padding: 2px 0 16px 0;
  border-bottom: 1px solid var(--line);
  margin-bottom: 18px;
}

#header h1 {
  margin: 0 0 8px 0;
  font-size: clamp(26px, 3vw, 40px);
  line-height: 1.05;
  font-weight: 600;
  letter-spacing: -0.03em;
}

#header p {
  margin: 0;
  color: var(--muted);
  font-size: 14px;
  line-height: 1.55;
  max-width: 88ch;
}

.meta-row {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  align-items: center;
  margin-bottom: 10px;
}

.eyebrow {
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--muted);
}

.chip {
  padding: 5px 10px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--surface);
  font-size: 12px;
  color: var(--ink);
}

#shell {
  gap: 18px;
}

.surface, .gr-group, .gr-form, .gr-box, .gradio-container .block {
  border-radius: 12px !important;
  box-shadow: none !important;
}

.surface {
  background: var(--surface);
  border: 1px solid var(--line);
  padding: 14px;
}

.panel-title {
  margin: 0 0 10px 0;
  font-size: 14px;
  color: var(--muted);
}

.progress-shell {
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--surface);
  padding: 12px;
}

.progress-head {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: baseline;
  margin-bottom: 8px;
}

.progress-phase {
  font-size: 15px;
  font-weight: 600;
}

.progress-percent {
  font-size: 13px;
  color: var(--muted);
}

.progress-track {
  width: 100%;
  height: 10px;
  border-radius: 999px;
  background: #ece9e1;
  overflow: hidden;
}

.progress-fill {
  height: 100%;
  background: var(--accent);
  transition: width 180ms linear;
}

.progress-caption {
  margin-top: 8px;
  font-size: 13px;
  color: var(--muted);
  line-height: 1.45;
}

.status-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 14px;
  margin-top: 12px;
  font-size: 13px;
}

.status-grid strong {
  display: block;
  color: var(--muted);
  font-weight: 500;
}

.status-grid span {
  display: block;
  color: var(--ink);
  margin-top: 2px;
  word-break: break-word;
}

.gr-button-primary {
  background: var(--accent) !important;
  border-color: var(--accent) !important;
}

.danger-btn {
  border-color: #d2b8b8 !important;
  color: var(--danger) !important;
}

.footer-note {
  font-size: 12px;
  color: var(--muted);
}

@media (max-width: 860px) {
  .gradio-container {
    padding: 14px 12px 18px 12px !important;
  }

  .status-grid {
    grid-template-columns: 1fr;
  }
}
"""

PHASE_LABELS = {
    "idle": "Listo",
    "queued": "En cola",
    "validating": "Validando entrada",
    "downloading_audio": "Descargando audio",
    "download_done": "Audio descargado",
    "runtime_setup": "Preparando runtime",
    "model_loading": "Cargando modelo",
    "audio_probe": "Inspeccionando audio",
    "transcribing": "Transcribiendo",
    "writing_outputs": "Guardando archivos",
    "done": "Completado",
    "error": "Error",
    "cancelled": "Cancelado",
}

HISTORY_HEADERS = ["Inicio", "Fuente", "Estado", "Salida", "Duracion"]


def _build_options(
    model: str,
    language: str,
    device: str,
    task: str,
    word_timestamps: bool,
    disable_vad: bool,
) -> TranscriptionOptions:
    return TranscriptionOptions(
        model=model,
        language=language,
        device=device,
        task=task,
        word_timestamps=word_timestamps,
        vad_filter=not disable_vad,
    )


def _phase_label(phase: str) -> str:
    return PHASE_LABELS.get(phase, phase.replace("_", " ").title())


def _progress_html(snapshot: JobSnapshot) -> str:
    percent = min(max(snapshot.progress, 0.0), 100.0)
    phase_label = _phase_label(snapshot.phase if snapshot.phase != "idle" else snapshot.state)
    caption = snapshot.status_message or "Listo para iniciar."
    return f"""
    <div class="progress-shell">
      <div class="progress-head">
        <div class="progress-phase">{phase_label}</div>
        <div class="progress-percent">{percent:.1f}%</div>
      </div>
      <div class="progress-track">
        <div class="progress-fill" style="width: {percent:.1f}%"></div>
      </div>
      <div class="progress-caption">{caption}</div>
    </div>
    """


def _status_meta_html(snapshot: JobSnapshot) -> str:
    started = snapshot.started_at or "-"
    source = snapshot.source_label or "-"
    output_dir = snapshot.output_dir or "-"
    return f"""
    <div class="status-grid">
      <div><strong>Trabajo</strong><span>{snapshot.job_id or '-'}</span></div>
      <div><strong>Estado</strong><span>{snapshot.state}</span></div>
      <div><strong>Inicio</strong><span>{started}</span></div>
      <div><strong>Tiempo corrido</strong><span>{snapshot.elapsed_seconds:.1f}s</span></div>
      <div><strong>Fuente</strong><span>{source}</span></div>
      <div><strong>Salida</strong><span>{output_dir}</span></div>
    </div>
    """


def _events_to_text(events: Iterable) -> str:
    lines = []
    for event in events:
        suffix = f" | {event.detail}" if event.detail else ""
        lines.append(f"[{event.timestamp}] {event.phase} | {event.message}{suffix}")
    return "\n".join(lines) if lines else "Sin eventos todavia."


def _render_snapshot(snapshot: JobSnapshot) -> tuple:
    transcript_visible = snapshot.state == "done"
    transcript_value = snapshot.transcript_text or "(No se detecto texto en el audio.)"
    downloads_visible = snapshot.state == "done" and bool(snapshot.artifact_paths)
    start_enabled = snapshot.can_start
    cancel_enabled = snapshot.can_cancel

    return (
        snapshot.job_id or "",
        _progress_html(snapshot),
        _status_meta_html(snapshot),
        _events_to_text(snapshot.events),
        snapshot.summary_markdown,
        gr.update(value=transcript_value, visible=transcript_visible),
        gr.update(value=snapshot.artifact_paths, visible=downloads_visible),
        snapshot.history_rows or [["-", "-", "-", "-", "-"]],
        gr.update(interactive=start_enabled),
        gr.update(interactive=start_enabled),
        gr.update(interactive=cancel_enabled),
    )


def _refresh_ui(job_id: str | None) -> tuple:
    return _render_snapshot(JOB_MANAGER.get_snapshot(job_id or None))


def _start_file_job(
    file_paths: list[str],
    model: str,
    language: str,
    device: str,
    task: str,
    word_timestamps: bool,
    disable_vad: bool,
) -> tuple:
    if not file_paths:
        raise gr.Error("Upload at least one file before starting.")
    
    last_job_id = None
    for path in file_paths:
        try:
            last_job_id = JOB_MANAGER.start_file_job(
                path,
                _build_options(model, language, device, task, word_timestamps, disable_vad),
            )
        except RuntimeError as exc:
            raise gr.Error(str(exc)) from exc
    
    # We return the status of the last queued job to update the UI
    return _refresh_ui(last_job_id)


def _start_youtube_job(
    url: str,
    model: str,
    language: str,
    device: str,
    task: str,
    word_timestamps: bool,
    disable_vad: bool,
) -> tuple:
    if not url.strip():
        raise gr.Error("Paste a YouTube URL before starting.")
    try:
        job_id = JOB_MANAGER.start_youtube_job(
            url.strip(),
            _build_options(model, language, device, task, word_timestamps, disable_vad),
        )
    except RuntimeError as exc:
        raise gr.Error(str(exc)) from exc
    return _refresh_ui(job_id)


def _cancel_job(job_id: str | None) -> tuple:
    JOB_MANAGER.cancel_job(job_id or None)
    return _refresh_ui(job_id)


def build_demo() -> gr.Blocks:
    runtime = get_runtime_status()
    gpu_chip = (
        f"<span class='chip'>GPU CUDA: {runtime['cuda_devices']}</span>"
        if runtime["cuda_devices"]
        else "<span class='chip'>No CUDA GPU detected</span>"
    )
    initial_rows = [["-", "-", "-", "-", "-"]]
    initial = _render_snapshot(JOB_MANAGER.get_snapshot())

    with gr.Blocks(title=APP_TITLE) as demo:
        gr.HTML(
            f"""
            <section id="header">
              <div class="meta-row">
                <span class="eyebrow">Local Transcription with Whisper</span>
                {gpu_chip}
                <span class="chip">127.0.0.1:7860</span>
              </div>
              <h1>Upload audio, monitor the process, and download the results.</h1>
              <p>
                The app runs on your machine, handles one active job at a time, writes live logs,
                and saves <strong>TXT, SRT, VTT, and JSON</strong> to <code>{DEFAULT_OUTPUT_DIR}</code>.
                Temporary YouTube downloads are stored in <code>{DEFAULT_DOWNLOAD_DIR}</code>.
              </p>
            </section>
            """
        )

        job_state = gr.State(initial[0])

        with gr.Row(elem_id="shell"):
            with gr.Column(scale=4, min_width=340):
                with gr.Tabs():
                    with gr.Tab("Local File"):
                        gr.Markdown("#### Local Source")
                        file_input = gr.File(
                            label="File(s)",
                            type="filepath",
                            file_count="multiple",
                        )
                        file_button = gr.Button("Transcribe File", variant="primary")

                    with gr.Tab("YouTube"):
                        gr.Markdown("#### YouTube URL")
                        youtube_url = gr.Textbox(
                            label="URL",
                            placeholder="https://www.youtube.com/watch?v=...",
                        )
                        yt_button = gr.Button("Download and Transcribe", variant="primary")

                with gr.Accordion("Advanced Options", open=False):
                    model = gr.Dropdown(
                        choices=list(MODEL_CHOICES),
                        value="large-v3",
                        label="Model",
                    )
                    language = gr.Dropdown(
                        choices=list(LANGUAGE_CHOICES),
                        value="es",
                        label="Language",
                    )
                    device = gr.Dropdown(
                        choices=["auto", "cuda", "cpu"],
                        value=str(runtime["default_device"]),
                        label="Device",
                    )
                    task = gr.Dropdown(
                        choices=["transcribe", "translate"],
                        value="transcribe",
                        label="Task",
                    )
                    word_timestamps = gr.Checkbox(
                        label="Word-level timestamps in JSON",
                        value=False,
                    )
                    disable_vad = gr.Checkbox(
                        label="Disable Voice Activity Detection (VAD)",
                        value=False,
                    )

                gr.Markdown(
                    "Uses `large-v3` by default. For a faster first pass, switch to `turbo`.",
                    elem_classes="footer-note",
                )

            with gr.Column(scale=6, min_width=360):
                gr.Markdown("#### Current Status", elem_classes="panel-title")
                progress = gr.HTML(initial[1])
                meta = gr.HTML(initial[2])
                cancel_button = gr.Button("Cancel active job", elem_classes="danger-btn")

                logs = gr.Textbox(
                    label="Live Log",
                    value=_events_to_text([]),
                    lines=16,
                    max_lines=20,
                    interactive=False,
                )
                summary = gr.Markdown(initial[4])
                transcript = gr.Textbox(
                    label="Final Transcription",
                    lines=16,
                    max_lines=24,
                    visible=False,
                    interactive=False,
                )
                downloads = gr.Files(
                    label="Ready Files",
                    visible=False,
                    interactive=False,
                )
                history = gr.Dataframe(
                    headers=HISTORY_HEADERS,
                    value=initial_rows,
                    row_count=(1, "dynamic"),
                    column_count=(5, "fixed"),
                    interactive=False,
                    label="Session History",
                )

        outputs = [
            job_state,
            progress,
            meta,
            logs,
            summary,
            transcript,
            downloads,
            history,
            file_button,
            yt_button,
            cancel_button,
        ]

        file_button.click(
            fn=_start_file_job,
            inputs=[file_input, model, language, device, task, word_timestamps, disable_vad],
            outputs=outputs,
            queue=False,
            show_progress="hidden",
        )
        yt_button.click(
            fn=_start_youtube_job,
            inputs=[youtube_url, model, language, device, task, word_timestamps, disable_vad],
            outputs=outputs,
            queue=False,
            show_progress="hidden",
        )
        cancel_button.click(
            fn=_cancel_job,
            inputs=[job_state],
            outputs=outputs,
            queue=False,
            show_progress="hidden",
        )

        timer = gr.Timer(value=0.8)
        timer.tick(
            fn=_refresh_ui,
            inputs=[job_state],
            outputs=outputs,
            queue=False,
            show_progress="hidden",
        )

    return demo


def main() -> None:
    demo = build_demo()
    demo.launch(
        server_name="127.0.0.1",
        server_port=7860,
        inbrowser=True,
        css=APP_CSS,
        theme=APP_THEME,
        footer_links=[],
        allowed_paths=[str(DEFAULT_OUTPUT_DIR), str(DEFAULT_DOWNLOAD_DIR)],
    )


if __name__ == "__main__":
    main()
