"""Explicit model-selected spans for presentation tests; never stored as hotword data."""
def seed_rule(store, term, span='', note=''):
    store.add_hotword(term, note=note)
    identifier = next(w.id for w in store.list_hotwords() if w.term == term)
    if not hasattr(store, '_test_spans'):
        store._test_spans = {}
    store._test_spans[identifier] = span


def seed_raw(store, term, span):
    with store._connect() as conn:
        identifier = conn.execute("INSERT INTO hotwords(term,created_at,updated_at) VALUES (?,'now','now')", (term,)).lastrowid
    if not hasattr(store, '_test_spans'):
        store._test_spans = {}
    store._test_spans[identifier] = span
