# Publicações do @metaocupacional

Robô que publica no Instagram da Meta Saúde Ocupacional pela API oficial.

- `agenda.json`: a fila. Só sai post com `"aprovado": true` e com a hora (`quando`, fuso de Cuiabá) já passada.
- `posts/`: imagens em JPEG e vídeos MP4 (Reels), servidos pelo GitHub Pages para a API buscar.
- Reels: na agenda, use `"video": "posts/<id>/reel.mp4"` e, se quiser, `"capa": "posts/<id>/capa.jpg"`.
- `publicar.py`: o robô. `--conferir` testa sem publicar; `--renovar` renova a chave.
- `token.enc`: a chave de acesso, criptografada. Só abre com o segredo `TOKEN_KEY` do repositório.
- Agendamento: a cada 30 minutos procura post vencido; toda segunda renova a chave.
