#!/usr/bin/env bash
# Ponto de entrada no Render (startCommand do render.yaml). Num único serviço web:
#   1. coletor do TSE em segundo plano, num laço que o reinicia se ele cair;
#   2. Streamlit em primeiro plano (exec), escutando em 0.0.0.0:$PORT.
# Os dois usam o mesmo SQLite (ELEICOES_DB ou data/eleicoes.db), por isso precisam ficar no mesmo serviço.
#
# Variáveis de ambiente (todas opcionais):
#   PORT                    porta HTTP (o Render define; padrão 10000)
#   ELEICOES_DB             caminho do banco (padrão data/eleicoes.db; num disco persistente, ex. /var/data/eleicoes.db)
#   ELEICOES_FOCUS          UFs cujos municípios o coletor varre, ex. "rs" ou "rs,sp" (padrão rs)
#   ELEICOES_TURNO          1 ou 2 (padrão 1)
#   ELEICOES_TIER1          intervalo do tier 1 em s (padrão do coletor: 60)
#   ELEICOES_TIER2          intervalo do tier 2 em s (padrão do coletor: 180)
#   ELEICOES_RAW            1 = arquiva os JSON brutos do TSE (gzip) ao lado do banco; padrão 0 (economiza disco)
#   ELEICOES_COLLECTOR      0 = não sobe o coletor (só a interface); padrão 1
#   ELEICOES_COLLECTOR_ARGS argumentos extras para `python -m eleicoes.collector`, ex. "--concurrency 4 -v"
#   ELEICOES_RESTART_DELAY  segundos de espera antes de reiniciar o coletor que caiu (padrão 5)
#
# Teste local (sem instalar nada):
#   PORT=8599 ELEICOES_DB=/tmp/teste.db conda run -n python3 --no-capture-output bash render_start.sh
set -u

cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1

PORT="${PORT:-10000}"
FOCUS="${ELEICOES_FOCUS:-rs}"
TURNO="${ELEICOES_TURNO:-1}"
RESTART_DELAY="${ELEICOES_RESTART_DELAY:-5}"
export PYTHONUNBUFFERED=1  # logs do coletor aparecem na hora no painel do Render

say() { echo "[render_start $(date '+%Y-%m-%d %H:%M:%S')] $*"; }

collector_args=(--turno "$TURNO" --focus "$FOCUS" --no-notify)
[ "${ELEICOES_RAW:-0}" = "1" ] || collector_args+=(--no-raw)
[ -n "${ELEICOES_TIER1:-}" ] && collector_args+=(--tier1 "$ELEICOES_TIER1")
[ -n "${ELEICOES_TIER2:-}" ] && collector_args+=(--tier2 "$ELEICOES_TIER2")
if [ -n "${ELEICOES_COLLECTOR_ARGS:-}" ]; then
  # shellcheck disable=SC2206  # separação por espaços é intencional
  collector_args+=($ELEICOES_COLLECTOR_ARGS)
fi

# PID deste shell. Depois do `exec` lá embaixo ele passa a ser o PID do Streamlit: quando esse processo
# some, o laço do coletor encerra o coletor e sai também (nada fica órfão).
MAIN_PID=$$

collector_loop() {
  local child="" rc

  stop_collector() {
    [ -n "$child" ] || return 0
    kill -TERM "$child" 2>/dev/null
    local i
    for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
      kill -0 "$child" 2>/dev/null || break
      sleep 0.5
    done
    kill -KILL "$child" 2>/dev/null
    wait "$child" 2>/dev/null
    child=""
  }
  main_alive() { kill -0 "$MAIN_PID" 2>/dev/null; }

  trap 'stop_collector; exit 0' TERM INT HUP

  while main_alive; do
    say "iniciando coletor: python -m eleicoes.collector ${collector_args[*]}"
    python -m eleicoes.collector "${collector_args[@]}" &
    child=$!
    # Vigia: sai do laço quando o coletor termina; derruba o coletor se o processo principal sumir.
    while kill -0 "$child" 2>/dev/null; do
      if ! main_alive; then
        say "processo principal ($MAIN_PID) encerrado; parando o coletor"
        stop_collector
        exit 0
      fi
      sleep 2
    done
    wait "$child"
    rc=$?
    child=""
    main_alive || exit 0
    say "coletor saiu (código $rc); reiniciando em ${RESTART_DELAY}s"
    sleep "$RESTART_DELAY"
  done
}

if [ "${ELEICOES_COLLECTOR:-1}" != "0" ]; then
  collector_loop &
  say "laço do coletor em segundo plano (pid $!); foco=$FOCUS turno=$TURNO db=${ELEICOES_DB:-data/eleicoes.db}"
else
  say "ELEICOES_COLLECTOR=0: coletor desligado, só a interface"
fi

say "iniciando Streamlit em 0.0.0.0:$PORT"
exec streamlit run app.py \
  --server.port "$PORT" \
  --server.address 0.0.0.0 \
  --server.headless true \
  --server.fileWatcherType none \
  --client.toolbarMode viewer \
  --client.showErrorDetails type
