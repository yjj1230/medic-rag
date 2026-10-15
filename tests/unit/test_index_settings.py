from medical_rag.api import composition
from medical_rag.core.config import Settings


def test_rag_index_names_override_defaults(monkeypatch):
    monkeypatch.setenv("RAG_PUBLIC_INDEX_NAME", "medical-rag-public-pg-local")
    monkeypatch.setenv("RAG_PRIVATE_INDEX_NAME", "medical-rag-private-pg-local")

    settings = Settings(_env_file=None)

    assert settings.public_index_name == "medical-rag-public-pg-local"
    assert settings.private_index_name == "medical-rag-private-pg-local"


def test_build_search_index_uses_configured_names(monkeypatch):
    monkeypatch.setenv("RAG_PUBLIC_INDEX_NAME", "medical-rag-public-pg-local")
    monkeypatch.setenv("RAG_PRIVATE_INDEX_NAME", "medical-rag-private-pg-local")
    settings = Settings(_env_file=None)

    class FakeOpenSearch:
        def __init__(self, url, timeout):
            self.url = url
            self.timeout = timeout

    class FakeStore:
        def __init__(self, client, public_index, private_index):
            self.client = client
            self.public_index = public_index
            self.private_index = private_index

    import opensearchpy

    monkeypatch.setattr(opensearchpy, "OpenSearch", FakeOpenSearch)
    monkeypatch.setattr("medical_rag.retrieval.opensearch_store.OpenSearchStore", FakeStore)

    store = composition.build_search_index(settings)

    assert store.public_index == "medical-rag-public-pg-local"
    assert store.private_index == "medical-rag-private-pg-local"
