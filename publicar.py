"""Robô de publicação do @metaocupacional (API do Instagram com login do Instagram).

Roda no GitHub Actions. Lê agenda.json e publica os posts APROVADOS cuja hora já chegou.
As imagens são servidas pelo GitHub Pages deste repositório (a API só aceita JPEG por URL pública).

Uso:
  python publicar.py             publica o que estiver vencido e aprovado (carrossel, imagem, Reels ou Stories: "stories": true)
  python publicar.py --conferir  só confere chave, limite e se as imagens abrem; não publica nada
  python publicar.py --renovar   renova a chave de 60 dias e regrava token.enc

YouTube: todo post com vídeo e com o bloco "youtube" (titulo, descricao, tags) sobe também como
Short, depois de publicado no Instagram. Falha no YouTube não trava o Instagram: tenta de novo nas
rodadas seguintes, até 3 vezes. Chaves nos segredos YT_CLIENT_ID, YT_CLIENT_SECRET e YT_REFRESH_TOKEN.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

RAIZ = Path(__file__).parent
AGENDA = RAIZ / "agenda.json"
TOKEN_ENC = RAIZ / "token.enc"
API = "https://graph.instagram.com/v23.0"
IG_ID = "17841421372960917"
PAGES = "https://luccasrgomes-bit.github.io/metaocupacional-publicacoes/"


def _openssl(args, entrada=None):
    k = os.environ.get("TOKEN_KEY")
    if not k:
        sys.exit("Falta o segredo TOKEN_KEY.")
    r = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-a", "-A", *args, "-pass", "env:TOKEN_KEY"],
                       input=entrada, capture_output=True, text=True, env={**os.environ, "TOKEN_KEY": k})
    if r.returncode:
        sys.exit("Falha na criptografia da chave: " + r.stderr.strip())
    return r.stdout.strip()


def ler_token():
    return _openssl(["-d", "-in", str(TOKEN_ENC)])


def gravar_token(tk):
    _openssl(["-out", str(TOKEN_ENC)], entrada=tk)


def chamar(metodo, caminho, **params):
    dados = urllib.parse.urlencode(params)
    url = caminho if caminho.startswith("http") else f"{API}/{caminho}"
    req = (urllib.request.Request(url + "?" + dados) if metodo == "GET"
           else urllib.request.Request(url, data=dados.encode(), method="POST"))
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        corpo = e.read().decode(errors="replace")
        raise RuntimeError(f"{metodo} {url.split('?')[0]} -> {e.code}: {corpo[:400]}") from None


def esperar_pronto(tk, container, tentativas=30):
    for _ in range(tentativas):
        st = chamar("GET", container, fields="status_code,status", access_token=tk)
        if st.get("status_code") == "FINISHED":
            return
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Container {container} falhou: {st}")
        time.sleep(5)
    raise RuntimeError(f"Container {container} não ficou pronto a tempo.")


def publicar_reels(tk, post):
    params = dict(media_type="REELS", video_url=PAGES + post["video"], caption=post["legenda"],
                  share_to_feed="true", access_token=tk)
    if post.get("capa"):
        params["cover_url"] = PAGES + post["capa"]
    c = chamar("POST", f"{IG_ID}/media", **params)["id"]
    esperar_pronto(tk, c, tentativas=120)  # vídeo processa mais devagar: até 10 min
    return chamar("POST", f"{IG_ID}/media_publish", creation_id=c, access_token=tk)["id"]


def publicar_stories(tk, post):
    """Cada imagem vira um Story, na ordem; devolve os ids separados por vírgula."""
    ids = []
    for p in post["imagens"]:
        c = chamar("POST", f"{IG_ID}/media", media_type="STORIES", image_url=PAGES + p, access_token=tk)["id"]
        esperar_pronto(tk, c)
        ids.append(chamar("POST", f"{IG_ID}/media_publish", creation_id=c, access_token=tk)["id"])
        time.sleep(3)
    return ",".join(ids)


def publicar_post(tk, post):
    if post.get("video"):
        return publicar_reels(tk, post)
    if post.get("stories"):
        return publicar_stories(tk, post)
    urls = [PAGES + p for p in post["imagens"]]
    if len(urls) == 1:
        c = chamar("POST", f"{IG_ID}/media", image_url=urls[0], caption=post["legenda"], access_token=tk)["id"]
    else:
        filhos = [chamar("POST", f"{IG_ID}/media", image_url=u, is_carousel_item="true", access_token=tk)["id"]
                  for u in urls]
        for f in filhos:
            esperar_pronto(tk, f)
        c = chamar("POST", f"{IG_ID}/media", media_type="CAROUSEL", children=",".join(filhos),
                   caption=post["legenda"], access_token=tk)["id"]
    esperar_pronto(tk, c)
    return chamar("POST", f"{IG_ID}/media_publish", creation_id=c, access_token=tk)["id"]


def yt_token():
    ids = [os.environ.get(k) for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN")]
    if not all(ids):
        return None
    dados = urllib.parse.urlencode(dict(client_id=ids[0], client_secret=ids[1], refresh_token=ids[2],
                                        grant_type="refresh_token")).encode()
    with urllib.request.urlopen("https://oauth2.googleapis.com/token", data=dados, timeout=60) as r:
        return json.load(r)["access_token"]


def publicar_youtube(yt, post):
    """Envio retomável da API do YouTube: primeiro os metadados, depois o arquivo inteiro."""
    meta = post["youtube"]
    corpo = {
        "snippet": {"title": meta["titulo"][:100], "description": meta["descricao"], "tags": meta.get("tags", []),
                    "categoryId": "27", "defaultLanguage": "pt-BR", "defaultAudioLanguage": "pt-BR"},
        "status": {"privacyStatus": meta.get("privacidade", "public"), "selfDeclaredMadeForKids": False},
    }
    video = (RAIZ / post["video"]).read_bytes()
    ini = urllib.request.Request(
        "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
        data=json.dumps(corpo).encode(), method="POST",
        headers={"Authorization": "Bearer " + yt, "Content-Type": "application/json; charset=UTF-8",
                 "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(len(video))})
    with urllib.request.urlopen(ini, timeout=60) as r:
        destino = r.headers["Location"]
    envio = urllib.request.Request(destino, data=video, method="PUT",
                                   headers={"Content-Type": "video/mp4", "Content-Length": str(len(video))})
    with urllib.request.urlopen(envio, timeout=600) as r:
        return json.load(r)["id"]


def pendentes_youtube(agenda, agora):
    """Sobe no YouTube o que ja saiu no Instagram e ainda nao saiu la."""
    fila = [p for p in agenda["posts"] if p.get("publicado") and p.get("video") and p.get("youtube")
            and not p.get("youtube_publicado") and p.get("youtube_tentativas", 0) < 3]
    if not fila:
        return False
    yt = yt_token()
    if not yt:
        print("YouTube: segredos ausentes, nada enviado.")
        return False
    for post in fila:
        try:
            vid = publicar_youtube(yt, post)
            post["youtube_publicado"] = {"video_id": vid, "em": agora.isoformat(timespec="seconds"),
                                         "link": f"https://youtube.com/shorts/{vid}"}
            print("  YouTube:", post["id"], "->", vid)
        except Exception as e:  # o Instagram ja saiu; registra e tenta na proxima rodada
            post["youtube_tentativas"] = post.get("youtube_tentativas", 0) + 1
            print("  YouTube falhou em", post["id"], f"(tentativa {post['youtube_tentativas']}):", str(e)[:300])
    return True


def conferir(tk, agenda):
    eu = chamar("GET", "me", fields="user_id,username,account_type", access_token=tk)
    lim = chamar("GET", f"{IG_ID}/content_publishing_limit", fields="quota_usage,config", access_token=tk)
    print("Conta:", eu.get("username"), eu.get("account_type"), "| limite:", lim["data"][0])
    for post in agenda["posts"]:
        for p in post.get("imagens", []) + [x for x in (post.get("video"), post.get("capa")) if x]:
            with urllib.request.urlopen(urllib.request.Request(PAGES + p, method="HEAD"), timeout=30) as r:
                print(f"  {post['id']} {p}: {r.headers.get('Content-Type')}")
    print("Nada foi publicado.")


def main():
    agenda = json.loads(AGENDA.read_text(encoding="utf-8"))
    tk = ler_token()
    if "--conferir" in sys.argv:
        return conferir(tk, agenda)
    if "--renovar" in sys.argv:
        novo = chamar("GET", "https://graph.instagram.com/refresh_access_token",
                      grant_type="ig_refresh_token", access_token=tk)
        gravar_token(novo["access_token"])
        print("Chave renovada por", round(novo.get("expires_in", 0) / 86400), "dias.")
        return
    agora = datetime.now(timezone.utc)
    mudou = False
    for post in agenda["posts"]:
        if post.get("publicado") or not post.get("aprovado"):
            continue
        if datetime.fromisoformat(post["quando"]) > agora:
            continue
        print("Publicando", post["id"], "...")
        media = publicar_post(tk, post)
        post["publicado"] = {"media_id": media, "em": agora.isoformat(timespec="seconds")}
        mudou = True
        print("  publicado:", media)
    if pendentes_youtube(agenda, agora):
        mudou = True
    if mudou:
        AGENDA.write_text(json.dumps(agenda, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        print("Nada vencido e aprovado para publicar agora.")


if __name__ == "__main__":
    main()
