"""Gera legendas (estilo TikTok) e título na tela no formato ASS.

Os tempos são relativos ao início do corte. Tudo é escalado pela altura do vídeo
de saída, então funciona tanto em 1080x1920 (vertical) quanto no formato original.
"""
import textwrap

SUB_FONT = 70       # tamanho base (pensado para 1920 px de altura)
SUB_OUTLINE = 6
SUB_MARGIN_V = 380  # distância da base
TITLE_FONT = 84
TITLE_BOX = 18
TITLE_MARGIN_V = 180

MAX_WORDS = 4       # palavras por linha de legenda
MAX_GAP = 0.6       # pausa (s) que quebra a legenda
MAX_CHUNK = 2.5     # duração máxima (s) de uma legenda


def _ts(t):
    t = max(0.0, t)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t - h * 3600 - m * 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _clean(text):
    return text.replace("{", "(").replace("}", ")").replace("\\", "").replace("\n", " ").strip()


def _chunks(words, start, end):
    inside = [w for w in words if w["end"] > start and w["start"] < end]
    out, cur = [], []
    for w in inside:
        if cur and (w["start"] - cur[-1]["end"] > MAX_GAP
                    or len(cur) >= MAX_WORDS
                    or w["end"] - cur[0]["start"] > MAX_CHUNK):
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return out


HIGHLIGHT = r"{\c&H00FFFF&}"   # amarelo (formato BGR do ASS)
RESET = r"{\c&HFFFFFF&}"       # volta para branco

SUB_STYLES = {"chunk", "word"}


def build_ass(clip_start, clip_end, words, title, title_seconds, width, height, subtitles,
              sub_style="chunk"):
    """Retorna o conteúdo de um arquivo .ass, ou None se não houver nada para mostrar.

    sub_style:
      "chunk" - blocos de até 4 palavras, aparecem e somem juntos.
      "word"  - o mesmo bloco, mas a palavra falada no momento fica amarela (estilo karaokê).
    `words` deve estar no tempo de saída (já editado), relativo ao início do vídeo final.
    """
    k = height / 1920
    dur = clip_end - clip_start
    events = []  # (style, start, end, text)

    if subtitles and words:
        for chunk in _chunks(words, clip_start, clip_end):
            a = max(0.0, chunk[0]["start"] - clip_start)
            b = min(dur, chunk[-1]["end"] - clip_start)
            if b - a < 0.2:
                b = min(dur, a + 0.2)
            if sub_style == "word":
                for j, w in enumerate(chunk):
                    wa = max(0.0, w["start"] - clip_start)
                    wb = min(dur, chunk[j + 1]["start"] - clip_start) if j + 1 < len(chunk) else b
                    if wb - wa < 0.12:
                        wb = min(dur, wa + 0.12)
                    parts = []
                    for m, other in enumerate(chunk):
                        word = _clean(other["word"])
                        parts.append(f"{HIGHLIGHT}{word}{RESET}" if m == j else word)
                    events.append(("Sub", wa, wb, " ".join(parts)))
            else:
                text = _clean(" ".join(w["word"] for w in chunk))
                if text:
                    events.append(("Sub", a, b, text))

    if title and title.strip():
        end_t = min(dur, title_seconds) if title_seconds and title_seconds > 0 else dur
        font_px = TITLE_FONT * k
        wrap = max(12, int(width * 0.85 / (font_px * 0.55)))
        lines = textwrap.wrap(_clean(title), wrap) or [_clean(title)]
        events.append(("Title", 0.0, end_t, "\\N".join(lines)))

    if not events:
        return None

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,Arial,{round(SUB_FONT * k)},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,{round(SUB_OUTLINE * k)},0,2,60,60,{round(SUB_MARGIN_V * k)},1
Style: Title,Arial,{round(TITLE_FONT * k)},&H00FFFFFF,&H000000FF,&HCC000000,&HCC000000,-1,0,0,0,100,100,0,0,3,{round(TITLE_BOX * k)},0,8,60,60,{round(TITLE_MARGIN_V * k)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [
        f"Dialogue: 0,{_ts(a)},{_ts(b)},{style},,0,0,0,,{text}"
        for style, a, b, text in events
    ]
    return header + "\n".join(lines) + "\n"
