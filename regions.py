"""
Regioes preferidas de hospedagem por destino (regra do Lucas).

Santiago: poligonos do Google My Maps "Area de Busca Zerando o Chile"
(Providencia, Las Condes e Centro). Ao montar pacote, o hotel escolhido
tem que cair dentro de um desses poligonos. Coordenadas (lat, lon).
"""
from __future__ import annotations

from typing import Any, Optional

# destino (chave normalizada do DESTINATION_MAP / CIT) -> lista de poligonos
PREFERRED_REGIONS: dict[str, dict[str, list[tuple[float, float]]]] = {
    "CIT_6624": {
        "Providência": [
                [
                        -33.430746,
                        -70.633995
                ],
                [
                        -33.455155,
                        -70.630694
                ],
                [
                        -33.453477,
                        -70.620252
                ],
                [
                        -33.441021,
                        -70.624287
                ],
                [
                        -33.433542,
                        -70.59756
                ],
                [
                        -33.429173,
                        -70.598933
                ],
                [
                        -33.427991,
                        -70.593869
                ],
                [
                        -33.421508,
                        -70.596745
                ],
                [
                        -33.412243,
                        -70.608416
                ],
                [
                        -33.416793,
                        -70.619016
                ],
                [
                        -33.425159,
                        -70.623395
                ],
                [
                        -33.430746,
                        -70.633995
                ]
        ],
        "Las condes": [
                [
                        -33.412243,
                        -70.608416
                ],
                [
                        -33.421508,
                        -70.596745
                ],
                [
                        -33.413878,
                        -70.565502
                ],
                [
                        -33.40738,
                        -70.561219
                ],
                [
                        -33.396013,
                        -70.544616
                ],
                [
                        -33.393433,
                        -70.546676
                ],
                [
                        -33.393057,
                        -70.576609
                ],
                [
                        -33.400366,
                        -70.600899
                ],
                [
                        -33.412243,
                        -70.608416
                ]
        ],
        "Centro": [
                [
                        -33.430937,
                        -70.666558
                ],
                [
                        -33.456086,
                        -70.665971
                ],
                [
                        -33.455155,
                        -70.630694
                ],
                [
                        -33.430746,
                        -70.633995
                ],
                [
                        -33.425338,
                        -70.642106
                ],
                [
                        -33.430937,
                        -70.666558
                ]
        ]
},
}


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
      3. mais barato primeiro; empate -> cafe da manha incluso primeiro
    Retorna None se nenhum hotel da lista cai na regiao.
    """
    if not has_preferred_regions(dest_cit):
        return None
    in_region = []
    for h in hotels:
        reg = region_of(dest_cit, h.get("latitude"), h.get("longitude"))
        if reg:
            h["region"] = reg
            in_region.append(h)
    if not in_region:
        return None
    good = [h for h in in_region if (h.get("review_score") or 0) >= 8.0] or in_region
    priced = [h for h in good if (h.get("total_price") or 0) > 0] or good

    def _key(h):
        board = (h.get("board") or "").lower()
        return (h.get("total_price") or 9e12, 0 if "cafe" in board else 1)

    return sorted(priced, key=_key)[0]
