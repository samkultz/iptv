# Ferramentas IPTV

Pipeline para descobrir playlists M3U em repositórios públicos do GitHub, juntar os canais, testar os streams e servir uma lista menor na rede local.

## Requisitos

- Python 3.10 ou superior; a pipeline usa somente a biblioteca padrão.
- A busca usa a API REST de repositórios do GitHub. `GITHUB_TOKEN` é opcional, mas aumenta os limites de uso da API.
- `ffmpeg`, `ffprobe` e VLC são necessários apenas para `iferror.py` e `runprogram_stream.py`.

## Pipeline

Execute:

```bash
python createIPTFList.py
```

O coletor procura repositórios cujo README corresponda às consultas padrão, examina arquivos `.m3u` na branch principal e grava canais aceitos em `output/allFound.m3u` à medida que os encontra. Ao concluir a busca, testa os canais em sequência e grava em `output/working.m3u` somente os que responderem. Entradas adultas reconhecidas são descartadas antes de entrar nas listas.

Por padrão, a busca examina até 30 repositórios e 100 arquivos M3U. Personalize a consulta e os limites:

```bash
python createIPTFList.py --query "iptv m3u in:readme" --max-repositories 50 --max-playlists 200
```

Também é possível configurar `--all-found`, `--working`, `--request-timeout`, `--stream-timeout` e `--check-delay`. A busca é limitada pelos resultados da API, pela consulta e pelos limites configurados; não enumera todo o GitHub. Configure `GITHUB_TOKEN` no ambiente para reduzir a chance de atingir o limite da API. Não coloque o token no código nem no Git.

A checagem confirma resposta HTTP e, para HLS, tenta carregar o primeiro segmento. Ela não decodifica o vídeo e não garante compatibilidade com a TV. A validação é sequencial e insere uma pequena pausa entre canais.

## Servir `working.m3u`

Depois da pipeline:

```bash
python runprogram.py
```

O servidor publica `http://<IP-da-rede>:8080/working.m3u` e gera `output/runprogram.m3u`. Por padrão, também disponibiliza o EPG se houver um `output/epg.xml` ao lado da playlist. Use apenas em uma rede confiável: o servidor não tem autenticação.

O teste manual em lotes continua disponível:

```bash
python runprogram.py --check-streams --playlist output/allFound.m3u --check-limit 25
python runprogram.py --check-streams --playlist output/allFound.m3u --check-offset 25 --check-limit 25
```

## Filtrar sem EPG

`runprogram_filtered.py` lê `output/working.m3u`, usa as regras de `filters.json`, grava `output/filtered.m3u` e serve apenas `/filtered.m3u`, sem rota EPG.

Edite `filters.json` para configurar `whitelist_terms`, `blacklist_terms`, `whitelist_countries`, `blacklist_countries` e `exclude_adult`. As opções de whitelist são inclusivas: se houver termos ou países permitidos, o canal precisa corresponder. Os países são lidos dos atributos `tvg-country`, `country`, `country-code` ou `tvg-country-code` da entrada M3U.

Também é possível adicionar regras pela linha de comando:

```bash
python runprogram_filtered.py --whitelist-country BR --whitelist-term news --blacklist-term shopping
```

Consulte `python runprogram_filtered.py --help` para as opções de entrada, saída, host e porta.

## Utilitários de vídeo

- `runprogram_stream.py [arquivo]` prepara um vídeo local para TV e transmite pelo VLC como um canal local.
- `iferror.py` transmite um vídeo local ou, com `--transcode`, cria uma cópia otimizada para TV.

Esses dois scripts têm um caminho de vídeo padrão configurado no próprio código. Informe um arquivo ao primeiro ou altere `VIDEO` em `iferror.py` antes de executar.

## Dados locais

`output/allFound.m3u`, `output/working.m3u`, `output/filtered.m3u` e `output/epg.xml` são arquivos gerados. URLs de streams podem conter tokens privados; evite publicá-los. `.gitignore` evita adicionar novas playlists geradas, mas arquivos que já são rastreados pelo Git continuam rastreados.
