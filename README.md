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
