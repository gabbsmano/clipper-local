"""Escolha de melhores momentos e títulos com a API da Claude.

Usa a transcrição (com tempos) e pede ao modelo trechos autocontidos, com gancho,
um título em português e uma nota de 0 a 100. A saída é forçada por JSON Schema
(output_config.format), então o resultado sempre é parseável.

Vídeos longos são divididos em janelas de ~20 min para caber bem no contexto e
manter a qualidade. Cada janela tem um pouco de sobreposição para não cortar
momentos na divisão.
"""
import json
import math
import os

from anthropic import Anthropic, AuthenticationError

# Modelo padrão. Pode ser trocado sem mexer no código:
#   set CLIPPER_CLAUDE_MODEL=claude-sonnet-5-5
DEFAULT_MODEL = os.environ.get("CLIPPER_CLAUDE_MODEL", "claude-opus-5-5")
CHUNK_SECONDS = 1200   # 20 min por chamada
OVERLAP = 60           # segundos extras no fim de cada janela
MAX_TOKENS = 16000

HIGHLIGHT_SCHEMA = {
    "type": "object",
    "properties": {
        "highlights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "score": {"type": "integer"},
                    "title": {"type": "string"},
                    "reason": {"type": "string"},
                    "description": {"type": "string"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["start", "end", "score", "title", "reason", "description", "hashtags"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["highlights"],
    "additionalProperties": False,
}

SYSTEM = (
    "Você é editor de vídeos virais para TikTok, Reels e YouTube Shorts. "
    "Recebe a transcrição com tempos de um trecho de vídeo (podcast, gameplay, "
    "entrevista ou similar) e escolhe os momentos com maior potencial de viralizar: "
    "piadas, reações fortes, revelações, opiniões polêmicas, histórias com final "
    "surpreendente ou frases de efeito. Prefira trechos que façam sentido sem contexto "
    "extra. Seja seletivo: é melhor devolver poucos bons momentos do que muitos fracos."
)


def is_configured():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _transcript_lines(segments, t0, t1):
    return "\n".join(
        f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}"
        for s in segments if s["end"] > t0 and s["start"] < t1
    )


def _ask_window(client, model, segments, t0, t1, n, length):
    text = _transcript_lines(segments, t0, t1)
    if not text.strip():
        return []
    prompt = f"""Trecho do vídeo: de {t0:.0f}s a {t1:.0f}s.

Transcrição (tempos em segundos):
{text}

Escolha até {n} trechos com alto potencial de viralizar. Cada trecho deve:
- durar entre {max(10, int(length * 0.6))} e {int(length * 1.3)} segundos;
- começar com um gancho (frase forte, pergunta, reação, piada) e fazer sentido sozinho;
- usar apenas tempos que aparecem na transcrição, dentro de [{t0:.0f}, {t1:.0f}];
- ter um título curto em português do Brasil, com no máximo 60 caracteres;
- ter score de 0 a 100 e uma razão curta (uma frase);
- ter uma legenda para o post (até 150 caracteres, em português, sem hashtags);
- ter de 3 a 5 hashtags relevantes, cada uma começando com #, sem espaços.
Se nenhum trecho valer a pena, devolva a lista vazia."""

    resp = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": HIGHLIGHT_SCHEMA},
        },
    )
    raw = next((b.text for b in resp.content if b.type == "text"), "{}")
    return json.loads(raw).get("highlights", [])


def _validate(cands, duration, length):
    lo, hi = max(5.0, length * 0.5), length * 1.5
    out = []
    for c in cands:
        try:
            start = max(0.0, float(c["start"]))
            end = min(float(duration), float(c["end"]))
        except (KeyError, TypeError, ValueError):
            continue
        if not (lo <= end - start <= hi):
            continue
        out.append({
            "start": round(start, 2),
            "end": round(end, 2),
            "score": int(c.get("score", 0) or 0),
            "title": str(c.get("title", "")).strip()[:80],
            "preview": str(c.get("reason", "")).strip()[:200],
            "description": str(c.get("description", "")).strip()[:300],
            "hashtags": _clean_tags(c.get("hashtags", [])),
        })
    return out


def _clean_tags(tags):
    out = []
    for tag in tags if isinstance(tags, list) else []:
        tag = "#" + "".join(ch for ch in str(tag) if ch.isalnum() or ch == "_").lstrip("#")
        if len(tag) > 1 and tag not in out:
            out.append(tag)
    return out[:5]


def _pick(cands, count, gap=3.0):
    picked = []
    for c in sorted(cands, key=lambda x: x["score"], reverse=True):
        if any(c["start"] < p["end"] + gap and p["start"] < c["end"] + gap for p in picked):
            continue
        picked.append(c)
        if len(picked) >= count:
            break
    return picked


def ai_highlights(transcript, duration, count=5, length=45, model=None):
    """Retorna momentos no mesmo formato de highlights.find_highlights, com 'title' e 'preview'."""
    if not is_configured():
        raise RuntimeError("Defina a variável ANTHROPIC_API_KEY para usar a IA (veja o README).")

    client = Anthropic()
    model = model or DEFAULT_MODEL
    segments = transcript["segments"]

    windows, t = [], 0.0
    while t < duration:
        windows.append((t, min(float(duration), t + CHUNK_SECONDS + OVERLAP)))
        t += CHUNK_SECONDS

    candidates = []
    try:
        for t0, t1 in windows:
            raw = _ask_window(client, model, segments, t0, t1, count + 2, length)
            candidates += _validate(raw, duration, length)
    except AuthenticationError:
        raise RuntimeError("Chave da API inválida. Confira a variável ANTHROPIC_API_KEY.")

    return _pick(candidates, count)
