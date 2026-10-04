# Eleições 2026 ao vivo

Acompanha a apuração oficial do TSE (`resultados.tse.jus.br`) com mapas e gráficos, localmente, usando só o ambiente conda `python3` (nada instalado).

## Rodar

```bash
./run.sh          # coletor ao vivo (TSE) + interface em http://localhost:8501
./run.sh sim      # simulador de apuração + interface (para testar antes das 17h)
```

Ou separadamente:

```bash
conda run -n python3 python -m eleicoes.collector --focus rs     # grava em data/eleicoes.db
conda run -n python3 streamlit run app.py
```

Na barra lateral: fonte de dados (Ao vivo / Simulação), turno, cargo, estado e intervalo de atualização.
Ao escolher um estado, o coletor passa a varrer os municípios dele (a cada ~3 min).

## Telas

| Tela | O que mostra |
|---|---|
| Mapa | Brasil por UF (cor do partido do líder, intensidade = margem) e municípios do estado escolhido |
| Ranking | Candidatos da área com votos, % válidos e situação; KPIs de apuração |
| Evolução | % de cada candidato conforme a apuração avança; viradas |
| Votos restantes | Onde estão os votos ainda não apurados e projeção ingênua do resultado |
| Congresso | Composição projetada da Câmara, do Senado e das Assembleias |
| Candidato | Busca qualquer candidato e mostra onde ele é forte |
| Comparecimento | Abstenção, brancos e nulos por UF/município e por cargo |
| 2022 × 2026 | Swing por partido em relação a 2022 (requer `python -m eleicoes.hist2022 download && ... build`) |

## Estrutura

- `eleicoes/collector.py` — processo que consulta o TSE (GET condicional) e grava snapshots em SQLite.
- `eleicoes/parse.py` / `store.py` — formato `-u.json` do TSE → SQLite (histórico em nível br/uf).
- `eleicoes/views/` — uma tela por arquivo; `app.py` é a casca Streamlit.
- `tools/simulate.py` — gera uma apuração sintética em `data/sim.db`.
- `docs/spec.md` — decisões e contratos.

Testes: `conda run -n python3 python -m unittest discover -s tests -t . -v`

## Deploy no Render

O repositório já traz tudo para o Render: `render.yaml` (Blueprint), `requirements.txt`, `.python-version`,
`.streamlit/config.toml` e `render_start.sh`. Um único serviço web roda o coletor em segundo plano (reiniciado
automaticamente se cair) e o Streamlit em primeiro plano, os dois no mesmo SQLite. As malhas do IBGE e a lista de
municípios (`data/geo/`, ~12 MB) e os resultados de 2022 já processados (`data/2022/*.parquet`, ~1,3 MB) vão no
repositório, então o deploy não depende de downloads grandes e a tela 2022 × 2026 funciona de saída.

1. No painel do Render: **New → Blueprint**, conecte o GitHub e escolha este repositório (branch `main`).
2. Confira o serviço `eleicoes-2026` (plano **free**, região virginia) e clique em **Apply**. O build roda
   `pip install -r requirements.txt` e o start, `bash render_start.sh`.
3. Abra a URL `https://<nome>.onrender.com` quando o health check (`/_stcore/health`) ficar verde.

Variáveis de ambiente (painel → Environment):

| Variável | Padrão | Para quê |
|---|---|---|
| `ELEICOES_FOCUS` | `rs` | UFs cujos municípios o coletor varre (ex. `rs,sc`) |
| `ELEICOES_TURNO` | `1` | troque para `2` no 2º turno |
| `ELEICOES_DB` | `data/eleicoes.db` | caminho do banco (use `/var/data/eleicoes.db` com disco persistente) |
| `ELEICOES_TIER1` / `ELEICOES_TIER2` | 60 / 180 | intervalos de coleta (s) |
| `ELEICOES_RAW` | `0` | `1` arquiva os JSON brutos do TSE (ocupa disco) |
| `ELEICOES_COLLECTOR` | `1` | `0` sobe só a interface |
| `ELEICOES_COLLECTOR_ARGS` | — | argumentos extras do coletor (ex. `--concurrency 4`) |

Limitações do plano gratuito:

- **Hiberna após ~15 min sem visitas** e o coletor para junto: o histórico fica com buracos nesse intervalo. A
  próxima visita acorda o serviço (~1 min) e o coletor retoma.
- **Disco efêmero**: o banco é apagado a cada deploy, reinício ou hibernação.
- Para manter o histórico, troque para um plano pago e anexe um disco persistente: descomente `disk:`
  (montado em `/var/data`) e `ELEICOES_DB=/var/data/eleicoes.db` no `render.yaml` e escolha um plano pago
  (ex. `0.5c-512mb` ou `1c-2g`). Isso tem custo mensal.

A fonte **Simulação** da barra lateral fica vazia no Render, a menos que o simulador (`tools/simulate.py`)
também rode lá. Para testar o deploy localmente sem instalar nada:
`PORT=8599 ELEICOES_DB=/tmp/teste.db conda run -n python3 --no-capture-output bash render_start.sh`.
