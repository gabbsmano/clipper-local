# Clipper Local

Ferramenta local para cortar vídeos longos (gameplay, podcasts, entrevistas, etc.) em cortes verticais 9:16 para TikTok, Reels e Shorts. Não precisa de internet nem de assinatura. O ffmpeg vem embutido no pacote pip `imageio-ffmpeg`.

## Instalação (Windows)

```bash
cd caminho\da\pasta\do\projeto
python -m pip install -r requirements.txt
python app.py
```

Abra http://127.0.0.1:5000 (o navegador abre sozinho).

## Como usar

1. **Vídeo de origem**: cole o caminho completo do arquivo (ex: `C:\Videos\gta6.mp4`) e clique em Carregar. Use o player para achar os momentos.
2. **Cortes automáticos**: escolha a duração alvo (ex: 45 s). Para podcast, clique em *Detectar pausas* antes de gerar, assim os cortes caem nos silêncios e não no meio de uma frase. Depois clique em *Gerar cortes*.
3. **Transcrição e melhores momentos**:
   - *Transcrever*: roda o Whisper localmente e gera o texto com tempos palavra a palavra. Escolha o modelo (`small` é o recomendado para português) e o idioma. O resultado fica salvo em `output\_cache`, então não repete o trabalho.
   - *Achar melhores momentos*: pontua janelas do vídeo pelo volume, pelo ritmo de fala e por risadas/exclamações. Mostra os melhores com um botão **+ Adicionar**. Se o vídeo já foi transcrito, a análise também usa o texto.
4. **Cortes manuais**: use *Marcar início / fim* com o player ou digite `1:20` e `1:45`. Dá para renomear, colocar um **título** na tela, testar com ▶ Ver e remover.
5. **Exportação**:
   - *Formato*:
     - *Vertical (recorte central)*: corta o centro do vídeo para 1080x1920. Bom para gameplay.
     - *Vertical (fundo desfocado)*: mostra o vídeo inteiro sobre um fundo borrado. Bom para vídeos horizontais.
     - *Original*: mantém a proporção.
   - *Legendas automáticas*: queima as legendas no vídeo, em blocos de até 4 palavras, no estilo TikTok. Precisa ter transcrito antes.
   - *Título*: o título de cada corte aparece no topo pelos segundos escolhidos (0 = o corte inteiro).
6. Clique em **Exportar**. Os arquivos ficam em `output\<nome-do-video>\`.

## Usar a IA da Claude para os melhores momentos (opcional, pago)

A opção *IA Claude* lê a transcrição e escolhe os trechos com mais chance de viralizar, com título em português e nota. Ela usa a sua conta da API e é cobrada por uso.

1. Crie uma chave em https://console.anthropic.com (não cole a chave em nenhum arquivo nem no chat).
2. No Windows, rode uma vez no terminal (troque pelo valor da sua chave):

```bash
setx ANTHROPIC_API_KEY "sua-chave-aqui"
```

3. Feche e abra o terminal de novo, depois rode `python app.py`.
4. Na seção 4, escolha o método **IA Claude** e clique em *Achar melhores momentos*. Se a IA estiver disponível, a tela mostra isso.

- O modelo padrão é `claude-opus-5-5`. Para trocar (por exemplo, para reduzir custo), defina `CLIPPER_CLAUDE_MODEL`, como `claude-sonnet-5-5` ou `claude-haiku-5-5`.
- Vídeos longos são enviados em janelas de ~20 min. Cada janela é uma chamada.
- A IA precisa da transcrição, então o app transcreve automaticamente se ainda não tiver feito isso.
- O método *Heurística* continua grátis e funciona sem chave.

## Observações

- **Primeira transcrição**: o Whisper baixa o modelo (`small` ≈ 500 MB) da internet uma única vez. Depois funciona offline.
- **Velocidade**: a transcrição roda na CPU. Um vídeo de 1 h com o modelo `small` pode levar bastante tempo. Use `tiny` ou `base` para testar mais rápido.
- **Melhores momentos** são uma heurística local, não uma IA que entende o conteúdo. Ela acerta bem em picos de volume e de animação, e menos em conteúdo que depende de contexto. Sempre confira antes de postar.
- O título e as legendas são queimados no vídeo (não são faixas separadas), então aparecem em qualquer app.
- O player do navegador não toca `.mkv` nem alguns codecs. Você ainda consegue exportar, só não pré-visualizar.
- Direitos autorais: cortes de jogos e de terceiros podem gerar strikes no TikTok. Prefira conteúdo seu ou com permissão, e cite a fonte quando for o caso.

## Edição avançada

- **Remover pausas (jump cut):** marque *Remover pausas* na seção 3. Silêncios maiores que o valor escolhido (padrão 0,6 s) são encurtados para ~0,15 s de cada lado. Legendas acompanham o corte editado.
- **Seguir quem fala (2 pessoas):** marque *Seguir quem fala* (só no formato vertical). O app detecta rostos com o modelo YuNet (OpenCV), mede qual boca se mexe mais e troca o foco quando a pessoa muda. Durante silêncios o foco não muda. O modelo (~230 KB) é baixado uma vez para `models\`.
  - Limitação: é heurística. Funciona melhor com câmeras frontais e rostos visíveis. Movimentos grandes de cabeça ou pouca luz podem confundir a detecção.
- **Legenda destacada:** em *Legendas automáticas*, escolha *Destaque palavra por palavra*. A palavra falada fica amarela.
- **Título, legenda e hashtags:** o método IA gera, na mesma chamada, um título, uma legenda curta e de 3 a 5 hashtags. Cada corte exportado ganha um `.txt` ao lado do vídeo com essas informações.

## Fila em lote

Na seção 5, cole um caminho de vídeo por linha e clique em *Processar fila*. Para cada vídeo o app transcreve (se precisar), escolhe os momentos (heurística ou IA, conforme a seção 4), exporta com as opções atuais e salva um `manifest.json` com os metadados. Um vídeo com erro não interrompe a fila. Limite: 50 vídeos por vez.

## Publicar no TikTok (opcional)

A publicação usa a Content Posting API do TikTok. Requisitos:

1. Crie um app em developers.tiktok.com com o escopo `video.publish` e obtenha um access token de usuário. O fluxo OAuth exige uma URL de redirecionamento HTTPS, então o token é obtido fora deste app.
2. Defina a variável de ambiente e reinicie o app:

```bash
setx TIKTOK_ACCESS_TOKEN "seu-token-aqui"
```

3. Na seção 6, escolha a privacidade e clique em *Publicar no TikTok* em um corte. Antes de enviar, o app mostra a legenda e pede confirmação.

Importante:
- Enquanto o app não for aprovado pelo TikTok (auditoria), os posts só podem ser **privados** (*Só eu*). Mantenha essa opção até a aprovação.
- O módulo `tiktok.py` segue a documentação de *Direct Post*. Não consegui abrir a página de status do post para confirmar o nome dos campos; confira antes de usar em produção. Veja também a documentação de limites de taxa.
- O app nunca publica sem confirmação explícita.
