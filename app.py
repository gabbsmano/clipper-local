"""Clipper local: corta vídeos longos (GTA 6, podcasts, etc.) em cortes prontos
para TikTok / Reels / Shorts. Roda 100% no seu computador (exceto a IA, opcional).
"""
import json
import re
import subprocess
import threading
import uuid
import webbrowser
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file, send_from_directory

from ai_highlights import DEFAULT_MODEL, ai_highlights, is_configured
from cuts import find_silences, keep_segments, map_time, pauses_from_silences
from highlights import audio_levels, find_highlights
from render import FFMPEG, render_clip
from reframe import can_reframe, crop_expression, speaker_track
from subtitles import SUB_STYLES, build_ass
import tiktok
from transcribe import all_words, load_cached, transcribe

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
CACHE_DIR = OUTPUT_DIR / "_cache"
WORK_DIR = OUTPUT_DIR / "_work"
OUTPUT_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
state = {"source": None, "info": None, "transcript": None}  # vídeo carregado atualmente
silence_cache = {}                                          # source -> [(início, fim)]
jobs = {}                                                   # exportações e análises
FORMATS = {"original", "vertical", "vertical_blur"}
WHISPER_SIZES = {"tiny", "base", "small", "medium"}

DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
VIDEO_RE = re.compile(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})")


