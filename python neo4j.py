from neo4j import GraphDatabase
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

NEO4J_URI = os.getenv("NEO4J_URI", "neo4j://localhost:7687")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "Gunjan_09")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE", "neo4j")

class Neo4jConnection:
    def __init__(self, uri, user, password, database):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        self.database = database

    def close(self):
        self.driver.close()

    def query(self, cypher_query, parameters=None):
        with self.driver.session(database=self.database) as session:
            result = session.run(cypher_query, parameters or {})
            return [record.data() for record in result]

if __name__ == "__main__":
    conn = Neo4jConnection(NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE)
    try:
        # Example: Get all nodes
        cypher = "MATCH (n) RETURN n LIMIT 5"
        results = conn.query(cypher)
        for record in results:
            print(record)
    finally:
        conn.close()