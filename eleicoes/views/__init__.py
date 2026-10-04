"""Registro das telas: (rótulo, módulo). Cada módulo expõe render(ctx: ViewContext)."""

VIEWS: list[tuple[str, str]] = [
    ("Mapa", "mapa"),
    ("Visão unificada", "unificada"),
    ("Ranking", "ranking"),
    ("Evolução", "evolucao"),
    ("Votos restantes", "restantes"),
    ("Congresso", "congresso"),
    ("Candidato", "candidato"),
    ("Comparecimento", "comparecimento"),
    ("2022 × 2026", "comparacao2022"),
]