def _ffmpeg(args):
    return subprocess.run(
        [FFMPEG, "-hide_banner", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def probe(path):
    """Lê duração, resolução e presença de áudio usando a saída do ffmpeg (sem ffprobe)."""
    info = _ffmpeg(["-i", str(path)]).stderr
    match = DURATION_RE.search(info)
    if not match:
        raise ValueError("Não consegui ler esse arquivo como vídeo.")
    h, m, s = match.groups()
    video = VIDEO_RE.search(info)
    return {
        "duration": int(h) * 3600 + int(m) * 60 + float(s),
        "width": int(video.group(1)) if video else None,
        "height": int(video.group(2)) if video else None,
        "has_audio": "Audio:" in info,
    }


def get_silences(source):
    if source not in silence_cache:
        silence_cache[source] = find_silences(source, FFMPEG)
    return silence_cache[source]


def safe_name(text):
    cleaned = re.sub(r"[^\w\- ]+", "", text).strip().replace(" ", "_")
    return cleaned[:60] or "clip"


def new_job(**extra):
    job = {"state": "running", "done": 0, "total": 0, "files": [], "errors": [],
           "message": "", "result": {}, "error": None}
    job.update(extra)
    return job


def render_all(job, source, info, clips, opts, transcript):
    """Renderiza todos os cortes de um vídeo, aplicando as opções de edição."""
    out_dir = OUTPUT_DIR / safe_name(Path(source).stem)
    out_dir.mkdir(parents=True, exist_ok=True)
    fmt = opts["format"]
    out_w, out_h = (1920, 1080) if fmt == "original" and info["width"] else (1080, 1920)
    if fmt == "original" and info["width"]:
        out_w, out_h = info["width"], info["height"]

    silences = get_silences(source) if (opts["jump_cuts"] or opts["reframe"]) else []
    words_all = all_words(transcript) if (opts["subtitles"] and transcript) else []
    job["total"] = len(clips)

    for i, clip in enumerate(clips, 1):
        name = safe_name(clip["name"] or f"clip_{i:03d}")
        out_file = out_dir / f"{name}.mp4"
        job["message"] = f"Renderizando {name}..."
        try:
            start, end = clip["start"], clip["end"]
            dur = end - start
            silences_local = [(max(0.0, s - start), min(dur, e - start))
                              for s, e in silences if e > start and s < end]

            if opts["jump_cuts"]:
                keep = keep_segments(start, end, silences, min_gap=opts["min_gap"])
            else:
                keep = [(0.0, dur)]
            total = sum(b - a for a, b in keep)

            crop = None
            if opts["reframe"] and fmt == "vertical" and can_reframe(info["width"], info["height"]):
                job["message"] = f"Seguindo quem fala em {name}..."
                kps = speaker_track(FFMPEG, source, start, dur, info["width"], info["height"],
                                    silences_local)
                if kps:
                    expr, cw = crop_expression(kps, info["width"], info["height"])
                    crop = (expr, cw, info["height"])

            words = []
            for w in words_all:
                if w["end"] > start and w["start"] < end:
                    words.append({
                        "word": w["word"],
                        "start": map_time(max(0.0, w["start"] - start), keep),
                        "end": map_time(min(dur, w["end"] - start), keep),
                    })
            ass = build_ass(0.0, total, words, clip.get("title", ""), opts["title_seconds"],
                            out_w, out_h, opts["subtitles"], opts["sub_style"])

            render_clip(source, start, dur, out_file, fmt, keep, info["has_audio"], WORK_DIR,
                        ass_text=ass, crop=crop)
            caption = clip.get("description", "").strip()
            tags = " ".join(clip.get("hashtags", []))
            sidecar = (f"Título: {clip.get('title', '')}\n\n"
                       f"Legenda: {caption}\n\n"
                       f"Hashtags: {tags}\n")
            (out_dir / f"{name}.txt").write_text(sidecar, encoding="utf-8")
            job["files"].append(f"{out_dir.name}/{out_file.name}")
        except Exception as exc:  # noqa: BLE001 - mostra o erro na tela e segue para o próximo
            job["errors"].append(f"{name}: {exc}")
        job["done"] = i
    job["state"] = "done"
    job["message"] = "Concluído."


def run_export(job_id, source, info, clips, opts, transcript):
    job = jobs[job_id]
    try:
        render_all(job, source, info, clips, opts, transcript)
    except Exception as exc:  # noqa: BLE001
        job["error"] = str(exc)
        job["state"] = "error"
        job["message"] = "Falhou."


def run_analyze(job_id, source, opts):
    job = jobs[job_id]
    use_ai = opts["highlights"] and opts["method"] == "ai"
    try:
        transcript = None
        # A IA precisa da transcrição; a heurística só a usa se ela já existir.
        if opts["transcribe"] or use_ai:
            def progress(done, total):
                pct = 100 * done / total if total else 0
                job["message"] = f"Transcrevendo... {pct:.0f}% (a primeira vez baixa o modelo)"
            transcript = transcribe(source, opts["size"], opts["language"], CACHE_DIR, progress)
            state["transcript"] = transcript
        else:
            transcript = load_cached(source, opts["size"], CACHE_DIR)
            if transcript is not None:
                state["transcript"] = transcript

        if use_ai:
            job["message"] = f"Claude ({DEFAULT_MODEL}) está escolhendo os melhores momentos..."
            job["result"]["highlights"] = ai_highlights(
                transcript, opts["duration"], opts["count"], opts["length"]
            )
        elif opts["highlights"]:
            job["message"] = "Analisando áudio para achar os melhores momentos..."
            levels = audio_levels(source, FFMPEG)
            job["result"]["highlights"] = find_highlights(
                opts["duration"], levels, transcript, opts["count"], opts["length"]
            )
        job["result"]["has_transcript"] = transcript is not None
        job["state"] = "done"
        job["message"] = "Concluído."
    except Exception as exc:  # noqa: BLE001
        job["error"] = str(exc)
        job["state"] = "error"
        job["message"] = "Falhou."


def parse_render_opts(data):
    """Valida as opções de edição/exportação vindas da interface (usado por exportação e fila)."""
    fmt = data.get("format", "vertical")
    if fmt not in FORMATS:
        raise ValueError("Formato inválido.")
    sub_style = data.get("sub_style", "chunk")
    if sub_style not in SUB_STYLES:
        raise ValueError("Estilo de legenda inválido.")
    reframe = bool(data.get("reframe"))
    if reframe and fmt != "vertical":
        raise ValueError("O reenquadramento para duas pessoas só funciona no formato vertical (recorte).")
    return {
        "format": fmt,
        "subtitles": bool(data.get("subtitles")),
        "sub_style": sub_style,
        "title_seconds": max(0.0, float(data.get("title_seconds", 4))),
        "jump_cuts": bool(data.get("jump_cuts")),
        "min_gap": max(0.2, min(float(data.get("min_gap", 0.6)), 3.0)),
        "reframe": reframe,
    }


def new_item(path):
    return {"path": path, "name": Path(path).name, "state": "waiting", "message": "",
            "done": 0, "total": 0, "files": [], "errors": []}


def run_queue(job_id, paths, opts):
    """Processa vários vídeos em sequência: transcreve, escolhe momentos, renderiza."""
    job = jobs[job_id]
    for idx, path in enumerate(paths):
        item = job["items"][idx]
        item["state"] = "running"
        try:
            info = probe(path)
            if opts["subtitles"] or opts["method"] == "ai":
                def progress(done, total, item=item):
                    pct = 100 * done / total if total else 0
                    item["message"] = f"Transcrevendo... {pct:.0f}%"
                transcript = transcribe(path, opts["size"], opts["language"], CACHE_DIR, progress)
            else:
                transcript = load_cached(path, opts["size"], CACHE_DIR)

            if opts["method"] == "ai":
                item["message"] = f"Claude ({DEFAULT_MODEL}) escolhendo os momentos..."
                highlights_found = ai_highlights(transcript, info["duration"], opts["count"], opts["length"])
            else:
                item["message"] = "Analisando áudio para achar os momentos..."
                levels = audio_levels(path, FFMPEG)
                highlights_found = find_highlights(info["duration"], levels, transcript,
                                                   opts["count"], opts["length"])
            if not highlights_found:
                raise RuntimeError("Nenhum momento encontrado neste vídeo.")

            clips = [{
                "start": h["start"], "end": h["end"], "name": f"destaque_{k}",
                "title": h.get("title", ""), "description": h.get("description", ""),
                "hashtags": h.get("hashtags", []),
            } for k, h in enumerate(highlights_found, 1)]

            out_dir = OUTPUT_DIR / safe_name(Path(path).stem)
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "manifest.json").write_text(
                json.dumps(clips, ensure_ascii=False, indent=2), encoding="utf-8")

            item["message"] = "Renderizando..."
            render_all(item, path, info, clips, opts, transcript)
            item["state"] = "done"
        except Exception as exc:  # noqa: BLE001 - um vídeo com erro não para a fila
            item["state"] = "error"
            item["message"] = str(exc)
        job["done"] = idx + 1
    job["state"] = "done"
    job["message"] = "Fila concluída."


def _error(message, code=400):
    return jsonify(error=message), code


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/load")
def api_load():
    raw = (request.get_json(silent=True) or {}).get("path", "")
    path = Path(raw.strip().strip('"').strip("'"))
    if not path.is_file():
        return _error("Arquivo não encontrado. Confira o caminho completo.")
    try:
        info = probe(path)
    except ValueError as exc:
        return _error(str(exc))
    state.update(source=str(path), info=info, transcript=None)
    return jsonify(name=path.name, **info)


@app.get("/api/ai-status")
def api_ai_status():
    # Só informa se a chave existe; nunca devolve o valor da chave.
    return jsonify(configured=is_configured(), model=DEFAULT_MODEL)


@app.post("/api/pauses")
def api_pauses():
    if not state["source"]:
        return _error("Carregue um vídeo primeiro.")
    silences = get_silences(state["source"])
    return jsonify(points=pauses_from_silences(silences, min_pause=0.4))


@app.post("/api/analyze")
def api_analyze():
    if not state["source"]:
        return _error("Carregue um vídeo primeiro.")
    data = request.get_json(silent=True) or {}
    size = data.get("size", "small")
    if size not in WHISPER_SIZES:
        return _error("Modelo de transcrição inválido.")
    method = data.get("method", "heuristic")
    if method not in {"heuristic", "ai"}:
        return _error("Método de destaques inválido.")
    if method == "ai" and data.get("highlights") and not is_configured():
        return _error("Para usar a IA, defina a variável ANTHROPIC_API_KEY e reinicie o app (veja o README).")
    try:
        opts = {
            "transcribe": bool(data.get("transcribe")),
            "highlights": bool(data.get("highlights")),
            "method": method,
            "size": size,
            "language": (data.get("language") or "").strip(),
            "count": max(1, min(int(data.get("count", 5)), 30)),
            "length": max(5.0, min(float(data.get("length", 45)), 180.0)),
            "duration": state["info"]["duration"],
        }
    except (TypeError, ValueError):
        return _error("Parâmetros inválidos.")
    if not (opts["transcribe"] or opts["highlights"]):
        return _error("Escolha transcrever ou buscar melhores momentos.")

    job_id = uuid.uuid4().hex
    jobs[job_id] = new_job()
    threading.Thread(target=run_analyze, args=(job_id, state["source"], opts), daemon=True).start()
    return jsonify(job_id=job_id)


@app.post("/api/export")
def api_export():
    if not state["source"]:
        return _error("Carregue um vídeo primeiro.")
    data = request.get_json(silent=True) or {}
    try:
        opts = parse_render_opts(data)
        clips = [
            {"start": float(c["start"]), "end": float(c["end"]),
             "name": str(c.get("name", "")), "title": str(c.get("title", "")),
             "description": str(c.get("description", "")),
             "hashtags": [str(h) for h in c.get("hashtags", [])] if isinstance(c.get("hashtags"), list) else []}
            for c in data.get("clips", [])
        ]
    except (KeyError, TypeError, ValueError) as exc:
        return _error(str(exc) if isinstance(exc, ValueError) and str(exc) else "Lista de cortes inválida.")
    if opts["subtitles"] and state["transcript"] is None:
        return _error("Para legendas, transcreva o vídeo primeiro (botão Transcrever).")
    clips = [c for c in clips if c["end"] > c["start"] >= 0]
    if not clips:
        return _error("Nenhum corte válido para exportar.")

    info = state["info"]
    job_id = uuid.uuid4().hex
    jobs[job_id] = new_job(total=len(clips))
    threading.Thread(
        target=run_export,
        args=(job_id, state["source"], info, clips, opts, state["transcript"]),
        daemon=True,
    ).start()
    return jsonify(job_id=job_id)


@app.post("/api/queue")
def api_queue():
    data = request.get_json(silent=True) or {}
    paths = [str(p).strip().strip('"').strip("'") for p in data.get("paths", []) if str(p).strip()]
    if not paths:
        return _error("Cole pelo menos um caminho de vídeo (um por linha).")
    if len(paths) > 50:
        return _error("A fila aceita até 50 vídeos por vez.")
    missing = [p for p in paths if not Path(p).is_file()]
    if missing:
        return _error("Arquivo não encontrado: " + missing[0])
    method = data.get("method", "heuristic")
    if method not in {"heuristic", "ai"}:
        return _error("Método de destaques inválido.")
    if method == "ai" and not is_configured():
        return _error("Para usar a IA, defina a variável ANTHROPIC_API_KEY e reinicie o app (veja o README).")
    size = data.get("size", "small")
    if size not in WHISPER_SIZES:
        return _error("Modelo de transcrição inválido.")
    try:
        opts = parse_render_opts(data)
        opts.update({
            "method": method,
            "size": size,
            "language": (data.get("language") or "").strip(),
            "count": max(1, min(int(data.get("count", 3)), 10)),
            "length": max(5.0, min(float(data.get("length", 45)), 180.0)),
        })
    except (TypeError, ValueError) as exc:
        return _error(str(exc) if str(exc) else "Parâmetros inválidos.")

    job_id = uuid.uuid4().hex
    jobs[job_id] = new_job(total=len(paths), items=[new_item(p) for p in paths])
    threading.Thread(target=run_queue, args=(job_id, paths, opts), daemon=True).start()
    return jsonify(job_id=job_id)


def output_path(rel):
    """Caminho seguro dentro da pasta de saída (impede sair da pasta com '..')."""
    p = (OUTPUT_DIR / rel).resolve()
    root = OUTPUT_DIR.resolve()
    if root not in p.parents or p.suffix.lower() != ".mp4" or not p.is_file():
        raise ValueError("Arquivo de corte inválido.")
    return p


def caption_for(video):
    """Legenda e hashtags salvas ao lado do vídeo (arquivo .txt)."""
    sidecar = video.with_suffix(".txt")
    legenda, tags = "", ""
    if sidecar.is_file():
        for line in sidecar.read_text(encoding="utf-8").splitlines():
            if line.startswith("Legenda:"):
                legenda = line[len("Legenda:"):].strip()
            elif line.startswith("Hashtags:"):
                tags = line[len("Hashtags:"):].strip()
    return "\n\n".join(x for x in (legenda, tags) if x)


def run_tiktok_publish(job_id, video, privacy):
    job = jobs[job_id]
    try:
        caption = caption_for(video)
        job["message"] = "Enviando para o TikTok..."
        publish_id = tiktok.publish_file(str(video), caption, privacy, confirm=True)
        job["message"] = "Aguardando o TikTok processar o vídeo..."
        tiktok.wait_for_publish(publish_id, on_status=lambda st: job.update(message=f"TikTok: {st}"))
        job["message"] = "Publicado no TikTok."
        job["state"] = "done"
    except Exception as exc:  # noqa: BLE001
        job["error"] = str(exc)
        job["state"] = "error"
        job["message"] = "Falhou."


@app.get("/api/tiktok/status")
def api_tiktok_status():
    return jsonify(configured=tiktok.is_configured(), privacy_choices=tiktok.PRIVACY_CHOICES)


@app.get("/api/caption")
def api_caption():
    try:
        video = output_path(request.args.get("path", ""))
    except ValueError as exc:
        return _error(str(exc))
    return jsonify(caption=caption_for(video))


@app.post("/api/tiktok/publish")
def api_tiktok_publish():
    data = request.get_json(silent=True) or {}
    if data.get("confirm") is not True:
        return _error("Confirme a publicação antes de enviar.")
    if not tiktok.is_configured():
        return _error("Defina TIKTOK_ACCESS_TOKEN e reinicie o app (veja o README).")
    privacy = data.get("privacy", "SELF_ONLY")
    if privacy not in tiktok.PRIVACY_CHOICES:
        return _error("Privacidade inválida.")
    try:
        video = output_path(data.get("path", ""))
    except ValueError as exc:
        return _error(str(exc))
    job_id = uuid.uuid4().hex
    jobs[job_id] = new_job()
    threading.Thread(target=run_tiktok_publish, args=(job_id, video, privacy), daemon=True).start()
    return jsonify(job_id=job_id)


@app.get("/api/jobs/<job_id>")
def api_job(job_id):
    job = jobs.get(job_id)
    if job is None:
        abort(404)
    return jsonify(job)


@app.get("/media")
def media():
    """Serve o vídeo carregado para o player da página (suporta seek)."""
    if not state["source"]:
        abort(404)
    return send_file(state["source"], conditional=True)


@app.get("/output/<path:filename>")
def output(filename):
    return send_from_directory(OUTPUT_DIR, filename)


if __name__ == "__main__":
    url = "http://127.0.0.1:5000"
    print(f"Abra no navegador: {url}")
    threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
