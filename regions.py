"""
Regioes preferidas de hospedagem por destino (regra do Lucas).

Santiago: poligonos do Google My Maps "Area de Busca LC Turismo - Santiago"
(mid=1NZDuYPS2gWgCtBKttZrf9pHhk3swT6I, atualizado 04/11/2026)
(Providencia, Las Condes e Centro). Ao montar pacote, o hotel escolhido
tem que cair dentro de um desses poligonos. Coordenadas (lat, lon).
"""
from __future__ import annotations

import os
from typing import Any, Optional

# destino (chave normalizada do DESTINATION_MAP / CIT) -> lista de poligonos
PREFERRED_REGIONS: dict[str, dict[str, list[tuple[float, float]]]] = {
    "CIT_6624": {
        "Providência": [
            (-33.430746, -70.633995),
            (-33.448012, -70.631595),
            (-33.44711, -70.627265),
            (-33.446137, -70.622527),
            (-33.441075, -70.624051),
            (-33.438464, -70.607168),
            (-33.436299, -70.607793),
            (-33.433631, -70.597109),
            (-33.429173, -70.598933),
            (-33.427991, -70.593869),
            (-33.421508, -70.596745),
            (-33.412243, -70.608416),
            (-33.416793, -70.619016),
            (-33.418401, -70.621001),
            (-33.42069, -70.621871),
            (-33.422888, -70.622976),
            (-33.425159, -70.623395),
            (-33.430746, -70.633995),
        ],
        "Las condes": [
            (-33.412243, -70.608416),
            (-33.421508, -70.596745),
            (-33.422046, -70.580351),
            (-33.417066, -70.564815),
            (-33.4091, -70.558129),
            (-33.394293, -70.541183),
            (-33.39157, -70.543929),
            (-33.390451, -70.559067),
            (-33.393057, -70.576609),
            (-33.400366, -70.6009),
            (-33.412243, -70.608416),
        ],
        "Centro": [
            (-33.448807, -70.673253),
            (-33.450468, -70.679573),
            (-33.452093, -70.68512),
            (-33.452882, -70.688351),
            (-33.453438, -70.690917),
            (-33.454326, -70.690597),
            (-33.452309, -70.667536),
            (-33.448012, -70.631595),
            (-33.430746, -70.633995),
            (-33.425338, -70.642106),
            (-33.429318, -70.659869),
            (-33.438955, -70.668793),
            (-33.448807, -70.673253),
        ],
    },
}


# Nunca oferecer (regra do Lucas, 04/11): hotel com esses termos no nome/zona/endereco
# sai da lista mesmo que caia dentro de um poligono (o poligono "Centro" desenhado
# no mapa estica pela Alameda ate a Estacion Central).
EXCLUDED_TERMS: dict[str, list[str]] = {
    "CIT_6624": [
        "estación central", "estacion central", "estación", "pudahuel", "aeropuerto", "aeroporto",
        "maipú", "maipu", "quinta normal", "san bernardo", "la florida", "puente alto",
        "recoleta", "independencia", "franklin", "san miguel", "cerrillos", "lo prado",
        "barrio república", "barrio republica", "república", "republica",
    ],
}

# Limite oeste do "Centro" de Santiago: a oeste disso e Estacion Central / Republica,
# que o Lucas nao quer (clip do poligono desenhado).
MIN_LON: dict[str, float] = {"CIT_6624": -70.662}


def is_excluded(dest_cit: str, hotel: dict) -> bool:
    blob = " ".join(
        str(hotel.get(k) or "") for k in ("name", "zone", "address", "distance")
    ).lower()
    if any(t in blob for t in EXCLUDED_TERMS.get(dest_cit, [])):
        return True
    lon = hotel.get("longitude")
    if lon is not None and dest_cit in MIN_LON and lon < MIN_LON[dest_cit]:
        return True
    return False


def point_in_polygon(lat: float, lon: float, poly: list[tuple[float, float]]) -> bool:
    """Ray casting. poly = [(lat, lon), ...]."""
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        yi, xi = poly[i]
        yj, xj = poly[j]
        if (yi > lat) != (yj > lat):
            x_int = (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi
            if lon < x_int:
                inside = not inside
        j = i
    return inside


def region_of(dest_cit: str, lat: Optional[float], lon: Optional[float]) -> str:
    """Nome da regiao preferida em que o ponto cai, ou '' se fora de todas."""
    if lat is None or lon is None:
        return ""
    for name, poly in (PREFERRED_REGIONS.get(dest_cit) or {}).items():
        if point_in_polygon(lat, lon, poly):
            return name
    return ""


def has_preferred_regions(dest_cit: str) -> bool:
    return bool(PREFERRED_REGIONS.get(dest_cit))


def pick_preferred_hotel(dest_cit: str, hotels: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """
    Escolhe o hotel do pacote dentro das regioes preferidas:
      1. so hoteis com coordenadas dentro de algum poligono
      2. nota >= 8.0 quando houver (senao qualquer um da regiao)
      3. REGION_PICK_STRATEGY=relevance (padrao): 1o da ordem do HotelDo;
         =cheapest: mais barato, cafe da manha desempata
    Retorna None se nenhum hotel da lista cai na regiao.
    """
    if not has_preferred_regions(dest_cit):
        return None
    in_region = []
    for h in hotels:
        if is_excluded(dest_cit, h):
            continue
        reg = region_of(dest_cit, h.get("latitude"), h.get("longitude"))
        if reg:
            h["region"] = reg
            in_region.append(h)
    if not in_region:
        return None
    good = [h for h in in_region if (h.get("review_score") or 0) >= 8.0] or in_region
    priced = [h for h in good if (h.get("total_price") or 0) > 0] or good
    strategy = os.getenv("REGION_PICK_STRATEGY", "relevance").strip().lower()
    if strategy == "cheapest":
        def _key(h):
            board = (h.get("board") or "").lower()
            return (h.get("total_price") or 9e12, 0 if "cafe" in board else 1)
        return sorted(priced, key=_key)[0]
    # "relevance" (padrao): mantem a ordem de relevancia do proprio HotelDo
    return priced[0]
