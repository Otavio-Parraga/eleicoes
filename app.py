"""Interface Streamlit. Rodar: conda run -n python3 streamlit run app.py"""
from __future__ import annotations

import importlib
import logging

import pandas as pd
import streamlit as st

from eleicoes import config, geo, store
from eleicoes.views import VIEWS, brand
from eleicoes.views.common import K_CARGO, K_MUN, K_SOURCE, K_TURNO, K_UF, ViewContext, available, uf_name

log = logging.getLogger(__name__)

st.set_page_config(page_title="Eleições 2026 ao vivo", page_icon=":material/how_to_vote:", layout="wide",
                   initial_sidebar_state="expanded")

K_EVT_LAST, K_EVT_SRC = "_evt_last_id", "_evt_source"
SOURCES = ["Ao vivo (TSE)", "Simulação"]


def _db_path():
    return config.SIM_DB_PATH if st.session_state.get(K_SOURCE) == "Simulação" else config.DB_PATH


def _reset_event_cursor() -> None:
    st.session_state.pop(K_EVT_LAST, None)


def sidebar() -> tuple[int, int, str, int]:
    sb = st.sidebar
    brand.sidebar_mark()
    sb.radio("Fonte de dados", SOURCES, key=K_SOURCE, horizontal=True, on_change=_reset_event_cursor)
    turno = sb.radio("Turno", [1, 2], format_func=lambda t: f"{t}º turno", key=K_TURNO, horizontal=True)
    cargos = [c for c in config.UI_CARGOS if available(turno, c)]
    if st.session_state.get(K_CARGO) not in cargos:
        st.session_state[K_CARGO] = cargos[0]
    cargo = sb.selectbox("Cargo", cargos, format_func=config.UI_CARGOS.get, key=K_CARGO)
    ufs = [config.BRASIL] + config.UF_LIST + ([config.EXTERIOR] if cargo == 1 else [])
    if st.session_state.get(K_UF) not in ufs:
        st.session_state[K_UF] = config.BRASIL
    uf = sb.selectbox("Estado", ufs, format_func=lambda u: uf_name(u) if u != config.BRASIL else "Brasil (todos)",
                      key=K_UF)
    refresh = sb.slider("Atualizar a cada (s)", 10, 120, 30, step=5, key="refresh_s")
    return turno, cargo, uf, refresh


def _valid_mun(uf: str) -> str | None:
    """Município selecionado (session_state) só vale se pertencer à UF atual."""
    mun = st.session_state.get(K_MUN)
    if not mun or uf in (config.BRASIL, config.EXTERIOR):
        return None
    try:
        if mun in set(geo.municipios_uf(uf)["mun"]):
            return mun
    except Exception:  # noqa: BLE001
        log.warning("lista de municípios indisponível", exc_info=True)
    return None


@st.fragment(run_every=15)
def collector_led() -> None:
    """Luz de status do coletor no teclado (barra lateral); se atualiza sozinha."""
    try:
        conn = store.connect(_db_path())
        try:
            st.html(brand.led_html(store.get_meta(conn, "collector_heartbeat"),
                                   store.get_meta(conn, "collector_last_tse_ts")))
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        log.warning("status do coletor", exc_info=True)


def event_toasts(conn) -> None:
    """Mostra como toast os eventos novos do coletor. Na 1ª carga (ou troca de fonte) só memoriza o cursor."""
    src = st.session_state.get(K_SOURCE)
    if st.session_state.get(K_EVT_SRC) != src:
        st.session_state[K_EVT_SRC] = src
        st.session_state.pop(K_EVT_LAST, None)
    if K_EVT_LAST not in st.session_state:
        last = store.events_since(conn, 0, limit=1)
        st.session_state[K_EVT_LAST] = int(last["id"].max()) if not last.empty else 0
        return
    ev = store.events_since(conn, int(st.session_state[K_EVT_LAST]), limit=50)
    if ev.empty:
        return
    st.session_state[K_EVT_LAST] = int(ev["id"].max())
    for r in ev.tail(5).itertuples(index=False):
        st.toast(str(r.message), icon=":material/campaign:")
    if len(ev) > 5:
        st.toast(f"+{len(ev) - 5} eventos novos", icon=":material/notifications:")


def header(ctx: ViewContext) -> None:
    from eleicoes.views.ranking import area_totals, mun_name  # import tardio: telas são recarregáveis

    area = ctx.area_nome if ctx.uf != config.BRASIL else "Brasil"
    titulo, sub = (mun_name(ctx.uf, ctx.mun), area) if ctx.mun else (area, "")
    t = area_totals(ctx.conn, ctx.eleicao, ctx.cargo, ctx.uf, ctx.mun)
    olho = [ctx.cargo_nome, f"{ctx.turno}º turno"]
    if t is None:
        olho.append("aguardando dados do TSE")
    else:
        ts = t.get("tse_ts")
        olho.append(f"atualizado pelo TSE às {ts.strftime('%H:%M:%S')}" if pd.notna(ts) else "sem horário do TSE")
        if ctx.uf == config.BRASIL and ctx.cargo != 1:
            olho.append("soma das 27 UFs")
    st.html(brand.header_html(" · ".join(olho), titulo, sub, None if t is None else t["pct_secoes"],
                              encerrada=t is not None and bool(int(t.get("finalizada", 0) or 0))))


def main() -> None:
    brand.inject()
    turno, cargo, uf, refresh = sidebar()
    with st.sidebar:
        collector_led()
    view_label = st.segmented_control("Visão", [v[0] for v in VIEWS], default=VIEWS[0][0], key="view",
                                      label_visibility="collapsed") or VIEWS[0][0]
    module = importlib.import_module(f"eleicoes.views.{dict(VIEWS)[view_label]}")

    @st.fragment(run_every=refresh)
    def body() -> None:
        try:
            conn = store.connect(_db_path())
        except Exception as e:  # noqa: BLE001
            st.error(f"Não foi possível abrir o banco de dados ({e}).")
            return
        try:
            ctx = ViewContext(conn=conn, turno=turno, cargo=cargo, uf=uf, mun=_valid_mun(uf), refresh_s=refresh)
            try:
                event_toasts(conn)
            except Exception:  # noqa: BLE001
                log.warning("toasts", exc_info=True)
            try:
                if not getattr(module, "OWN_HEADER", False):  # telas com cabeçalho próprio (ex.: Visão unificada)
                    header(ctx)
            except Exception:  # noqa: BLE001
                log.warning("cabeçalho", exc_info=True)
            try:
                module.render(ctx)
            except Exception as e:  # noqa: BLE001 - rede de segurança: tela nunca derruba o app
                log.exception("tela %s", view_label)
                st.warning(f"A tela '{view_label}' falhou ({type(e).__name__}). Tentando de novo na próxima "
                           "atualização.")
        finally:
            conn.close()

    body()


main()
