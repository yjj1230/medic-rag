from medical_rag.core.config import Settings


class FakeIndices:
    def __init__(self):
        self.created = []

    def exists(self, *, index):
        return False

    def create(self, *, index, body):
        self.created.append((index, body))


class FakeOpenSearch:
    def __init__(self):
        self.indices = FakeIndices()


def test_init_indexes_creates_distinct_public_and_private_mappings(monkeypatch):
    from scripts.run_worker import init_indexes

    monkeypatch.setenv("RAG_PUBLIC_INDEX_NAME", "medical-rag-public-pg-local")
    monkeypatch.setenv("RAG_PRIVATE_INDEX_NAME", "medical-rag-private-pg-local")
    monkeypatch.setenv("RAG_TEXT_ANALYZER", "cjk")
    monkeypatch.setenv("RAG_SEARCH_ANALYZER", "cjk")
    settings = Settings(_env_file=None)
    client = FakeOpenSearch()

    result = init_indexes(settings, client)

    assert result == {
        "public": "medical-rag-public-pg-local",
        "private": "medical-rag-private-pg-local",
    }
    assert [name for name, _ in client.indices.created] == [
        "medical-rag-public-pg-local",
        "medical-rag-private-pg-local",
    ]
    public_mapping = client.indices.created[0][1]
    private_mapping = client.indices.created[1][1]
    assert public_mapping["mappings"]["properties"]["embedding"]["dimension"] == 1536
    assert private_mapping["mappings"]["properties"]["embedding"]["dimension"] == 1536


def test_build_index_writers_preserve_visibility_boundary(monkeypatch):
    from scripts.run_worker import build_index_writers

    monkeypatch.setenv("RAG_PUBLIC_INDEX_NAME", "public-pg")
    monkeypatch.setenv("RAG_PRIVATE_INDEX_NAME", "private-pg")
    settings = Settings(_env_file=None)

    public_writer, private_writer = build_index_writers(settings, FakeOpenSearch())

    assert public_writer.visibility == "public"
    assert private_writer.visibility == "private"
    assert public_writer.index_name == "public-pg"
    assert private_writer.index_name == "private-pg"
