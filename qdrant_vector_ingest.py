import json
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, VectorParams, Distance

# Connect to Qdrant running locally
client = QdrantClient("localhost", port=6333)

# Load chunk_embeddings.json
with open("doc_chunk_embeddings.json", "r", encoding="utf-8") as f:
    data = json.load(f)

print(f"Loaded {len(data)} chunks from doc_chunk_embeddings.json")  # Debug: show number of chunks

# Filter out entries with non-empty embeddings
points = []
for chunk in data:
    embedding = chunk.get("embedding")
    # Use 'chunk_id' if present, else fallback to 'id' for backward compatibility
    chunk_id = chunk.get("chunk_id") or chunk.get("id")
    if embedding and chunk_id:
        # Add all metadata fields except embedding and vector/id to payload
        payload = {k: v for k, v in chunk.items() if k != "embedding" and k != "id"}
        # Always include chunk_id in payload for search and filtering
        payload["chunk_id"] = chunk_id
        points.append(
            PointStruct(
                id=chunk_id,
                vector=embedding,
                payload=payload
            )
        )
    else:
        print(f"Chunk with id {chunk_id} has no embedding or empty embedding")  # Debug: show missing embeddings

if not points:
    print("No valid embeddings found. Please check the structure of doc_chunk_embeddings.json and ensure 'embedding' fields are present and non-empty.")
    raise ValueError("No valid embeddings found in doc_chunk_embeddings.json")

# Create a collection with the correct vector size
vector_size = len(points[0].vector)
collection_name = "doc_chunk_embeddings"
client.recreate_collection(
    collection_name=collection_name,
    vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE)
)

# Upload the vectors
client.upsert(
    collection_name=collection_name,
    points=points
)

print("doc_chunk_embeddings.json uploaded successfully to Qdrant!")
