"""Detecção local de 'melhores momentos' — sem IA paga.

Pontua janelas de N segundos combinando:
  * energia do áudio (momentos mais altos/intensos, risadas, gritos, trilha forte);
  * densidade de fala (muitas palavras por segundo) — se houver transcrição;
  * sinais de empolgação no texto (!, ?, risadas tipo "kkkk", "haha").
Depois escolhe as janelas com maior pontuação sem sobreposição.
"""
import re
import subprocess

import numpy as np

SR = 8000  # taxa de amostragem usada só para análise de volume
LAUGH = re.compile(r"\b(k{3,}|rs{2,}|ha(ha)+|hehe+)\b", re.IGNORECASE)


def audio_levels(source, ffmpeg):
    """Volume RMS em dB, um valor por segundo do vídeo."""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(source),
           "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    n = len(samples) // SR
    if n == 0:
        return np.array([])
    per_sec = samples[: n * SR].reshape(n, SR)
    rms = np.sqrt(np.mean(per_sec ** 2, axis=1) + 1e-10)
    return 20 * np.log10(rms)


def _z(x):
    std = x.std()
    return (x - x.mean()) / std if std > 1e-9 else np.zeros_like(x)


def find_highlights(duration, levels_db, transcript=None, count=5, length=45, step=5):
    D = int(duration)
    if D <= 0:
        return []
    length = max(5, min(int(length), D))

    energy = np.full(D, -80.0)
    n = min(D, len(levels_db))
    energy[:n] = levels_db[:n]
    z_energy = _z(energy)

    words_per_sec = np.zeros(D)
    excitement = np.zeros(D)
    if transcript:
        for seg in transcript["segments"]:
            for w in seg["words"]:
                i = int(w["start"])
                if i < D:
                    words_per_sec[i] += 1
            text = seg["text"]
            if "!" in text or "?" in text or LAUGH.search(text):
                a = int(seg["start"])
                b = min(D, int(seg["end"]) + 1)
                excitement[a:b] += 1
    z_words = _z(words_per_sec) if transcript else np.zeros(D)

    candidates = []
    for s in range(0, max(1, D - length + 1), step):
        e = s + length
        score = (z_energy[s:e].mean()
                 + z_words[s:e].mean()
                 + 0.5 * excitement[s:e].mean())
        candidates.append((float(score), s, e))
    candidates.sort(reverse=True)

    picked = []
    for score, s, e in candidates:
        if any(s < p["end"] + 3 and p["start"] < e + 3 for p in picked):
            continue  # evita janelas vizinhas repetidas
        preview = ""
        if transcript:
            texts = [seg["text"] for seg in transcript["segments"]
                     if seg["end"] > s and seg["start"] < e]
            preview = " ".join(texts)[:140]
        picked.append({"start": float(s), "end": float(e), "score": round(score, 2), "preview": preview})
        if len(picked) >= count:
            break
    return picked
