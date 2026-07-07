from ollama import Client
from .memory.mem_class import Memory_Manager
from .memory.extraction import Entity_Extractor

c = Client(host="127.0.0.1:11434")
MODEL="qwen3.5:9b-mlx"
mm = Memory_Manager()

agg_msg = "CONTEXT"
message_history = []
while True:
    user_msg = input("> ")
    if user_msg=="$STOP":
        break
    if not user_msg:
        continue
    #1. Get the three most relevant system messages
    retrieved_logs_num =3
    for i in range(retrieved_logs_num):
        ctx = mm.search_related_episodic_logs(user_msg, k = retrieved_logs_num).iloc[i]['content']
        agg_msg = agg_msg +f"PREVIOUS LOG CONTEXT {i}: \n" + ctx
    #2. Get the most relevant relationships from the graph database
    msg_entities = mm.get_entities(user_msg)
    total_entities = set(msg_entities)
    for entity in msg_entities:
        related = mm.search_related_facts(entity, 2)
        for item in related:
            total_entities.add(item)
    agg_msg += "\n RELATED RELATIONSHIPS \n"
    for ent in total_entities:
        rel = mm.search_related_rel_graph(ent)
        agg_msg +=mm.normalize_graph_result(rel)
    
    #3. Augment user_msg
    agg_msg=agg_msg + "\n USER MESSAGE:"+user_msg
    #4. get response from model
    response = c.chat(model="qwen3.5:9b-mlx",
        messages = [{"role":"user", "content": agg_msg}])
    
    #5. Tool calling
    #5. asynchronously add the response to database
    #5. If we're close to lru cache limit, extract entities and relationships from the response and add to graph database then evict
    #6. If we're close to token limit, summarize 



    
    
    #Add response to database




        