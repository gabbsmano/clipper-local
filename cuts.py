"""Silêncios, pausas e 'jump cuts' (remover pausas longas).

Os tempos de um corte são sempre locais: 0 = início do corte.
"""
import re
import subprocess

SILENCE_RE = re.compile(r"silence_(start|end): (-?[\d.]+)")


def find_silences(source, ffmpeg, noise_db=-30, min_dur=0.3):
    """Lista de (início, fim) em segundos de todos os silêncios do arquivo."""
    res = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(source), "-vn",
         "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    intervals, start = [], None
    for kind, value in SILENCE_RE.findall(res.stderr):
        v = float(value)
        if kind == "start":
            start = max(0.0, v)
        elif start is not None:
            intervals.append((start, v))
            start = None
    return intervals


def pauses_from_silences(silences, min_pause=0.4):
    """Meio de cada pausa, útil como ponto de corte para gerar cortes automáticos."""
    return [round((s + e) / 2, 2) for s, e in silences if e - s >= min_pause]


def keep_segments(clip_start, clip_end, silences, min_gap=0.6, pad=0.15):
    """Trechos a manter dentro do corte, em tempo local.

    Silêncios com duração >= min_gap são encurtados: fica só `pad` segundos de
    cada lado. O resto da pausa é removido.
    """
    removed = []
    for s, e in silences:
        if e <= clip_start or s >= clip_end or e - s < min_gap:
            continue
        a = max(s, clip_start) + pad
        b = min(e, clip_end) - pad
        if b > a:
            removed.append((a, b))

    keep, cur = [], clip_start
    for a, b in sorted(removed):
        if a > cur:
            keep.append((cur, a))
        cur = max(cur, b)
    if cur < clip_end:
        keep.append((cur, clip_end))

    local = [(a - clip_start, b - clip_start) for a, b in keep if b - a > 0.05]
    return local or [(0.0, clip_end - clip_start)]


def map_time(t_local, keep):
    """Converte um tempo local do corte original para o tempo do corte já editado."""
    offset = 0.0
    for a, b in keep:
        if t_local <= a:
            return offset
        if t_local <= b:
            return offset + (t_local - a)
        offset += b - a
    return offset


def is_speech(t_local, silences_local):
    return not any(a <= t_local < b for a, b in silences_local)
