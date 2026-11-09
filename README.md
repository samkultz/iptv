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

O coletor procura repositórios cujo README corresponda às consultas padrão, examina arquivos `.m3u` na branch principal e grava canais aceitos em `output/allFound.m3u` à medida que os encontra. Ao concluir a busca, testa os canais e grava em `output/working.m3u` somente os que responderem. Entradas adultas reconhecidas são descartadas antes de entrar nas listas.

As consultas independentes, árvores dos repositórios, downloads M3U e verificações de stream usam workers concorrentes. O padrão é 4 workers e o limite aceito é de 1 a 4; o espaçamento global entre o início de verificações é controlado por `--check-delay`. A paginação dentro de cada consulta do GitHub continua sequencial.

Por padrão, a busca examina até 30 repositórios e 100 arquivos M3U. Personalize a consulta e os limites:

```bash
python createIPTFList.py --query "iptv m3u in:readme" --max-repositories 50 --max-playlists 200
python createIPTFList.py --workers 2
```

Também é possível configurar `--all-found`, `--working`, `--request-timeout`, `--stream-timeout` e `--check-delay`. A busca é limitada pelos resultados da API, pela consulta e pelos limites configurados; não enumera todo o GitHub. Configure `GITHUB_TOKEN` no ambiente para reduzir a chance de atingir o limite da API. Não coloque o token no código nem no Git.

Por padrão, a execução retoma (`--resume`): preserva `allFound.m3u`, evita duplicar URLs já coletadas e pula na validação os canais já presentes em `working.m3u`. Para apagar os dois arquivos e começar do zero, use `--no-resume`:

```bash
python createIPTFList.py --no-resume
```

A retomada não guarda o cursor da busca GitHub: ela recomeça a consultar os repositórios desde o início, mas conserva os M3Us e ignora canais já coletados. Canais que falharam antes e não estão em `working.m3u` são testados novamente.

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

Por padrão, scripts com país mapeado recebem esse país como `group-title`: Han → China, Hiragana/Katakana → Japan, Hangul → South Korea, Thai → Thailand, Armenian → Armenia, Georgian → Georgia, Greek → Greece, Hebrew → Israel, Khmer → Cambodia, Lao → Laos, Myanmar → Myanmar e Sinhala → Sri Lanka. Kana tem prioridade sobre Han para não classificar títulos japoneses como chineses. Esses mapeamentos são heurísticos: Han também é usado fora da China, e grego/hebraico não identificam exclusivamente um país.

Outras escritas são mantidas e agrupadas pelo nome do script, sem inventar um país: por exemplo, árabe → `Arabic`, cirílico → `Cyrillic`, devanágari → `Devanagari`. Letras específicas também podem sugerir um idioma: `ñ/¿/¡` → `spanish_presumed`; `þ/ð/æ` → `islandic_presumed`. São indícios, não detecção certa: `ñ` aparece em outros idiomas e `ð/æ` também podem ocorrer em feroês. Os caracteres e grupos são configuráveis por `language_character_groups` em `filters.json`. Para excluir scripts não latinos sem país mapeado, defina `exclude_unmapped_non_latin` como `true` no JSON ou use `--exclude-unmapped-non-latin`. `--include-unmapped-non-latin` mantém esses scripts mesmo se o JSON pedir para excluí-los. Defina `group_script_channels` como `false` para não alterar `group-title`.

Também é possível adicionar regras pela linha de comando:

```bash
python runprogram_filtered.py --whitelist-country BR --whitelist-term news --blacklist-term shopping
```

`--include-unmapped-non-latin` mantém scripts sem grupo configurado apenas nessa execução.

Consulte `python runprogram_filtered.py --help` para as opções de entrada, saída, host e porta.

## Utilitários de vídeo

- `runprogram_stream.py [arquivo]` prepara um vídeo local para TV e transmite pelo VLC como um canal local.
- `iferror.py` transmite um vídeo local ou, com `--transcode`, cria uma cópia otimizada para TV.

Esses dois scripts têm um caminho de vídeo padrão configurado no próprio código. Informe um arquivo ao primeiro ou altere `VIDEO` em `iferror.py` antes de executar.

## Dados locais

`output/allFound.m3u`, `output/working.m3u`, `output/filtered.m3u` e `output/epg.xml` são arquivos gerados. URLs de streams podem conter tokens privados; evite publicá-los. `.gitignore` evita adicionar novas playlists geradas, mas arquivos que já são rastreados pelo Git continuam rastreados.
