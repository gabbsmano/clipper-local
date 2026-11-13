"""Transcrição local com faster-whisper (Whisper rodando no seu PC, sem API paga).

O resultado fica em cache (output/_cache), então o mesmo vídeo não é transcrito duas vezes.
"""
import hashlib
import json
import subprocess
import threading
from pathlib import Path

import imageio_ffmpeg
import numpy as np
from faster_whisper import WhisperModel

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
_models = {}
_lock = threading.Lock()


def decode_audio(source):
    """Decodifica o áudio com o ffmpeg embutido (16 kHz mono float32).

    Usamos o ffmpeg direto em vez do decodificador do faster-whisper, que
    quebra com algumas versões do PyAV.
    """
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(source),
           "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    if not raw:
        raise RuntimeError("O vídeo não tem áudio para transcrever.")
    return np.frombuffer(raw, dtype=np.float32)


def _model(size):
    with _lock:
        if size not in _models:
            # O primeiro uso baixa o modelo (tiny ~75 MB, small ~500 MB) e fica salvo no cache do Hugging Face.
            _models[size] = WhisperModel(size, device="cpu", compute_type="int8")
        return _models[size]


def cache_key(source, size):
    p = Path(source)
    st = p.stat()
    raw = f"{p.resolve()}|{st.st_size}|{int(st.st_mtime)}|{size}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def load_cached(source, size, cache_dir):
    f = Path(cache_dir) / f"{cache_key(source, size)}.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    return None


def transcribe(source, size, language, cache_dir, progress=None):
    """Retorna {"language": str, "segments": [{start, end, text, words: [{start, end, word}]}]}."""
    cached = load_cached(source, size, cache_dir)
    if cached is not None:
        return cached

    model = _model(size)
    segments, info = model.transcribe(
        decode_audio(source),
        language=language or None,  # None = detecção automática
        word_timestamps=True,
        vad_filter=True,            # ignora trechos sem fala (música, silêncio)
        beam_size=5,
    )
    result = {"language": info.language, "segments": []}
    for seg in segments:  # o gerador só processa de fato ao ser consumido
        words = [
            {"start": round(w.start, 2), "end": round(w.end, 2), "word": w.word.strip()}
            for w in (seg.words or []) if w.word.strip()
        ]
        result["segments"].append({
            "start": round(seg.start, 2),
            "end": round(seg.end, 2),
            "text": seg.text.strip(),
            "words": words,
        })
        if progress:
            progress(seg.end, info.duration)

    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    (Path(cache_dir) / f"{cache_key(source, size)}.json").write_text(
        json.dumps(result, ensure_ascii=False), encoding="utf-8"
    )
    return result


def all_words(transcript):
    return [w for seg in transcript["segments"] for w in seg["words"]]
