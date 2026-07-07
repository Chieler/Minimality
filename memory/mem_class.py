import json
import sqlite3
import uuid
from graphqlite import Graph
import hashlib
from ollama import Client
import pandas as pd
import spacy
import sqlite_vec
import torch
from transformers import AutoModel


class Memory_Manager:
    DB_URL = "/Users/chielerli/Programming/Agent_Instructions/agent_workspace/memory/sqlite.db"
    
    OLLAMA_SYS_PROMPT = """
    Extract computer science, tools, frameworks, and architecture terms from the User Text.
    Also extract relationships like: depends_on, uses, is_a, references, calls, part_of.
    
    Return the result ONLY as a raw valid JSON array of objects. Do not write markdown blocks or conversational text.
    Format example: [{"source": "python", "target": "ollama", "relationship": "uses"}]

    TEXT TO ANALYZE:
    """

    def __init__(self):
        # Establish database connections inside instance initialization scope
        self.conn = sqlite3.connect(self.DB_URL, timeout=30.0)
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self.cursor = self.conn.cursor()
        
        # Enable Write-Ahead Logging for high-throughput pipeline execution
        self.cursor.execute("PRAGMA journal_mode=WAL;")
        self.cursor.execute("PRAGMA synchronous=NORMAL;")
        
        # Verify and build standard schema structure patterns
        self._initialize_core_schemas()
        
        model = AutoModel.from_pretrained(
            "jinaai/jina-embeddings-v5-text-nano",
            trust_remote_code=True,
            dtype=torch.bfloat16
        )
        device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
        self.embedding_model = model.to(device=device)
        self.nlp = spacy.load("en_core_web_sm")
        self.G = Graph(self.DB_URL)
        self.client = Client(host="127.0.0.1:11434")

    def _initialize_core_schemas(self):
        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS entities(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity TEXT NOT NULL UNIQUE,
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
        self.cursor.execute("CREATE VIRTUAL TABLE IF NOT EXISTS vector_fact_mem using vec0(id INTEGER PRIMARY KEY, node_embedding float[256]);")
        self.conn.commit()

    def check_entity_exists_table(self, entity):
        entity_clean = entity.casefold().strip()
        row = self.cursor.execute("SELECT id FROM entities WHERE entity = ?", (entity_clean,)).fetchone()
        return row[0] if row else None

    def get_or_create_entity(self, entity_name, prompt_name="document"):
        entity_clean = entity_name.casefold().strip()
        
        # Safe structural verification strategy without side-effects
        existing_id = self.check_entity_exists_table(entity_clean)
        if existing_id:
            return existing_id
            
        self.cursor.execute("INSERT INTO entities (entity) VALUES(?)", (entity_clean,))
        row_int_id = self.cursor.lastrowid
        
        raw_vec = self.get_embeddings(entity_clean, prompt_name=prompt_name)
        vector = sqlite_vec.serialize_float32(raw_vec)
        self.cursor.execute(
            "INSERT INTO vector_fact_mem (id, node_embedding) VALUES(?, ?)",
            (row_int_id, vector)
        )
        self.conn.commit()
        return row_int_id
    def insert_entities_table(self, entities: list, prompt_name="document"):
        """
        Inserts a list of text entities into the SQLite relational table,
        generates their embeddings, and stores them in the sqlite-vec virtual table.
        """
        for entity in entities:
            fact = entity.casefold().strip()
            if not self.check_entity_exists_table(fact):
                print("new entity")
                # FIX: (fact,) with a trailing comma ensures Python handles it as a tuple
                self.cursor.execute("INSERT INTO entities (entity) VALUES(?)", (fact,))
                row_id = self.cursor.lastrowid
                
                # Generate vectors for the semantic search index
                raw_vec = self.get_embeddings(fact, prompt_name=prompt_name)
                vector = sqlite_vec.serialize_float32(raw_vec)
                
                # Link the vector embedding to the exact primary key 'id'
                self.cursor.execute(
                    "INSERT INTO vector_fact_mem (id, node_embedding) VALUES(?, ?)",
                    (row_id, vector)
                )
        self.commit_all()
    def check_rel_exists_graph(self, source_id, target_id):
        return self.G.has_edge(source_id, target_id)

    def get_embeddings(self, input_str: str, prompt_name="document"):
        emb = self.embedding_model.encode(texts=[input_str], task='retrieval', prompt_name=prompt_name)[0]
        return emb[:256].tolist()
    def get_entities(self, input:str):
        import ast
        import re
        ENTITY_SYS_MSG="""
                        GOAL:
                        Extract only strict computer science, programming languages, tools, frameworks, and technical software architecture terms alongside named entities from the User Text.
                        Return the result ONLY as a raw, valid array of lowercase strings. Do not write any introductory text, markdown formatting, or explanations. Only source, target, relationship.
                        Example: ["python", "ollama"]
                        USER TEXT:
                        """

        response = self.client.chat(model="llama3.2:1b",
            messages = [{"role":"user", "content": ENTITY_SYS_MSG+input}])
        if hasattr(response, 'message') and hasattr(response.message, 'content'):
            response_text = response.message.content
        elif isinstance(response, dict):
            response_text = response.get('message', {}).get('content', '')
        else:
            response_text = str(response)

        # 2. FIX: Since llama3.2:1b can output multiple lists or extra markdown chat,
        # use a regex to grab all valid [...] arrays safely instead of a blind literal_eval.
        try:
            # Find everything wrapped in square brackets
            found_arrays = re.findall(r'\[.*?\]', response_text, re.DOTALL)
            
            entities = []
            for array_str in found_arrays:
                try:
                    # json.loads is typically faster and safer for string arrays than ast
                    parsed_list = json.loads(array_str)
                    if isinstance(parsed_list, list):
                        entities.extend(parsed_list)
                except json.JSONDecodeError:
                    # Fallback to ast if the model used single quotes instead of double quotes
                    try:
                        parsed_list = ast.literal_eval(array_str)
                        if isinstance(parsed_list, list):
                            entities.extend(parsed_list)
                    except Exception:
                        continue

            # Clean, de-duplicate, and normalize everything to lowercase
            clean_entities = list(set([str(e).strip().casefold() for e in entities]))
            return clean_entities

        except Exception as e:
            print(f"[Entity Extraction Error] Failed to parse response text: {e}")
            return []
    def get_relationships(self, full_text: str):
        response = self.client.chat(
            model="llama3.2:1b",
            messages=[{"role": "user", "content": self.OLLAMA_SYS_PROMPT + full_text}],
            options={"temperature": 0.0} 
        )
        try:
            clean_content = response["message"]["content"].strip()
            if clean_content.startswith("```json"):
                clean_content = clean_content.split("```json")[1].split("```")[0].strip()
            elif clean_content.startswith("```"):
                clean_content = clean_content.split("```")[1].split("```")[0].strip()
            return json.loads(clean_content)
        except Exception as e:
            print(f"Failed to parse LLM JSON response: {e}")
            return []
    
    # def get_node_id(self, source):
    #     df = self.search_related_facts(source, k=1)
    #     # ✅ FIXED: Changed reference from 'entity_id' to matching primary key identifier 'id'
    #     return int(df.iloc[0]['id']) if not df.empty else None
    def get_node_id(self, source):
        # FIX: Swap fuzzy vector search for a 100% deterministic text lookup
        entity_clean = source.casefold().strip()
        row = self.cursor.execute("SELECT id FROM entities WHERE entity = ?", (entity_clean,)).fetchone()
        return row[0] if row else None
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
            
            # ✅ FIXED: Atomic processing loop prevents multi-connection deadlocks
            source_id = self.get_or_create_entity(source)
            target_id = self.get_or_create_entity(target)
            
            if self.G.has_edge(source_id, target_id):
                continue
                
            # ✅ FIXED: ID mapping fields explicitly written directly to Node property scopes
            self.G.upsert_node(source_id, {"id": source_id, "name": source}, label="Entity")
            self.G.upsert_node(target_id, {"id": target_id, "name": target}, label="Entity")
            self.G.upsert_edge(source_id, target_id,{}, rel_type=relationship)
            
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
        
        # ✅ FIXED: Structural alignment utilizing matching primary integer identifiers
        knn = """SELECT e.id, e.entity
                 FROM vector_fact_mem v 
                 JOIN entities e ON e.id = v.id
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
                """, {"source_id": source_id}
            )
        return []

    def normalize_graph_result(self, rel_rows):
        normalized_res = ""
        for i, row in enumerate(rel_rows):
            normalized_res += f"relationship {i}: {row['source']} {row['relationship']} {row['target']}\n"
        return normalized_res

    def commit_all(self):
        self.conn.commit()