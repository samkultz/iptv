# Ferramentas IPTV

Pequenos utilitários Python para servir playlists M3U, verificar respostas de streams e preparar vídeos locais para reprodução em TVs. Os scripts são independentes; não há um scanner de canais neste repositório.

## Requisitos

- Python 3.
- `ffmpeg` e `ffprobe` para `iferror.py` e `runprogram_stream.py`.
- VLC para `runprogram_stream.py`; o script usa recursos do macOS/VideoToolbox.
- `runprogram.py` usa apenas a biblioteca padrão do Python.
- `runprogram_filtered.py` também importa `stream_tags.py` e espera um `filters.json`. Esses arquivos não estão incluídos no checkout atual, então esse script não funciona sem eles.

## Verificar canais

`runprogram.py` testa um lote de URLs HTTP/HTTPS da playlist. Para HLS, tenta carregar o primeiro segmento. O teste não decodifica o stream e não garante compatibilidade com a TV; uma resposta positiva só confirma que o endpoint e, no caso de HLS, o primeiro segmento responderam. O script não remove canais com falha.

```bash
python runprogram.py --check-streams --playlist output/minha-lista.m3u
```

A execução verifica até 25 URLs únicas. Para continuar em lotes, avance o offset:

```bash
python runprogram.py --check-streams --playlist output/minha-lista.m3u --check-offset 25 --check-limit 25
```

`--check-limit` aceita de 1 a 100 URLs e `--check-timeout` de 1 a 15 segundos (padrão: 4). O comando termina com código 1 se houver falhas no lote.

## Servir uma playlist

```bash
python runprogram.py --playlist output/minha-lista.m3u
```

Por padrão, o servidor escuta em todas as interfaces e usa a porta 8080. Ele publica `/working.m3u` e tenta disponibilizar `/epg.xml` ao lado do arquivo M3U. As entradas adultas reconhecidas pelo filtro são omitidas ao servir e exportar a playlist. Use somente em uma rede confiável: o servidor não tem autenticação.

Opções úteis:

```bash
python runprogram.py --help
python runprogram.py --playlist output/minha-lista.m3u --port 8081
python runprogram.py --playlist output/minha-lista.m3u --output output/playlist-local.m3u
```

O caminho padrão da playlist é `output/working.m3u`, mas esse arquivo não está presente no checkout atual. Informe `--playlist` apontando para uma M3U existente.

## Outros scripts

- `runprogram_filtered.py`: filtra canais por idioma, palavras-chave e metadados, e serve a playlist resultante. Requer `stream_tags.py` e `filters.json`, ausentes neste checkout.
- `runprogram_stream.py [arquivo]`: prepara e transmite um vídeo local usando VLC. Sem argumento, usa um caminho de vídeo configurado no próprio script.
- `iferror.py`: transmite um vídeo local ou, com `--transcode`, gera uma cópia otimizada para TV. O caminho de origem está configurado no próprio script.

Confira as opções disponíveis com `python runprogram_stream.py --help` e `python iferror.py --help`.

## Arquivos gerados

As playlists e guias podem conter URLs privadas ou tokens. Evite publicá-los. Arquivos M3U e o guia EPG dentro de `output/` são ignorados pelo Git quando ainda não estão sendo rastreados.
