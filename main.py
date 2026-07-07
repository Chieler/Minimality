from ollama import Client
from memory.mem_class import Memory_Manager
from memory.extraction import Entity_Extractor
import streamlit as st




MODEL="qwen3.5:9b-mlx"
if "mm" not in st.session_state:
    st.session_state.mm = Memory_Manager()
# if "c" not in st.session_state:
#     st.session_state.c = Client(host="127.0.0.1:11434")

# message_history = [{"role": "sysrem", "content": SYS_INSTRUCTION}]
# while True:
#     user_msg = input("> ")
#     if user_msg=="$STOP":
#         break
#     if not user_msg:
#         continue
message_history=[]
def harness(user_msg, message_history):
    agg_msg = "CONTEXT"
    #Make this into an lru
    
    #1. Get the three most relevant system messages
    retrieved_logs_num =3
    hist = st.session_state.mm.search_related_episodic_logs(user_msg, k = retrieved_logs_num)
    if not hist.empty:
        for i in range(len(hist)):
            ctx = hist.iloc[i]['content']
            agg_msg = agg_msg +f"PREVIOUS LOG CONTEXT {i}: \n" + ctx

    #2. Get the most relevant relationships from the graph database
    msg_entities = st.session_state.mm.get_entities(user_msg)
    print(f"Entities extracted: {msg_entities}")

    total_entities = set(msg_entities)

    for entity in msg_entities:
        # This returns a pandas DataFrame with ['id', 'entity'] columns
        related = st.session_state.mm.search_related_facts(entity, 2)
        
        if not related.empty:
            # ✅ FIXED: Use Pandas series conversion to instantly update your set 
            # without running clunky manual index loops
            total_entities.update(related['entity'].tolist())

    agg_msg += "\n RELATED RELATIONSHIPS \n"
    for ent in total_entities:

        rel = st.session_state.mm.search_related_rel_graph(ent)
        agg_msg +=st.session_state.mm.normalize_graph_result(rel)
    print(agg_msg)
    #3. Augment user_msg
    agg_msg=agg_msg + "\n USER MESSAGE:"+user_msg
    message_history.append({"role":"user", "content": agg_msg})
    #4. get response from model
    stream = st.session_state.mm.client.chat(model="gemma4:e4b-mlx",
        messages = message_history, stream=True)
    for chunk in stream:
        if "message" in chunk:
            yield chunk["message"]["content"]
    #5. Tool calling


    #5. asynchronously add the response to database
    #5. If we're close to lru cache limit, extract entities and relationships from the response and add to graph database then evict
    #6. If we're close to token limit, summarize 
    #Problen: How do we instantiate mm once?

st.title("Minimalist")
if prompt := st.chat_input("Enter your message"):
    st.write_stream(harness(prompt, message_history))
    
