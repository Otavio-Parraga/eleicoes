#!/usr/bin/env bash
# Sobe o coletor (ou o simulador) em segundo plano e abre a interface Streamlit.
#   ./run.sh                 coletor ao vivo (args extras vão para o coletor, ex. ./run.sh --focus rs,sp)
#   ./run.sh sim [args]      simulador (tools/simulate.py) em vez do coletor
# Ctrl-C encerra a interface e o processo de fundo.
set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR" || exit 1
mkdir -p "$DIR/data"
LOG="$DIR/data/collector.log"
BG_PID=""

kill_tree() {
  local pid="$1" child
  for child in $(pgrep -P "$pid" 2>/dev/null); do
    kill_tree "$child"
  done
  kill -TERM "$pid" 2>/dev/null
}

cleanup() {
  trap - EXIT INT TERM
  if [ -n "$BG_PID" ] && kill -0 "$BG_PID" 2>/dev/null; then
    echo "Encerrando processo de fundo ($BG_PID)..."
    kill_tree "$BG_PID"
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$BG_PID" 2>/dev/null || break
      sleep 0.5
    done
    kill -0 "$BG_PID" 2>/dev/null && kill -KILL "$BG_PID" 2>/dev/null
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [ "${1:-}" = "sim" ]; then
  shift
  echo "Iniciando simulador (log: $DIR/data/simulate.log)"
  conda run -n python3 --no-capture-output python "$DIR/tools/simulate.py" "$@" >>"$DIR/data/simulate.log" 2>&1 &
  BG_PID=$!
else
  echo "Iniciando coletor (log: $LOG)"
  conda run -n python3 --no-capture-output python -m eleicoes.collector "$@" >>"$LOG" 2>&1 &
  BG_PID=$!
fi

conda run -n python3 --no-capture-output streamlit run "$DIR/app.py"
