"""Publicação de vídeos no TikTok pela Content Posting API (Direct Post).

Requisitos (fora deste código):
  * um app de desenvolvedor em developers.tiktok.com com o escopo video.publish;
  * um access token de usuário, na variável de ambiente TIKTOK_ACCESS_TOKEN.
    O fluxo OAuth exige uma URL de redirecionamento HTTPS, então o token é obtido
    fora deste app (por exemplo, pelo seu próprio servidor ou pelo painel de testes do TikTok).

Enquanto o app não for aprovado pelo TikTok (auditoria), os posts só podem ser
privados (SELF_ONLY). Este módulo nunca publica sem confirmação explícita: quem
chama deve passar confirm=True, e a interface pede confirmação antes.
"""
import json
import os
import time
import urllib.error
import urllib.request

API = "https://open.tiktokapis.com/v2/post/publish"
PRIVACY_CHOICES = ["SELF_ONLY", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "PUBLIC_TO_EVERYONE"]
MAX_SINGLE_UPLOAD = 64 * 1024 * 1024
CAPTION_MAX = 2200


class TikTokError(RuntimeError):
    pass


def token():
    return os.environ.get("TIKTOK_ACCESS_TOKEN", "").strip()


def is_configured():
    return bool(token())


def _post_json(url, payload, access_token):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {access_token}",
                 "Content-Type": "application/json; charset=UTF-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise TikTokError(f"TikTok respondeu HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}")
    except urllib.error.URLError as exc:
        raise TikTokError(f"Sem conexão com o TikTok: {exc.reason}")
    err = body.get("error", {})
    if err.get("code") not in (None, "ok"):
        raise TikTokError(f"TikTok: {err.get('code')} - {err.get('message', '')}")
    return body.get("data", {})


def creator_info(access_token=None):
    """Opções de privacidade permitidas para esta conta."""
    return _post_json(f"{API}/creator_info/query/", {}, access_token or token())


def publish_file(video_path, caption, privacy, confirm, access_token=None):
    """Envia o arquivo e retorna o publish_id. Lança TikTokError em qualquer falha."""
    if confirm is not True:
        raise TikTokError("Publicação não confirmada.")
    if privacy not in PRIVACY_CHOICES:
        raise TikTokError("Privacidade inválida.")
    access_token = access_token or token()
    if not access_token:
        raise TikTokError("Defina a variável TIKTOK_ACCESS_TOKEN e reinicie o app.")

    info = creator_info(access_token)
    allowed = info.get("privacy_level_options", [])
    if privacy not in allowed:
        raise TikTokError(f"Esta conta não permite a privacidade {privacy}. Opções: {', '.join(allowed)}")

    size = os.path.getsize(video_path)
    if size > MAX_SINGLE_UPLOAD:
        raise TikTokError("Vídeo maior que 64 MB: reduza o tamanho antes de publicar.")

    init = _post_json(f"{API}/video/init/", {
        "post_info": {
            "title": caption[:CAPTION_MAX],
            "privacy_level": privacy,
            "disable_duet": False,
            "disable_stitch": False,
            "disable_comment": False,
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": size,
            "chunk_size": size,
            "total_chunk_count": 1,
        },
    }, access_token)
    publish_id, upload_url = init["publish_id"], init["upload_url"]

    with open(video_path, "rb") as f:
        data = f.read()
    req = urllib.request.Request(
        upload_url, data=data, method="PUT",
        headers={"Content-Type": "video/mp4",
                 "Content-Length": str(size),
                 "Content-Range": f"bytes 0-{size - 1}/{size}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300):
            pass
    except urllib.error.HTTPError as exc:
        raise TikTokError(f"Falha no upload (HTTP {exc.code}).")
    return publish_id


def publish_status(publish_id, access_token=None):
    """Status do post. Confirme os nomes dos campos na documentação antes de usar em produção."""
    return _post_json(f"{API}/status/fetch/", {"publish_id": publish_id}, access_token or token())


def wait_for_publish(publish_id, timeout=180, every=5, access_token=None, on_status=None):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        data = publish_status(publish_id, access_token)
        last = data.get("status")
        if on_status:
            on_status(last)
        if last == "PUBLISH_COMPLETE":
            return data
        if last == "FAILED":
            raise TikTokError(f"TikTok recusou o post: {data.get('fail_reason', 'motivo não informado')}")
        time.sleep(every)
    raise TikTokError(f"Tempo esgotado aguardando o TikTok (último status: {last}).")
