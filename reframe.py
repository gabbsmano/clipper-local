"""Reenquadramento vertical inteligente para dois falantes.

Como funciona (tudo local):
1. Amostra o vídeo a 2 quadros por segundo e detecta rostos com YuNet (OpenCV).
2. Acompanha cada rosto entre quadros pela posição mais próxima.
3. Para cada rosto, mede a 'atividade da boca': quanto a região da boca muda
   entre quadros. Quem fala mexe mais a boca.
4. Usa o áudio: durante silêncios ninguém troca de quem está em foco.
5. Gera uma sequência de pontos (tempo, posição horizontal) para o crop 9:16.

Limitação: é uma heurística. Funciona bem em podcasts com câmeras frontais;
movimento grande de cabeça pode confundir a detecção de quem fala.
"""
import subprocess
import urllib.request
from pathlib import Path

import cv2
import numpy as np

from cuts import is_speech

BASE_DIR = Path(__file__).resolve().parent
MODEL_DIR = BASE_DIR / "models"
MODEL_PATH = MODEL_DIR / "face_detection_yunet_2023mar.onnx"
MODEL_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/"
             "face_detection_yunet/face_detection_yunet_2023mar.onnx")

SAMPLE_FPS = 2
ANALYSIS_WIDTH = 480
SWITCH_RATIO = 1.5    # o outro rosto precisa ser 50% mais ativo para trocar o foco
MIN_HOLD = 1.0        # segundos mínimos antes de trocar de falante
MOVE_THRESHOLD = 0.03  # mudança mínima de posição (fração da largura) para novo ponto
LOST_SECONDS = 3.0    # tempo sem ver o falante ativo antes de trocar de pessoa
MAX_MISS = 8          # amostras (4 s) que um rosto pode sumir e continuar sendo acompanhado


def ensure_model():
    if not MODEL_PATH.exists():
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)
    return str(MODEL_PATH)


def crop_width_for(src_h):
    return int(src_h * 9 / 16) // 2 * 2


def can_reframe(src_w, src_h):
    return bool(src_w and src_h and src_w > crop_width_for(src_h) + 16)


def _sample_frames(ffmpeg, source, start, dur, src_h, src_w):
    """Gera (t_local, frame BGR pequeno) em streaming, sem guardar o vídeo inteiro."""
    h = round(src_h * ANALYSIS_WIDTH / src_w / 2) * 2
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}",
           "-i", str(source), "-t", f"{dur:.3f}",
           "-vf", f"fps={SAMPLE_FPS},scale={ANALYSIS_WIDTH}:{h}",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    frame_bytes = ANALYSIS_WIDTH * h * 3
    i = 0
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            yield i / SAMPLE_FPS, np.frombuffer(buf, np.uint8).reshape(h, ANALYSIS_WIDTH, 3)
            i += 1
    finally:
        proc.stdout.close()
        proc.wait()


def _mouth_activity(gray, prev_gray, box):
    """Quanto a região da boca mudou desde o quadro anterior (0 = parado)."""
    if prev_gray is None:
        return 0.0
    h, w = gray.shape[:2]
    x, y, fw, fh = box
    my0, my1 = int(y + fh * 0.62), int(y + fh * 0.95)
    mx0, mx1 = int(x + fw * 0.25), int(x + fw * 0.75)
    if not (0 <= my0 < my1 <= h and 0 <= mx0 < mx1 <= w):
        return 0.0
    diff = cv2.absdiff(gray[my0:my1, mx0:mx1], prev_gray[my0:my1, mx0:mx1])
    return float(diff.mean())


