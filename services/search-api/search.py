from elasticsearch import Elasticsearch

from models import Hit, SearchRequest, SearchResponse


def build_query(req: SearchRequest) -> dict:
    filters = []
    must = []

    # --- Exact-match conditions go in filter (no scoring, cached) ---

    if req.log_type is not None:
        filters.append({"term": {"log_type": req.log_type}})

    if req.sus is not None:
        filters.append({"term": {"labels.sus": req.sus}})

    if req.evil is not None:
        filters.append({"term": {"labels.evil": req.evil}})

    if req.host_name is not None:
        filters.append({"term": {"attributes.host_name.keyword": req.host_name}})

    if req.process_name is not None:
        filters.append({"term": {"attributes.process_name.keyword": req.process_name}})

    if req.timestamp_from is not None or req.timestamp_to is not None:
        range_clause: dict = {}
        if req.timestamp_from:
            range_clause["gte"] = req.timestamp_from
        if req.timestamp_to:
            range_clause["lte"] = req.timestamp_to
        filters.append({"range": {"timestamp": range_clause}})

    # --- Full-text condition goes in must (scores by relevance) ---

    if req.dns_query is not None:
        must.append({"match": {"attributes.dns_query": req.dns_query}})

    # If nothing was specified, match_all returns everything
    if not filters and not must:
        return {"match_all": {}}

    bool_clause: dict = {}
    if filters:
        bool_clause["filter"] = filters
    if must:
        bool_clause["must"] = must

    return {"bool": bool_clause}


def execute_search(es: Elasticsearch, index: str, req: SearchRequest) -> SearchResponse:
    from_offset = (req.page - 1) * req.page_size

    response = es.search(
        index=index,
        query=build_query(req),
        from_=from_offset,
        size=req.page_size,
        track_total_hits=True,
    )

    total = response["hits"]["total"]["value"]
    took_ms = response["took"]

    hits = [
        Hit(
            id=h["_id"],
            score=h.get("_score"),
            source=h["_source"],
        )
        for h in response["hits"]["hits"]
    ]

    return SearchResponse(
        total=total,
        page=req.page,
        page_size=req.page_size,
        took_ms=took_ms,
        hits=hits,
    )
