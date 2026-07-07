from ollama import Client
from memory.mem_class import Memory_Manager
from memory.extraction import Entity_Extractor


c = Client(host="127.0.0.1:11434")
MODEL="qwen3.5:9b-mlx"
mm = Memory_Manager()

agg_msg = "CONTEXT"
#Make this into an lru
message_history=[]
# message_history = [{"role": "sysrem", "content": SYS_INSTRUCTION}]
while True:
    user_msg = input("> ")
    if user_msg=="$STOP":
        break
    if not user_msg:
        continue
    #1. Get the three most relevant system messages
    retrieved_logs_num =3
    hist = mm.search_related_episodic_logs(user_msg, k = retrieved_logs_num)
    if not hist.empty:
        for i in range(retrieved_logs_num):
            ctx = hist.iloc[i]['content']
            agg_msg = agg_msg +f"PREVIOUS LOG CONTEXT {i}: \n" + ctx

    #2. Get the most relevant relationships from the graph database
    msg_entities = mm.get_entities(user_msg)
    print(f"Entities extracted: {msg_entities}")

    total_entities = set(msg_entities)

    for entity in msg_entities:
        # This returns a pandas DataFrame with ['id', 'entity'] columns
        related = mm.search_related_facts(entity, 2)
        
        if not related.empty:
            # ✅ FIXED: Use Pandas series conversion to instantly update your set 
            # without running clunky manual index loops
            total_entities.update(related['entity'].tolist())

    agg_msg += "\n RELATED RELATIONSHIPS \n"
    for ent in total_entities:

        rel = mm.search_related_rel_graph(ent)
        agg_msg +=mm.normalize_graph_result(rel)
    print(agg_msg)
    #3. Augment user_msg
    agg_msg=agg_msg + "\n USER MESSAGE:"+user_msg
    message_history.append({"role":"user", "content": agg_msg})
    #4. get response from model
    response = c.chat(model="qwen3.5:9b-mlx",
        messages = message_history, think = True)
    print(response)
    #5. Tool calling


    #5. asynchronously add the response to database
    #5. If we're close to lru cache limit, extract entities and relationships from the response and add to graph database then evict
    #6. If we're close to token limit, summarize 



    
    
#     #Add response to database
# import json

# Assuming your Memory_Manager class is imported or defined above
# if True:
#     # Initialize your memory manager
#     mm = Memory_Manager()
    
    # print("Initializing synthetic data injection...")

    # # ==========================================
    # # 1. Insert Episodic Logs (Conversation history)
    # # ==========================================
    # conversations = [
    #     {
    #         "role": "user", 
    #         "content": "We are refactoring our backend data pipelines. We want to use Apache Kafka to handle real-time streaming events from our application, and then use PySpark to clean the data before storing it in Snowflake.",
    #         "metadata": json.dumps({"session_id": "session_001", "project": "data_pipeline"})
    #     },
    #     {
    #         "role": "agent", 
    #         "content": "Using Apache Kafka for event streaming and PySpark for transformation is an excellent architectural pattern. For orchestrating these jobs, I highly recommend using dbt (Data Build Tool) once the data lands in Snowflake to handle your warehouse transformations.",
    #         "metadata": json.dumps({"session_id": "session_001", "project": "data_pipeline"})
    #     },
    #     {
    #         "role": "user", 
    #         "content": "That sounds smart. I will setup Snowflake as our core data lakehouse warehouse and configure dbt to handle the semantic layer models.",
    #         "metadata": json.dumps({"session_id": "session_001", "project": "data_pipeline"})
    #     }
    # ]

    # print("Inserting conversation logs...")
    # for turn in conversations:
    #     mm.insert_turn_log(role=turn["role"], content=turn["content"], metadata=turn["metadata"])


    # # ==========================================
    # # 2. Insert Core Relationships & Entities
    # # ==========================================
    # # This matches the structured output format your 1B model generates
    # synthetic_relationships = [
    #     {"source": "kafka", "target": "event streaming", "relationship": "is_a"},
    #     {"source": "pyspark", "target": "kafka", "relationship": "consumes_event"},
    #     {"source": "pyspark", "target": "snowflake", "relationship": "writes_to"},
    #     {"source": "dbt", "target": "snowflake", "relationship": "transforms"},
    #     {"source": "dbt", "target": "semantic layer", "relationship": "defines"},
    #     {"source": "backend pipeline", "target": "kafka", "relationship": "uses"},
    #     {"source": "snowflake", "target": "data lakehouse", "relationship": "is_a"}
    # ]

    # # print("Injecting entities and constructing knowledge graph edges...")
    # # mm.insert_relationships_graph(synthetic_relationships)

    # # print("Data injection successfully completed!")
    # # print("--- Testing Searches ---")

    # # # ==========================================
    # # # 3. Quick Verification Search Test
    # # # ==========================================
    # # # Test vector semantic search on episodic memory
    # # print("\n[Test 1] Searching episodic memory for 'streaming data framework':")
    # # logs_df = mm.search_related_episodic_logs("streaming data framework", k=1)
    # # if not logs_df.empty:
    # #     print(f"Found match: \"{logs_df.iloc[0]['content'][:100]}...\"")

    # # print("\n[Test 2] Searching facts/entities table for 'data warehouse':")
    # # facts_df = mm.search_related_facts("data warehouse", k=1)
    # # if not facts_df.empty:
    # #     print(f"Found entity: '{facts_df.iloc[0]['entity']}'")

    # # Test Cypher graph lookup
    # unique_entities = set()
    # for rel in synthetic_relationships:
    #     unique_entities.add(rel["source"])
    #     unique_entities.add(rel["target"])
        
    # # Convert back to a list and inject them into SQLite/sqlite-vec
    # mm.insert_entities_table(list(unique_entities))
    # print("\n[Test 3] Traversing Knowledge Graph for neighbors of 'pyspark':")
    # graph_rows = mm.search_related_rel_graph("pyspark")
    # if graph_rows:
    #     formatted_output = mm.normalize_graph_result(graph_rows)
    #     print(formatted_output)
    # else:
    #     print("No structural graph nodes linked to 'pyspark' found.")