def _detect_tracks(ffmpeg, source, start, dur, src_w, src_h):
    """Retorna ({track_id: {índice: (cx_norm, atividade_da_boca)}}, número de amostras).

    Um rosto que some por alguns quadros (cabeça virada, oclusão, ruído) continua
    sendo acompanhado com a última posição conhecida, por até MAX_MISS amostras.
    """
    model = ensure_model()
    detector = None
    tracks = {}   # id -> {"cx": pixels, "box": (x, y, w, h), "seen": índice}
    series = {}   # id -> {índice: (cx_norm, atividade)}
    prev_gray = None
    n = 0
    for _, frame in _sample_frames(ffmpeg, source, start, dur, src_h, src_w):
        h, w = frame.shape[:2]
        if detector is None:
            detector = cv2.FaceDetectorYN.create(model, "", (w, h), 0.7, 0.3, 10)
        detector.setInputSize((w, h))
        _, faces = detector.detect(frame)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        detections = []
        if faces is not None:
            # só os dois maiores rostos
            for f in sorted(faces, key=lambda f: f[2] * f[3], reverse=True)[:2]:
                x, y, fw, fh = [int(v) for v in f[:4]]
                detections.append((x + fw / 2, (x, y, fw, fh)))

        # associa cada rosto ao acompanhamento mais próximo; cria novo se não houver
        assigned = set()
        for cx, box in detections:
            best, best_d = None, None
            for tid, tr in tracks.items():
                d = abs(cx - tr["cx"])
                if tid not in assigned and d < tr["box"][2] * 0.8 and (best_d is None or d < best_d):
                    best, best_d = tid, d
            if best is None:
                best = max(list(tracks.keys()) + [-1]) + 1
                tracks[best] = {}
            assigned.add(best)
            tracks[best].update(cx=cx, box=box, seen=n)

        for tid, tr in tracks.items():
            if n - tr["seen"] > MAX_MISS:
                continue  # rosto perdido há tempo demais: não entra na análise deste quadro
            activity = _mouth_activity(gray, prev_gray, tr["box"])
            series.setdefault(tid, {})[n] = (tr["cx"] / w, activity)

        prev_gray = gray
        n += 1
    return series, n


def _smooth(values, k=3):
    if not values:
        return values
    out = []
    for i in range(len(values)):
        lo = max(0, i - k // 2)
        hi = min(len(values), i + k // 2 + 1)
        out.append(sum(values[lo:hi]) / (hi - lo))
    return out


def speaker_track(ffmpeg, source, start, dur, src_w, src_h, silences_local=()):
    """Retorna [(t_local, cx_norm)] com pontos só quando o foco muda (ou se move bastante)."""
    series, n = _detect_tracks(ffmpeg, source, start, dur, src_w, src_h)
    if not series or n == 0:
        return None

    ids = sorted(series.keys())
    last_cx = {tid: None for tid in ids}
    activity = {tid: _smooth([series[tid].get(i, (None, 0.0))[1] for i in range(n)]) for tid in ids}

    active = None
    active_seen = -1e9   # último instante em que o falante ativo foi visto
    last_switch_t = -MIN_HOLD
    keypoints = []
    current_cx = None
    for i in range(n):
        t = i / SAMPLE_FPS
        for tid in ids:
            if i in series[tid]:
                last_cx[tid] = series[tid][i][0]
        visible = [tid for tid in ids if i in series[tid]]
        if not visible:
            continue  # ninguém na tela: mantém o foco atual

        speech = is_speech(t, silences_local)
        if active in visible:
            active_seen = t
        if active is None or (active not in visible and t - active_seen > LOST_SECONDS):
            # sem falante ativo, ou ele sumiu por tempo demais: pega quem aparece
            active = visible[0]
            active_seen = t
            last_switch_t = t
        elif active in visible and len(visible) > 1 and speech and t - last_switch_t >= MIN_HOLD:
            other = next(tid for tid in visible if tid != active)
            if activity[other][i] > activity[active][i] * SWITCH_RATIO + 1.0:
                active = other
                last_switch_t = t

        cx = series[active][i][0] if i in series[active] else last_cx[active]
        if cx is None:
            continue
        if current_cx is None or abs(cx - current_cx) > MOVE_THRESHOLD:
            keypoints.append((t, cx))
            current_cx = cx

    if not keypoints:
        return None
    if keypoints[0][0] > 0:
        keypoints.insert(0, (0.0, keypoints[0][1]))
    return keypoints


def crop_expression(keypoints, src_w, src_h):
    """Expressão do filtro crop: x varia com o tempo local do corte."""
    cw = crop_width_for(src_h)
    xs = []
    for _, cx in keypoints:
        x = round(cx * src_w - cw / 2)
        xs.append(int(min(max(0, x), src_w - cw)))
    expr = str(xs[-1])
    for i in range(len(keypoints) - 1, 0, -1):
        expr = f"if(lt(t,{keypoints[i][0]:.2f}),{xs[i - 1]},{expr})"
    return f"'{expr}'", cw
