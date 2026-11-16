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
    # 16/11/2026: "teste": "MANUAL" ou "SS_PERFORMANCE" publica como Reel de teste (só não seguidores).
    # A API não promove o teste para o perfil; com SS_PERFORMANCE o próprio Instagram promove se for bem.
    if post.get("teste"):
        params["trial_params"] = json.dumps({"graduation_strategy": post["teste"]})
        params.pop("share_to_feed")
    c = chamar("POST", f"{IG_ID}/media", **params)["id"]
    esperar_pronto(tk, c, tentativas=120)  # vídeo processa mais devagar: até 10 min
    return chamar("POST", f"{IG_ID}/media_publish", creation_id=c, access_token=tk)["id"]


def publicar_stories(tk, post):
    """Cada imagem vira um Story, na ordem; devolve os ids separados por vírgula."""
    ids = []
    # 03/11/2026: Story de vídeo (video_story, até 60 s) ou imagens, na ordem.
    midias = [("video_url", post["video_story"])] if post.get("video_story") else [("image_url", p) for p in post["imagens"]]
    for campo, p in midias:
        c = chamar("POST", f"{IG_ID}/media", media_type="STORIES", access_token=tk, **{campo: PAGES + p})["id"]
        esperar_pronto(tk, c)
        # 22/10/2026: a API respondeu "Media Not Found" logo depois de FINISHED; tenta de novo.
        for tentativa in range(4):
            try:
                ids.append(chamar("POST", f"{IG_ID}/media_publish", creation_id=c, access_token=tk)["id"])
                break
            except RuntimeError as e:
                if "2207006" not in str(e) or tentativa == 3:
                    raise
                time.sleep(8)
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


LI_ORG = "urn:li:organization:145064076"  # página Meta Saúde Ocupacional
LI_VERSAO = "202610"


def li_token():
    ids = [os.environ.get(k) for k in ("LI_CLIENT_ID", "LI_CLIENT_SECRET", "LI_REFRESH_TOKEN")]
    if not all(ids):
        return None
    dados = urllib.parse.urlencode(dict(grant_type="refresh_token", refresh_token=ids[2],
                                        client_id=ids[0], client_secret=ids[1])).encode()
    with urllib.request.urlopen("https://www.linkedin.com/oauth/v2/accessToken", data=dados, timeout=60) as r:
        return json.load(r)["access_token"]


def li_texto(t):
    """O LinkedIn lê o texto do post como 'little text': estes caracteres precisam de barra invertida."""
    return "".join("\\" + c if c in "\\|{}@[]()<>#*_~" else c for c in t)


def li_chamar(li, metodo, caminho, corpo=None):
    req = urllib.request.Request("https://api.linkedin.com/rest/" + caminho, method=metodo,
                                 data=json.dumps(corpo).encode() if corpo is not None else None,
                                 headers={"Authorization": "Bearer " + li, "LinkedIn-Version": LI_VERSAO,
                                          "X-Restli-Protocol-Version": "2.0.0", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            texto = r.read().decode()
            return (json.loads(texto) if texto else {}), r.headers
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"LinkedIn {metodo} {caminho} -> {e.code}: {e.read().decode()[:400]}") from None


def publicar_linkedin(li, post):
    """Vídeo pela Videos API (envio em partes), depois o post da página e, se houver, o comentário com o link."""
    video = (RAIZ / post["video"]).read_bytes()
    ini, _ = li_chamar(li, "POST", "videos?action=initializeUpload", {"initializeUploadRequest": {
        "owner": LI_ORG, "fileSizeBytes": len(video), "uploadCaptions": False, "uploadThumbnail": False}})
    v = ini["value"]
    etags = []
    for parte in v["uploadInstructions"]:
        pedaco = video[parte["firstByte"]:parte["lastByte"] + 1]
        req = urllib.request.Request(parte["uploadUrl"], data=pedaco, method="PUT",
                                     headers={"Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(req, timeout=600) as r:
            etags.append(r.headers["ETag"])
    li_chamar(li, "POST", "videos?action=finalizeUpload", {"finalizeUploadRequest": {
        "video": v["video"], "uploadToken": v.get("uploadToken", ""), "uploadedPartIds": etags}})
    for _ in range(60):  # o LinkedIn processa o vídeo antes de aceitar o post
        st, _ = li_chamar(li, "GET", "videos/" + urllib.parse.quote(v["video"], safe=""))
        if st.get("status") == "AVAILABLE":
            break
        if st.get("status") == "PROCESSING_FAILED":
            raise RuntimeError("LinkedIn: processamento do vídeo falhou")
        time.sleep(10)
    meta = post["linkedin"]
    _, cab = li_chamar(li, "POST", "posts", {
        "author": LI_ORG, "commentary": li_texto(meta["texto"]), "visibility": "PUBLIC",
        "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
        "content": {"media": {"title": meta.get("titulo", ""), "id": v["video"]}},
        "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False})
    urn = cab.get("x-restli-id")
    if meta.get("comentario") and urn:
        try:  # link no comentário, não no corpo (no corpo o alcance cai)
            li_chamar(li, "POST", "socialActions/" + urllib.parse.quote(urn, safe="") + "/comments",
                      {"actor": LI_ORG, "object": urn, "message": {"text": meta["comentario"]}})
        except Exception as e:
            print("  LinkedIn: post saiu, comentário falhou:", str(e)[:200])
    return urn


def pendentes_linkedin(agenda, agora):
    """Publica na página do LinkedIn o que já saiu no Instagram e tem o bloco "linkedin"."""
    fila = [p for p in agenda["posts"] if p.get("publicado") and p.get("video") and p.get("linkedin")
            and not p.get("linkedin_publicado") and p.get("linkedin_tentativas", 0) < 3]
    if not fila:
        return False
    li = li_token()
    if not li:
        print("LinkedIn: segredos ausentes, nada enviado.")
        return False
    for post in fila:
        try:
            urn = publicar_linkedin(li, post)
            post["linkedin_publicado"] = {"post": urn, "em": agora.isoformat(timespec="seconds")}
            print("  LinkedIn:", post["id"], "->", urn)
        except Exception as e:
            post["linkedin_tentativas"] = post.get("linkedin_tentativas", 0) + 1
            print("  LinkedIn falhou em", post["id"], f"(tentativa {post['linkedin_tentativas']}):", str(e)[:300])
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
    if pendentes_linkedin(agenda, agora):
        mudou = True
    if mudou:
        AGENDA.write_text(json.dumps(agenda, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        print("Nada vencido e aprovado para publicar agora.")


if __name__ == "__main__":
    main()
