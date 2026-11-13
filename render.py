"""Renderização de um corte com ffmpeg.

Um único grafo de filtros cuida de tudo, nesta ordem:
  reenquadramento (crop dinâmico, opcional) -> jump cuts (trim + concat, opcional)
  -> formato (vertical, vertical com fundo desfocado ou original) -> legenda/título (ASS).
"""
import subprocess
from pathlib import Path

import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

BLUR_BG = ("split=2[bg][fg];"
           "[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:3[bgb];"
           "[fg]scale=1080:-2[fgs];"
           "[bgb][fgs]overlay=(W-w)/2:(H-h)/2")


def build_graph(fmt, dur, keep, has_audio, ass_name=None, crop=None):
    """Retorna (grafo, rótulo de vídeo, mapeamento de áudio ou None)."""
    parts = []
    vsrc = "[0:v]"

    if crop:
        crop_x, crop_w, src_h = crop
        parts.append(f"[0:v]crop={crop_w}:{src_h}:{crop_x}:0[vr]")
        vsrc = "[vr]"

    whole = len(keep) == 1 and keep[0][0] <= 0.001 and abs(keep[0][1] - dur) <= 0.01
    amap = "0:a?" if has_audio else None
    if not whole:
        n = len(keep)
        # Cada entrada de filtro só pode ser usada uma vez: divide o vídeo e o áudio antes de cortar.
        vlabels = [f"[vs{i}]" for i in range(n)]
        alabels = [f"[as{i}]" for i in range(n)]
        if n > 1:
            parts.append(f"{vsrc}split={n}" + "".join(vlabels))
            if has_audio:
                parts.append(f"[0:a]asplit={n}" + "".join(alabels))
        else:
            vlabels = [vsrc]
            alabels = ["[0:a]"]
        for i, (a, b) in enumerate(keep):
            parts.append(f"{vlabels[i]}trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[sv{i}]")
            if has_audio:
                parts.append(f"{alabels[i]}atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[sa{i}]")
        if has_audio:
            # concat espera os pares na ordem: vídeo1, áudio1, vídeo2, áudio2...
            pairs = "".join(f"[sv{i}][sa{i}]" for i in range(n))
            parts.append(f"{pairs}concat=n={n}:v=1:a=1[vc][ac]")
            amap = "[ac]"
        else:
            parts.append("".join(f"[sv{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vc]")
        vsrc = "[vc]"

    if fmt == "vertical_blur":
        parts.append(f"{vsrc}{BLUR_BG}[v0]")
    elif fmt == "vertical":
        if crop:
            parts.append(f"{vsrc}scale=1080:1920[v0]")
        else:
            parts.append(f"{vsrc}scale=-2:1920,crop=1080:1920[v0]")
    else:
        parts.append(f"{vsrc}null[v0]")

    vout = "[v0]"
    if ass_name:
        parts.append(f"[v0]ass={ass_name}[vout]")
        vout = "[vout]"
    return ";".join(parts), vout, amap


def render_clip(source, start, dur, out_file, fmt, keep, has_audio, work_dir,
                ass_text=None, crop=None):
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    ass_name = None
    if ass_text:
        ass_name = "legenda.ass"
        (work_dir / ass_name).write_text(ass_text, encoding="utf-8")

    graph, vout, amap = build_graph(fmt, dur, keep, has_audio, ass_name, crop)
    (work_dir / "graph.txt").write_text(graph, encoding="utf-8")

    args = ["-y", "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{dur:.3f}",
            "-/filter_complex", "graph.txt", "-map", vout]
    if amap:
        args += ["-map", amap]
    args += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out_file),
    ]
    res = subprocess.run([FFMPEG, "-hide_banner", *args], cwd=work_dir,
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode != 0:
        lines = res.stderr.strip().splitlines()
        raise RuntimeError(lines[-1] if lines else "erro no ffmpeg")
