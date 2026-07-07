import sqlite3
import sqlite_vec
import graphqlite
import torch
import pandas as pd
import json
from transformers import AutoModel
from graphqlite import Graph
from ollama import Client
import spacy
import uuid


class Memory_Manager:
    DB_URL="/Users/chielerli/Programming/Agent_Instructions/agent_workspace/memory/sqlite.db"
    
    # Adjusted prompt block using structural JSON definitions to prevent 1B hallucinations
    OLLAMA_SYS_PROMPT="""[System]
Extract strict computer science, tools, and technical software architecture terms alongside their relationship properties from the User Text.
Return the result ONLY as a raw, valid JSON array of objects. 
Allowed relationship keys: depends_on, uses, is_a, references, calls, part_of
Do not include introductory text, markdown formatting, or formatting fences.
Example: [{"source": "python", "target": "ollama", "relationship": "uses"}]

[User Text]
"""

    conn = sqlite3.connect(DB_URL)
    conn.enable_load_extension(True)
    cursor = None
    embedding_model = None
    nlp = None
    G = None
    client = None
    namespace = uuid.NAMESPACE_OID
    
    def __init__(self):
        sqlite_vec.load(self.conn)
        self.cursor = self.conn.cursor()
        
        # Schema verification safety checks
        self._initialize_database_tables()
        
        model = AutoModel.from_pretrained(
            "jinaai/jina-embeddings-v5-text-nano",
            trust_remote_code=True,
            force_download=False,
            dtype=torch.bfloat16
        )
        device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
        self.embedding_model = model.to(device=device)
        self.nlp = spacy.load("en_core_web_sm")
        self.G = Graph(self.DB_URL)
        self.client = Client(host ="127.0.0.1:11434")

    def _initialize_database_tables(self):
        # Enforcing schema rules on launch
        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS entities(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id TEXT UNIQUE, 
            entity TEXT NOT NULL,
            last_accessed TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );""")
        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS episodic_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT
        );""")
        self.cursor.execute("CREATE VIRTUAL TABLE IF NOT EXISTS vector_mem using vec0(id INTEGER PRIMARY KEY, embeddings float[256]);")
        # Change node_id to TEXT to store clean UUIDs directly inside the vector engine context mapping
        self.cursor.execute("CREATE VIRTUAL TABLE IF NOT EXISTS vector_fact_mem using vec0(node_id text PRIMARY KEY, node_embedding float[256]);")
        self.conn.commit()

    def check_entity_exists_table(self, entity):
        # Return entity_id (the UUID string string format) if exists
        row = self.cursor.execute("SELECT entity_id FROM entities WHERE entity = ?", (entity.casefold().strip(),)).fetchone()
        return row[0] if row else None
    
    def check_rel_exists_graph(self, source_id, target_id):
        return self.G.has_edge(source_id, target_id)

    def get_embeddings(self, input_str: str, prompt_name="document"):
        emb = self.embedding_model.encode(texts=[input_str], task='retrieval', prompt_name=prompt_name)[0]
        return emb[:256].tolist()

    def get_relationships(self, full_text: str):
        response = self.client.chat(
            model="llama3.2:1b",
            options={"temperature": 0.0},
            messages=[{"role": "user", "content": self.OLLAMA_SYS_PROMPT + full_text}]
        )
        try:
            # Crucial: Parse clean string JSON objects safely
            return json.loads(response["message"]["content"].strip())
        except Exception:
            return []
    
    def get_entities(self, input_str: str):
        entity_sys_msg = """[System]
Extract strict framework, language, and technical computer science items from the text.
Return ONLY a valid JSON list of lowercase strings.
Example: ["python", "ollama"]

[User Text]
"""
        response = self.client.chat(
            model="llama3.2:1b",
            options={"temperature": 0.0},
            messages=[{"role": "user", "content": entity_sys_msg + input_str}]
        )
        try:
            return json.loads(response['message']['content'].strip())
        except Exception:
            return []
    
    def get_node_id(self, source):
        df = self.search_related_facts(source, k=1)
        return df.iloc[0]['entity_id'] if not df.empty else None

    def insert_turn_log(self, role: str, content: str, metadata=None):
        raw_vec = self.get_embeddings(content)
        self.cursor.execute(
            "INSERT INTO episodic_logs (role, content, metadata) VALUES(?, ?, ?)",
            (role, content, metadata)
        )
        log_id = self.cursor.lastrowid
        vector = sqlite_vec.serialize_float32(raw_vec)
        self.cursor.execute(
            "INSERT INTO vector_mem (id, embeddings) VALUES(?, ?)",
            (log_id, vector)
        )
        self.commit_all()

    def insert_relationships_graph(self, relationships: list):
        for rel in relationships:
            source = rel['source'].casefold().strip()
            target = rel['target'].casefold().strip()
            relationship = rel['relationship'].casefold().strip()
            
            source_id = self.check_entity_exists_table(source)
            target_id = self.check_entity_exists_table(target)
            
            # Logic branch fixes: ensure explicit execution parameters for edge insertions
            if source_id and target_id and self.G.has_edge(source_id, target_id):
                continue
            
            if not source_id:
                source_id = str(uuid.uuid5(self.namespace, source))
                self.insert_entities_table([source], [source_id])
                self.G.upsert_node(source_id, {"name": source}, label="Entity")
                
            if not target_id:
                target_id = str(uuid.uuid5(self.namespace, target))
                self.insert_entities_table([target], [target_id])
                self.G.upsert_node(target_id, {"name": target}, label="Entity")

            self.G.upsert_edge(source_id, target_id, rel_type=relationship)
        self.commit_all()

    def insert_entities_table(self, entities: list, entity_ids: list, prompt_name="document"):
        for i in range(len(entities)):
            fact = entities[i].casefold().strip()
            entity_id = str(entity_ids[i])

            if not self.check_entity_exists_table(fact):
                self.cursor.execute("INSERT INTO entities (entity_id, entity) VALUES(?, ?)", (entity_id, fact))
                raw_vec = self.get_embeddings(fact, prompt_name=prompt_name)
                vector = sqlite_vec.serialize_float32(raw_vec)
                # Save vectors against global UUID strings directly
                self.cursor.execute(
                    "INSERT INTO vector_fact_mem (node_id, node_embedding) VALUES(?, ?)",
                    (entity_id, vector)
                )
        self.commit_all()
    
    def search_related_episodic_logs(self, input_str: str, k=3):
        raw_vec = self.get_embeddings(input_str, prompt_name="query")
        vector = sqlite_vec.serialize_float32(raw_vec)
        knn = """SELECT v.id, e.content
                 FROM vector_mem v
                 JOIN episodic_logs e ON e.id = v.id
                 WHERE embeddings MATCH (?) AND k = (?)"""
        return pd.read_sql_query(knn, self.conn, params=[vector, k])

    def search_related_facts(self, input_str: str, k=3):
        raw_vec = self.get_embeddings(input_str, prompt_name="query")
        vector = sqlite_vec.serialize_float32(raw_vec)
        # Fixed mapping column alignments
        knn = """SELECT e.entity_id, e.entity
                 FROM vector_fact_mem v 
                 JOIN entities e ON e.entity_id = v.node_id
                 WHERE node_embedding MATCH (?) AND k = (?)"""
        return pd.read_sql_query(knn, self.conn, params=[vector, k])

    def search_related_rel_graph(self, source):
        source_id = self.get_node_id(source)
        if source_id:
            return self.G.connection.cypher(
                """
                MATCH (e: Entity {id: $source_id}) -[r]-(related: Entity) 
                WHERE related.id <> $source_id
                RETURN DISTINCT 
                    e.name AS source, 
                    TYPE(r) AS relationship, 
                    related.name AS target
                """, 
                parameters={"source_id": source_id}
            )
        return []

    def normalize_graph_result(self, rel_rows):
        normalized_res = ""
        # Fix array enumeration error type loop conversions
        for i, row in enumerate(rel_rows):
            normalized_res += f"relationship {i}: {row['source']} {row['relationship']} {row['target']}\n"
        return normalized_res

    def commit_all(self):
        self.conn.commit()