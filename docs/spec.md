# Eleições 2026 ao vivo — spec

## Objetivo
App local (Mac, navegador, um usuário) para acompanhar a apuração de **hoje (1º turno, 04/10/2026, divulgação a partir das 17h de Brasília)** e do 2º turno (25/10). Prioridade: funcionar hoje às 17h.

## Restrições
- **Não instalar nada.** Só Python do ambiente conda `python3` (`conda run -n python3 ...`). Disponíveis: streamlit 1.54, plotly 6.9, pandas 2.3, numpy, httpx, requests, pyarrow, altair, scipy, fastapi. **Não há** pytest, geopandas, folium, dash.
- Baixar *arquivos de dados* (JSON/CSV/ZIP) é permitido.
- Testes com `unittest`: `conda run -n python3 python -m unittest discover -s tests -t . -v` (rodar da raiz do projeto).
- Fonte de dados: só o TSE oficial (`resultados.tse.jus.br`) e IBGE (malhas). Ser educado com o TSE: GET condicional (ETag/If-Modified-Since), no máx. 8 requisições simultâneas.

## Arquitetura
```
collector.py (processo longo) --TSE -u.json--> parse.py --> store.py (SQLite WAL, data/eleicoes.db)
app.py (Streamlit) --lê--> store.py ; views/*.py renderizam ; geo.py fornece malhas
tools/simulate.py --> data/sim.db (mesmo schema, votos sintéticos) — a sidebar alterna "Ao vivo"/"Simulação"
```

## Dados do TSE (verificado)
- URL: `{TSE_BASE}/ele2026/{eleicao}/dados/{uf}/{arquivo}`; arquivo: `br-c0001-e006257-u.json` (nacional), `{uf}-c{cargo:04d}-e{eleicao:06d}-u.json` (estadual), `{uf}{mun}-c{cargo:04d}-e{eleicao:06d}-u.json` (município, mun = código TSE de 5 dígitos).
- Mesmo formato nos três níveis; `parse.parse_u()` já converte. Antes das 17h os arquivos existem com votos zerados.
- Eleições (config.ELECTIONS): 1º turno 6257 (Presidente) e 6259 (Gov 3, Sen 5, DepFed 6, DepEst 7, DepDist 8 só DF); 2º turno 6258/6260. Senado 2026: 2 vagas por UF.
- Presidente tem arquivos br, 27 UFs e `zz` (exterior). Demais cargos: só as 27 UFs (não existe br para 3/5/6/7/8).
- Lista de municípios: `geo.municipios()` (código TSE ↔ IBGE).

## Contratos (o código é a fonte da verdade — leia antes de usar)
- `eleicoes/config.py` — constantes. **Só o orquestrador edita.**
- `eleicoes/parse.py` — `parse_u(doc, uf=None) -> Snapshot` (Candidate, Party, Totals).
- `eleicoes/store.py` — `connect`, `write_snapshot`, `latest_totals`, `latest_candidates`, `leaders`, `history`, `history_totals`, `latest_parties`, `search_candidates`, focus/events/meta/http_cache. **cargo=7 casa 7 e 8.**
- `eleicoes/fake.py` — `fill_votes(doc, pct_secoes, seed=, when=, bias=)` para gerar dados de teste.
- `eleicoes/geo.py` — `states_geojson()`, `municipios_geojson(uf)`, `municipios()`, `municipios_uf(uf)`. Já em cache em data/geo/.
- `eleicoes/views/common.py` — `ViewContext`, `party_color`, `fmt_int`, `fmt_pct`, `uf_name`, `select_uf`, `no_data`, chaves de session_state.
- `eleicoes/views/maps.py` — `leader_map(...)`, `value_map(...)`.
- `eleicoes/views/__init__.py` — registro das telas. Cada tela: `render(ctx: ViewContext) -> None`.
- `app.py` — sidebar (fonte, turno, cargo, estado, intervalo), seletor de tela, fragmento com `run_every`.

## Convenções de UI
- Textos em português do Brasil. Números: `fmt_int` / `fmt_pct` (vírgula decimal).
- Cores de partido via `party_color(sigla)` (convenção brasileira); para todo o resto (escalas, layout, acessibilidade) seguir a skill `dataviz`.
- Telas rodam dentro de um `st.fragment` que se repete a cada N s: nada de `st.sidebar` dentro de `render`; use `key=` estáveis em widgets; operações pesadas devem ser cacheadas (`st.cache_data` com ttl) ou pré-computadas.
- Sem dados → `no_data()`; nunca lançar exceção para o usuário.
- `ctx.uf == 'br'`: visão nacional (mapa de UFs). `ctx.uf == 'rs'`: visão do estado (mapa de municípios via nível 'mu', disponível quando a UF está em foco — chamar `store.add_focus(ctx.conn, uf)` ao abrir a UF).

## Fases
1. Núcleo: coletor + mapa + ranking + progresso (hoje, antes das 17h).
2. Noite da apuração: evolução, votos restantes/projeção, notificações.
3. Congresso: hemiciclos Câmara/Senado, vagas por partido/UF, mais votados.
4. Candidato: busca + mapa da força do candidato.
5. Comparecimento + comparação com 2022 (dados abertos do TSE).
